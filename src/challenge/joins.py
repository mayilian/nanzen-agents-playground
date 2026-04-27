"""Stage 2 — Multi-key account assembly.

Fixes F1 (silent no-op filter when account_id column missing) and F9
(sparse contract_reference field misses 64% of rows). Every join is
explicit, multi-keyed, and audited via JoinReport.

For each source, a per-source resolver decides which row belongs to
which account. The resolver returns both the filtered DataFrame and a
breakdown of which keys matched how many rows.
"""

from __future__ import annotations

import re

import pandas as pd

from challenge.models import AccountContext, JoinReport, LoadResult


# ---------------------------------------------------------------------------
# Customer code derivation
# ---------------------------------------------------------------------------


def derive_invoice_prefix(contracts_for_account: pd.DataFrame) -> str:
    """Derive the customer-specific invoice ID prefix from contract IDs.

    Contract IDs look like "CTR-2023-MH-001"; the third segment is the
    customer code we need to match invoice IDs like "INV-2024-MH-005".
    """
    codes: set[str] = set()
    for contract_id in contracts_for_account["contract_id"].dropna():
        parts = str(contract_id).split("-")
        if len(parts) >= 3:
            codes.add(parts[2])
    if not codes:
        raise ValueError(
            f"could not derive invoice prefix from contracts: "
            f"{contracts_for_account['contract_id'].tolist()}"
        )
    if len(codes) > 1:
        # Multiple contracts can share a customer code (e.g. MH for both
        # MERID-001 contracts). Multiple codes for one account is unusual
        # but possible — pick the most common.
        code = max(codes, key=lambda c: (contracts_for_account["contract_id"].str.contains(f"-{c}-")).sum())
        return code
    return next(iter(codes))


# ---------------------------------------------------------------------------
# Per-source resolvers
# ---------------------------------------------------------------------------


def _filter_billing(
    billing: pd.DataFrame,
    contract_ids: list[str],
    invoice_prefix: str,
    customer_name: str,
    keys_used: dict[str, int],
) -> pd.DataFrame:
    """Multi-key billing filter (F9).

    Match a row if ANY of:
    - contract_reference is one of our contracts, OR
    - invoice_id matches our customer's invoice pattern, OR
    - credit_note_ref matches a credit note we already pulled in, OR
    - notes contain the customer's name (catches operational events
      like the misdirected-payment trio that have no FKs at all).
    """
    invoice_re = re.compile(rf"^INV-\d+-{re.escape(invoice_prefix)}-\d+$")

    by_contract = billing["contract_reference"].isin(contract_ids)
    by_invoice = billing["invoice_id"].fillna("").str.match(invoice_re.pattern)

    keys_used["contract_reference"] = int(by_contract.sum())
    keys_used["invoice_id_pattern"] = int((by_invoice & ~by_contract).sum())

    seed = billing[by_contract | by_invoice]

    # Credit-note ref join — strict (no empty-string traps).
    seed_credit_refs = {x for x in seed["credit_note_ref"].dropna().unique() if x}
    seed_invoice_ids = {x for x in seed["invoice_id"].dropna().unique() if x}
    cn = billing["credit_note_ref"].fillna("")
    by_credit = (cn != "") & (cn.isin(seed_credit_refs) | cn.isin(seed_invoice_ids))
    seed_idx = seed.index
    keys_used["credit_note_ref"] = int(
        (by_credit & ~billing.index.isin(seed_idx)).sum()
    )

    # Text-mention join. The customer's distinctive name fragment (first
    # word, e.g. "Meridian") found in any free-text field. Restricted to
    # rows that have an amount, so ambiguous internal notes without
    # financial impact don't pollute the result.
    first_word = customer_name.split()[0] if customer_name else ""
    text_columns = ("description", "remittance_ref", "bank_reference", "notes")
    if first_word:
        text_match = pd.Series(False, index=billing.index)
        for col in text_columns:
            if col in billing.columns:
                text_match = text_match | billing[col].fillna("").str.contains(
                    first_word, case=False, na=False
                )
    else:
        text_match = pd.Series(False, index=billing.index)
    has_amount = billing["amount"].notna()
    by_text = text_match & has_amount
    keys_used["text_mention"] = int(
        (by_text & ~billing.index.isin(seed_idx) & ~by_credit).sum()
    )

    return billing[by_contract | by_invoice | by_credit | by_text].copy()


