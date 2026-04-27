"""End-to-end pipeline: account_id → renewal-risk PDF.

Stages 1..7 wired together. Single entry point: `run(account_id)`.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from challenge.data_io import load_all
from challenge.joins import build_account_context
from challenge.llm import estimate_cost_usd, resolve_model_id
from challenge.models import (
    AccountSummary,
    RenewalVerdict,
    RunMetadata,
    TextSignal,
    VerificationResult,
)
from challenge.render import render_renewal_pdf
from challenge.signals import retrieve_signals
from challenge.summary import build_account_summary
from challenge.synthesis import SynthesisValidationError, synthesize_verdict
from challenge.verify import verify

logger = logging.getLogger(__name__)

CODE_VERSION = "0.2.0"


def run(
    account_id: str,
    *,
    skip_llm: bool = False,
    output_path: Optional[Path] = None,
) -> dict:
    """Run the full pipeline. Returns a result dict with metrics + path."""
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).replace(tzinfo=None)

    # Stage 1: ingest
    load = load_all()

    # Stage 2: assemble account context
    ctx = build_account_context(load, account_id)

    # Stage 3: deterministic summary
    summary: AccountSummary = build_account_summary(ctx, as_of=started_at)

    # Stage 4: signal retrieval
    signals: list[TextSignal] = retrieve_signals(ctx, summary)

    # Stage 5–6: synthesis + verify (with deterministic fallback)
    verdict: Optional[RenewalVerdict] = None
    verification: Optional[VerificationResult] = None
    fallback_used = False
    input_tokens = 0
    output_tokens = 0
    llm_calls = 0
    notes: list[str] = []
    model_id = resolve_model_id() if not skip_llm else "deterministic-only"

    if skip_llm:
        notes.append("skip_llm=True; rendering deterministic-only PDF.")
        fallback_used = True
    else:
        try:
            verdict, response = synthesize_verdict(summary, signals)
            input_tokens = response.input_tokens
            output_tokens = response.output_tokens
            llm_calls = 1
            verification = verify(summary, signals, verdict)
            if not verification.passed:
                notes.append(
                    f"verification failed with {len(verification.diagnostics)} "
                    "issue(s); falling back to deterministic-only narrative."
                )
                verdict = None
                fallback_used = True
        except SynthesisValidationError as exc:
            # Output had right tokens but wrong shape; preserve token cost.
            input_tokens = exc.response.input_tokens
            output_tokens = exc.response.output_tokens
            llm_calls = 1
            notes.append(f"synthesis output failed schema validation: {exc}")
            fallback_used = True
        except Exception as exc:
            logger.exception("synthesis failed")
            notes.append(f"synthesis raised {type(exc).__name__}: {exc}")
            fallback_used = True

    wall = time.perf_counter() - started
    cost = estimate_cost_usd(model_id, input_tokens, output_tokens) if not skip_llm else 0.0

    metadata = RunMetadata(
        account_id=account_id,
        started_at=started_at,
        code_version=CODE_VERSION,
        model_id=model_id,
        wall_time_s=round(wall, 3),
        llm_input_tokens=input_tokens,
        llm_output_tokens=output_tokens,
        llm_calls=llm_calls,
        cost_usd_estimate=round(cost, 5),
        verification=verification,
        fallback_used=fallback_used,
        notes=notes,
    )

    pdf_path = render_renewal_pdf(
        summary=summary,
        contract_ids=ctx.contract_ids,
        verdict=verdict,
        verification=verification,
        metadata=metadata,
        load_report=load.report,
        join_report=ctx.join_report,
        output_path=output_path,
    )

    return {
        "summary": summary,
        "verdict": verdict,
        "verification": verification,
        "metadata": metadata,
        "pdf_path": pdf_path,
        "signals": signals,
    }
