"""Stage 1+2+3 regression: AccountSummary numbers must equal the oracle.

These are the load-bearing tests. If the data pipeline drifts, this
catches it.
"""

from challenge.data_io import load_all
from challenge.joins import build_account_context
from challenge.summary import build_account_summary


def test_meridian_summary_matches_oracle():
    load = load_all()
    ctx = build_account_context(load, "MERID-001")
    s = build_account_summary(ctx)

    # Frozen ground-truth numbers from scratch/oracle.py.
    assert s.account_id == "MERID-001"
    assert s.customer_name == "Meridian Health"

    # Billing
    assert s.billing.invoiced_eur == 509852.11
    assert s.billing.paid_eur == 491682.45
    assert s.billing.credits_eur == -4529.18
    assert s.billing.refunds_eur == -18997.50
    assert s.billing.outstanding_eur == 3701.34
    assert s.billing.invoices_issued == 28
    assert s.billing.payments_received == 31
    assert s.billing.partial_payments == 3
    assert s.billing.disputes_opened == 1
    assert s.billing.disputes_resolved == 1
    assert s.billing.reminders_sent == 16
    assert s.billing.overdue_events == 17
    assert s.billing.max_days_past_due == 17

    # Usage
    assert s.usage.weeks_observed == 123
    assert s.usage.top_department_by_sessions == "Engineering"
    # F6: typo canonicalised away — only 2 departments, not 3.
    assert sorted(s.usage.by_department.keys()) == ["Engineering", "Marketing"]
    assert s.usage.q1_to_q4_pct_change == 15.7

    # Support
    assert s.support.unique_tickets == 22
    assert s.support.resolved_tickets == 12
    assert s.support.open_tickets == 10
    assert s.support.sla_breaches == 8
    assert s.support.mean_csat == 4.5

    # Rule flags expected
    flag_ids = {f.id for f in s.rule_flags}
    assert "late-payment-pattern" in flag_ids
    assert "sla-breaches" in flag_ids


def test_load_report_records_dropped_rows_and_normalisations():
    load = load_all()
    # F3: malformed support_tickets row drops some interactions.
    assert load.report.rows_dropped_per_source.get("support_tickets", 0) > 0
    # F6: 'Enginering' -> 'Engineering' applied.
    assert (
        "product_usage",
        "Enginering",
        "Engineering",
    ) in load.report.normalisations_applied


def test_join_report_uses_multiple_keys_for_billing():
    """F1, F9: billing must be assembled across multiple keys."""
    load = load_all()
    ctx = build_account_context(load, "MERID-001")
    keys = ctx.join_report.keys_used_per_source["billing"]
    assert keys["contract_reference"] > 0
    assert keys["invoice_id_pattern"] > 0
    # customer_alias_text catches the misdirected-payment trio (rows
    # with no FKs at all — see joins.py for the third resolver).
    assert keys["customer_alias_text"] >= 1


def test_unknown_account_id_fails_loudly():
    """F1's silent no-op behaviour must not return."""
    import pytest
    load = load_all()
    with pytest.raises(ValueError, match="not found"):
        build_account_context(load, "NOSUCH-001")