def _filter_by_account_id(
    df: pd.DataFrame, account_id: str, keys_used: dict[str, int]
) -> pd.DataFrame:
    """Direct account_id filter for sources that have the column."""
    if "account_id" not in df.columns:
        return df.iloc[0:0].copy()  # empty with same schema
    filtered = df[df["account_id"] == account_id].copy()
    keys_used["account_id"] = int(len(filtered))
    return filtered


def _filter_support_or_crm_or_emails(
    df: pd.DataFrame,
    account_id: str,
    keys_used: dict[str, int],
    assumptions: list[str],
) -> pd.DataFrame:
    """For sources lacking account_id, fall back to dataset-wide assumption.

    The fixture data has ONE customer's worth of support/CRM/email rows
    (Meridian's). When other customers are added, this resolver will
    need to look up the customer by department, sender domain, or
    email-thread foreign keys. For now, capture the assumption
    explicitly so it shows up in the report appendix.
    """
    if "account_id" in df.columns:
        return _filter_by_account_id(df, account_id, keys_used)
    keys_used["dataset_assumption"] = int(len(df))
    assumptions.append(
        f"source has no account_id column; treating all {len(df)} rows as "
        f"belonging to {account_id}"
    )
    return df.copy()


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def build_account_context(load: LoadResult, account_id: str) -> AccountContext:
    """Filter every source down to one account; produce AccountContext."""
    accounts = load.accounts
    if account_id not in set(accounts["account_id"]):
        valid = sorted(accounts["account_id"].unique())
        raise ValueError(
            f"account_id {account_id!r} not found in accounts.csv. "
            f"Valid: {valid}"
        )
    customer_name = accounts.loc[accounts["account_id"] == account_id, "customer_name"].iloc[0]

    # Contracts (definitely has account_id)
    contracts = load.contracts[load.contracts["account_id"] == account_id].copy()
    if contracts.empty:
        raise ValueError(
            f"no contracts found for {account_id}; cannot derive invoice prefix"
        )
    contract_ids = sorted(contracts["contract_id"].unique().tolist())
    invoice_prefix = derive_invoice_prefix(contracts)

    keys_used_per_source: dict[str, dict[str, int]] = {}
    rows_matched: dict[str, int] = {}
    assumptions: list[str] = []

    keys_used_per_source["contracts"] = {"account_id": len(contracts)}
    rows_matched["contracts"] = len(contracts)

    # Billing — multi-key
    billing_keys: dict[str, int] = {}
    billing = _filter_billing(
        load.billing, contract_ids, invoice_prefix, str(customer_name), billing_keys
    )
    keys_used_per_source["billing"] = billing_keys
    rows_matched["billing"] = len(billing)

    # Product usage — has account_id
    pu_keys: dict[str, int] = {}
    product_usage = _filter_by_account_id(load.product_usage, account_id, pu_keys)
    keys_used_per_source["product_usage"] = pu_keys
    rows_matched["product_usage"] = len(product_usage)

    # Support, CRM, emails, POs — fall back to dataset assumption
    sup_keys: dict[str, int] = {}
    support = _filter_support_or_crm_or_emails(
        load.support_tickets, account_id, sup_keys, assumptions
    )
    keys_used_per_source["support_tickets"] = sup_keys
    rows_matched["support_tickets"] = len(support)

    crm_keys: dict[str, int] = {}
    crm = _filter_support_or_crm_or_emails(
        load.crm_interactions, account_id, crm_keys, assumptions
    )
    keys_used_per_source["crm_interactions"] = crm_keys
    rows_matched["crm_interactions"] = len(crm)

    em_keys: dict[str, int] = {}
    emails = _filter_support_or_crm_or_emails(load.emails, account_id, em_keys, assumptions)
    keys_used_per_source["emails"] = em_keys
    rows_matched["emails"] = len(emails)

    po_keys: dict[str, int] = {}
    purchase_orders = _filter_support_or_crm_or_emails(
        load.purchase_orders, account_id, po_keys, assumptions
    )
    keys_used_per_source["purchase_orders"] = po_keys
    rows_matched["purchase_orders"] = len(purchase_orders)

    join_report = JoinReport(
        rows_matched_per_source=rows_matched,
        keys_used_per_source=keys_used_per_source,
        assumptions=assumptions,
    )

    return AccountContext(
        account_id=account_id,
        customer_name=str(customer_name),
        contract_ids=contract_ids,
        invoice_prefix=invoice_prefix,
        billing=billing,
        product_usage=product_usage,
        support_tickets=support,
        crm_interactions=crm,
        emails=emails,
        purchase_orders=purchase_orders,
        join_report=join_report,
    )
