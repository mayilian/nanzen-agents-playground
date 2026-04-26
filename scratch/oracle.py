"""Deterministic ground truth for MERID-001 (Meridian Health).

Computes the canonical answers to the three task prompts in src/challenge/tasks.py
using only pandas — no LLM. The agent's output will be graded against this.

Run:
    uv run python scratch/oracle.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).resolve().parents[1] / "data"
ACCOUNT_ID = "MERID-001"


def _hr(title: str) -> None:
    print(f"\n─── {title} ".ljust(60, "─"))


def _line(label: str, value, width: int = 36) -> None:
    print(f"  {label.ljust(width)}{value}")


def load_billing_for_account(account_id: str) -> pd.DataFrame:
    """Billing has no account_id; assemble from THREE join paths because
    contract_reference is sparsely populated (F9). Operational events
    (disputes, refunds, manual notes) frequently leave it blank.

    1. contract_reference matches a known contract for the account.
    2. invoice_id matches the customer's invoice pattern.
    3. credit_note_ref matches a known credit note.
    """
    contracts = pd.read_csv(DATA / "contracts.csv")
    refs = contracts.loc[contracts["account_id"] == account_id, "contract_id"].unique()
    billing = pd.read_csv(DATA / "billing.csv", engine="python", on_bad_lines="skip")

    # Customer-specific invoice prefix. For Meridian, invoices look like
    # "INV-YYYY-MH-NNN". Derive the customer code from the contract id.
    cust_codes = {ref.split("-")[2] for ref in refs}  # {"MH"}
    invoice_re = (
        r"^INV-\d+-(" + "|".join(sorted(cust_codes)) + r")-\d+$"
    )

    by_contract = billing["contract_reference"].isin(refs)
    by_invoice = billing["invoice_id"].fillna("").str.match(invoice_re)
    # Once we have MERID-tagged rows, pull in any credit_note_refs they
    # reference, then bring in events for those credit notes.
    seed = billing[by_contract | by_invoice]
    credit_refs = set(seed["credit_note_ref"].dropna().unique())
    by_credit = billing["credit_note_ref"].isin(credit_refs) | billing[
        "credit_note_ref"
    ].fillna("").isin(seed["invoice_id"].fillna(""))

    mh = billing[by_contract | by_invoice | by_credit].copy()
    return mh, list(refs)


def billing_summary(account_id: str) -> dict:
    bill, refs = load_billing_for_account(account_id)

    def _sum(mask):
        return float(bill.loc[mask, "amount"].fillna(0).astype(float).sum())

    invoices = bill[bill["event_type"] == "invoice_issued"]
    payments = bill[bill["event_type"] == "payment_received"]
    credits = bill[bill["event_type"] == "credit_note_issued"]
    refunds = bill[bill["event_type"] == "refund_completed"]
    disputes_opened = bill[bill["event_type"] == "dispute_opened"]
    disputes_resolved = bill[bill["event_type"] == "dispute_resolved"]
    reminders = bill[bill["event_type"] == "payment_reminder_sent"]
    overdue_events = bill[bill["status"] == "overdue"]

    total_invoiced = _sum(bill["event_type"] == "invoice_issued")
    total_paid = _sum(bill["event_type"] == "payment_received")
    total_credits = _sum(bill["event_type"] == "credit_note_issued")
    total_refunds = _sum(bill["event_type"] == "refund_completed")
    outstanding = total_invoiced - total_paid - total_credits + total_refunds

    days_past_due_max = float(
        pd.to_numeric(bill["days_past_due"], errors="coerce").fillna(0).max()
    )

    partial_payments = bill[
        (bill["event_type"] == "payment_received") & (bill["status"] == "partial")
    ]

    return {
        "contracts": refs,
        "rows_for_account": len(bill),
        "invoices_issued": len(invoices),
        "payments_received": len(payments),
        "partial_payments": len(partial_payments),
        "credit_notes_issued": len(credits),
        "refunds_completed": len(refunds),
        "disputes_opened": len(disputes_opened),
        "disputes_resolved": len(disputes_resolved),
        "open_disputes": len(disputes_opened) - len(disputes_resolved),
        "reminders_sent": len(reminders),
        "overdue_events": len(overdue_events),
        "total_invoiced_eur": round(total_invoiced, 2),
        "total_paid_eur": round(total_paid, 2),
        "total_credits_eur": round(total_credits, 2),
        "total_refunds_eur": round(total_refunds, 2),
        "outstanding_eur": round(outstanding, 2),
        "max_days_past_due": int(days_past_due_max),
    }


def usage_summary(account_id: str) -> dict:
    df = pd.read_csv(DATA / "product_usage.csv")
    df = df[df["account_id"] == account_id].copy()

    # F6: canonicalize the typo before any groupby.
    df["department"] = df["department"].replace({"Enginering": "Engineering"})
    df["week_start"] = pd.to_datetime(df["week_start"])

    weekly = (
        df.groupby("week_start")
        .agg(
            total_sessions=("total_sessions", "sum"),
            active_users=("active_users", "sum"),
            licensed_seats=("licensed_seats", "sum"),
        )
        .reset_index()
        .sort_values("week_start")
    )

    # Linear regression slope of total_sessions vs week index.
    x = np.arange(len(weekly))
    y = weekly["total_sessions"].to_numpy(dtype=float)
    slope, intercept = np.polyfit(x, y, 1) if len(weekly) >= 2 else (0.0, float(y[0]))

    by_dept = (
        df.groupby("department")
        .agg(
            total_sessions=("total_sessions", "sum"),
            mean_active_users=("active_users", "mean"),
            mean_feat_adoption=("feature_adoption_pct", "mean"),
            mean_seat_util=("seat_utilization_pct", "mean"),
        )
        .sort_values("total_sessions", ascending=False)
    )

    first_quartile = weekly.head(max(1, len(weekly) // 4))["total_sessions"].mean()
    last_quartile = weekly.tail(max(1, len(weekly) // 4))["total_sessions"].mean()
    pct_change = (
        ((last_quartile - first_quartile) / first_quartile * 100)
        if first_quartile
        else 0.0
    )

    return {
        "rows": len(df),
        "weeks": len(weekly),
        "date_range": (str(weekly["week_start"].min().date()), str(weekly["week_start"].max().date())),
        "departments_raw": sorted(pd.read_csv(DATA / "product_usage.csv")["department"].unique()),
        "departments_canonical": sorted(df["department"].unique()),
        "trend_slope_sessions_per_week": round(float(slope), 2),
        "first_quartile_avg_sessions": round(float(first_quartile), 1),
        "last_quartile_avg_sessions": round(float(last_quartile), 1),
        "pct_change_first_to_last_quartile": round(float(pct_change), 1),
        "top_dept_by_sessions": str(by_dept.index[0]),
        "by_dept": by_dept.round(2).to_dict(orient="index"),
    }


def support_summary(account_id: str) -> dict:
    raw = pd.read_csv(DATA / "support_tickets.csv", engine="python", on_bad_lines="skip")
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], errors="coerce")
    # F1: support_tickets has no account_id. Departments seen are Meridian's
    # internal departments ('Engineering', 'Marketing', 'Procurement', 'All'),
    # consistent with this being a Meridian-only support extract.

    # Category/priority are populated on the ticket_created row only. Take the
    # first row per ticket for those.
    first = (
        raw.sort_values("timestamp")
        .groupby("ticket_id", as_index=False)
        .head(1)
        .set_index("ticket_id")
    )
    # Status: latest status_change wins. If no status_change, ticket is open.
    sc = raw[raw["interaction_type"] == "status_change"].sort_values("timestamp")
    last_sc_status = sc.groupby("ticket_id")["ticket_status"].last()
    final_status = pd.Series("open", index=first.index, name="final_status")
    final_status.update(last_sc_status)

    by_cat = first["category"].value_counts(dropna=False).to_dict()
    by_pri = first["priority"].value_counts(dropna=False).to_dict()
    by_status = final_status.value_counts(dropna=False).to_dict()

    sla_breaches = int(
        pd.to_numeric(raw["sla_breach"], errors="coerce").fillna(0).astype(int).sum()
    )

    surveys = raw[raw["interaction_type"] == "survey"]
    sat_scores = pd.to_numeric(surveys["satisfaction_score"], errors="coerce").dropna()

    by_ticket = raw.groupby("ticket_id")["timestamp"].agg(["min", "max"])
    durations_days = (by_ticket["max"] - by_ticket["min"]).dt.total_seconds() / 86400

    # Resolution time only for tickets whose final status is "resolved".
    resolved_ids = set(final_status[final_status == "resolved"].index)
    resolved_durations = durations_days.loc[durations_days.index.isin(resolved_ids)]

    return {
        "raw_lines_in_file": 327,
        "interactions_parsed": len(raw),
        "interactions_dropped_due_to_bad_csv": 327 - len(raw) - 1,  # -1 for header
        "unique_tickets": int(raw["ticket_id"].nunique()),
        "resolved_tickets": len(resolved_ids),
        "open_tickets": int(raw["ticket_id"].nunique()) - len(resolved_ids),
        "by_category": by_cat,
        "by_priority": by_pri,
        "by_status": by_status,
        "sla_breaches": sla_breaches,
        "survey_count": len(surveys),
        "mean_satisfaction": round(float(sat_scores.mean()), 2) if len(sat_scores) else None,
        "mean_resolution_days": (
            round(float(resolved_durations.mean()), 2) if len(resolved_durations) else None
        ),
        "max_resolution_days": (
            round(float(resolved_durations.max()), 2) if len(resolved_durations) else None
        ),
        "assumption": "no account_id column on support_tickets; treating all rows as MERID-001",
    }


def renewal_flags(billing: dict, usage: dict, support: dict) -> list[str]:
    flags: list[str] = []
    if billing["outstanding_eur"] > 5000:
        flags.append(
            f"outstanding balance €{billing['outstanding_eur']:,.2f} unsettled"
        )
    if billing["reminders_sent"] > 3:
        flags.append(
            f"{billing['reminders_sent']} payment reminders / {billing['overdue_events']} "
            f"overdue events — late-payment pattern (max {billing['max_days_past_due']} days past due)"
        )
    if billing["open_disputes"] > 0:
        flags.append(f"{billing['open_disputes']} unresolved dispute(s)")
    if usage["trend_slope_sessions_per_week"] < 0:
        flags.append(
            f"usage trending down: slope {usage['trend_slope_sessions_per_week']} sessions/wk; "
            f"first→last quartile {usage['pct_change_first_to_last_quartile']:+.1f}%"
        )
    if support["sla_breaches"] > 0:
        flags.append(
            f"{support['sla_breaches']} SLA breach(es) across {support['unique_tickets']} tickets"
        )
    if (
        support["mean_satisfaction"] is not None
        and support["mean_satisfaction"] < 3.5
    ):
        flags.append(f"mean CSAT {support['mean_satisfaction']} is below 3.5 threshold")
    return flags


def main() -> None:
    bill = billing_summary(ACCOUNT_ID)
    use = usage_summary(ACCOUNT_ID)
    sup = support_summary(ACCOUNT_ID)
    flags = renewal_flags(bill, use, sup)

    print(f"Ground truth for {ACCOUNT_ID} (Meridian Health)")
    print(f"Generated by scratch/oracle.py — deterministic, no LLM.")

    _hr("Billing")
    _line("Contracts", ", ".join(bill["contracts"]))
    _line("Billing rows for account", bill["rows_for_account"])
    _line("Invoices issued (count)", bill["invoices_issued"])
    _line("Payments received (count)", f"{bill['payments_received']} ({bill['partial_payments']} partial)")
    _line("Credit notes issued (count)", bill["credit_notes_issued"])
    _line("Refunds completed (count)", bill["refunds_completed"])
    _line("Disputes (opened / resolved)", f"{bill['disputes_opened']} / {bill['disputes_resolved']}")
    _line("Reminders sent (count)", bill["reminders_sent"])
    _line("Overdue-status events (count)", bill["overdue_events"])
    _line("Total invoiced (EUR)", f"{bill['total_invoiced_eur']:>15,.2f}")
    _line("Total paid (EUR)", f"{bill['total_paid_eur']:>15,.2f}")
    _line("Total credits (EUR)", f"{bill['total_credits_eur']:>15,.2f}")
    _line("Total refunds (EUR)", f"{bill['total_refunds_eur']:>15,.2f}")
    _line("Outstanding (EUR)", f"{bill['outstanding_eur']:>15,.2f}")
    _line("Max days past due", bill["max_days_past_due"])
    print(
        "\n  Notable events:\n"
        "  * Payment behaviour shifted Oct 2024: 16 reminders sent and 17 overdue\n"
        "    statuses, all dated Oct 2024 onwards. Coincides with the customer's\n"
        "    new CFO and revised AP workflow (per CRM/email notes).\n"
        "  * Resolved dispute DISP-2024-MH-001 (INV-2024-MH-013, Dec 2024):\n"
        "    line-item display issue, total amount was correct.\n"
        "  * SLA credit CN-2025-MH-001 (-€3,600) for the Jan 14 2025 outage\n"
        "    (TKT-4893, 3-hour downtime); applied to INV-2025-MH-015.\n"
        "  * Duplicate payment on INV-2024-MH-006: refunded €18,150 (Jun 2024).\n"
        "  * Misdirected payment €847.50 (Apr 2025): returned to customer."
    )

    _hr("Usage")
    _line("Rows for account", use["rows"])
    _line("Weeks of data", use["weeks"])
    _line("Date range", f"{use['date_range'][0]} → {use['date_range'][1]}")
    _line("Departments (raw, with typo)", use["departments_raw"])
    _line("Departments (canonicalised)", use["departments_canonical"])
    _line("Trend (sessions/week slope)", use["trend_slope_sessions_per_week"])
    _line("Avg sessions/wk Q1 → Q4", f"{use['first_quartile_avg_sessions']} → {use['last_quartile_avg_sessions']} ({use['pct_change_first_to_last_quartile']:+.1f}%)")
    _line("Top dept by total sessions", use["top_dept_by_sessions"])
    print("\n  By department:")
    for dept, vals in use["by_dept"].items():
        print(f"    {dept:14s}  sessions={vals['total_sessions']:>10,.0f}  mean_active_users={vals['mean_active_users']:.1f}  mean_adoption={vals['mean_feat_adoption']:.1f}%  mean_seat_util={vals['mean_seat_util']:.1f}%")

    _hr("Support")
    _line("Raw lines in file", sup["raw_lines_in_file"])
    _line("Interactions parsed", sup["interactions_parsed"])
    _line("Interactions dropped (bad CSV)", sup["interactions_dropped_due_to_bad_csv"])
    _line("Unique tickets", sup["unique_tickets"])
    _line("Resolved / Open", f"{sup['resolved_tickets']} / {sup['open_tickets']}")
    _line("SLA breaches", sup["sla_breaches"])
    _line("Mean CSAT (1-5)", sup["mean_satisfaction"])
    _line("Mean resolution time (days, resolved only)", sup["mean_resolution_days"])
    _line("Max resolution time (days)", sup["max_resolution_days"])
    print(f"\n  By category: {sup['by_category']}")
    print(f"  By priority: {sup['by_priority']}")
    print(f"  By status:   {sup['by_status']}")
    print(f"  Caveat:      {sup['assumption']}")

    _hr("Renewal flags (rule-based)")
    if not flags:
        print("  (none)")
    else:
        for f in flags:
            print(f"  [!] {f}")
    print()


if __name__ == "__main__":
    main()
