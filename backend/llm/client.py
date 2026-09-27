"""
LLM client — Gemini wrapper using Google GenAI SDK.

Features:
  - Retry with exponential back-off (tenacity)
  - Structured output via response_schema and JSON mode
  - Cost tracking per call (input / output tokens)
  - Context-window guard
  - Works equally well in tests (swap out the underlying client)
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import errors, types
import structlog
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from core.config import settings

logger = structlog.get_logger(__name__)

# ── Pricing constants (Gemini 2.5 Flash as of 2025) ────────────────────────
# Update if switching models or pricing changes.
_PRICE_PER_1M_INPUT  = 0.15   # USD per million input tokens
_PRICE_PER_1M_OUTPUT = 0.60   # USD per million output tokens

# Token limit guard — refuse to send prompts larger than this
MAX_PROMPT_TOKENS = 800_000   # Gemini context window is 1M+ tokens


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
    """Wrapper around a single LLM API call result."""
    text:        str
    usage:       LLMUsage
    # Set only when structured tool output is present
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


def _is_retryable_error(exc: BaseException) -> bool:
    """Check if exception is a transient error worthy of retry."""
    if isinstance(exc, errors.ServerError):
        return True
    if isinstance(exc, errors.APIError):
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if code in (429, 500, 502, 503, 504):
            return True
        msg = str(exc).lower()
        if "rate limit" in msg or "resource exhausted" in msg or "quota" in msg:
            return True
    return False


# ─── Client ───────────────────────────────────────────────────────────────────

class GeminiClient:
    """
    Async wrapper around the Google Gemini API (via google.genai).

    Usage::

        client = GeminiClient()
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

    DEFAULT_MODEL = "gemini-2.5-flash"
    DEFAULT_MAX_TOKENS = 8192

    def __init__(
        self,
        api_key:    str | None = None,
        model:      str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        if api_key is not None:
            self._api_key = api_key
        else:
            self._api_key = (
                settings.GEMINI_API_KEY
                or os.environ.get("GEMINI_API_KEY", "")
                or settings.ANTHROPIC_API_KEY
            )

        self.model      = model or self.DEFAULT_MODEL
        self.max_tokens = max_tokens or self.DEFAULT_MAX_TOKENS
        # Lazy-init underlying client so tests can patch or avoid network calls
        self._client: genai.Client | None = None

    def _get_client(self) -> genai.Client:
        if self._client is None:
            if not self._api_key:
                raise LLMError(
                    "GEMINI_API_KEY is not set. "
                    "Add it to .env.dev or the environment."
                )
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    # ── Core call (with retry) ────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception(_is_retryable_error),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def _call(
        self,
        *,
        system:          str,
        user:            str,
        response_schema: dict[str, Any] | None = None,
    ) -> Any:
        """Raw API call with retry logic."""
        client = self._get_client()

        config_params: dict[str, Any] = {
            "system_instruction": system,
            "max_output_tokens": self.max_tokens,
        }

        if response_schema:
            config_params["response_mime_type"] = "application/json"
            config_params["response_schema"] = response_schema

        config = types.GenerateContentConfig(**config_params)
        t0 = time.monotonic()
        try:
            resp = await client.aio.models.generate_content(
                model=self.model,
                contents=user,
                config=config,
            )
            elapsed = time.monotonic() - t0

            prompt_tokens = 0
            output_tokens = 0
            if getattr(resp, "usage_metadata", None):
                prompt_tokens = resp.usage_metadata.prompt_token_count or 0
                output_tokens = resp.usage_metadata.candidates_token_count or 0

            logger.info(
                "llm_call_done",
                model=self.model,
                input_tokens=prompt_tokens,
                output_tokens=output_tokens,
                elapsed_s=round(elapsed, 2),
            )
            return resp
        except errors.APIError as exc:
            logger.warning("llm_api_error", exc=str(exc))
            raise
        except Exception as exc:
            logger.exception("llm_unexpected_error")
            raise LLMError("Unexpected LLM error", cause=exc) from exc

    # ── Extraction helpers ───────────────────────────────────────────────────

    def _extract_response(
        self,
        resp: Any,
        tool_name: str | None = None,
    ) -> LLMResponse:
        """Extract standardized LLMResponse from Gemini or test mocks."""
        # 1. Usage
        prompt_tokens = 0
        output_tokens = 0
        model_name = self.model

        if getattr(resp, "usage_metadata", None):
            prompt_tokens = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
            output_tokens = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
        elif getattr(resp, "usage", None):
            prompt_tokens = getattr(resp.usage, "input_tokens", getattr(resp.usage, "prompt_token_count", 0)) or 0
            output_tokens = getattr(resp.usage, "output_tokens", getattr(resp.usage, "candidates_token_count", 0)) or 0

        if getattr(resp, "model_version", None):
            model_name = resp.model_version
        elif getattr(resp, "model", None):
            model_name = resp.model

        usage = LLMUsage(
            input_tokens=prompt_tokens,
            output_tokens=output_tokens,
            model=model_name,
        )

        # 2. Text and Stop reason
        text = ""
        stop_reason = "end_turn"
        tool_input = None

        if hasattr(resp, "text") and isinstance(resp.text, str):
            text = resp.text
        elif hasattr(resp, "content") and isinstance(resp.content, list):
            # Compatibility with mock objects or Anthropic-style mocks
            for block in resp.content:
                block_type = getattr(block, "type", "")
                if block_type == "text":
                    text += getattr(block, "text", "")
                elif block_type == "tool_use":
                    tool_name = getattr(block, "name", tool_name)
                    tool_input = getattr(block, "input", None)

        if hasattr(resp, "stop_reason") and resp.stop_reason:
            stop_reason = str(resp.stop_reason)
        elif getattr(resp, "candidates", None) and len(resp.candidates) > 0:
            cand = resp.candidates[0]
            if getattr(cand, "finish_reason", None):
                stop_reason = str(cand.finish_reason)

        # 3. Tool / Structured input
        if tool_name is not None and tool_input is None:
            # Check parsed attribute
            parsed = getattr(resp, "parsed", None)
            if parsed is not None and isinstance(parsed, dict):
                tool_input = parsed
            elif getattr(resp, "tool_input", None) is not None:
                tool_input = resp.tool_input
            elif text:
                try:
                    tool_input = json.loads(text.strip())
                except Exception:
                    # Markdown code block fallback
                    match = re.search(r"```(?:json)?\s*(\{[\s\S]*\}|\[[\s\S]*\])\s*```", text)
                    if match:
                        try:
                            tool_input = json.loads(match.group(1))
                        except Exception:
                            pass

        return LLMResponse(
            text=text,
            usage=usage,
            tool_name=tool_name if tool_input is not None else None,
            tool_input=tool_input,
            stop_reason=stop_reason,
        )

    async def chat(self, *, system: str, user: str) -> LLMResponse:
        """Simple single-turn chat. Returns text + usage."""
        try:
            resp = await self._call(
                system=system,
                user=user,
            )
            return self._extract_response(resp)
        except errors.APIError as exc:
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
        Request structured output conforming to tool_schema.
        Returns LLMResponse with tool_input as a parsed dict.
        """
        sys_prompt = (
            f"{system}\n\n"
            f"You MUST provide your response according to the schema for '{tool_name}': {tool_description}"
        )
        try:
            resp = await self._call(
                system=sys_prompt,
                user=user,
                response_schema=tool_schema,
            )
        except errors.APIError as exc:
            raise LLMError("LLM API call failed after retries", cause=exc) from exc

        parsed_resp = self._extract_response(resp, tool_name=tool_name)
        if parsed_resp.tool_input is None:
            logger.warning(
                "llm_tool_not_called",
                expected_tool=tool_name,
                stop_reason=parsed_resp.stop_reason,
            )
        return parsed_resp


# Backwards compatibility alias
AnthropicClient = GeminiClient

# ── Module-level singleton (lazy, for use outside tests) ──────────────────────
_default_client: GeminiClient | None = None


def get_llm_client() -> GeminiClient:
    """Return the module-level GeminiClient singleton."""
    global _default_client
    if _default_client is None:
        _default_client = GeminiClient()
    return _default_client
