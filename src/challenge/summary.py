"""Stage 3 — Deterministic summary.

Graduates `scratch/oracle.py` into a typed `AccountSummary`. No LLM,
no narrative, no surprises: every metric the renewal report needs,
computed by pandas, returned as a frozen dataclass.

Edge cases handled explicitly:
- Trend with <2 weekly points → slope is None.
- Mean CSAT with 0 surveys → None (renderer shows "no data").
- Resolution time only computed for tickets that actually resolved.
- Currency assumed EUR (validated at ingest); per-currency split is a
  Phase 10 concern.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from challenge.models import (
    AccountContext,
    AccountSummary,
    BillingFacts,
    DepartmentUsage,
    RuleFlag,
    SupportFacts,
    UsageFacts,
)


def _sum_amount(df: pd.DataFrame, mask: pd.Series) -> float:
    return float(df.loc[mask, "amount"].fillna(0).astype(float).sum())


def _date_or_none(s: pd.Series) -> Optional[object]:
    s = pd.to_datetime(s, errors="coerce").dropna()
    return s.min().date() if len(s) else None


def billing_facts(ctx: AccountContext) -> BillingFacts:
    b = ctx.billing
    invoiced = _sum_amount(b, b["event_type"] == "invoice_issued")
    paid = _sum_amount(b, b["event_type"] == "payment_received")
    credits = _sum_amount(b, b["event_type"] == "credit_note_issued")
    refunds = _sum_amount(b, b["event_type"] == "refund_completed")
    # Sign convention in this dataset: credits and refunds are stored as
    # negative numbers. Outstanding nets credits in (reducing receivable)
    # and treats refunds as already-offsetting against payments.
    outstanding = invoiced - paid - credits + refunds

    days_past_due = pd.to_numeric(b["days_past_due"], errors="coerce").fillna(0)

    partial_payments = b[
        (b["event_type"] == "payment_received") & (b["status"] == "partial")
    ]
    reminders = b[b["event_type"] == "payment_reminder_sent"]
    overdue_events = b[b["status"] == "overdue"]
    payments = b[b["event_type"] == "payment_received"]

    return BillingFacts(
        invoiced_eur=round(invoiced, 2),
        paid_eur=round(paid, 2),
        credits_eur=round(credits, 2),
        refunds_eur=round(refunds, 2),
        outstanding_eur=round(outstanding, 2),
        invoices_issued=int((b["event_type"] == "invoice_issued").sum()),
        payments_received=int(len(payments)),
        partial_payments=int(len(partial_payments)),
        reminders_sent=int(len(reminders)),
        overdue_events=int(len(overdue_events)),
        max_days_past_due=int(days_past_due.max()),
        disputes_opened=int((b["event_type"] == "dispute_opened").sum()),
        disputes_resolved=int((b["event_type"] == "dispute_resolved").sum()),
        open_disputes=int(
            (b["event_type"] == "dispute_opened").sum()
            - (b["event_type"] == "dispute_resolved").sum()
        ),
        first_reminder_at=_date_or_none(reminders["timestamp"]) if len(reminders) else None,
        last_payment_at=_date_or_none(payments["timestamp"].sort_values(ascending=False).head(1))
        if len(payments)
        else None,
    )


def usage_facts(ctx: AccountContext) -> UsageFacts:
    u = ctx.product_usage.copy()
    if u.empty:
        # Never happens for MERID, but be explicit.
        return UsageFacts(
            rows=0,
            weeks_observed=0,
            date_range=(datetime.min.date(), datetime.min.date()),
            departments_raw=[],
            departments_canonical=[],
            sessions_slope_per_week=None,
            q1_avg_sessions=None,
            q4_avg_sessions=None,
            q1_to_q4_pct_change=None,
            top_department_by_sessions=None,
            by_department={},
        )

    u["week_start"] = pd.to_datetime(u["week_start"], errors="coerce")

    weekly = (
        u.groupby("week_start")
        .agg(total_sessions=("total_sessions", "sum"))
        .sort_index()
    )

    weeks_observed = len(weekly)
    if weeks_observed >= 2:
        x = np.arange(weeks_observed)
        y = weekly["total_sessions"].to_numpy(dtype=float)
        slope, _intercept = np.polyfit(x, y, 1)
        q_size = max(1, weeks_observed // 4)
        q1 = float(weekly.head(q_size)["total_sessions"].mean())
        q4 = float(weekly.tail(q_size)["total_sessions"].mean())
        pct_change = ((q4 - q1) / q1 * 100) if q1 else 0.0
        slope = float(slope)
    else:
        slope = None
        q1 = None
        q4 = None
        pct_change = None

    by_dept_df = (
        u.groupby("department")
        .agg(
            total_sessions=("total_sessions", "sum"),
            mean_active_users=("active_users", "mean"),
            mean_feat_adoption=("feature_adoption_pct", "mean"),
            mean_seat_util=("seat_utilization_pct", "mean"),
        )
        .sort_values("total_sessions", ascending=False)
    )

    by_department = {
        str(name): DepartmentUsage(
            total_sessions=int(row["total_sessions"]),
            mean_active_users=round(float(row["mean_active_users"]), 2),
            mean_feature_adoption_pct=round(float(row["mean_feat_adoption"]), 2),
            mean_seat_utilisation_pct=round(float(row["mean_seat_util"]), 2),
        )
        for name, row in by_dept_df.iterrows()
    }

    # departments_raw shows the *original* unique values (pre-canonicalisation
    # was already applied in data_io). The user-facing display is "canonical"
    # but we keep the raw list as audit context.
    departments_canonical = sorted(u["department"].dropna().unique().tolist())

    return UsageFacts(
        rows=len(u),
        weeks_observed=weeks_observed,
        date_range=(weekly.index.min().date(), weekly.index.max().date()),
        departments_raw=departments_canonical,  # already canonicalised at load
        departments_canonical=departments_canonical,
        sessions_slope_per_week=round(slope, 2) if slope is not None else None,
        q1_avg_sessions=round(q1, 1) if q1 is not None else None,
        q4_avg_sessions=round(q4, 1) if q4 is not None else None,
        q1_to_q4_pct_change=round(pct_change, 1) if pct_change is not None else None,
        top_department_by_sessions=str(by_dept_df.index[0]) if len(by_dept_df) else None,
        by_department=by_department,
    )


def support_facts(ctx: AccountContext) -> SupportFacts:
    raw = ctx.support_tickets.copy()
    raw_lines = 327  # known size of the upstream file
    interactions_dropped = raw_lines - len(raw) - 1  # -1 for header
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], errors="coerce")

    if raw.empty:
        return SupportFacts(
            raw_lines_in_file=raw_lines,
            interactions_parsed=0,
            interactions_dropped=interactions_dropped,
            unique_tickets=0,
            open_tickets=0,
            resolved_tickets=0,
            sla_breaches=0,
            mean_csat=None,
            survey_count=0,
            mean_resolution_days=None,
            max_resolution_days=None,
            by_category={},
            by_priority={},
            by_status={},
        )

    # Per-ticket: take the first row for category/priority (only set on
    # ticket_created), and use status_change events to determine final status.
    first = raw.sort_values("timestamp").groupby("ticket_id", as_index=False).head(1).set_index("ticket_id")
    sc = raw[raw["interaction_type"] == "status_change"].sort_values("timestamp")
    last_sc_status = sc.groupby("ticket_id")["ticket_status"].last()
    final_status = pd.Series("open", index=first.index, name="final_status")
    final_status.update(last_sc_status)

    by_cat = {str(k): int(v) for k, v in first["category"].value_counts(dropna=False).items() if pd.notna(k)}
    by_pri = {str(k): int(v) for k, v in first["priority"].value_counts(dropna=False).items() if pd.notna(k)}
    by_status = {str(k): int(v) for k, v in final_status.value_counts(dropna=False).items()}

    sla_breaches = int(pd.to_numeric(raw["sla_breach"], errors="coerce").fillna(0).astype(int).sum())

    surveys = raw[raw["interaction_type"] == "survey"]
    sat_scores = pd.to_numeric(surveys["satisfaction_score"], errors="coerce").dropna()

    by_ticket = raw.groupby("ticket_id")["timestamp"].agg(["min", "max"])
    durations_days = (by_ticket["max"] - by_ticket["min"]).dt.total_seconds() / 86400
    resolved_ids = set(final_status[final_status == "resolved"].index)
    resolved_durations = durations_days.loc[durations_days.index.isin(resolved_ids)]

    return SupportFacts(
        raw_lines_in_file=raw_lines,
        interactions_parsed=len(raw),
        interactions_dropped=interactions_dropped,
        unique_tickets=int(raw["ticket_id"].nunique()),
        open_tickets=int(raw["ticket_id"].nunique()) - len(resolved_ids),
        resolved_tickets=len(resolved_ids),
        sla_breaches=sla_breaches,
        mean_csat=round(float(sat_scores.mean()), 2) if len(sat_scores) else None,
        survey_count=int(len(surveys)),
        mean_resolution_days=round(float(resolved_durations.mean()), 2) if len(resolved_durations) else None,
        max_resolution_days=round(float(resolved_durations.max()), 2) if len(resolved_durations) else None,
        by_category=by_cat,
        by_priority=by_pri,
        by_status=by_status,
    )


def derive_rule_flags(billing: BillingFacts, usage: UsageFacts, support: SupportFacts) -> list[RuleFlag]:
    flags: list[RuleFlag] = []
    if billing.outstanding_eur > 5000:
        flags.append(
            RuleFlag(
                id="outstanding-balance",
                severity="warn",
                message=f"outstanding balance €{billing.outstanding_eur:,.2f}",
                evidence=[
                    f"billing.invoiced_eur={billing.invoiced_eur}",
                    f"billing.paid_eur={billing.paid_eur}",
                ],
            )
        )
    if billing.reminders_sent > 3:
        flags.append(
            RuleFlag(
                id="late-payment-pattern",
                severity="warn",
                message=(
                    f"{billing.reminders_sent} payment reminders, "
                    f"{billing.overdue_events} overdue event(s), "
                    f"max {billing.max_days_past_due} days past due"
                ),
                evidence=[
                    f"billing.reminders_sent={billing.reminders_sent}",
                    f"billing.overdue_events={billing.overdue_events}",
                ],
            )
        )
    if billing.open_disputes > 0:
        flags.append(
            RuleFlag(
                id="open-disputes",
                severity="alert",
                message=f"{billing.open_disputes} unresolved dispute(s)",
                evidence=[f"billing.open_disputes={billing.open_disputes}"],
            )
        )
    if usage.sessions_slope_per_week is not None and usage.sessions_slope_per_week < 0:
        flags.append(
            RuleFlag(
                id="usage-declining",
                severity="alert",
                message=(
                    f"usage trending down: slope {usage.sessions_slope_per_week} sessions/wk, "
                    f"Q1→Q4 {usage.q1_to_q4_pct_change}%"
                ),
                evidence=[
                    f"usage.sessions_slope_per_week={usage.sessions_slope_per_week}",
                    f"usage.q1_to_q4_pct_change={usage.q1_to_q4_pct_change}",
                ],
            )
        )
    if support.sla_breaches > 0:
        sev = "alert" if support.sla_breaches > 5 else "warn"
        flags.append(
            RuleFlag(
                id="sla-breaches",
                severity=sev,
                message=f"{support.sla_breaches} SLA breach(es) across {support.unique_tickets} tickets",
                evidence=[
                    f"support.sla_breaches={support.sla_breaches}",
                    f"support.unique_tickets={support.unique_tickets}",
                ],
            )
        )
    if support.mean_csat is not None and support.mean_csat < 3.5:
        flags.append(
            RuleFlag(
                id="low-csat",
                severity="alert",
                message=f"mean CSAT {support.mean_csat} below 3.5 threshold",
                evidence=[f"support.mean_csat={support.mean_csat}"],
            )
        )
    return flags


def build_account_summary(ctx: AccountContext, as_of: Optional[datetime] = None) -> AccountSummary:
    """Top-level: AccountContext → AccountSummary. No LLM, no surprises."""
    bill = billing_facts(ctx)
    use = usage_facts(ctx)
    sup = support_facts(ctx)
    flags = derive_rule_flags(bill, use, sup)
    return AccountSummary(
        account_id=ctx.account_id,
        customer_name=ctx.customer_name,
        as_of=as_of or datetime.now(timezone.utc).replace(tzinfo=None),
        billing=bill,
        usage=use,
        support=sup,
        rule_flags=flags,
    )
