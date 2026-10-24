"""Central configuration loader for the trading agent.

SOURCE OF TRUTH RULE:
    config/settings.json holds every tunable and toggle (models, thresholds,
    schedule, monitor speed, dry-run...). .env contains ONLY secrets
    (ANTHROPIC_API_KEY, FINNHUB_API_KEY). Nothing else lives in the env.

FAIL-SAFE RULE:
    Any missing or malformed critical value degrades to the SAFE behaviour —
    e.g. execution.dry_run can ONLY be enabled by an explicit boolean `false`
    in settings.json; anything else (missing key, typo, null) means dry-run.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

logger = logging.getLogger("robinhood-agent.utils.config")


_DEFAULTS: dict[str, Any] = {
    "models": {
        "decision": "claude-sonnet-5-5",
        "max_tokens": 4096,
        "send_temperature": False,
    },
    "execution": {
        "dry_run": True,
        "mcp_url": "https://agent.robinhood.com/mcp/trading",
        "tradability_fail_mode": "closed",
        "min_dollar_volume": 75_000_000,
    },
    "trading": {
        "max_positions": 12,
        "max_position_pct": 0.08,
        "edge_score_threshold": 3.0,
        "take_profit_pct": 0.20,
        "profit_review_pct": 0.15,
        "max_holding_days": 15,
        "cycle_edge_bonus": {"OPEN": 0.0, "MID": 0.25, "CLOSE": 0.5},
        "stop_loss_bands": [[0, -0.07], [30, -0.10], [60, -0.13], [90, -0.16]],
        "size_ladder": [[3.0, 0.03], [3.5, 0.04], [4.0, 0.06], [4.5, 0.08]],
        "vol_sizing": [[30, 0.8], [60, 0.6], [90, 0.45]],
        "extreme_vol_reject_30d": 120.0,
        "market_hours": {"calendar": "NYSE", "stale_data_guard": True},
    },
    "schedule": {
        "open_hour": 9, "open_minute": 35,
        "mid_hour": 12, "mid_minute": 30,
        "close_hour": 15, "close_minute": 30,
        "timezone": "America/New_York",
    },
    "monitor": {
        "enabled": True,
        "historicals_span": "3month",
        "interval_seconds": 15,
        "profit_review_cooldown_min": 15,
        "profit_target_cooldown_min": 5,
        "check_max_holding": True,
    },
    "watchlist": {
        "default_tickers": [],
        "robinhood_watchlist_name": "Default",
    },
    "news": {},
    "sentiment_selection": {},
    "cost_tracking": {"enabled": True, "pricing": {}},
    "reporting": {"attribution_enabled": True, "attribution_hour": 16, "attribution_minute": 5},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base; override wins. Extra keys kept."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _safe_bool(value: Any, default: bool = True) -> bool:
    """Coerce anything to bool with a fail-safe default.

    For dry-run semantics: live trading (False) is enabled ONLY by an
    explicit `false`. Everything unknown/unparseable falls back to default.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text == "false":
        return False
    if text == "true":
        return True
    return default


def load_config() -> dict:
    """Load + validate config/settings.json with fail-safe defaults applied."""
    load_dotenv(".env", override=False)
    base = Path(__file__).resolve().parent.parent
    path = base / "config" / "settings.json"

    try:
        user_settings = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.error("config_missing path=%s — running on SAFE DEFAULTS (dry-run)", path)
        user_settings = {}
    except json.JSONDecodeError as exc:
        logger.error("config_malformed error=%s — running on SAFE DEFAULTS (dry-run)", exc)
        user_settings = {}

    if not isinstance(user_settings, dict):
        logger.error("config_not_a_dict — running on SAFE DEFAULTS (dry-run)")
        user_settings = {}

    settings = _deep_merge(_DEFAULTS, user_settings)

    # ---- Fail-safe coercions on the critical safety switches ----
    execution = settings["execution"]
    execution["dry_run"] = _safe_bool(execution.get("dry_run"), default=True)
    if str(execution.get("mcp_url", "")).strip() == "":
        execution["mcp_url"] = _DEFAULTS["execution"]["mcp_url"]
    if str(execution.get("tradability_fail_mode", "")).lower() not in ("closed", "open"):
        execution["tradability_fail_mode"] = "closed"

    # env block: secrets from .env, safety switches from settings.json ONLY.
    settings["env"] = {
        "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
        "FINNHUB_API_KEY": os.getenv("FINNHUB_API_KEY", ""),
        "DRY_RUN": settings["execution"]["dry_run"],
    }
    return settings
