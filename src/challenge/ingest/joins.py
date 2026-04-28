"""Stage 2 — Multi-key account assembly.

Given an ``account_id``, produce the per-source DataFrames containing
only that customer's rows. Audited end-to-end: every match records
which key fired, every assumption made by a "soft" resolver is
surfaced on the JoinReport.

Why this matters:
- The original system filtered by ``account_id`` only, which silently
  passed through *all rows* on sources that lacked the column. That
  bug is fixed structurally here: if a source has no FK and the
  customer config doesn't tell us how to resolve, the policy decides
  what to do (``hard_fail`` / ``skip`` / ``include_with_warning``).
- Per-customer behaviour is driven by ``config/customers/<id>.yaml``
  (aliases, email domains, departments, invoice prefix) so onboarding
  a new customer never requires a code change.

Per-source resolvers (in priority order, OR-joined):
  - ``account_id_column``     — when the CSV has it (accounts, contracts,
                                product_usage)
  - ``contract_reference``    — billing rows linked by known contract id
  - ``invoice_id_pattern``    — billing rows whose invoice_id encodes the
                                customer code
  - ``credit_note_ref``       — billing rows referencing a known credit
                                note or invoice
  - ``customer_alias_text``   — billing rows whose free-text fields
                                mention the customer (aliases set)
  - ``email_domain``          — emails whose ``from`` / ``to`` matches a
                                customer email domain
  - ``customer_department``   — support tickets in a known customer
                                department
  - ``po_number``             — purchase orders that match billing
                                ``po_number`` for this customer
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import pandas as pd

from challenge.config.customer import CustomerConfig, load_customer_config
from challenge.models import AccountContext, JoinReport, LoadResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Customer-code derivation
# ---------------------------------------------------------------------------


def derive_invoice_prefix(
    contracts_for_account: pd.DataFrame,
    customer_cfg: CustomerConfig,
) -> str:
    """Pick the customer's invoice-id prefix.

    Priority:
      1. ``customer_cfg.invoice_prefix`` if explicitly set.
      2. Match ``contract_id`` against ``customer_cfg.contract_id_pattern``
         and pull the named ``code`` group.

    Raises:
      ValueError: if neither path yields a code (signals a mis-configured
        customer file).
    """
    if customer_cfg.invoice_prefix:
        return customer_cfg.invoice_prefix

    codes: set[str] = set()
    for contract_id in contracts_for_account["contract_id"].dropna():
        m = customer_cfg.contract_id_pattern.match(str(contract_id))
        if m:
            try:
                codes.add(m.group("code"))
            except IndexError:
                # Pattern didn't expose a `code` group — caller's problem,
                # but raise a clear error rather than silent miss.
                raise ValueError(
                    f"contract_id_pattern for {customer_cfg.account_id} matches "
                    f"but has no `code` named group: "
                    f"{customer_cfg.contract_id_pattern.pattern}"
                )

    if not codes:
        raise ValueError(
            f"could not derive invoice prefix for {customer_cfg.account_id}; "
            f"contract_ids={contracts_for_account['contract_id'].tolist()!r}, "
            f"pattern={customer_cfg.contract_id_pattern.pattern}"
        )
    if len(codes) == 1:
        return next(iter(codes))
    # Multiple distinct codes is unusual but tolerable; pick the most common.
    return max(codes, key=lambda c: int(contracts_for_account["contract_id"].str.contains(f"-{c}-").sum()))


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------


def _filter_billing(
    billing: pd.DataFrame,
    contract_ids: list[str],
    invoice_prefix: str,
    customer_cfg: CustomerConfig,
    keys_used: dict[str, int],
) -> pd.DataFrame:
    """Multi-key billing resolver.

    Match a row if ANY of:
      - ``contract_reference`` is one of our known contract ids
      - ``invoice_id`` matches the customer's invoice pattern
      - ``credit_note_ref`` references a credit note (or invoice id) we
        already pulled in
      - any free-text field contains a customer alias (used to catch
        operational events such as misdirected-payment refunds that
        leave every FK blank).
    """
    invoice_re = re.compile(rf"^INV-\d+-{re.escape(invoice_prefix)}-\d+$")

    by_contract = billing["contract_reference"].isin(contract_ids)
    by_invoice = billing["invoice_id"].fillna("").str.match(invoice_re.pattern)
    keys_used["contract_reference"] = int(by_contract.sum())
    keys_used["invoice_id_pattern"] = int((by_invoice & ~by_contract).sum())

    seed = billing[by_contract | by_invoice]

    # Credit-note ref join, strict (no empty-string trap).
    seed_credit_refs = {x for x in seed["credit_note_ref"].dropna().unique() if x}
    seed_invoice_ids = {x for x in seed["invoice_id"].dropna().unique() if x}
    cn = billing["credit_note_ref"].fillna("")
    by_credit = (cn != "") & (cn.isin(seed_credit_refs) | cn.isin(seed_invoice_ids))
    seed_idx = seed.index
    keys_used["credit_note_ref"] = int(
        (by_credit & ~billing.index.isin(seed_idx)).sum()
    )

    # Alias text mention — last-resort resolver for rows that have NO
    # joinable identifier at all (typical of operational events like
    # the misdirected-payment trio: blank contract_reference, blank
    # invoice_id, blank credit_note_ref, but the customer name appears
    # in description/remittance/bank fields).
    #
    # Critical guard: only match rows where invoice_id AND
    # contract_reference are both empty. Otherwise a payment for
    # *another* customer that happens to mention us in a free-text
    # field would be wrongly attributed (caught by the multi-customer
    # regression tests).
    aliases = list(customer_cfg.aliases) or [customer_cfg.canonical_name]
    text_fields = customer_cfg.text_mention.fields or (
        "description",
        "remittance_ref",
        "bank_reference",
        "notes",
    )
    text_match = pd.Series(False, index=billing.index)
    for col in text_fields:
        if col in billing.columns:
            col_lower = billing[col].fillna("").str.lower()
            for alias in aliases:
                if not alias:
                    continue
                text_match = text_match | col_lower.str.contains(
                    re.escape(alias.lower()), regex=True, na=False
                )
    has_amount = billing["amount"].notna()
    no_other_keys = (
        billing["invoice_id"].fillna("").eq("")
        & billing["contract_reference"].fillna("").eq("")
        & billing["credit_note_ref"].fillna("").eq("")
    )
    by_text = text_match & has_amount & no_other_keys
    keys_used["customer_alias_text"] = int(
        (by_text & ~billing.index.isin(seed_idx) & ~by_credit).sum()
    )

    return billing[by_contract | by_invoice | by_credit | by_text].copy()


def _filter_account_id_column(
    df: pd.DataFrame, account_id: str, keys_used: dict[str, int]
) -> pd.DataFrame:
    """Filter sources that have an explicit ``account_id`` column."""
    if "account_id" not in df.columns:
        keys_used["account_id"] = 0
        return df.iloc[0:0].copy()
    out = df[df["account_id"] == account_id].copy()
    keys_used["account_id"] = int(len(out))
    return out


def _filter_emails(
    df: pd.DataFrame,
    customer_cfg: CustomerConfig,
    keys_used: dict[str, int],
    assumptions: list[str],
) -> pd.DataFrame:
    """Resolve emails by the customer's email domains.

    Looks at ``from`` and ``to`` columns; matches if either contains a
    customer-domain substring (case-insensitive).
    """
    if "account_id" in df.columns:
        return _filter_account_id_column(df, customer_cfg.account_id, keys_used)

    domains = [d.lower() for d in customer_cfg.email_domains if d]
    if not domains:
        keys_used["email_domain"] = 0
        return _apply_unresolvable_policy(
            df, customer_cfg, "emails", keys_used, assumptions
        )

    fromcol = df["from"].fillna("").str.lower() if "from" in df.columns else pd.Series("", index=df.index)
    tocol = df["to"].fillna("").str.lower() if "to" in df.columns else pd.Series("", index=df.index)

    mask = pd.Series(False, index=df.index)
    for d in domains:
        mask = mask | fromcol.str.contains(re.escape(d), regex=True, na=False) | tocol.str.contains(
            re.escape(d), regex=True, na=False
        )
    keys_used["email_domain"] = int(mask.sum())
    if mask.sum() == 0:
        return _apply_unresolvable_policy(
            df, customer_cfg, "emails", keys_used, assumptions
        )
    return df[mask].copy()


def _filter_support_tickets(
    df: pd.DataFrame,
    customer_cfg: CustomerConfig,
    keys_used: dict[str, int],
    assumptions: list[str],
) -> pd.DataFrame:
    """Resolve support tickets via ``customer_department``.

    ``customer_department`` is only populated on ``ticket_created`` rows
    (the rest of the interaction stream — replies, status changes,
    surveys — leaves it blank). Resolution therefore happens in two
    steps:
      1. Find ticket_ids whose creation row matches one of the
         customer's known departments.
      2. Return *all* interactions for those ticket_ids, blank-department
         rows included.
    """
    if "account_id" in df.columns:
        return _filter_account_id_column(df, customer_cfg.account_id, keys_used)

    if (
        "customer_department" in df.columns
        and "ticket_id" in df.columns
        and customer_cfg.departments
    ):
        creation = df["customer_department"].isin(list(customer_cfg.departments))
        qualifying_tickets = set(df.loc[creation, "ticket_id"].dropna().unique())
        mask = df["ticket_id"].isin(qualifying_tickets)
        keys_used["customer_department"] = int(mask.sum())
        if mask.sum() > 0:
            return df[mask].copy()

    keys_used["customer_department"] = 0
    return _apply_unresolvable_policy(
        df, customer_cfg, "support_tickets", keys_used, assumptions
    )


def _filter_crm(
    df: pd.DataFrame,
    customer_cfg: CustomerConfig,
    keys_used: dict[str, int],
    assumptions: list[str],
) -> pd.DataFrame:
    """Resolve CRM via contact_email domain match where available."""
    if "account_id" in df.columns:
        return _filter_account_id_column(df, customer_cfg.account_id, keys_used)

    domains = [d.lower() for d in customer_cfg.email_domains if d]
    if domains and "contact_email" in df.columns:
        col = df["contact_email"].fillna("").str.lower()
        mask = pd.Series(False, index=df.index)
        for d in domains:
            mask = mask | col.str.contains(re.escape(d), regex=True, na=False)
        keys_used["contact_email_domain"] = int(mask.sum())
        if mask.sum() > 0:
            return df[mask].copy()
        keys_used["contact_email_domain"] = 0

    return _apply_unresolvable_policy(
        df, customer_cfg, "crm_interactions", keys_used, assumptions
    )


def _filter_purchase_orders(
    df: pd.DataFrame,
    contract_ids: list[str],
    customer_cfg: CustomerConfig,
    keys_used: dict[str, int],
    assumptions: list[str],
) -> pd.DataFrame:
    """Resolve POs via ``contract_reference`` linking back to known contracts."""
    if "account_id" in df.columns:
        return _filter_account_id_column(df, customer_cfg.account_id, keys_used)

    if "contract_reference" in df.columns:
        mask = df["contract_reference"].isin(contract_ids)
        keys_used["contract_reference"] = int(mask.sum())
        if mask.sum() > 0:
            return df[mask].copy()
        keys_used["contract_reference"] = 0

    return _apply_unresolvable_policy(
        df, customer_cfg, "purchase_orders", keys_used, assumptions
    )


def _apply_unresolvable_policy(
    df: pd.DataFrame,
    customer_cfg: CustomerConfig,
    source_name: str,
    keys_used: dict[str, int],
    assumptions: list[str],
) -> pd.DataFrame:
    """Decide what to do with a source we can't resolve confidently.

    Driven by ``customer_cfg.unresolvable_row_policy``:
      - ``hard_fail`` — raise; production safe default
      - ``skip``      — drop all rows
      - ``include_with_warning`` — keep rows, warn on JoinReport
    """
    policy = customer_cfg.unresolvable_row_policy
    if policy == "hard_fail":
        raise RuntimeError(
            f"cannot resolve any rows in '{source_name}' for "
            f"{customer_cfg.account_id}: no FK column, no email/department "
            f"match, and policy is hard_fail. Either add a resolver hint to "
            f"config/customers/{customer_cfg.account_id}.yaml or change the "
            f"policy."
        )
    if policy == "skip":
        keys_used["unresolved_skipped"] = int(len(df))
        assumptions.append(
            f"{source_name}: no resolvable key for {customer_cfg.account_id}; "
            f"all {len(df)} rows skipped per policy=skip."
        )
        return df.iloc[0:0].copy()
    # include_with_warning
    keys_used["unresolved_included"] = int(len(df))
    assumptions.append(
        f"{source_name}: no resolvable key for {customer_cfg.account_id}; "
        f"keeping all {len(df)} rows per policy=include_with_warning. "
        f"This is unsafe at multi-customer scale."
    )
    return df.copy()


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def build_account_context(
    load: LoadResult,
    account_id: str,
    *,
    customer_cfg: Optional[CustomerConfig] = None,
) -> AccountContext:
    """Filter every CSV down to one customer; return ``AccountContext``.

    Args:
      load:         output of ``data_io.load_all``.
      account_id:   the customer to scope to.
      customer_cfg: optional pre-loaded customer config (for tests).

    Raises:
      ValueError:        account not found in accounts.csv.
      RuntimeError:      a source could not be resolved and the
        customer's policy is ``hard_fail``.
      FileNotFoundError: no per-customer config file exists.
    """
    accounts = load.accounts
    if account_id not in set(accounts["account_id"]):
        valid = sorted(accounts["account_id"].unique())
        raise ValueError(
            f"account_id {account_id!r} not found in accounts.csv. Valid: {valid}"
        )
    customer_name = accounts.loc[
        accounts["account_id"] == account_id, "customer_name"
    ].iloc[0]

    customer_cfg = customer_cfg or load_customer_config(account_id)

    contracts = load.contracts[load.contracts["account_id"] == account_id].copy()
    if contracts.empty:
        raise ValueError(
            f"no contracts for {account_id}; cannot derive invoice prefix"
        )
    contract_ids = sorted(contracts["contract_id"].unique().tolist())
    invoice_prefix = derive_invoice_prefix(contracts, customer_cfg)

    keys_used_per_source: dict[str, dict[str, int]] = {}
    rows_matched: dict[str, int] = {}
    assumptions: list[str] = []

    keys_used_per_source["contracts"] = {"account_id": len(contracts)}
    rows_matched["contracts"] = len(contracts)

    # Billing
    billing_keys: dict[str, int] = {}
    billing = _filter_billing(
        load.billing, contract_ids, invoice_prefix, customer_cfg, billing_keys
    )
    keys_used_per_source["billing"] = billing_keys
    rows_matched["billing"] = len(billing)

    # Product usage
    pu_keys: dict[str, int] = {}
    product_usage = _filter_account_id_column(load.product_usage, account_id, pu_keys)
    keys_used_per_source["product_usage"] = pu_keys
    rows_matched["product_usage"] = len(product_usage)

    # Support tickets
    sup_keys: dict[str, int] = {}
    support = _filter_support_tickets(
        load.support_tickets, customer_cfg, sup_keys, assumptions
    )
    keys_used_per_source["support_tickets"] = sup_keys
    rows_matched["support_tickets"] = len(support)

    # CRM
    crm_keys: dict[str, int] = {}
    crm = _filter_crm(load.crm_interactions, customer_cfg, crm_keys, assumptions)
    keys_used_per_source["crm_interactions"] = crm_keys
    rows_matched["crm_interactions"] = len(crm)

    # Emails
    em_keys: dict[str, int] = {}
    emails = _filter_emails(load.emails, customer_cfg, em_keys, assumptions)
    keys_used_per_source["emails"] = em_keys
    rows_matched["emails"] = len(emails)

    # Purchase orders
    po_keys: dict[str, int] = {}
    purchase_orders = _filter_purchase_orders(
        load.purchase_orders, contract_ids, customer_cfg, po_keys, assumptions
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
