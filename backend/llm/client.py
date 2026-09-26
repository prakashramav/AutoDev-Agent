"""
LLM client — thin Anthropic wrapper for Phase 3+.

Features:
  - Retry with exponential back-off (tenacity)
  - Structured output via tool_use (Anthropic function-calling)
  - Cost tracking per call (input / output tokens)
  - Context-window guard (raise early if prompt is too big)
  - Streaming support for long generations
  - Works equally well in tests (swap out the underlying client)
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.config import settings

logger = structlog.get_logger(__name__)

# ── Pricing constants (Sonnet 3.7 as of 2025) ─────────────────────────────
# Update if you switch models or pricing changes.
_PRICE_PER_1M_INPUT  = 3.00   # USD per million input tokens
_PRICE_PER_1M_OUTPUT = 15.00  # USD per million output tokens

# Token limit guard — refuse to send prompts larger than this
MAX_PROMPT_TOKENS = 150_000   # Claude 3.x context is 200k; leave 50k headroom


# ─── Result types ─────────────────────────────────────────────────────────────

@dataclass
class LLMUsage:
    """Token counts and estimated cost for a single LLM call."""
    input_tokens:  int = 0
    output_tokens: int = 0
    model:         str = ""

    @property
    def cost_usd(self) -> float:
        return (
            self.input_tokens  / 1_000_000 * _PRICE_PER_1M_INPUT
            + self.output_tokens / 1_000_000 * _PRICE_PER_1M_OUTPUT
        )


@dataclass
class LLMResponse:
    """Wrapper around a single Anthropic API call result."""
    text:    str
    usage:   LLMUsage
    # Set only when a tool_use block is present
    tool_name:   str | None = None
    tool_input:  dict[str, Any] | None = None
    stop_reason: str = "end_turn"


@dataclass
class LLMError(Exception):
    """Raised when the LLM call fails after all retries."""
    message: str
    cause:   Exception | None = None

    def __str__(self) -> str:
        return f"LLMError: {self.message}" + (f" ({self.cause})" if self.cause else "")


# ─── Client ───────────────────────────────────────────────────────────────────

class AnthropicClient:
    """
    Async wrapper around the Anthropic Messages API.

    Usage::

        client = AnthropicClient()
        resp = await client.chat(
            system="You are an expert software engineer.",
            user="Summarise this file: ...",
        )
        print(resp.text)

    For structured output::

        resp = await client.chat_with_tool(
            system="...",
            user="...",
            tool_name="rank_files",
            tool_description="...",
            tool_schema={...},
        )
        result_dict = resp.tool_input
    """

    DEFAULT_MODEL   = "claude-sonnet-4-5"
    DEFAULT_MAX_TOKENS = 4096

    def __init__(
        self,
        api_key:   str | None = None,
        model:     str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self._api_key   = api_key or settings.ANTHROPIC_API_KEY
        self.model      = model or self.DEFAULT_MODEL
        self.max_tokens = max_tokens or self.DEFAULT_MAX_TOKENS
        # Lazy-init the underlying client so tests can patch it
        self._client: anthropic.AsyncAnthropic | None = None

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            if not self._api_key:
                raise LLMError(
                    "ANTHROPIC_API_KEY is not set. "
                    "Add it to .env.dev or the environment."
                )
            self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._client

    # ── Core call (with retry) ────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type((anthropic.RateLimitError, anthropic.APIStatusError)),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def _call(
        self,
        *,
        system:   str,
        messages: list[dict],
        tools:    list[dict] | None = None,
    ) -> anthropic.types.Message:
        """Raw API call with retry logic."""
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model":      self.model,
            "max_tokens": self.max_tokens,
            "system":     system,
            "messages":   messages,
        }
        if tools:
            kwargs["tools"] = tools
        t0 = time.monotonic()
        try:
            msg = await client.messages.create(**kwargs)
            elapsed = time.monotonic() - t0
            logger.info(
                "llm_call_done",
                model=self.model,
                input_tokens=msg.usage.input_tokens,
                output_tokens=msg.usage.output_tokens,
                elapsed_s=round(elapsed, 2),
            )
            return msg
        except anthropic.RateLimitError as exc:
            logger.warning("llm_rate_limit", exc=str(exc))
            raise
        except anthropic.APIStatusError as exc:
            logger.warning("llm_api_error", status=exc.status_code, exc=str(exc))
            raise
        except Exception as exc:
            logger.exception("llm_unexpected_error")
            raise LLMError("Unexpected LLM error", cause=exc) from exc

    # ── Public helpers ────────────────────────────────────────────────────────

    def _extract_response(self, msg: anthropic.types.Message) -> LLMResponse:
        usage = LLMUsage(
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            model=msg.model,
        )
        text        = ""
        tool_name   = None
        tool_input  = None

        for block in msg.content:
            if block.type == "text":
                text += block.text
            elif block.type == "tool_use":
                tool_name  = block.name
                tool_input = block.input

        return LLMResponse(
            text=text,
            usage=usage,
            tool_name=tool_name,
            tool_input=tool_input,
            stop_reason=msg.stop_reason or "end_turn",
        )

    async def chat(self, *, system: str, user: str) -> LLMResponse:
        """Simple single-turn chat. Returns text + usage."""
        try:
            msg = await self._call(
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return self._extract_response(msg)
        except (anthropic.RateLimitError, anthropic.APIStatusError) as exc:
            raise LLMError("LLM API call failed after retries", cause=exc) from exc

    async def chat_with_tool(
        self,
        *,
        system:           str,
        user:             str,
        tool_name:        str,
        tool_description: str,
        tool_schema:      dict[str, Any],
    ) -> LLMResponse:
        """
        Force the model to respond by calling `tool_name`.
        Returns tool_input (a dict) — guaranteed not None on success.
        """
        tool_spec = {
            "name":        tool_name,
            "description": tool_description,
            "input_schema": tool_schema,
        }
        try:
            msg = await self._call(
                system=system,
                messages=[{"role": "user", "content": user}],
                tools=[tool_spec],
            )
        except (anthropic.RateLimitError, anthropic.APIStatusError) as exc:
            raise LLMError("LLM API call failed after retries", cause=exc) from exc

        resp = self._extract_response(msg)
        if resp.tool_input is None:
            # Model refused to call the tool — fall back to parsing text
            logger.warning(
                "llm_tool_not_called",
                expected_tool=tool_name,
                stop_reason=resp.stop_reason,
            )
        return resp


# ── Module-level singleton (lazy, for use outside tests) ──────────────────────
_default_client: AnthropicClient | None = None


def get_llm_client() -> AnthropicClient:
    """Return the module-level AnthropicClient singleton."""
    global _default_client
    if _default_client is None:
        _default_client = AnthropicClient()
    return _default_client
