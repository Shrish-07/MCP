"""Anthropic-only LLM cost tracking — PURE OBSERVABILITY, ZERO ENFORCEMENT.

Design decisions (per project policy):
  - This module NEVER stops, blocks, warns-as-gate, or changes trading
    behaviour. It records and reports; nothing more.
  - Accuracy contract: every number comes directly from the Anthropic API
    response `usage` object (input_tokens, cache_creation_input_tokens,
    cache_read_input_tokens, output_tokens). No estimation, no guessing.
    estimated_usd = (input - cache_create - cache_read) * in_rate
                  + cache_create * cache_write_rate
                  + cache_read   * cache_read_rate
                  + output       * out_rate
    This mirrors the Anthropic dashboard formula exactly, so totals here
    will match the dashboard given the correct rates in settings.json.
  - Rates are configurable in config/settings.json → cost_tracking.pricing
    (per-1M-token USD). set_pricing() should be called once after config
    load; built-in defaults are used otherwise.
  - Storage: per-call records are APPEND-ONLY (logs/cost_records.jsonl, one
    JSON object per line — O(1) writes, grep-friendly). Daily/monthly
    aggregates live in the small logs/cost_tracker.json summary file.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, DefaultDict


# Built-in fallback rates (USD per 1M tokens) used until set_pricing() is
# called with the values from config/settings.json.
_DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "sonnet": {"input": 3.00, "cache_write": 3.75, "cache_read": 0.30, "output": 15.00},
    "*": {"input": 3.00, "cache_write": 3.75, "cache_read": 0.30, "output": 15.00},
}


class CostTracker:
    """Anthropic-only cost tracker. Singleton. Observability only."""

    _instance: "CostTracker | None" = None

    def __new__(cls) -> "CostTracker":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.logger = logging.getLogger("robinhood-agent.cost")
            cls._instance._daily_totals: DefaultDict[str, float] = defaultdict(float)
            cls._instance._monthly_totals: DefaultDict[str, float] = defaultdict(float)
            cls._instance._pricing: dict[str, dict[str, float]] = dict(_DEFAULT_PRICING)
            cls._instance._load()
        return cls._instance

    # ------------------------------------------------------------------ #
    #  Storage paths
    # ------------------------------------------------------------------ #

    def _logs_dir(self) -> Path:
        path = Path(__file__).resolve().parent.parent / "logs"
        path.mkdir(exist_ok=True)
        return path

    def _summary_path(self) -> Path:
        return self._logs_dir() / "cost_tracker.json"

    def _records_path(self) -> Path:
        return self._logs_dir() / "cost_records.jsonl"

    # ------------------------------------------------------------------ #
    #  Pricing configuration
    # ------------------------------------------------------------------ #

    def set_pricing(self, pricing: dict[str, Any] | None) -> None:
        """Install the pricing table from config/settings.json.

        Accepts either per-model entries plus a "*" catch-all:
          {"claude-sonnet-5-5": {"input": 3.0, ...}, "*": {...}}
        Rates are per-1M-token USD.
        """
        if not isinstance(pricing, dict) or not pricing:
            return
        table: dict[str, dict[str, float]] = {}
        for model, rates in pricing.items():
            if not isinstance(rates, dict):
                continue
            try:
                table[str(model).lower()] = {
                    "input": float(rates.get("input", 0.0)),
                    "cache_write": float(rates.get("cache_write", 0.0)),
                    "cache_read": float(rates.get("cache_read", 0.0)),
                    "output": float(rates.get("output", 0.0)),
                }
            except (TypeError, ValueError):
                continue
        if table:
            if "*" not in table:
                table["*"] = table.get("sonnet", _DEFAULT_PRICING["*"])
            self._pricing = table
            self.logger.info("cost_pricing_set models=%s", sorted(table.keys()))

    def _rates_for(self, model: str) -> dict[str, float]:
        name = model.lower()
        if name in self._pricing:
            return self._pricing[name]
        for key, rates in self._pricing.items():
            if key != "*" and key in name:
                return rates
        return self._pricing.get("*", _DEFAULT_PRICING["*"])

    # ------------------------------------------------------------------ #
    #  Persistence
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        """Restore persisted daily/monthly aggregates (record detail lives in JSONL)."""
        path = self._summary_path()
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self._daily_totals = defaultdict(
                float, {k: float(v) for k, v in payload.get("daily_totals", {}).items()}
            )
            self._monthly_totals = defaultdict(
                float, {k: float(v) for k, v in payload.get("monthly_totals", {}).items()}
            )
        except Exception as exc:
            self.logger.warning("cost_tracker_load_failed: %s", exc)
            self._daily_totals = defaultdict(float)
            self._monthly_totals = defaultdict(float)

    def _persist_summary(self) -> None:
        try:
            payload = {
                "daily_totals": dict(self._daily_totals),
                "monthly_totals": dict(self._monthly_totals),
                "pricing": self._pricing,
            }
            self._summary_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            self.logger.warning("cost_tracker_summary_persist_failed: %s", exc)

    # ------------------------------------------------------------------ #
    #  Recording (the ONLY thing this class does)
    # ------------------------------------------------------------------ #

    def record(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> float:
        """Record one API call's usage exactly as reported by the API.

        `input_tokens` is the API-reported total input count; the non-cached
        (base-rate) portion is derived exactly as the Anthropic dashboard
        does: input - cache_creation - cache_read.

        Returns the estimated USD cost of this call.
        """
        rates = self._rates_for(model)
        regular_input = max(0, int(input_tokens) - int(cache_creation_tokens) - int(cache_read_tokens))

        estimated_usd = (
            (regular_input * rates["input"]
             + cache_creation_tokens * rates["cache_write"]
             + cache_read_tokens * rates["cache_read"]
             + output_tokens * rates["output"]) / 1_000_000
        )

        record = {
            "model": model,
            "input_tokens": int(input_tokens),
            "cache_creation_tokens": int(cache_creation_tokens),
            "cache_read_tokens": int(cache_read_tokens),
            "output_tokens": int(output_tokens),
            "regular_input_tokens": regular_input,
            "estimated_usd": estimated_usd,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Append-only per-call record (O(1), never rewrites history).
        try:
            with self._records_path().open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except Exception as exc:
            self.logger.warning("cost_record_append_failed: %s", exc)

        today = datetime.now(timezone.utc).date().isoformat()
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        self._daily_totals[today] += estimated_usd
        self._monthly_totals[month] += estimated_usd

        self.logger.info(
            "cost_record model=%s input=%d cache_create=%d cache_read=%d output=%d "
            "usd=%.6f today=%.4f month=%.4f",
            model, input_tokens, cache_creation_tokens, cache_read_tokens,
            output_tokens, estimated_usd,
            self._daily_totals[today], self._monthly_totals[month],
        )
        self._persist_summary()
        return estimated_usd

    # ------------------------------------------------------------------ #
    #  Read-only totals (informational)
    # ------------------------------------------------------------------ #

    def daily_total(self) -> float:
        return self._daily_totals.get(datetime.now(timezone.utc).date().isoformat(), 0.0)

    def monthly_total(self) -> float:
        return self._monthly_totals.get(datetime.now(timezone.utc).strftime("%Y-%m"), 0.0)
