"""Async Anthropic client wrapper with retry and exact cost recording.

Single-model by design (the cheap/Haiku tier was removed — all judgment
goes through the decision model configured in settings.json).

Public helpers:
  - strip_fences(text): canonical ```json fence remover (single source of
    truth; core/decision/engine.py and main.py import this).
"""
from __future__ import annotations

import asyncio
import logging
import re

import anthropic

from utils.cost_tracker import CostTracker


def strip_fences(text: str) -> str:
    """Remove leading/trailing markdown code fences from LLM output."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


class AnthropicClient:
    """Async Anthropic client wrapper with retry and cost tracking."""

    def __init__(self, model: str, max_tokens: int = 4096,
                 send_temperature: bool = False, temperature: float = 0.1) -> None:
        self.model = model
        self.max_tokens = max_tokens
        # New-generation models reject the temperature parameter entirely;
        # send it only when the config explicitly asks for it.
        self.send_temperature = send_temperature
        self.temperature = temperature
        self.logger = logging.getLogger("robinhood-agent.anthropic")
        self.cost_tracker = CostTracker()
        # Strategic model availability must not prevent the safety backend
        # from starting. Missing credentials fail only the advisory call.
        self.client = None

    async def complete(self, system: str | list[dict], user: str) -> str:
        """Complete a prompt and return the clean response text.

        `system` may be a plain string or a list of content blocks with
        cache_control markers. Either format is passed through unchanged.
        """
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                if self.client is None:
                    self.client = anthropic.AsyncAnthropic(max_retries=0)
                request_kwargs = {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                }
                if self.send_temperature:
                    request_kwargs["temperature"] = self.temperature
                response = await self.client.messages.create(**request_kwargs)
                text = "".join(
                    block.text for block in getattr(response, "content", [])
                    if getattr(block, "type", "") == "text"
                )
                usage = getattr(response, "usage", None)
                if usage is not None:
                    cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
                    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
                    try:
                        self.cost_tracker.record(
                            getattr(response, "model", self.model), usage.input_tokens, usage.output_tokens,
                            cache_creation_tokens=cache_create, cache_read_tokens=cache_read,
                            raw_usage=usage.model_dump(mode="json"), request_id=getattr(response, "id", None))
                    except Exception as exc:
                        # A ledger failure must not retry an already billed API request.
                        self.logger.error("cost_observability_failed error=%s", exc)
                else:
                    self._record_unpriced("usage unavailable", getattr(response, "id", None))
                return strip_fences(text)
            except Exception as exc:
                last_error = exc
                if self.client is not None:
                    self._record_unpriced(type(exc).__name__, getattr(exc, "request_id", None))
                if self._is_retryable(exc) and attempt < 3:
                    self.logger.warning("anthropic_retry attempt=%d error=%s", attempt, exc)
                    await asyncio.sleep(5)
                    continue
                self.logger.exception("anthropic_completion_failed model=%s: %s", self.model, exc)
                raise RuntimeError("Anthropic API request failed") from exc
        raise RuntimeError("Anthropic API request failed") from last_error

    def _record_unpriced(self, reason, request_id):
        try:
            self.cost_tracker.record_unpriced(self.model, reason, request_id)
        except (OSError, ValueError):
            self.logger.error("Unable to record unpriced Anthropic attempt")

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        message = str(exc).lower()
        return "rate limit" in message or "529" in message or "too many requests" in message
