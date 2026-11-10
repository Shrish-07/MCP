"""Anthropic observability only. Raw disjoint usage categories are retained.

Official contract: input_tokens already excludes cache reads/writes. Pricing
source: https://platform.claude.com/docs/en/about-claude/pricing (2026-11-07).
Unknown models/categories remain unpriced; cost can NEVER gate a trade.
"""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from utils.persistence import atomic_json

RATES = {"claude-sonnet-5-5": {"input": 2., "output": 10., "cache_write": 2.5,
    "cache_write_1h": 4., "cache_read": .1}}


class CostTracker:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            obj = super().__new__(cls)
            obj.logger = logging.getLogger("robinhood-agent.cost")
            obj._daily_totals, obj._monthly_totals = defaultdict(float), defaultdict(float)
            obj._pricing = dict(RATES)
            obj.pricing_version = "anthropic-2026-11-07"
            obj._request_ids = set()
            cls._instance = obj
            try:
                lines=obj._records_path().read_text(encoding="utf-8").splitlines() if obj._records_path().exists() else []
            except OSError as exc:
                obj.logger.error("cost_ledger_unavailable; totals incomplete error=%s",exc);lines=[]
            for line in lines:
                try:
                    row=json.loads(line);request_id=row["request_id"];amount=row["estimated_usd"];stamp=row["timestamp"]
                    if not isinstance(request_id,str) or not isinstance(stamp,str) or amount is not None and (not math.isfinite(amount) or amount<0):
                        raise ValueError("invalid cost record")
                except (ValueError,KeyError,TypeError):
                    obj.logger.error("cost_ledger_corrupt_line; totals incomplete");continue
                if request_id in obj._request_ids:continue
                obj._request_ids.add(request_id)
                if amount is not None:
                    obj._daily_totals[stamp[:10]]+=amount;obj._monthly_totals[stamp[:7]]+=amount
        return cls._instance

    def _logs_dir(self):
        path = Path(__file__).resolve().parents[1]/"logs"
        path.mkdir(exist_ok=True)
        return path

    def _records_path(self):
        return self._logs_dir()/"cost_records_v2.jsonl"

    def _summary_path(self):
        return self._logs_dir()/"cost_summary_v2.json"

    def set_pricing(self, pricing, version=None):
        if not isinstance(pricing, dict):
            return
        table = {}
        for model, rate in pricing.items():
            if not isinstance(rate, dict) or not all(k in rate for k in ("input", "output", "cache_write", "cache_read")):
                continue
            numeric = {k: float(v) for k, v in rate.items() if isinstance(v, (int, float))}
            if all(math.isfinite(v) and v >= 0 for v in numeric.values()):
                table[model] = dict(rate, **numeric)
        if table:
            self._pricing = table
            if version is None:
                import hashlib
                self.pricing_version="configured-"+hashlib.sha256(json.dumps(table,sort_keys=True).encode()).hexdigest()[:12]
        if version:
            self.pricing_version = version

    def record(self, model, input_tokens, output_tokens, cache_creation_tokens=0, cache_read_tokens=0,
               *, raw_usage=None, request_id=None, timestamp=None, batch=False):
        raw = raw_usage or {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation_tokens, "cache_read_input_tokens": cache_read_tokens}
        request_id = request_id or str(uuid4())
        if request_id in self._request_ids:
            return None
        categories = {"input": int(input_tokens), "output": int(output_tokens),
                      "cache_read": int(cache_read_tokens)}
        cache = raw.get("cache_creation") or {}
        one_hour = int(cache.get("ephemeral_1h_input_tokens") or 0)
        five_min = int(cache.get("ephemeral_5m_input_tokens", int(cache_creation_tokens)-one_hour) or 0)
        categories.update(cache_write=five_min, cache_write_1h=one_hour)
        if any(v < 0 for v in categories.values()) or one_hour+five_min != int(cache_creation_tokens):
            raise ValueError("inconsistent usage categories")
        rates = self._pricing.get(model)
        unpriced = []
        total_prompt = int(input_tokens)+int(cache_creation_tokens)+int(cache_read_tokens)
        if rates and rates.get("long_context_threshold") and total_prompt > rates["long_context_threshold"]:
            rates = rates.get("long_context_rates")
        if not rates:
            unpriced.append("model/context rate unavailable")
        # Server tool charges and nonstandard service tiers need explicit tariffs.
        if any(v for v in (raw.get("server_tool_use") or {}).values()):
            unpriced.append("server tool use")
        if raw.get("service_tier") not in (None, "standard", "batch"):
            unpriced.append("nonstandard service tier")
        if rates:
            for category, count in categories.items():
                if count and category not in rates:
                    unpriced.append(category)
        multiplier = (.5 if batch or raw.get("service_tier") == "batch" else 1.)
        if raw.get("inference_geo") == "us":
            multiplier *= 1.1
        usd = None if unpriced else sum(count*rates.get(category, 0) for category,count in categories.items())/1e6*multiplier
        stamp = timestamp or datetime.now(timezone.utc).isoformat()
        row = {"schema_version": 2, "request_id": request_id, "model": model, "timestamp": stamp,
            "raw_usage": raw, "categories": categories, "input_tokens": int(input_tokens),
            "regular_input_tokens": int(input_tokens), "output_tokens": int(output_tokens),
            "cache_creation_tokens": int(cache_creation_tokens), "cache_read_tokens": int(cache_read_tokens),
            "pricing_version": self.pricing_version, "rates": rates, "multiplier": multiplier,
            "estimated_usd": usd, "unpriced_categories": unpriced}
        with self._records_path().open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, allow_nan=False)+"\n")
            stream.flush()
            import os
            os.fsync(stream.fileno())
        self._request_ids.add(request_id)
        if usd is not None:
            self._daily_totals[stamp[:10]] += usd
            self._monthly_totals[stamp[:7]] += usd
        atomic_json(self._summary_path(), {"daily_totals": dict(self._daily_totals),
            "monthly_totals": dict(self._monthly_totals), "pricing_version": self.pricing_version,
            "status": "unpriced requests present" if unpriced else "estimated; dashboard reconciliation required"})
        return usd

    def daily_total(self):
        return self._daily_totals[datetime.now(timezone.utc).date().isoformat()]

    def record_unpriced(self, model, reason, request_id=None):
        request_id = request_id or str(uuid4())
        if request_id in self._request_ids:
            return
        row = {"schema_version": 2, "request_id": request_id, "model": model,
               "timestamp": datetime.now(timezone.utc).isoformat(),
               "pricing_version": self.pricing_version, "raw_usage": None,
               "estimated_usd": None, "unpriced_categories": [reason]}
        with self._records_path().open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, allow_nan=False) + "\n")
        self._request_ids.add(request_id)

    def monthly_total(self):
        return self._monthly_totals[datetime.now(timezone.utc).strftime("%Y-%m")]


def reconcile_window(records, dashboard, start, end):
    """Compare identical UTC [start,end) windows and request scopes; never certify a mismatch."""
    start, end = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if not start.tzinfo or not end.tzinfo or end <= start:
        raise ValueError("timezone-aware ordered window required")
    start,end=start.astimezone(timezone.utc),end.astimezone(timezone.utc)
    selected = [r for r in records if start <= datetime.fromisoformat(r["timestamp"]) < end]
    ids = set(dashboard["request_ids"])
    selected_ids = {r["request_id"] for r in selected}
    total = sum(r["estimated_usd"] or 0 for r in selected)
    delta = total-float(dashboard["usd"])
    return {"start": start.isoformat(), "end": end.isoformat(), "timezone": "UTC",
        "requests": len(selected), "scope_matches": ids == selected_ids,
        "tracker_usd": total, "dashboard_usd": float(dashboard["usd"]), "delta_usd": delta,
        "unpriced_requests": sum(r["estimated_usd"] is None for r in selected),
        "matches": ids == selected_ids and not any(r["estimated_usd"] is None for r in selected) and abs(delta) <= float(dashboard.get("rounding_tolerance_usd", .000001))}
