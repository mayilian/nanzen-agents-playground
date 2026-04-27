"""Stage 4 — Signal retriever.

Curates the small bundle of free-text excerpts the LLM should consider
when synthesising the renewal verdict (F8). Pure rules; no LLM.

Each excerpt has a typed `id` so the verifier in Stage 6 can confirm
that any cited TextSignal actually exists.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from challenge.models import AccountContext, AccountSummary, TextSignal

# ---------------------------------------------------------------------------
# Tunables. These are explicit so we can adjust without touching logic.
# ---------------------------------------------------------------------------

KEYWORDS = [
    "CFO",
    "renewal",
    "renew",
    "escalat",
    "BILLING ERROR",
    "compliance",
    "evaluat",
    "vendor review",
    "churn",
    "downgrade",
    "outage",
]

MAX_TEXT_CHARS = 600
MAX_SIGNALS = 24
RECENT_DAYS = 90


# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ts(value) -> Optional[datetime]:
    ts = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(ts) else ts.to_pydatetime()


def _truncate(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[: MAX_TEXT_CHARS - 1].rstrip() + "…"


def _has_keyword(text: str) -> Optional[str]:
    if not text:
        return None
    lower = text.lower()
    for kw in KEYWORDS:
        if kw.lower() in lower:
            return kw
    return None


def retrieve_signals(ctx: AccountContext, summary: AccountSummary) -> list[TextSignal]:
    """Return a deterministic, bounded list of TextSignals for the LLM."""
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
                        text=_truncate(str(row.get("content", ""))),
                        why_selected=f"open {priority}-priority ticket",
                    )
                )

    # 2) Notes/content fields with renewal-relevant keywords.
    notes_sources = [
        ("billing", ctx.billing, ["notes", "description"], "event_id", "timestamp"),
        ("support_tickets", ctx.support_tickets, ["content", "notes"], "interaction_id", "timestamp"),
        ("crm_interactions", ctx.crm_interactions, ["body", "subject"], "id", "timestamp"),
        ("emails", ctx.emails, ["body", "subject"], "email_id", "timestamp"),
    ]
    for src_name, df, fields, id_col, ts_col in notes_sources:
        if df.empty or id_col not in df.columns:
            continue
        for field in fields:
            if field not in df.columns:
                continue
            for idx, row in df.iterrows():
                text = str(row.get(field, "") or "")
                kw = _has_keyword(text)
                if kw is None:
                    continue
                row_id = row.get(id_col, f"row{idx}")
                signals.append(
                    TextSignal(
                        id=f"{src_name}:{row_id}.{field}",
                        source=src_name,
                        timestamp=_ts(row.get(ts_col)),
                        text=_truncate(text),
                        why_selected=f"keyword '{kw}'",
                    )
                )

    # 3) Recent CRM interactions (last RECENT_DAYS days)
    if not ctx.crm_interactions.empty and "timestamp" in ctx.crm_interactions.columns:
        crm = ctx.crm_interactions.copy()
        crm["timestamp"] = pd.to_datetime(crm["timestamp"], errors="coerce")
        cutoff = _now() - timedelta(days=RECENT_DAYS)
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
                    text=_truncate(text),
                    why_selected=f"CRM activity in last {RECENT_DAYS} days",
                )
            )

    # 4) Deduplicate by id, keep first occurrence.
    seen: set[str] = set()
    deduped: list[TextSignal] = []
    for s in signals:
        if s.id in seen:
            continue
        seen.add(s.id)
        deduped.append(s)

    # 5) Bound the list. Sort by recency desc, then keep top N.
    def _key(s: TextSignal):
        return s.timestamp or datetime.min

    deduped.sort(key=_key, reverse=True)
    return deduped[:MAX_SIGNALS]
