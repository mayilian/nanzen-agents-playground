"""Stage 6 — Verification gate.

Hard checks before the PDF is rendered. The pipeline either lets the
verdict through (verification passed) or marks the run for fallback to
a deterministic-only narrative.

Three classes of check:

  Numeric — every numeric token in the verdict's prose must derive
            from the AccountSummary or a cited TextSignal text. Years
            (4-digit ints in the configured range) are tolerated.

  Citation — every TKT-/INV-/DISP-/CN-/etc. token must exist in the
             source data (either explicitly in the summary or in the
             curated signals).

  Structural — talking points each cite ≥1 thing; narrative meets a
               minimum length (Pydantic already enforces the upper
               bound).

Configuration lives in ``config/verification/<policy>.yaml``: number
tolerance, year range, entity-id prefix list, etc.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from challenge.config import config_dir, load_yaml
from challenge.models import (
    AccountSummary,
    RenewalVerdict,
    TextSignal,
    VerificationResult,
)


# ---------------------------------------------------------------------------
# Regex constants — purely structural, not policy-tunable.
# ---------------------------------------------------------------------------

# Numeric tokens with optional thousands separators, decimals, currency,
# percent. Matches "€509,852.11", "15.7%", "27,044", "0.41".
NUMBER_RE = re.compile(
    r"""
    (?:€|\$|£)?         # optional currency
    \d{1,3}             # leading digits
    (?:,\d{3})+         # thousands groups
    (?:\.\d+)?          # optional decimals
    %?                  # optional percent
    |
    (?:€|\$|£)?
    \d+(?:\.\d+)?
    %?
    """,
    re.VERBOSE,
)

_YEAR_RE = re.compile(r"^\d{4}$")


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationPolicy:
    """Verifier configuration loaded from
    ``config/verification/<name>.yaml``.

    Tightening these without changing tests will likely produce more
    fallback runs; loosening admits more model output. Either is a
    deliberate trade-off.
    """

    small_numbers_tolerated: set[str]
    year_range: tuple[int, int]
    relative_tolerance: float
    narrative_min_chars: int
    entity_id_prefixes: tuple[str, ...]
    entity_id_re: re.Pattern[str]


def _build_entity_re(prefixes: list[str]) -> re.Pattern[str]:
    """Compile the entity-id prefix list into a single regex."""
    alt = "|".join(re.escape(p) for p in prefixes) or "ENTITY"
    return re.compile(rf"\b(?:{alt})-[\w/-]+\b")


def load_verification_policy(name: str = "renewal") -> VerificationPolicy:
    """Load a named verifier policy from
    ``config/verification/{name}.yaml``."""
    cfg = load_yaml(config_dir() / "verification" / f"{name}.yaml")
    prefixes = list(cfg.get("entity_id_prefixes", []))
    return VerificationPolicy(
        small_numbers_tolerated={
            str(n) for n in cfg.get("small_numbers_tolerated", [0, 1, 2, 3])
        },
        year_range=tuple(cfg.get("year_range", [1990, 2100])),  # type: ignore[arg-type]
        relative_tolerance=float(cfg.get("relative_tolerance", 0.005)),
        narrative_min_chars=int(cfg.get("narrative_min_chars", 50)),
        entity_id_prefixes=tuple(prefixes),
        entity_id_re=_build_entity_re(prefixes),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_number(token: str) -> float | None:
    """Best-effort numeric parse. Returns None on failure."""
    s = token.replace("€", "").replace("$", "").replace("£", "").replace(",", "")
    s = s.rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def _summary_numbers(
    summary: AccountSummary,
    signals: list[TextSignal],
    entity_re: re.Pattern[str],
) -> set[float]:
    """Collect every numeric value derivable from the summary or a cited
    signal.

    The model is permitted to mention numbers it found in either place.
    Signal text has entity-id tokens stripped first so digits inside
    ticket ids don't masquerade as numbers.
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

    for sig in signals:
        cleaned = entity_re.sub(" ", sig.text)
        for token in NUMBER_RE.findall(cleaned):
            n = _parse_number(token)
            if n is not None:
                numbers.add(n)
                numbers.add(round(n, 1))
                numbers.add(round(n, 2))

    numbers.update({0.0, 1.0, 2.0, 3.0})
    return numbers


