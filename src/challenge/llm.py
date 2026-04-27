"""Thin Anthropic client wrapper.

Reads MODEL_ID and ANTHROPIC_API_KEY (or API_KEY) from environment.
Provides a single function: `call_with_tool` — make one Anthropic
Messages API call with tool-use to enforce a Pydantic schema on the
output, return the parsed JSON dict and usage metadata.

Lives in its own module so the rest of the pipeline doesn't depend on
the SDK directly — easier to mock and easier to swap providers later.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# Pricing in USD per 1M tokens (rough — for BENCHMARK.md only).
PRICING_USD_PER_M_TOKENS: dict[str, tuple[float, float]] = {
    # (input, output)
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-7": (15.0, 75.0),
}


def estimate_cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    inp, out = PRICING_USD_PER_M_TOKENS.get(model_id, (3.0, 15.0))
    return (input_tokens * inp + output_tokens * out) / 1_000_000


def resolve_model_id() -> str:
    """Resolve model from env. Defaults to Sonnet 4.6 for production."""
    return os.environ.get("MODEL_ID") or "claude-sonnet-4-6"


def resolve_api_key() -> Optional[str]:
    """Anthropic API key from environment.

    Accepts either ANTHROPIC_API_KEY (native) or API_KEY (the .env
    convention from the original repo).
    """
    return (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("API_KEY")
        or None
    )


@dataclass
class LLMResponse:
    raw_input_dict: dict
    tool_use_input: dict          # what the model emitted as the tool input
    input_tokens: int
    output_tokens: int
    stop_reason: Optional[str]


def call_with_tool(
    *,
    system_prompt: str,
    user_message: str,
    tool_name: str,
    tool_description: str,
    tool_input_schema: dict,
    model_id: Optional[str] = None,
    max_tokens: int = 2048,
    temperature: float = 0.0,
) -> LLMResponse:
    """Call Anthropic Messages API once, forcing a tool-use response.

    Forces the model to emit a structured payload conforming to
    `tool_input_schema` (a JSON Schema dict). Returns the parsed
    payload plus usage metadata.

    Lives in its own function so unit tests can patch it cleanly.
    """
    import anthropic

    api_key = resolve_api_key()
    if not api_key:
        raise EnvironmentError(
            "no Anthropic API key in env (set ANTHROPIC_API_KEY or API_KEY)"
        )

    client = anthropic.Anthropic(api_key=api_key)
    model = model_id or resolve_model_id()

    tool = {
        "name": tool_name,
        "description": tool_description,
        "input_schema": tool_input_schema,
    }

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_prompt,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": user_message}],
    )

    # Extract the tool-use block
    tool_input: Optional[dict] = None
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            tool_input = block.input
            break

    if tool_input is None:
        raise RuntimeError(
            f"model did not emit a {tool_name} tool_use block; "
            f"stop_reason={response.stop_reason}, content={response.content!r}"
        )

    return LLMResponse(
        raw_input_dict=tool_input,
        tool_use_input=tool_input,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        stop_reason=response.stop_reason,
    )
