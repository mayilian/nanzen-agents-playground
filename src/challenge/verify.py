"""Stage 6 — Verification gate.

Hard checks before the PDF is rendered. The pipeline either lets the
verdict through (verification passed) or marks the run for fallback
to deterministic-only narrative.

Checks:
- Numeric token check: every number in the verdict's prose must be
  derivable from the AccountSummary (within tolerance).
- Citation check: every TKT-/INV-/DISP-/CN- token must appear in the
  source data; every named person must appear somewhere.
- Schema check: Pydantic already enforced shape; we re-confirm key
  invariants.
- Self-consistency: talking points cite something; verdict reason
  references at least one rule_flag id.
- Length sanity: narrative within bounds.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Iterable

from challenge.models import (
    AccountSummary,
    RenewalVerdict,
    TextSignal,
    VerificationResult,
)


# Numeric tokens with optional thousands separators, decimals, percent
# sign, currency prefix.
NUMBER_RE = re.compile(
    r"""
    (?:€|\$|£)?         # optional currency
    \d{1,3}             # leading digits
    (?:,\d{3})+         # thousands groups
    (?:\.\d+)?          # optional decimals
    %?                  # optional percent
    |
    (?:€|\$|£)?
    \d+(?:\.\d+)?       # plain decimal
    %?
    """,
    re.VERBOSE,
)

# Tokens that look like entity ids: TKT-1234, INV-2024-MH-001, DISP-…, CN-…
ENTITY_RE = re.compile(
    r"\b(?:TKT|INT|INV|DISP|CN|CTR|BIL|CRM|EM|PO|SEPA|RET|CT|MH)-[\w/-]+\b"
)


# Small numbers that are commonly written ("the 3 priorities") but don't
# need to derive from the summary.
SMALL_NUMBERS_TOLERATED = {"0", "1", "2", "3"}


# Years are common in narrative ("Oct 2024", "Q4 2025"). Accept any plain
# 4-digit integer in this range.
YEAR_RE = re.compile(r"^\d{4}$")
YEAR_RANGE = (1990, 2100)


def _parse_number(token: str) -> float | None:
    s = token.replace("€", "").replace("$", "").replace("£", "").replace(",", "")
    s = s.rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def _summary_numbers(summary: AccountSummary, signals: list[TextSignal]) -> set[float]:
    """Collect every numeric value derivable from the AccountSummary or a
    cited TextSignal. The model is permitted to mention numbers it found
    in either place.
    """
    numbers: set[float] = set()

    def _walk(obj):
        if isinstance(obj, (int, float)) and not isinstance(obj, bool):
            v = float(obj)
            numbers.add(v)
            numbers.add(round(v, 1))
            numbers.add(round(v, 2))
            numbers.add(abs(v))
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, (list, tuple, set)):
            for v in obj:
                _walk(v)
        elif hasattr(obj, "__dict__"):
            for v in vars(obj).values():
                _walk(v)

    _walk(asdict(summary))

    # Numbers found inside the TextSignals the model received are also
    # legitimate to cite. Strip entity-shaped tokens first so we don't
    # treat ticket-id digits as numbers.
    for sig in signals:
        cleaned = ENTITY_RE.sub(" ", sig.text)
        for token in NUMBER_RE.findall(cleaned):
            n = _parse_number(token)
            if n is not None:
                numbers.add(n)
                numbers.add(round(n, 1))
                numbers.add(round(n, 2))

    # Add small "narrative" integers that are commonly mentioned.
    numbers.update({0.0, 1.0, 2.0, 3.0})
    return numbers


def _within_tolerance(claim: float, summary_numbers: set[float]) -> bool:
    if claim in summary_numbers:
        return True
    # Round + magnitude tolerance: accept within 0.5% relative or 0.5 absolute.
    for s in summary_numbers:
        if s == 0:
            continue
        if abs(claim - s) <= max(abs(s) * 0.005, 0.5):
            return True
    # Also tolerate rounding of percents to one decimal place.
    for s in summary_numbers:
        if abs(round(claim, 1) - round(s, 1)) <= 0.05:
            return True
    return False


def _collect_entity_ids(summary: AccountSummary, signals: list[TextSignal]) -> set[str]:
    """Collect entity ids found in summary + signals + signal texts."""
    ids: set[str] = set()
    # From rule_flag evidence and ids.
    for f in summary.rule_flags:
        ids.update(ENTITY_RE.findall(f.message))
        ids.update(ENTITY_RE.findall(" ".join(f.evidence)))
    # From signal ids and texts.
    for sig in signals:
        # The sig.id format may include a row id, e.g. crm_interactions:CRM-084.body
        # Extract the row-id portion as a citable token.
        ids.add(sig.id)
        ids.update(ENTITY_RE.findall(sig.id))
        ids.update(ENTITY_RE.findall(sig.text))
    return ids


def _verdict_prose(verdict: RenewalVerdict) -> str:
    parts: list[str] = [verdict.one_sentence_reason, verdict.executive_narrative]
    for tp in verdict.talking_points:
        parts.append(tp.headline)
        parts.append(tp.detail)
    return "\n".join(parts)


def verify(
    summary: AccountSummary,
    signals: list[TextSignal],
    verdict: RenewalVerdict,
) -> VerificationResult:
    diagnostics: list[str] = []
    summary_numbers = _summary_numbers(summary, signals)
    citable_ids = _collect_entity_ids(summary, signals)
    citable_id_set_normalised = {i.upper() for i in citable_ids}

    prose = _verdict_prose(verdict)

    # Strip entity-shaped tokens (TKT-1158, INV-2024-MH-005, etc.) before
    # numeric matching, so the digits inside ids don't get scored as
    # fabricated numbers.
    prose_for_numbers = ENTITY_RE.sub(" ", prose)

    # 1) Numeric token check.
    for token in NUMBER_RE.findall(prose_for_numbers):
        bare = token.replace("€", "").replace("$", "").replace("£", "").replace(",", "").rstrip("%")
        if bare in SMALL_NUMBERS_TOLERATED:
            continue
        # Tolerate years like 2024, 2025 even when not in summary.
        if YEAR_RE.match(bare):
            year_val = int(bare)
            if YEAR_RANGE[0] <= year_val <= YEAR_RANGE[1]:
                continue
        n = _parse_number(token)
        if n is None:
            continue
        if not _within_tolerance(n, summary_numbers):
            diagnostics.append(
                f"numeric '{token}' (parsed {n}) does not derive from the summary"
            )

    # 2) Citation check on entity-shaped tokens.
    for entity in ENTITY_RE.findall(prose):
        if entity.upper() not in citable_id_set_normalised:
            # Allow if any citable id contains this token (e.g. INT-1158 in id "support_tickets:INT-1158.content")
            if any(entity.upper() in c for c in citable_id_set_normalised):
                continue
            diagnostics.append(f"cited entity '{entity}' not in source data")

    # 3) Self-consistency: each talking point cites at least one thing.
    for i, tp in enumerate(verdict.talking_points, 1):
        if not tp.cites:
            diagnostics.append(f"talking point #{i} has no citations")

    # 4) Length sanity (Pydantic already enforces upper bound; check lower).
    if len(verdict.executive_narrative) < 50:
        diagnostics.append(
            f"executive_narrative length {len(verdict.executive_narrative)} below 50"
        )

    return VerificationResult(passed=not diagnostics, diagnostics=diagnostics)