def _within_tolerance(
    claim: float, summary_numbers: set[float], rel_tol: float
) -> bool:
    """Match `claim` against the allowed-numbers set with rounding tolerance."""
    if claim in summary_numbers:
        return True
    for s in summary_numbers:
        if s == 0:
            continue
        if abs(claim - s) <= max(abs(s) * rel_tol, 0.5):
            return True
    for s in summary_numbers:
        if abs(round(claim, 1) - round(s, 1)) <= 0.05:
            return True
    return False


def _collect_entity_ids(
    summary: AccountSummary,
    signals: list[TextSignal],
    entity_re: re.Pattern[str],
) -> set[str]:
    """Collect citable entity ids from rule flags + signals + signal text."""
    ids: set[str] = set()
    for f in summary.rule_flags:
        ids.update(entity_re.findall(f.message))
        ids.update(entity_re.findall(" ".join(f.evidence)))
    for sig in signals:
        ids.add(sig.id)
        ids.update(entity_re.findall(sig.id))
        ids.update(entity_re.findall(sig.text))
    return ids


def _verdict_prose(verdict: RenewalVerdict) -> str:
    parts: list[str] = [verdict.one_sentence_reason, verdict.executive_narrative]
    for tp in verdict.talking_points:
        parts.append(tp.headline)
        parts.append(tp.detail)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify(
    summary: AccountSummary,
    signals: list[TextSignal],
    verdict: RenewalVerdict,
    policy: VerificationPolicy | None = None,
) -> VerificationResult:
    """Apply the verification gate to a candidate ``RenewalVerdict``.

    Args:
      summary: deterministic facts the model was given.
      signals: text excerpts the model was given.
      verdict: model output to verify.
      policy:  optional verifier policy. Defaults to the renewal policy.

    Returns:
      ``VerificationResult`` with ``passed=True`` and an empty
      ``diagnostics`` when the verdict is acceptable; ``passed=False``
      with one human-readable diagnostic per violation otherwise.
    """
    policy = policy or load_verification_policy("renewal")
    entity_re = policy.entity_id_re

    diagnostics: list[str] = []
    summary_numbers = _summary_numbers(summary, signals, entity_re)
    citable_ids = _collect_entity_ids(summary, signals, entity_re)
    citable_id_set = {i.upper() for i in citable_ids}

    prose = _verdict_prose(verdict)
    prose_for_numbers = entity_re.sub(" ", prose)

    # 1) Numeric token check.
    for token in NUMBER_RE.findall(prose_for_numbers):
        bare = (
            token.replace("€", "").replace("$", "").replace("£", "")
            .replace(",", "").rstrip("%")
        )
        if bare in policy.small_numbers_tolerated:
            continue
        if _YEAR_RE.match(bare):
            year_val = int(bare)
            if policy.year_range[0] <= year_val <= policy.year_range[1]:
                continue
        n = _parse_number(token)
        if n is None:
            continue
        if not _within_tolerance(n, summary_numbers, policy.relative_tolerance):
            diagnostics.append(
                f"numeric '{token}' (parsed {n}) does not derive from the summary"
            )

    # 2) Citation check.
    for entity in entity_re.findall(prose):
        upper = entity.upper()
        if upper in citable_id_set:
            continue
        # Substring match: an entity like "INT-1158" is OK if a citable id
        # contains it (e.g. "support_tickets:INT-1158.content").
        if any(upper in c for c in citable_id_set):
            continue
        diagnostics.append(f"cited entity '{entity}' not in source data")

    # 3) Self-consistency.
    for i, tp in enumerate(verdict.talking_points, 1):
        if not tp.cites:
            diagnostics.append(f"talking point #{i} has no citations")

    # 4) Length sanity (lower bound only; Pydantic enforces upper).
    if len(verdict.executive_narrative) < policy.narrative_min_chars:
        diagnostics.append(
            f"executive_narrative length {len(verdict.executive_narrative)} below "
            f"{policy.narrative_min_chars}"
        )

    return VerificationResult(passed=not diagnostics, diagnostics=diagnostics)
