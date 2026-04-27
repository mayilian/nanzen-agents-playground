"""Stage 5 — LLM synthesis (single Anthropic call).

Takes ``AccountSummary`` + ``list[TextSignal]``, returns a
``RenewalVerdict``. The model never sees raw CSVs and is structurally
prevented from inventing numbers (system prompt + tool-use schema +
Stage 6 verifier).

The system prompt lives in ``config/synthesis/<name>.prompt.md`` so
prompt iteration doesn't require a Python diff. The tool-input schema
is generated from the Pydantic ``RenewalVerdict`` class.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from typing import Optional

from challenge.config import config_dir
from challenge.llm import LLMResponse, call_with_tool
from challenge.models import AccountSummary, RenewalVerdict, TextSignal


def load_system_prompt(name: str = "renewal") -> str:
    """Load the markdown prompt file for the named report type."""
    path = config_dir() / "synthesis" / f"{name}.prompt.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    raise TypeError(f"not serialisable: {type(o)}")


def _summary_to_json(s: AccountSummary) -> dict:
    """Convert AccountSummary to a JSON-friendly dict."""
    d = asdict(s)
    return d


def _signals_to_blocks(signals: list[TextSignal]) -> str:
    """Render the list of TextSignals as a structured untrusted-content block."""
    if not signals:
        return "(no text signals)"
    out = []
    for sig in signals:
        ts = sig.timestamp.isoformat(timespec="seconds") if sig.timestamp else "n/a"
        out.append(
            f"<text_signal id={sig.id!r} source={sig.source!r} timestamp={ts!r} "
            f"why={sig.why_selected!r}>\n{sig.text}\n</text_signal>"
        )
    return "\n\n".join(out)


def _build_user_message(summary: AccountSummary, signals: list[TextSignal]) -> str:
    summary_json = json.dumps(_summary_to_json(summary), indent=2, default=_json_default)
    signal_block = _signals_to_blocks(signals)
    return (
        f"<account_summary>\n{summary_json}\n</account_summary>\n\n"
        f"<text_signals>\n{signal_block}\n</text_signals>\n\n"
        "Produce a renewal-risk verdict by calling the `emit_renewal_verdict` tool."
    )


# Pydantic-derived JSON schema for the tool. Built once.
def _renewal_verdict_schema() -> dict:
    schema = RenewalVerdict.model_json_schema()
    # Inline $defs so Anthropic's tool-use sees a self-contained schema.
    if "$defs" in schema:
        defs = schema.pop("$defs")

        def _inline(node):
            if isinstance(node, dict):
                if "$ref" in node and node["$ref"].startswith("#/$defs/"):
                    name = node["$ref"].split("/")[-1]
                    return _inline(defs[name])
                return {k: _inline(v) for k, v in node.items()}
            if isinstance(node, list):
                return [_inline(v) for v in node]
            return node

        schema = _inline(schema)
    return schema


def synthesize_verdict(
    summary: AccountSummary,
    signals: list[TextSignal],
    *,
    prompt_name: str = "renewal",
    system_prompt: Optional[str] = None,
) -> tuple[RenewalVerdict, LLMResponse]:
    """Run the single LLM call. Returns ``(verdict, response_metadata)``.

    Args:
      summary:        deterministic facts (Stage 3 output).
      signals:        curated text excerpts (Stage 4 output).
      prompt_name:    the report-type prompt to load from
        ``config/synthesis/<name>.prompt.md``.
      system_prompt:  inline override (for tests/research).

    Raises:
      ``SynthesisValidationError``: when the model's output does not
        conform to ``RenewalVerdict``. The exception carries the
        ``LLMResponse`` so the caller can still record token usage.
    """
    schema = _renewal_verdict_schema()
    sys_prompt = system_prompt or load_system_prompt(prompt_name)
    response = call_with_tool(
        system_prompt=sys_prompt,
        user_message=_build_user_message(summary, signals),
        tool_name="emit_renewal_verdict",
        tool_description=(
            "Emit the renewal-risk verdict for this customer. The verdict "
            "consists of a traffic-light rating, one-sentence reason, "
            "executive narrative, and exactly three talking points."
        ),
        tool_input_schema=schema,
    )
    try:
        verdict = RenewalVerdict.model_validate(response.tool_use_input)
    except Exception as exc:
        raise SynthesisValidationError(str(exc), response) from exc
    return verdict, response


class SynthesisValidationError(Exception):
    """Raised when the model's structured output fails Pydantic validation.

    Carries the LLMResponse so the caller can still account for tokens
    spent.
    """

    def __init__(self, message: str, response: LLMResponse):
        super().__init__(message)
        self.response = response
