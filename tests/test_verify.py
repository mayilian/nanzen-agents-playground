"""Stage 6: verifier accepts good output, rejects fabricated content."""

from datetime import datetime

from challenge.models import (
    AccountSummary,
    BillingFacts,
    DepartmentUsage,
    RenewalVerdict,
    RuleFlag,
    SupportFacts,
    TalkingPoint,
    TextSignal,
    UsageFacts,
)
from challenge.verify import verify


def _fixture_summary() -> AccountSummary:
    return AccountSummary(
        account_id="MERID-001",
        customer_name="Meridian Health",
        as_of=datetime(2026, 4, 27),
        billing=BillingFacts(
            invoiced_eur=509852.11,
            paid_eur=491682.45,
            credits_eur=-4529.18,
            refunds_eur=-18997.50,
            outstanding_eur=3701.34,
            invoices_issued=28,
            payments_received=31,
            partial_payments=3,
            reminders_sent=16,
            overdue_events=17,
            max_days_past_due=17,
            disputes_opened=1,
            disputes_resolved=1,
            open_disputes=0,
            first_reminder_at=None,
            last_payment_at=None,
        ),
        usage=UsageFacts(
            rows=247,
            weeks_observed=123,
            date_range=(datetime(2023, 11, 6).date(), datetime(2026, 3, 9).date()),
            departments_raw=["Engineering", "Marketing"],
            departments_canonical=["Engineering", "Marketing"],
            sessions_slope_per_week=0.41,
            q1_avg_sessions=213.3,
            q4_avg_sessions=246.8,
            q1_to_q4_pct_change=15.7,
            top_department_by_sessions="Engineering",
            by_department={
                "Engineering": DepartmentUsage(27044, 19.9, 62.7, 79.8),
                "Marketing": DepartmentUsage(2976, 6.9, 18.4, 42.6),
            },
        ),
        support=SupportFacts(
            raw_lines_in_file=327,
            interactions_parsed=156,
            interactions_dropped=170,
            unique_tickets=22,
            open_tickets=10,
            resolved_tickets=12,
            sla_breaches=8,
            mean_csat=4.5,
            survey_count=14,
            mean_resolution_days=5.49,
            max_resolution_days=20.97,
            by_category={"billing": 5, "configuration": 3},
            by_priority={"medium": 10, "high": 4},
            by_status={"resolved": 12, "open": 10},
        ),
        rule_flags=[
            RuleFlag(
                id="late-payment-pattern",
                severity="warn",
                message="16 reminders, 17 overdue, max 17 days past due",
                evidence=["billing.reminders_sent=16"],
            ),
        ],
    )


def _fixture_signals() -> list[TextSignal]:
    return [
        TextSignal(
            id="support_tickets:INT-1158.content",
            source="support_tickets",
            timestamp=datetime(2026, 2, 24),
            text="Elena confirmed she contacted Lisa on Feb 20. Renewal discussion is now active.",
            why_selected="keyword 'renewal'",
        ),
        TextSignal(
            id="crm_interactions:CRM-084.body",
            source="crm_interactions",
            timestamp=datetime(2026, 3, 1),
            text="Renewal kick-off call with James. Was tense — billing concerns raised.",
            why_selected="keyword 'renewal'",
        ),
    ]


def _good_verdict() -> RenewalVerdict:
    return RenewalVerdict(
        verdict="Yellow",
        one_sentence_reason=(
            "Healthy customer with operational late-payment friction "
            "but real growth and high satisfaction."
        ),
        executive_narrative=(
            "Meridian Health (MERID-001) is paying reliably — 491,682.45 EUR "
            "received against 509,852.11 invoiced — but with a marked late-payment "
            "pattern (16 reminders, max 17 days past due) since the AP workflow change. "
            "Usage grew 15.7% from Q1 to Q4 and CSAT is 4.5. AM Elena is engaging on the renewal."
        ),
        talking_points=[
            TalkingPoint(
                headline="Discuss the AP workflow change directly",
                detail="Acknowledge the late-payment pattern; offer payment-terms or automation.",
                cites=["billing.reminders_sent", "crm_interactions:CRM-084.body"],
            ),
            TalkingPoint(
                headline="Anchor on usage growth as the upgrade story",
                detail="Engineering has 79.8% seat utilisation and 15.7% sessions growth.",
                cites=["usage.q1_to_q4_pct_change"],
            ),
            TalkingPoint(
                headline="Confirm next steps with the AM",
                detail="INT-1158 shows the renewal conversation is active.",
                cites=["support_tickets:INT-1158.content"],
            ),
        ],
        citations_used=["billing.reminders_sent"],
    )


def test_verify_accepts_grounded_verdict():
    s = _fixture_summary()
    sigs = _fixture_signals()
    res = verify(s, sigs, _good_verdict())
    assert res.passed, res.diagnostics


def test_verify_rejects_fabricated_number():
    s = _fixture_summary()
    sigs = _fixture_signals()
    bad = _good_verdict().model_copy(
        update={
            "executive_narrative": (
                "Outstanding is 999,999.99 EUR, which is alarming. "
                + _good_verdict().executive_narrative
            )
        }
    )
    res = verify(s, sigs, bad)
    assert not res.passed
    assert any("999" in d for d in res.diagnostics)


def test_verify_rejects_fabricated_ticket():
    s = _fixture_summary()
    sigs = _fixture_signals()
    good = _good_verdict()
    bad = good.model_copy(
        update={
            "executive_narrative": good.executive_narrative + " See TKT-9999 for details."
        }
    )
    res = verify(s, sigs, bad)
    assert not res.passed
    assert any("TKT-9999" in d for d in res.diagnostics)
