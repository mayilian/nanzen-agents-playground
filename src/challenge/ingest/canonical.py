"""Tiered categorical canonicalisation.

Replaces the older hardcoded ``NORMALISATIONS`` table with a three-tier
strategy whose behaviour is driven by per-(source, column) YAML files
under ``config/canonicalisations/``:

  Tier 1 — strict aliases.  Explicitly enumerated typo → canonical
           mappings. Highest confidence, applied silently (but logged).

  Tier 2 — fuzzy match.    For values not in ``known_values`` and not
           in the alias table, score against ``known_values`` with
           rapidfuzz. Apply if ``score >= threshold`` and string
           length >= ``min_length``. Recorded with the score.

  Tier 3 — surface.        If neither tier applied, the value passes
           through unchanged but is recorded as ``unknown`` on the
           LoadReport for human triage.

This module is deliberately decoupled from the data shape: the caller
(``data_io``) iterates over (source, column) pairs in the file system
and applies whichever rules exist. New customers / columns onboarded
by adding a YAML file — no Python edit.

Why three tiers (and not just one):
- Tier 1 catches the typos a human has confirmed once. Cheap, exact.
- Tier 2 catches the ones we *would* confirm. Auto-applied with audit
  trail; reviewable in the LoadReport.
- Tier 3 ensures novel values are visible. We don't silently fix
  things we haven't decided about — drift detection requires drift
  to be visible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz

from challenge.config import config_dir, load_yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CanonicalisationRule:
    """Rules for one (source, column) pair, loaded from YAML."""

    known_values: list[str]
    aliases: dict[str, str]
    fuzzy_enabled: bool
    fuzzy_threshold: float
    fuzzy_min_length: int


@dataclass
class CanonicalisationOutcome:
    """The result of normalising a single value.

    ``tier`` indicates how (and whether) we corrected the value:
    - ``"alias"``   — explicit aliases table hit
    - ``"fuzzy"``   — fuzzy match against known_values
    - ``"known"``   — value is already canonical
    - ``"unknown"`` — value not corrected; surfaced for review
    """

    original: str
    canonical: str
    tier: str
    score: Optional[float] = None  # populated for tier == "fuzzy"


@dataclass
class CanonicalisationLog:
    """Per-run accumulator passed to LoadReport. Each list is append-only."""

    aliases_applied: list[tuple[str, str, str]] = field(default_factory=list)  # (col, from, to)
    fuzzy_applied: list[tuple[str, str, str, float]] = field(default_factory=list)  # (col, from, to, score)
    unknown_values: list[tuple[str, str]] = field(default_factory=list)  # (col, value)


def _rule_path(source: str, column: str, override_dir: Optional[Path] = None) -> Path:
    base = override_dir or (config_dir() / "canonicalisations")
    return base / f"{source}.{column}.yaml"


def load_rule(
    source: str, column: str, override_dir: Optional[Path] = None
) -> Optional[CanonicalisationRule]:
    """Load the canonicalisation rule for one (source, column).

    Returns ``None`` if no rule file exists — in which case the value
    is passed through unchanged.
    """
    path = _rule_path(source, column, override_dir)
    if not path.exists():
        return None
    cfg = load_yaml(path)
    fuzzy = cfg.get("fuzzy_match") or {}
    return CanonicalisationRule(
        known_values=list(cfg.get("known_values", [])),
        aliases=dict(cfg.get("aliases", {})),
        fuzzy_enabled=bool(fuzzy.get("enabled", False)),
        fuzzy_threshold=float(fuzzy.get("threshold", 0.88)),
        fuzzy_min_length=int(fuzzy.get("min_length", 4)),
    )


def normalise_value(
    value: str,
    rule: CanonicalisationRule,
) -> CanonicalisationOutcome:
    """Apply tiered canonicalisation to a single value.

    Args:
        value: the raw value as it appears in the CSV (string).
        rule:  loaded rule for this (source, column).

    Returns:
        CanonicalisationOutcome describing the action taken.
    """
    if value is None or value == "":
        return CanonicalisationOutcome(original=value, canonical=value, tier="known")

    if value in rule.known_values:
        return CanonicalisationOutcome(original=value, canonical=value, tier="known")

    # Tier 1: explicit alias.
    if value in rule.aliases:
        return CanonicalisationOutcome(
            original=value,
            canonical=rule.aliases[value],
            tier="alias",
        )

    # Tier 2: fuzzy match.
    if rule.fuzzy_enabled and len(value) >= rule.fuzzy_min_length and rule.known_values:
        best_score = 0.0
        best_match: Optional[str] = None
        for canonical in rule.known_values:
            score = fuzz.ratio(value, canonical) / 100.0
            if score > best_score:
                best_score = score
                best_match = canonical
        if best_match is not None and best_score >= rule.fuzzy_threshold:
            return CanonicalisationOutcome(
                original=value,
                canonical=best_match,
                tier="fuzzy",
                score=round(best_score, 3),
            )

    # Tier 3: pass through; surface as unknown.
    return CanonicalisationOutcome(
        original=value,
        canonical=value,
        tier="unknown",
    )


def normalise_column(
    df,
    source: str,
    column: str,
    log: CanonicalisationLog,
    override_dir: Optional[Path] = None,
):
    """Apply canonicalisation in-place to one column of a DataFrame.

    No-op if no rule exists for this (source, column). Mutates ``log``
    with every applied/surfaced action.
    """
    rule = load_rule(source, column, override_dir)
    if rule is None or column not in df.columns:
        return df

    seen_unknown: set[str] = set()
    series = df[column].astype(object)

    def _apply(val):
        if not isinstance(val, str):
            return val
        outcome = normalise_value(val, rule)
        if outcome.tier == "alias":
            log.aliases_applied.append((column, outcome.original, outcome.canonical))
        elif outcome.tier == "fuzzy":
            log.fuzzy_applied.append(
                (column, outcome.original, outcome.canonical, outcome.score or 0.0)
            )
        elif outcome.tier == "unknown" and outcome.original not in seen_unknown:
            seen_unknown.add(outcome.original)
            log.unknown_values.append((column, outcome.original))
        return outcome.canonical

    df = df.copy()
    df[column] = series.map(_apply)
    return df
