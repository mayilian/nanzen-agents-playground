"""Customer dictionary loader.

Per-customer configuration (aliases, invoice prefix, email domains,
risk thresholds, resolution policy) lives in
``config/customers/<account_id>.yaml``. Onboarding a new customer is
adding one YAML file; the pipeline never grows new branches per
customer.

This module handles:
  - Reading and validating the per-customer YAML.
  - Merging with ``_defaults.yaml`` so customer files only need to
    override what's special.
  - Returning a typed ``CustomerConfig`` to the rest of the pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from challenge.config import config_dir, load_yaml


@dataclass(frozen=True)
class TextMentionPolicy:
    """How to fall back to a free-text join when no FK matches."""

    use_customer_first_word: bool
    fields: tuple[str, ...]


@dataclass(frozen=True)
class RiskThresholds:
    """Numeric thresholds that drive ``derive_rule_flags``."""

    outstanding_eur_alert: float
    reminder_count_warn: int
    sla_breach_alert: int
    csat_min_warn: float


@dataclass(frozen=True)
class CustomerConfig:
    """Resolved configuration for one customer.

    Read by joins (resolver behaviour) and summary (risk thresholds).
    """

    account_id: str
    canonical_name: str
    aliases: tuple[str, ...]
    email_domains: tuple[str, ...]
    departments: tuple[str, ...]
    invoice_prefix: Optional[str]
    contract_id_pattern: re.Pattern[str]
    text_mention: TextMentionPolicy
    risk_thresholds: RiskThresholds
    unresolvable_row_policy: str  # "hard_fail" | "skip" | "include_with_warning"


def _customer_path(account_id: str, override_dir: Optional[Path] = None) -> Path:
    base = override_dir or (config_dir() / "customers")
    return base / f"{account_id}.yaml"


def _defaults_path(override_dir: Optional[Path] = None) -> Path:
    base = override_dir or (config_dir() / "customers")
    return base / "_defaults.yaml"


def _merge(default: dict, override: dict) -> dict:
    """Shallow merge with one level of nesting for dicts."""
    out = dict(default)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def load_customer_config(
    account_id: str,
    override_dir: Optional[Path] = None,
) -> CustomerConfig:
    """Load a typed ``CustomerConfig`` for ``account_id``.

    Raises:
      FileNotFoundError: if no per-customer file exists. Surfacing this
        as an explicit error is intentional — a missing customer file
        means we have no policy for resolving ambiguous rows, and
        silently treating "all rows" as belonging to the customer (the
        prior behaviour) is a multi-customer scaling foot-gun.
    """
    defaults_path = _defaults_path(override_dir)
    customer_path = _customer_path(account_id, override_dir)

    defaults = load_yaml(defaults_path) if defaults_path.exists() else {}
    if not customer_path.exists():
        raise FileNotFoundError(
            f"no customer config at {customer_path}. "
            f"Onboard the customer by creating that file (see "
            f"config/customers/_defaults.yaml for the schema)."
        )
    customer = load_yaml(customer_path)
    cfg = _merge(defaults, customer)

    aliases = list(cfg.get("aliases", []))
    if not aliases and cfg.get("canonical_name"):
        aliases = [cfg["canonical_name"]]

    text_mention_cfg = cfg.get("text_mention") or {}
    text_mention = TextMentionPolicy(
        use_customer_first_word=bool(text_mention_cfg.get("use_customer_first_word", True)),
        fields=tuple(text_mention_cfg.get("fields", [])),
    )

    rt_cfg = cfg.get("risk_thresholds") or {}
    risk_thresholds = RiskThresholds(
        outstanding_eur_alert=float(rt_cfg.get("outstanding_eur_alert", 5000)),
        reminder_count_warn=int(rt_cfg.get("reminder_count_warn", 3)),
        sla_breach_alert=int(rt_cfg.get("sla_breach_alert", 5)),
        csat_min_warn=float(rt_cfg.get("csat_min_warn", 3.5)),
    )

    pattern = cfg.get("contract_id_pattern", r"^CTR-\d{4}-(?P<code>[A-Z]{2,4})-\d{3}$")

    return CustomerConfig(
        account_id=str(cfg.get("account_id", account_id)),
        canonical_name=str(cfg.get("canonical_name", account_id)),
        aliases=tuple(aliases),
        email_domains=tuple(cfg.get("email_domains", [])),
        departments=tuple(cfg.get("departments", [])),
        invoice_prefix=cfg.get("invoice_prefix"),
        contract_id_pattern=re.compile(pattern),
        text_mention=text_mention,
        risk_thresholds=risk_thresholds,
        unresolvable_row_policy=str(cfg.get("unresolvable_row_policy", "skip")),
    )


def list_customers(override_dir: Optional[Path] = None) -> list[str]:
    """Return all account_ids for which we have a customer config."""
    base = override_dir or (config_dir() / "customers")
    if not base.exists():
        return []
    return sorted(
        p.stem
        for p in base.glob("*.yaml")
        if not p.stem.startswith("_")
    )
