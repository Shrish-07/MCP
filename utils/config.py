"""Central configuration loader for the trading agent.

SOURCE OF TRUTH RULE:
    config/settings.json holds every tunable and toggle (models, thresholds,
    schedule, monitor speed, dry-run...). .env contains ONLY secrets
    (ANTHROPIC_API_KEY, FINNHUB_API_KEY). Nothing else lives in the env.

Live execution requires the JSON boolean false for execution.dry_run and
separate explicit release approval. Keys beginning with "// " document
settings in standard JSON and are removed before validation and hashing.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from utils.strategy_config import DEFAULTS as STRATEGY_DEFAULTS
from utils.strategy_config import validate

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
    "logging": {"level": "INFO"},
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
        "open_hour": 9,
        "open_minute": 35,
        "mid_hour": 12,
        "mid_minute": 30,
        "close_hour": 15,
        "close_minute": 30,
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
    "news": {
        "provider": "finnhub",
        "lookback_days": 3,
        "max_per_ticker": 5,
        "fresh_window_hours": 24,
        "general_market": {"enabled": True, "category": "general", "count": 8},
    },
    "sentiment_selection": {
        "enabled": False,
        "universe_size": 10,
        "lookback_days": 7,
        "min_sentiment_score": -1.0,
        "schedule_day": "sun",
        "schedule_hour": 20,
        "schedule_minute": 0,
        "schedule_timezone": "America/New_York",
        "batch_size": 16,
        "cache_max_age_hours": 24,
    },
    "cost_tracking": {"enabled": True, "pricing": {}},
    "reporting": {
        "attribution_enabled": True,
        "attribution_hour": 16,
        "attribution_minute": 5,
    },
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


def _settings_object(pairs):
    result = {}
    for key, value in pairs:
        if key.startswith("// "):
            continue
        if key in result:
            raise ValueError("Duplicate settings key")
        result[key] = value
    return result


def load_config(*, include_secrets: bool = True, root: Path | None = None) -> dict:
    """Load + validate config/settings.json with fail-safe defaults applied."""
    base = (
        Path(root).resolve()
        if root is not None
        else Path(__file__).resolve().parents[1]
    )
    path = base / "config" / "settings.json"

    try:
        user_settings = json.loads(
            path.read_text(encoding="utf-8-sig"), object_pairs_hook=_settings_object
        )
    except FileNotFoundError:
        logger.error(
            "config_missing path=%s — running on SAFE DEFAULTS (dry-run)", path
        )
        user_settings = {}
    except (OSError, ValueError) as exc:
        logger.error(
            "config_unreadable type=%s — running on SAFE DEFAULTS (dry-run)",
            type(exc).__name__,
        )
        user_settings = {}

    if not isinstance(user_settings, dict):
        logger.error("config_not_a_dict — running on SAFE DEFAULTS (dry-run)")
        user_settings = {}

    defaults = _deep_merge(_DEFAULTS, STRATEGY_DEFAULTS)
    settings = _deep_merge(defaults, user_settings)
    errors = validate(settings, defaults)
    for section in defaults:
        if not isinstance(settings.get(section), dict):
            settings[section] = defaults[section]

    # ---- Fail-safe coercions on the critical safety switches ----
    execution = settings["execution"]
    execution["dry_run"] = bool(errors) or execution.get("dry_run") is not False
    execution["live_release_approved"] = execution.get("live_release_approved") is True

    # env block: secrets from .env, safety switches from settings.json ONLY.
    if include_secrets and not errors:
        load_dotenv(base / ".env", override=False)
    settings["env"] = {
        "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", "")
        if include_secrets and not errors
        else "",
        "FINNHUB_API_KEY": os.getenv("FINNHUB_API_KEY", "")
        if include_secrets and not errors
        else "",
        "DRY_RUN": settings["execution"]["dry_run"],
    }
    settings["configuration_errors"] = errors
    if errors:
        logger.error("configuration_invalid errors=%s", errors)

    settings["config_revision"] = hashlib.sha256(
        json.dumps(user_settings, sort_keys=True).encode()
    ).hexdigest()
    return settings
