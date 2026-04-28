"""Stage 4 — Signal retriever.

Curates the small bundle of free-text excerpts the LLM should consider
when synthesising the renewal verdict. Pure rules; no LLM.

Each excerpt has a typed ``id`` so the verifier in Stage 6 can confirm
that any cited TextSignal actually exists.

The retrieval policy (keywords, char limits, max signal count, the set
of fields per source to scan) lives in
``config/signals/<policy>.yaml``. Different report types (renewal-risk,
QBR, churn, expansion) use different policy files.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from challenge.config import config_dir, load_yaml
from challenge.models import AccountContext, AccountSummary, TextSignal


@dataclass(frozen=True)
class SignalPolicy:
    """Retrieval policy loaded from ``config/signals/<policy>.yaml``."""

    keywords: list[str]
    text_field_max_chars: int
    max_signals: int
    recent_window_days: int
    sources: dict[str, list[str]]  # csv name -> field names to scan


def load_signal_policy(name: str = "renewal") -> SignalPolicy:
    """Load a named retrieval policy from ``config/signals/{name}.yaml``."""
    path = config_dir() / "signals" / f"{name}.yaml"
    cfg = load_yaml(path)
    return SignalPolicy(
        keywords=list(cfg.get("keywords", [])),
        text_field_max_chars=int(cfg.get("text_field_max_chars", 600)),
        max_signals=int(cfg.get("max_signals", 24)),
        recent_window_days=int(cfg.get("recent_window_days", 90)),
        sources={k: list(v.get("fields", [])) for k, v in (cfg.get("sources") or {}).items()},
    )


# ---------------------------------------------------------------------------


def _now() -> datetime:
    """Naive UTC ``datetime`` for date-window comparisons."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ts(value) -> Optional[datetime]:
    """Best-effort timestamp parse. Returns ``None`` on failure."""
    ts = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(ts) else ts.to_pydatetime()


def _truncate(text: str, max_chars: int) -> str:
    """Strip and bound a free-text excerpt, appending ``…`` when cut."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _has_keyword(text: str, keywords: list[str]) -> Optional[str]:
    """Return the first matching keyword (case-insensitive) or ``None``."""
    if not text:
        return None
    lower = text.lower()
    for kw in keywords:
        if kw.lower() in lower:
            return kw
    return None


def retrieve_signals(
    ctx: AccountContext,
    summary: AccountSummary,
    policy: Optional[SignalPolicy] = None,
) -> list[TextSignal]:
    """Curate a bounded, deterministic list of TextSignals for the LLM.

    Args:
      ctx:     account-scoped CSV slices (output of Stage 2).
      summary: structured facts (output of Stage 3).
      policy:  retrieval policy. Defaults to the renewal-risk policy.

    Returns:
      A list of ``TextSignal`` of size ``<= policy.max_signals``,
      ordered by timestamp descending. Stable for identical inputs.
    """
    policy = policy or load_signal_policy("renewal")
    signals: list[TextSignal] = []

    # 1) Open high/critical priority tickets — full ticket_created content.
    if not ctx.support_tickets.empty:
        st = ctx.support_tickets.copy()
        st["timestamp"] = pd.to_datetime(st["timestamp"], errors="coerce")
        first = (
            st.sort_values("timestamp")
            .groupby("ticket_id", as_index=False)
            .head(1)
            .set_index("ticket_id")
        )
        sc = st[st["interaction_type"] == "status_change"].sort_values("timestamp")
        last_sc_status = sc.groupby("ticket_id")["ticket_status"].last()
        final_status = pd.Series("open", index=first.index)
        final_status.update(last_sc_status)
        for tid, row in first.iterrows():
            status = final_status.get(tid, "open")
            priority = str(row.get("priority", "")).lower()
            if status == "open" and priority in {"critical", "high"}:
                signals.append(
                    TextSignal(
                        id=f"TKT-{tid}.content",
                        source="support_tickets",
                        timestamp=_ts(row.get("timestamp")),
                        text=_truncate(str(row.get("content", "")), policy.text_field_max_chars),
                        why_selected=f"open {priority}-priority ticket",
                    )
                )

    # 2) Notes/content fields with renewal-relevant keywords. The
    # (source -> [fields]) mapping is from the policy file.
    id_columns = {
        "billing": "event_id",
        "support_tickets": "interaction_id",
        "crm_interactions": "id",
        "emails": "email_id",
    }
    source_dfs = {
        "billing": ctx.billing,
        "support_tickets": ctx.support_tickets,
        "crm_interactions": ctx.crm_interactions,
        "emails": ctx.emails,
    }
    for src_name, fields in policy.sources.items():
        df = source_dfs.get(src_name)
        id_col = id_columns.get(src_name)
        if df is None or df.empty or id_col is None or id_col not in df.columns:
            continue
        for field in fields:
            if field not in df.columns:
                continue
            for idx, row in df.iterrows():
                text = str(row.get(field, "") or "")
                kw = _has_keyword(text, policy.keywords)
                if kw is None:
                    continue
                row_id = row.get(id_col, f"row{idx}")
                signals.append(
                    TextSignal(
                        id=f"{src_name}:{row_id}.{field}",
                        source=src_name,
                        timestamp=_ts(row.get("timestamp")),
                        text=_truncate(text, policy.text_field_max_chars),
                        why_selected=f"keyword '{kw}'",
                    )
                )

    # 3) Recent CRM interactions (last `recent_window_days` days).
    if not ctx.crm_interactions.empty and "timestamp" in ctx.crm_interactions.columns:
        crm = ctx.crm_interactions.copy()
        crm["timestamp"] = pd.to_datetime(crm["timestamp"], errors="coerce")
        cutoff = _now() - timedelta(days=policy.recent_window_days)
        recent = crm[crm["timestamp"] >= cutoff]
        for idx, row in recent.iterrows():
            text = " — ".join(
                str(row.get(c, "") or "")
                for c in ("subject", "body")
                if row.get(c)
            )
            if not text:
                continue
            row_id = row.get("id", f"row{idx}")
            signals.append(
                TextSignal(
                    id=f"crm_interactions:{row_id}.recent",
                    source="crm_interactions",
                    timestamp=_ts(row.get("timestamp")),
                    text=_truncate(text, policy.text_field_max_chars),
                    why_selected=f"CRM activity in last {policy.recent_window_days} days",
                )
            )

    # 4) Deduplicate by id, keep first occurrence (preserves earliest "why").
    seen: set[str] = set()
    deduped: list[TextSignal] = []
    for s in signals:
        if s.id in seen:
            continue
        seen.add(s.id)
        deduped.append(s)

    # 5) Bound the list deterministically: by timestamp desc, ties by id.
    deduped.sort(key=lambda s: (s.timestamp or datetime.min, s.id), reverse=True)
    return deduped[: policy.max_signals]
