"""Stage 1 — CSV ingestion with validation and normalisation.

Loads the eight CSVs once. Records every data-quality anomaly on the
LoadReport rather than silently swallowing it. Replaces the silent
behaviour in the old `tools/csv_reader.py`.

Design notes:
- F3: malformed `support_tickets.csv` row at line 153 — we use
  `engine="python"` with `on_bad_lines="skip"` to recover what we can,
  and record the dropped count.
- F6: `product_usage.department` typo (`Enginering`) is canonicalised
  via `canonical.NORMALISATIONS`.
- Schemas are validated by required-column presence; missing columns
  raise a clear error rather than silently propagating as KeyError
  later.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from challenge.canonical import CanonicalisationLog, normalise_column
from challenge.config import config_dir
from challenge.models import LoadReport, LoadResult

logger = logging.getLogger(__name__)

# Project-relative resolution. data_io.py lives in src/challenge/, so
# the project root is parents[2].
DATA_DIR_DEFAULT = Path(__file__).resolve().parents[2] / "data"


# Required columns per CSV. Used for schema validation at load time.
REQUIRED_COLUMNS: dict[str, set[str]] = {
    "accounts": {"account_id", "customer_name", "industry", "segment"},
    "billing": {"event_id", "event_type", "amount", "contract_reference"},
    "contracts": {"contract_id", "account_id", "customer_name"},
    "crm_interactions": {"id", "timestamp", "activity_type"},
    "emails": {"email_id", "timestamp", "from", "to"},
    "product_usage": {"week_start", "account_id", "department", "total_sessions"},
    "purchase_orders": {"po_number", "issued_date"},
    "support_tickets": {"interaction_id", "ticket_id", "interaction_type"},
}


def _read_csv(
    path: Path,
    source: str,
    parse_warnings: list[str],
    rows_dropped: dict[str, int],
) -> pd.DataFrame:
    """Defensive CSV read.

    Tries the C parser first (fast); falls back to the Python parser
    with `on_bad_lines="skip"` so a single malformed row doesn't
    nuke the whole file (F3). Records the dropped count.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"required CSV not found: {path}. "
            f"Did the data/ directory get moved?"
        )

    # Count physical lines so we can compute "dropped" against bad-line skips.
    with open(path, "rb") as f:
        physical_lines = sum(1 for _ in f)
    expected_data_rows = max(physical_lines - 1, 0)  # minus header

    try:
        df = pd.read_csv(path)
    except pd.errors.ParserError as exc:
        parse_warnings.append(
            f"{source}: C parser failed ({exc.args[0].splitlines()[0]}); "
            f"falling back to python parser with on_bad_lines='skip'"
        )
        df = pd.read_csv(path, engine="python", on_bad_lines="skip")
    except UnicodeDecodeError:
        parse_warnings.append(f"{source}: utf-8 decode failed; retrying as latin-1")
        df = pd.read_csv(path, encoding="latin-1", engine="python", on_bad_lines="skip")

    dropped = max(expected_data_rows - len(df), 0)
    if dropped > 0:
        rows_dropped[source] = dropped
        parse_warnings.append(
            f"{source}: dropped {dropped} row(s) due to malformed CSV (likely "
            f"unterminated quoted field)"
        )

    # Schema check: required columns present.
    required = REQUIRED_COLUMNS.get(source, set())
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"{source}: missing required column(s) {sorted(missing)}. "
            f"Schema may have drifted; aborting before garbage-in-garbage-out."
        )

    return df


def _apply_canonicalisations(
    df: pd.DataFrame,
    source: str,
    log: CanonicalisationLog,
) -> pd.DataFrame:
    """Apply tiered canonicalisation to every column with a config rule.

    Iterates over each column in the DataFrame and asks ``canonical``
    whether a YAML rule exists for ``(source, column)``. If yes, the
    rule is applied (alias / fuzzy / unknown surfacing). Otherwise the
    column passes through unchanged.

    Mutates ``log`` with every applied/surfaced action.
    """
    for column in df.columns:
        df = normalise_column(df, source, column, log)
    return df


def load_all(data_dir: Path | None = None) -> LoadResult:
    """Load all eight CSVs into a typed bundle with full audit trail.

    The single entry point for raw data; nothing else in the pipeline
    reads CSVs directly.

    Args:
      data_dir: optional override for the data directory. Defaults to
        the project's ``data/`` folder.

    Returns:
      ``LoadResult`` whose ``report`` field carries everything the
      renderer should know about data quality (parse warnings, dropped
      rows, normalisation actions, surfaced unknown values).

    Raises:
      FileNotFoundError: if a required CSV is missing.
      ValueError: if a CSV is missing required columns (schema drift).
    """
    data_dir = data_dir or DATA_DIR_DEFAULT
    parse_warnings: list[str] = []
    rows_dropped: dict[str, int] = {}
    canon_log = CanonicalisationLog()

    # Aggregate (source, column, …) tuples for the LoadReport.
    aliases_with_source: list[tuple[str, str, str]] = []
    fuzzy_with_source: list[tuple[str, str, str, float]] = []
    unknown_with_source: list[tuple[str, str]] = []

    sources: dict[str, pd.DataFrame] = {}
    for source in REQUIRED_COLUMNS:
        per_source_log = CanonicalisationLog()
        df = _read_csv(data_dir / f"{source}.csv", source, parse_warnings, rows_dropped)
        df = _apply_canonicalisations(df, source, per_source_log)
        sources[source] = df

        # Annotate each entry with its source so the LoadReport carries
        # source-attributed tuples.
        for col, frm, to in per_source_log.aliases_applied:
            aliases_with_source.append((source, frm, to))
        for col, frm, to, score in per_source_log.fuzzy_applied:
            fuzzy_with_source.append((source, frm, to, score))
        for col, val in per_source_log.unknown_values:
            unknown_with_source.append((source, val))

    rows_per_source = {s: len(df) for s, df in sources.items()}

    report = LoadReport(
        rows_per_source=rows_per_source,
        rows_dropped_per_source=rows_dropped,
        parse_warnings=parse_warnings,
        normalisations_applied=aliases_with_source,
        fuzzy_normalisations=fuzzy_with_source,
        unknown_categoricals=unknown_with_source,
    )

    return LoadResult(
        accounts=sources["accounts"],
        billing=sources["billing"],
        contracts=sources["contracts"],
        crm_interactions=sources["crm_interactions"],
        emails=sources["emails"],
        product_usage=sources["product_usage"],
        purchase_orders=sources["purchase_orders"],
        support_tickets=sources["support_tickets"],
        report=report,
    )
