"""Thin Anthropic client wrapper with bounded retries and timeout.

Reads ``MODEL_ID`` and ``ANTHROPIC_API_KEY`` (or ``API_KEY``) from
environment. Provides one entry point: :func:`call_with_tool` — make
one Anthropic Messages API call with tool-use to enforce a Pydantic
schema on the output, return the parsed JSON dict and usage metadata.

Lives in its own module so the rest of the pipeline doesn't depend on
the SDK directly — easier to mock and easier to swap providers later.

Resilience:
- Bounded retries (3 attempts) on transient errors (rate-limit, 5xx,
  network) with exponential backoff via ``tenacity``.
- Hard timeout (default 60 s) on each request.
- Schema-violation responses do not retry — the caller decides what
  to do (Pydantic validation is upstream of this module).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from challenge.config import config_dir, load_yaml

logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT_S = 60.0
DEFAULT_RETRY_ATTEMPTS = 3


@dataclass(frozen=True)
class _Pricing:
    input_per_m_usd: float
    output_per_m_usd: float


_PRICING_CACHE: dict[str, _Pricing] = {}
_DEFAULT_PRICING: Optional[_Pricing] = None


def _load_pricing() -> tuple[dict[str, _Pricing], _Pricing]:
    """Load model pricing from ``config/synthesis/pricing.yaml`` once."""
    global _PRICING_CACHE, _DEFAULT_PRICING
    if _PRICING_CACHE and _DEFAULT_PRICING is not None:
        return _PRICING_CACHE, _DEFAULT_PRICING
    cfg = load_yaml(config_dir() / "synthesis" / "pricing.yaml")
    models = cfg.get("models", {}) or {}
    _PRICING_CACHE = {
        name: _Pricing(
            input_per_m_usd=float(spec["input_per_m_usd"]),
            output_per_m_usd=float(spec["output_per_m_usd"]),
        )
        for name, spec in models.items()
    }
    default = cfg.get("default", {"input_per_m_usd": 3.0, "output_per_m_usd": 15.0})
    _DEFAULT_PRICING = _Pricing(
        input_per_m_usd=float(default["input_per_m_usd"]),
        output_per_m_usd=float(default["output_per_m_usd"]),
    )
    return _PRICING_CACHE, _DEFAULT_PRICING


def estimate_cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost from token counts using the configured pricing.

    Falls back to ``default`` pricing when the model id is unknown.
    """
    pricing, default = _load_pricing()
    p = pricing.get(model_id, default)
    return (input_tokens * p.input_per_m_usd + output_tokens * p.output_per_m_usd) / 1_000_000


def resolve_model_id() -> str:
    """Resolve model from env. Defaults to Sonnet 4.6."""
    return os.environ.get("MODEL_ID") or "claude-sonnet-4-6"


def resolve_api_key() -> Optional[str]:
    """Read the Anthropic key from ``ANTHROPIC_API_KEY`` or ``API_KEY``."""
    return os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("API_KEY") or None


@dataclass
class LLMResponse:
    """Structured response from a single tool-use Anthropic call."""

    raw_input_dict: dict
    tool_use_input: dict
    input_tokens: int
    output_tokens: int
    stop_reason: Optional[str]


def _build_client(timeout_s: float):
    """Construct the Anthropic SDK client with an explicit timeout."""
    import anthropic

    api_key = resolve_api_key()
    if not api_key:
        raise EnvironmentError(
            "no Anthropic API key in env (set ANTHROPIC_API_KEY or API_KEY)"
        )
    return anthropic.Anthropic(api_key=api_key, timeout=timeout_s)


def _is_transient(exc: BaseException) -> bool:
    """Decide whether a tenacity retry should consume an attempt."""
    import anthropic

    return isinstance(
        exc,
        (
            anthropic.APIConnectionError,
            anthropic.RateLimitError,
            anthropic.APITimeoutError,
            anthropic.InternalServerError,
        ),
    )


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
    timeout_s: float = DEFAULT_TIMEOUT_S,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
) -> LLMResponse:
    """Call the Messages API once, forcing a tool-use response shape.

    Args:
      system_prompt:       instructions the model treats as system role.
      user_message:        the single user-turn payload.
      tool_name:           the tool name the model must call (forced).
      tool_description:    one-line summary, shown to the model.
      tool_input_schema:   JSON Schema for the tool's input.
      model_id:            override for ``MODEL_ID`` env var.
      max_tokens:          per-call output limit.
      temperature:         0 by default for reproducibility.
      timeout_s:           hard timeout for the HTTP request.
      retry_attempts:      bounded retries on transient errors.

    Returns:
      ``LLMResponse`` with the parsed tool input and usage counts.

    Raises:
      EnvironmentError:    no API key in env.
      RuntimeError:        model returned no tool_use block.
      anthropic.APIError:  on non-transient errors after retries.
    """
    model = model_id or resolve_model_id()
    tool = {
        "name": tool_name,
        "description": tool_description,
        "input_schema": tool_input_schema,
    }

    @retry(
        retry=retry_if_exception(_is_transient),
        stop=stop_after_attempt(retry_attempts),
        wait=wait_exponential(multiplier=1.0, min=1.0, max=15.0),
        reraise=True,
    )
    def _do_call():
        client = _build_client(timeout_s)
        return client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": user_message}],
        )

    response = _do_call()

    tool_input: Optional[dict] = None
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            tool_input = block.input
            break

    if tool_input is None:
        raise RuntimeError(
            f"model did not emit a {tool_name} tool_use block; "
            f"stop_reason={response.stop_reason}"
        )

    return LLMResponse(
        raw_input_dict=tool_input,
        tool_use_input=tool_input,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        stop_reason=response.stop_reason,
    )


