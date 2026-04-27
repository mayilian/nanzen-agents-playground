"""Stage 7 — Deterministic PDF renderer.

Builds one combined renewal-risk PDF from `AccountSummary` (always
required) and optional `RenewalVerdict` (LLM-produced narrative). No
LLM calls in this layer; same inputs → byte-stable output.

The verdict is optional so the deterministic-only fallback path
(verification failed, or no API key) still produces a usable PDF.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from challenge.models import (
    AccountSummary,
    JoinReport,
    LoadReport,
    RenewalVerdict,
    RunMetadata,
    VerificationResult,
)

logger = logging.getLogger(__name__)


OUTPUT_DIR_DEFAULT = Path(__file__).resolve().parents[2] / "output"


VERDICT_COLOURS = {
    "Green": colors.HexColor("#2e7d32"),
    "Yellow": colors.HexColor("#f9a825"),
    "Red": colors.HexColor("#c62828"),
    "—": colors.HexColor("#616161"),
}


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "RTitle", parent=base["Title"], fontSize=20, spaceAfter=10
        ),
        "subtitle": ParagraphStyle(
            "RSub", parent=base["Normal"], fontSize=11, textColor=colors.grey, spaceAfter=18
        ),
        "h2": ParagraphStyle(
            "RH2", parent=base["Heading2"], fontSize=14, spaceBefore=14, spaceAfter=8
        ),
        "h3": ParagraphStyle(
            "RH3", parent=base["Heading3"], fontSize=12, spaceBefore=10, spaceAfter=6
        ),
        "body": ParagraphStyle(
            "RBody", parent=base["Normal"], fontSize=10, leading=14, spaceAfter=8
        ),
        "verdict_label": ParagraphStyle(
            "RVerdict",
            parent=base["Normal"],
            fontSize=18,
            leading=22,
            spaceAfter=6,
        ),
        "muted": ParagraphStyle(
            "RMuted",
            parent=base["Normal"],
            fontSize=9,
            textColor=colors.grey,
            leading=12,
        ),
        "caveat": ParagraphStyle(
            "RCaveat",
            parent=base["Normal"],
            fontSize=10,
            textColor=colors.HexColor("#c62828"),
            leading=14,
            spaceAfter=8,
        ),
    }


def _kv_table(rows: list[tuple[str, str]]) -> Table:
    t = Table(rows, colWidths=[6 * cm, 9 * cm])
    t.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
            ]
        )
    )
    return t


def _data_table(headers: list[str], rows: list[list[Any]], col_widths: Optional[list[float]] = None) -> Table:
    data = [headers, *rows]
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 9),
                ("FONTSIZE", (0, 1), (-1, -1), 8),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.HexColor("#f5f5f5")],
                ),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return t


def _verdict_block(verdict: Optional[str], reason: Optional[str], styles: dict) -> list[Any]:
    label = verdict or "—"
    colour = VERDICT_COLOURS.get(label, colors.grey)
    txt = f'<font color="{colour.hexval()}"><b>RENEWAL RISK: {label.upper()}</b></font>'
    out: list[Any] = [Paragraph(txt, styles["verdict_label"])]
    if reason:
        out.append(Paragraph(reason, styles["body"]))
    return out


def _usage_chart_png(s: AccountSummary, ctx_product_usage_df=None) -> Optional[bytes]:
    """Bar chart of weekly total_sessions for the renewal report.

    Drawn from AccountSummary alone if we have department breakdowns, or
    the raw usage DataFrame when passed (for higher fidelity).
    """
    try:
        depts = list(s.usage.by_department.keys())
        if not depts:
            return None
        fig, ax = plt.subplots(figsize=(10, 4.5))
        sessions = [s.usage.by_department[d].total_sessions for d in depts]
        bars = ax.bar(depts, sessions, color=["#2c3e50", "#7b8da4"])
        ax.set_ylabel("Total sessions (all weeks)")
        ax.set_title(f"Total sessions by department ({s.usage.weeks_observed} weeks)")
        for bar, val in zip(bars, sessions):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height(),
                f"{val:,}",
                ha="center",
                va="bottom",
                fontsize=9,
            )
        plt.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150)
        plt.close(fig)
        return buf.getvalue()
    except Exception:
        logger.exception("usage chart failed")
        return None


def _render_billing_section(s: AccountSummary, styles: dict) -> list[Any]:
    b = s.billing
    story: list[Any] = [Paragraph("Billing", styles["h2"])]
    rows = [
        ("Total invoiced (EUR)", f"€{b.invoiced_eur:,.2f}"),
        ("Total paid (EUR)", f"€{b.paid_eur:,.2f}"),
        ("Credits issued (EUR)", f"€{b.credits_eur:,.2f}"),
        ("Refunds completed (EUR)", f"€{b.refunds_eur:,.2f}"),
        ("Outstanding (EUR)", f"€{b.outstanding_eur:,.2f}"),
        ("Invoices issued", str(b.invoices_issued)),
        ("Payments received", f"{b.payments_received} ({b.partial_payments} partial)"),
        ("Disputes opened / resolved", f"{b.disputes_opened} / {b.disputes_resolved}"),
        ("Reminders sent", str(b.reminders_sent)),
        ("Overdue-status events", str(b.overdue_events)),
        ("Max days past due", str(b.max_days_past_due)),
    ]
    story.append(_kv_table(rows))
    story.append(Spacer(1, 8))
    return story


def _render_usage_section(s: AccountSummary, styles: dict) -> list[Any]:
    u = s.usage
    story: list[Any] = [Paragraph("Product usage", styles["h2"])]
    if u.weeks_observed == 0:
        story.append(Paragraph("No usage data available.", styles["body"]))
        return story
    rows = [
        ("Weeks of data", f"{u.weeks_observed}  ({u.date_range[0]} → {u.date_range[1]})"),
        (
            "Sessions/week trend",
            (
                f"slope {u.sessions_slope_per_week:+.2f} / wk; "
                f"Q1→Q4 {u.q1_avg_sessions} → {u.q4_avg_sessions} "
                f"({u.q1_to_q4_pct_change:+.1f}%)"
            )
            if u.sessions_slope_per_week is not None
            else "insufficient data",
        ),
        ("Top department by sessions", u.top_department_by_sessions or "—"),
        ("Departments observed", ", ".join(u.departments_canonical)),
    ]
    story.append(_kv_table(rows))
    story.append(Spacer(1, 6))

    dept_table = _data_table(
        headers=["Department", "Total sessions", "Mean active users", "Mean adoption %", "Mean seat util %"],
        rows=[
            [
                d,
                f"{stats.total_sessions:,}",
                f"{stats.mean_active_users:.1f}",
                f"{stats.mean_feature_adoption_pct:.1f}",
                f"{stats.mean_seat_utilisation_pct:.1f}",
            ]
            for d, stats in u.by_department.items()
        ],
    )
    story.append(dept_table)
    story.append(Spacer(1, 6))

    chart = _usage_chart_png(s)
    if chart:
        story.append(Image(io.BytesIO(chart), width=15 * cm, height=6.7 * cm))
    return story


def _render_support_section(s: AccountSummary, styles: dict) -> list[Any]:
    sup = s.support
    story: list[Any] = [Paragraph("Support", styles["h2"])]
    rows = [
        ("Tickets (unique)", str(sup.unique_tickets)),
        ("Resolved / Open", f"{sup.resolved_tickets} / {sup.open_tickets}"),
        ("SLA breaches", str(sup.sla_breaches)),
        ("Mean CSAT (1–5)", f"{sup.mean_csat}" if sup.mean_csat is not None else "no surveys"),
        (
            "Resolution time (days)",
            f"mean {sup.mean_resolution_days} / max {sup.max_resolution_days}"
            if sup.mean_resolution_days is not None
            else "no resolved tickets",
        ),
    ]
    story.append(_kv_table(rows))
    story.append(Spacer(1, 6))

    if sup.by_category:
        cat_table = _data_table(
            headers=["Category", "Count"],
            rows=[[k, str(v)] for k, v in sup.by_category.items()],
            col_widths=[6 * cm, 3 * cm],
        )
        story.append(Paragraph("By category", styles["h3"]))
        story.append(cat_table)
    return story


def _render_flags_section(s: AccountSummary, styles: dict) -> list[Any]:
    if not s.rule_flags:
        return []
    story: list[Any] = [Paragraph("Rule-based flags", styles["h2"])]
    rows = [["Severity", "ID", "Message"]]
    for f in s.rule_flags:
        rows.append([f.severity.upper(), f.id, f.message])
    t = Table(rows, colWidths=[2.5 * cm, 4 * cm, 9.5 * cm], repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(t)
    return story


def _render_appendix(
    load_report: Optional[LoadReport],
    join_report: Optional[JoinReport],
    metadata: Optional[RunMetadata],
    verification: Optional[VerificationResult],
    styles: dict,
) -> list[Any]:
    story: list[Any] = [PageBreak(), Paragraph("Appendix — provenance & data quality", styles["h2"])]

    if metadata:
        story.append(Paragraph("Run metadata", styles["h3"]))
        rows = [
            ("Account", metadata.account_id),
            ("Started at (UTC)", metadata.started_at.isoformat(timespec="seconds")),
            ("Code version", metadata.code_version),
            ("Model", metadata.model_id),
            ("Wall time", f"{metadata.wall_time_s:.2f} s"),
            ("LLM calls", str(metadata.llm_calls)),
            ("LLM tokens (in / out)", f"{metadata.llm_input_tokens:,} / {metadata.llm_output_tokens:,}"),
            ("Estimated cost (USD)", f"${metadata.cost_usd_estimate:.4f}"),
            ("Verification", verification.summary if verification else "n/a"),
            ("Fallback used", "yes" if metadata.fallback_used else "no"),
        ]
        story.append(_kv_table(rows))
        story.append(Spacer(1, 8))

    if load_report:
        story.append(Paragraph("Load report (Stage 1)", styles["h3"]))
        rows = [
            ("Rows per source", ", ".join(f"{k}={v}" for k, v in load_report.rows_per_source.items())),
        ]
        if load_report.rows_dropped_per_source:
            rows.append(
                ("Rows dropped (malformed)", ", ".join(f"{k}={v}" for k, v in load_report.rows_dropped_per_source.items()))
            )
        if load_report.normalisations_applied:
            rows.append(
                (
                    "Normalisations",
                    "; ".join(
                        f"{src}: {wrong!r}→{right!r}"
                        for src, wrong, right in load_report.normalisations_applied
                    ),
                )
            )
        for w in load_report.parse_warnings[:6]:
            rows.append(("Parse warning", w))
        story.append(_kv_table(rows))
        story.append(Spacer(1, 8))

    if join_report:
        story.append(Paragraph("Join report (Stage 2)", styles["h3"]))
        rows = [
            ("Rows matched per source", ", ".join(f"{k}={v}" for k, v in join_report.rows_matched_per_source.items())),
        ]
        for src, keys in join_report.keys_used_per_source.items():
            if keys:
                rows.append(
                    (
                        f"{src} keys used",
                        ", ".join(f"{k}={v}" for k, v in keys.items() if v),
                    )
                )
        for a in join_report.assumptions:
            rows.append(("Assumption", a))
        story.append(_kv_table(rows))

    if verification and not verification.passed:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Verification diagnostics", styles["h3"]))
        for d in verification.diagnostics[:20]:
            story.append(Paragraph(f"• {d}", styles["caveat"]))

    return story


def render_renewal_pdf(
    summary: AccountSummary,
    contract_ids: Optional[list[str]] = None,
    verdict: Optional[RenewalVerdict] = None,
    verification: Optional[VerificationResult] = None,
    metadata: Optional[RunMetadata] = None,
    load_report: Optional[LoadReport] = None,
    join_report: Optional[JoinReport] = None,
    output_path: Optional[Path] = None,
) -> Path:
    """Build the renewal-risk PDF and write it to disk."""
    output_dir = (output_path.parent if output_path else OUTPUT_DIR_DEFAULT)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_path or output_dir / f"{summary.account_id}-renewal.pdf"

    styles = _styles()

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        title=f"Renewal-risk report — {summary.customer_name} ({summary.account_id})",
        author=metadata.code_version if metadata else "challenge-pipeline",
    )

    story: list[Any] = []

    # Cover
    story.append(
        Paragraph(
            f"Renewal-risk report — {summary.customer_name}",
            styles["title"],
        )
    )
    contracts_str = ", ".join(contract_ids) if contract_ids else "n/a"
    story.append(
        Paragraph(
            f"{summary.account_id} · contracts: {contracts_str}"
            f" · generated {summary.as_of.isoformat(timespec='seconds')}",
            styles["subtitle"],
        )
    )
    story.extend(_verdict_block(
        verdict.verdict if verdict else None,
        verdict.one_sentence_reason if verdict else None,
        styles,
    ))

    # Verification banner if fallback was used
    if metadata and metadata.fallback_used:
        story.append(
            Paragraph(
                "<b>Note.</b> The narrative for this report was generated by "
                "rules only — the LLM synthesis step did not pass verification "
                "(see appendix). Numbers are unaffected.",
                styles["caveat"],
            )
        )

    if verdict:
        story.append(Paragraph("Executive narrative", styles["h2"]))
        story.append(Paragraph(verdict.executive_narrative, styles["body"]))
        story.append(Paragraph("Talking points for the renewal", styles["h2"]))
        for i, tp in enumerate(verdict.talking_points, 1):
            story.append(Paragraph(f"<b>{i}. {tp.headline}</b>", styles["body"]))
            story.append(Paragraph(tp.detail, styles["body"]))
            if tp.cites:
                story.append(
                    Paragraph(
                        "<font size=9 color='#666666'>cites: "
                        + ", ".join(tp.cites)
                        + "</font>",
                        styles["body"],
                    )
                )

    story.extend(_render_flags_section(summary, styles))
    story.extend(_render_billing_section(summary, styles))
    story.extend(_render_usage_section(summary, styles))
    story.extend(_render_support_section(summary, styles))

    story.extend(
        _render_appendix(
            load_report=load_report,
            join_report=join_report,
            metadata=metadata,
            verification=verification,
            styles=styles,
        )
    )

    doc.build(story)
    return output_path
