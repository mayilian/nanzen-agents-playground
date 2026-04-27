"""Typed contracts at every boundary of the renewal-risk pipeline.

These are the load-bearing types. Each stage takes a typed input and
produces a typed output; bugs are caught at the boundary, not in the
middle of a 200-line function. See PLAN.md §5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, Optional

import pandas as pd
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Stage 1 — Ingestion contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadReport:
    """Audit trail of how the raw CSVs were loaded.

    Surfaces every data-quality anomaly (F3: dropped rows, F6: typo
    normalisations, schema mismatches) so they end up in the report
    appendix rather than vanishing.
    """

    rows_per_source: dict[str, int]
    rows_dropped_per_source: dict[str, int]
    parse_warnings: list[str]
    normalisations_applied: list[tuple[str, str, str]]  # (source, from, to)


@dataclass(frozen=True)
class LoadResult:
    """All eight CSVs as DataFrames, plus the LoadReport."""

    accounts: pd.DataFrame
    billing: pd.DataFrame
    contracts: pd.DataFrame
    crm_interactions: pd.DataFrame
    emails: pd.DataFrame
    product_usage: pd.DataFrame
    purchase_orders: pd.DataFrame
    support_tickets: pd.DataFrame
    report: LoadReport


# ---------------------------------------------------------------------------
# Stage 2 — Account assembly contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JoinReport:
    """Audit trail of the multi-key joins (F1, F9).

    For each source, records which key matched how many rows. Lets us
    show "billing: 146 rows (53 by contract_reference, 90 by invoice_id
    pattern, 3 by credit_note_ref)" in the appendix.
    """

    rows_matched_per_source: dict[str, int]
    keys_used_per_source: dict[str, dict[str, int]]
    assumptions: list[str]


@dataclass(frozen=True)
class AccountContext:
    """Per-source DataFrames filtered to a single account."""

    account_id: str
    customer_name: str
    contract_ids: list[str]
    invoice_prefix: str  # e.g. "MH" for Meridian; derived from contracts
    billing: pd.DataFrame
    product_usage: pd.DataFrame
    support_tickets: pd.DataFrame
    crm_interactions: pd.DataFrame
    emails: pd.DataFrame
    purchase_orders: pd.DataFrame
    join_report: JoinReport


# ---------------------------------------------------------------------------
# Stage 3 — Deterministic summary contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BillingFacts:
    invoiced_eur: float
    paid_eur: float
    credits_eur: float
    refunds_eur: float
    outstanding_eur: float
    invoices_issued: int
    payments_received: int
    partial_payments: int
    reminders_sent: int
    overdue_events: int
    max_days_past_due: int
    disputes_opened: int
    disputes_resolved: int
    open_disputes: int
    first_reminder_at: Optional[date]
    last_payment_at: Optional[date]


@dataclass(frozen=True)
class DepartmentUsage:
    total_sessions: int
    mean_active_users: float
    mean_feature_adoption_pct: float
    mean_seat_utilisation_pct: float


@dataclass(frozen=True)
class UsageFacts:
    rows: int
    weeks_observed: int
    date_range: tuple[date, date]
    departments_raw: list[str]
    departments_canonical: list[str]
    sessions_slope_per_week: Optional[float]
    q1_avg_sessions: Optional[float]
    q4_avg_sessions: Optional[float]
    q1_to_q4_pct_change: Optional[float]
    top_department_by_sessions: Optional[str]
    by_department: dict[str, DepartmentUsage]


@dataclass(frozen=True)
class SupportFacts:
    raw_lines_in_file: int
    interactions_parsed: int
    interactions_dropped: int
    unique_tickets: int
    open_tickets: int
    resolved_tickets: int
    sla_breaches: int
    mean_csat: Optional[float]
    survey_count: int
    mean_resolution_days: Optional[float]
    max_resolution_days: Optional[float]
    by_category: dict[str, int]
    by_priority: dict[str, int]
    by_status: dict[str, int]


@dataclass(frozen=True)
class RuleFlag:
    id: str         # e.g. "late-payment-pattern"
    severity: Literal["info", "warn", "alert"]
    message: str
    evidence: list[str]  # AccountSummary paths or counts


@dataclass(frozen=True)
class AccountSummary:
    account_id: str
    customer_name: str
    as_of: datetime
    billing: BillingFacts
    usage: UsageFacts
    support: SupportFacts
    rule_flags: list[RuleFlag]


# ---------------------------------------------------------------------------
# Stage 4 — Signal retriever contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextSignal:
    """A bounded free-text excerpt that the LLM should consider.

    The id format is `<source>:<row_id>:<field>` so the verifier can
    check that any cited TextSignal actually exists in the source.
    """

    id: str
    source: str
    timestamp: Optional[datetime]
    text: str
    why_selected: str


# ---------------------------------------------------------------------------
# Stage 5 — LLM synthesis contracts (Pydantic for runtime validation)
# ---------------------------------------------------------------------------


class TalkingPoint(BaseModel):
    headline: str = Field(..., min_length=10, max_length=200)
    detail: str = Field(..., min_length=20, max_length=800)
    cites: list[str] = Field(..., min_length=1, max_length=8)


class RenewalVerdict(BaseModel):
    """The single artefact the LLM produces. Schema-enforced via
    Anthropic tool-use; content-verified by Stage 6.
    """

    verdict: Literal["Green", "Yellow", "Red"]
    one_sentence_reason: str = Field(..., min_length=20, max_length=400)
    executive_narrative: str = Field(..., min_length=100, max_length=3500)
    talking_points: list[TalkingPoint] = Field(..., min_length=3, max_length=3)
    citations_used: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 6 — Verification contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    diagnostics: list[str]  # human-readable. Empty if passed.

    @property
    def summary(self) -> str:
        if self.passed:
            return "verification passed"
        return f"verification FAILED ({len(self.diagnostics)} issue(s))"


# ---------------------------------------------------------------------------
# Pipeline-level metadata
# ---------------------------------------------------------------------------


@dataclass
class RunMetadata:
    """Captured per pipeline run for the PDF appendix and BENCHMARK.md."""

    account_id: str
    started_at: datetime
    code_version: str
    model_id: str
    wall_time_s: float = 0.0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_calls: int = 0
    cost_usd_estimate: float = 0.0
    verification: Optional[VerificationResult] = None
    fallback_used: bool = False
    notes: list[str] = field(default_factory=list)
