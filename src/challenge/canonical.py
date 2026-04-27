"""Categorical canonicalisation tables (F6).

Each table maps observed-but-wrong values to their canonical form.
Applied during ingestion; every applied normalisation is recorded on
the LoadReport so it ends up in the PDF appendix.

Keep these tables small and explicit. If a column ends up needing
hundreds of mappings, that's a signal it should be classified, not
mapped.
"""

from __future__ import annotations

# (source, column) -> {wrong: canonical}
NORMALISATIONS: dict[tuple[str, str], dict[str, str]] = {
    # F6: 'Enginering' (single 'e') appears alongside 'Engineering' in
    # product_usage.csv. Without normalisation, groupby splits totals.
    ("product_usage", "department"): {
        "Enginering": "Engineering",
    },
}


def normalise(
    source: str, column: str, value: str
) -> tuple[str, bool]:
    """Return (canonical_value, was_normalised)."""
    table = NORMALISATIONS.get((source, column))
    if table is None:
        return value, False
    if value in table:
        return table[value], True
    return value, False
