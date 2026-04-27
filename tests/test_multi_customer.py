"""Multi-customer hygiene: a second customer's data must not bleed into
the first one's report.

These tests build synthetic two-customer fixtures *in-memory* and run
them through the data layer. They are the regression coverage that
would have caught the original "treat all rows as customer X" bug.

We do not modify the on-disk data/ directory; we patch ``data_io``'s
data dir to point at a temp directory we materialise.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from challenge.customer import CustomerConfig, RiskThresholds, TextMentionPolicy
from challenge.data_io import load_all
from challenge.joins import build_account_context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_csv(dir_: Path, name: str, df: pd.DataFrame) -> None:
    """Write a DataFrame to CSV — we use this to assemble the fixture."""
    df.to_csv(dir_ / f"{name}.csv", index=False)


def _make_two_customer_fixture(dir_: Path) -> None:
    """Materialise a two-customer dataset: MERID-001 and ATLAS-001.

    Includes one explicit cross-contamination trap: a billing event that
    references an Atlas invoice but mentions Meridian in its notes
    field. Correct resolution should attribute it to ATLAS-001.
    """
    accounts = pd.DataFrame(
        [
            {
                "account_id": "MERID-001",
                "customer_name": "Meridian Health",
                "industry": "healthcare",
                "segment": "enterprise",
            },
            {
                "account_id": "ATLAS-001",
                "customer_name": "Atlas Logistics",
                "industry": "logistics",
                "segment": "mid-market",
            },
        ]
    )
    contracts = pd.DataFrame(
        [
            {
                "contract_id": "CTR-2024-MH-001",
                "account_id": "MERID-001",
                "customer_name": "Meridian Health",
            },
            {
                "contract_id": "CTR-2024-AL-001",
                "account_id": "ATLAS-001",
                "customer_name": "Atlas Logistics",
            },
        ]
    )
    billing = pd.DataFrame(
        [
            # Meridian invoice + payment
            {
                "event_id": "BIL-1",
                "event_type": "invoice_issued",
                "amount": 10000.0,
                "contract_reference": "CTR-2024-MH-001",
                "invoice_id": "INV-2024-MH-001",
                "credit_note_ref": None,
                "status": "issued",
                "days_past_due": None,
                "currency": "EUR",
                "timestamp": "2024-01-01",
                "description": None,
                "remittance_ref": None,
                "bank_reference": None,
                "notes": None,
            },
            {
                "event_id": "BIL-2",
                "event_type": "payment_received",
                "amount": 10000.0,
                "contract_reference": None,
                "invoice_id": "INV-2024-MH-001",
                "credit_note_ref": None,
                "status": "paid",
                "days_past_due": None,
                "currency": "EUR",
                "timestamp": "2024-01-15",
                "description": None,
                "remittance_ref": None,
                "bank_reference": None,
                "notes": None,
            },
            # Atlas invoice + payment
            {
                "event_id": "BIL-3",
                "event_type": "invoice_issued",
                "amount": 5000.0,
                "contract_reference": "CTR-2024-AL-001",
                "invoice_id": "INV-2024-AL-001",
                "credit_note_ref": None,
                "status": "issued",
                "days_past_due": None,
                "currency": "EUR",
                "timestamp": "2024-02-01",
                "description": None,
                "remittance_ref": None,
                "bank_reference": None,
                "notes": None,
            },
            # Cross-contamination trap: Atlas invoice but mentions Meridian
            # in description. Correct resolver should attribute to Atlas.
            {
                "event_id": "BIL-4",
                "event_type": "payment_received",
                "amount": 5000.0,
                "contract_reference": None,
                "invoice_id": "INV-2024-AL-001",
                "credit_note_ref": None,
                "status": "paid",
                "days_past_due": None,
                "currency": "EUR",
                "timestamp": "2024-02-15",
                "description": "Bank also references prior Meridian transfer (unrelated)",
                "remittance_ref": None,
                "bank_reference": None,
                "notes": None,
            },
        ]
    )
    product_usage = pd.DataFrame(
        [
            {
                "week_start": "2024-01-01",
                "account_id": "MERID-001",
                "department": "Engineering",
                "total_sessions": 100,
                "active_users": 5,
                "feature_adoption_pct": 50.0,
                "seat_utilization_pct": 80.0,
            },
            {
                "week_start": "2024-01-08",
                "account_id": "MERID-001",
                "department": "Engineering",
                "total_sessions": 120,
                "active_users": 6,
                "feature_adoption_pct": 55.0,
                "seat_utilization_pct": 85.0,
            },
            {
                "week_start": "2024-01-01",
                "account_id": "ATLAS-001",
                "department": "Operations",
                "total_sessions": 50,
                "active_users": 2,
                "feature_adoption_pct": 30.0,
                "seat_utilization_pct": 40.0,
            },
        ]
    )
    support_tickets = pd.DataFrame(
        [
            {
                "interaction_id": "INT-1",
                "ticket_id": "TKT-100",
                "interaction_type": "ticket_created",
                "timestamp": "2024-03-01",
                "ticket_status": "open",
                "priority": "medium",
                "category": "bug",
                "customer_department": "Engineering",
                "sla_breach": 0,
                "satisfaction_score": None,
                "content": "Meridian: API timeout on staging",
            },
            {
                "interaction_id": "INT-2",
                "ticket_id": "TKT-200",
                "interaction_type": "ticket_created",
                "timestamp": "2024-03-02",
                "ticket_status": "open",
                "priority": "low",
                "category": "how_to",
                "customer_department": "Operations",
                "sla_breach": 0,
                "satisfaction_score": None,
                "content": "Atlas: how do I export reports?",
            },
        ]
    )
    crm = pd.DataFrame(
        [
            {
                "id": "CRM-1",
                "timestamp": "2024-04-01",
                "activity_type": "call",
                "contact_email": "j.smith@meridianhealth.com",
                "subject": "Meridian renewal prep",
                "body": "Renewal prep call",
            },
            {
                "id": "CRM-2",
                "timestamp": "2024-04-02",
                "activity_type": "email",
                "contact_email": "ops@atlaslogistics.com",
                "subject": "Atlas QBR scheduling",
                "body": "QBR scheduling discussion",
            },
        ]
    )
    emails = pd.DataFrame(
        [
            {
                "email_id": "EM-1",
                "timestamp": "2024-04-03",
                "from": "billing@athenacloud.com",
                "to": "j.park@meridianhealth.com",
                "subject": "Invoice INV-2024-MH-001",
                "body": "Attached.",
            },
            {
                "email_id": "EM-2",
                "timestamp": "2024-04-04",
                "from": "billing@athenacloud.com",
                "to": "billing@atlaslogistics.com",
                "subject": "Invoice INV-2024-AL-001",
                "body": "Attached.",
            },
        ]
    )
    purchase_orders = pd.DataFrame(
        [
            {
                "po_number": "PO-MH-001",
                "issued_date": "2024-01-01",
                "contract_reference": "CTR-2024-MH-001",
            },
            {
                "po_number": "PO-AL-001",
                "issued_date": "2024-02-01",
                "contract_reference": "CTR-2024-AL-001",
            },
        ]
    )

    _write_csv(dir_, "accounts", accounts)
    _write_csv(dir_, "contracts", contracts)
    _write_csv(dir_, "billing", billing)
    _write_csv(dir_, "product_usage", product_usage)
    _write_csv(dir_, "support_tickets", support_tickets)
    _write_csv(dir_, "crm_interactions", crm)
    _write_csv(dir_, "emails", emails)
    _write_csv(dir_, "purchase_orders", purchase_orders)


def _make_customer_configs(dir_: Path) -> None:
    """Write per-customer config YAMLs alongside a tests-only defaults
    file with policy=skip so unresolvable rows are dropped (production
    safe default rather than dataset assumption)."""
    (dir_ / "_defaults.yaml").write_text(
        "contract_id_pattern: \"^CTR-\\\\d{4}-(?P<code>[A-Z]{2,4})-\\\\d{3}$\"\n"
        "text_mention:\n"
        "  use_customer_first_word: false\n"
        "  fields: [description, remittance_ref, bank_reference, notes]\n"
        "risk_thresholds:\n"
        "  outstanding_eur_alert: 5000\n"
        "  reminder_count_warn: 3\n"
        "  sla_breach_alert: 5\n"
        "  csat_min_warn: 3.5\n"
        "unresolvable_row_policy: skip\n"
    )
    (dir_ / "MERID-001.yaml").write_text(
        "account_id: MERID-001\n"
        "canonical_name: \"Meridian Health\"\n"
        "aliases: [\"Meridian Health\", \"Meridian\"]\n"
        "email_domains: [meridianhealth.com]\n"
        "departments: [Engineering, Marketing, Procurement, All]\n"
        "invoice_prefix: MH\n"
    )
    (dir_ / "ATLAS-001.yaml").write_text(
        "account_id: ATLAS-001\n"
        "canonical_name: \"Atlas Logistics\"\n"
        "aliases: [\"Atlas Logistics\", \"Atlas\"]\n"
        "email_domains: [atlaslogistics.com]\n"
        "departments: [Operations]\n"
        "invoice_prefix: AL\n"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _build_load_and_ctx(tmp_path: Path, account_id: str):
    """Materialise fixtures into tmp_path and return (load, ctx)."""
    data_dir = tmp_path / "data"
    cfg_dir = tmp_path / "customers"
    data_dir.mkdir()
    cfg_dir.mkdir()
    _make_two_customer_fixture(data_dir)
    _make_customer_configs(cfg_dir)

    from challenge.config import reset_cache
    from challenge.customer import load_customer_config

    reset_cache()
    load = load_all(data_dir=data_dir)
    customer_cfg = load_customer_config(account_id, override_dir=cfg_dir)
    ctx = build_account_context(load, account_id, customer_cfg=customer_cfg)
    return load, ctx


def test_no_cross_contamination_billing(tmp_path):
    """Meridian's report must not include Atlas's invoice."""
    _, ctx = _build_load_and_ctx(tmp_path, "MERID-001")
    invoice_ids = set(ctx.billing["invoice_id"].dropna().unique())
    assert "INV-2024-MH-001" in invoice_ids
    assert "INV-2024-AL-001" not in invoice_ids
    # The cross-contamination trap event (BIL-4) belongs to Atlas
    # and should not appear in Meridian's billing.
    assert "BIL-4" not in set(ctx.billing["event_id"].unique())


def test_no_cross_contamination_support(tmp_path):
    """Meridian must only see its own ticket (TKT-100)."""
    _, ctx = _build_load_and_ctx(tmp_path, "MERID-001")
    ticket_ids = set(ctx.support_tickets["ticket_id"].unique())
    assert "TKT-100" in ticket_ids
    assert "TKT-200" not in ticket_ids


def test_no_cross_contamination_emails(tmp_path):
    """Meridian must only see emails involving its domain."""
    _, ctx = _build_load_and_ctx(tmp_path, "MERID-001")
    email_ids = set(ctx.emails["email_id"].unique())
    assert "EM-1" in email_ids
    assert "EM-2" not in email_ids


def test_atlas_resolves_correctly(tmp_path):
    """The reverse case — Atlas sees only Atlas's data."""
    _, ctx = _build_load_and_ctx(tmp_path, "ATLAS-001")
    invoice_ids = set(ctx.billing["invoice_id"].dropna().unique())
    assert "INV-2024-AL-001" in invoice_ids
    assert "INV-2024-MH-001" not in invoice_ids
    ticket_ids = set(ctx.support_tickets["ticket_id"].unique())
    assert ticket_ids == {"TKT-200"}


def test_unknown_account_raises(tmp_path):
    """An account not in accounts.csv must fail loudly."""
    data_dir = tmp_path / "data"
    cfg_dir = tmp_path / "customers"
    data_dir.mkdir()
    cfg_dir.mkdir()
    _make_two_customer_fixture(data_dir)
    _make_customer_configs(cfg_dir)

    from challenge.config import reset_cache

    reset_cache()
    load = load_all(data_dir=data_dir)
    with pytest.raises(ValueError, match="not found"):
        build_account_context(load, "NOPE-001")


def test_missing_customer_config_raises(tmp_path):
    """A customer in accounts.csv without a config file must fail loudly."""
    data_dir = tmp_path / "data"
    cfg_dir = tmp_path / "customers"
    data_dir.mkdir()
    cfg_dir.mkdir()
    _make_two_customer_fixture(data_dir)
    # Intentionally do NOT create the customer config files.

    from challenge.config import reset_cache
    from challenge.customer import load_customer_config

    reset_cache()
    with pytest.raises(FileNotFoundError, match="customer config"):
        load_customer_config("MERID-001", override_dir=cfg_dir)
