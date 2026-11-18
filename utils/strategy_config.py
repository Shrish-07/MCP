"""Versioned strategy defaults. New discovery remains disabled."""

DEFAULTS = {
    "discovery": {
        "enabled": False,
        "universe_file": "data/sp500_universe.json",
        "shortlist_file": "data/discovery_shortlist.json",
        "features_dir": "data",
        "universe_max_age_days": 14,
        "shortlist_max_age_days": 8,
        "min_cohort": 20,
        "gates": {
            "min_dollar_volume": 75000000,
            "min_price": 5.0,
            "max_vol_30d_pct_annualized": 120.0,
            "min_bars": 250,
        },
        "shortlist": {
            "new_candidate_count": 10,
            "divergence_pool_cap": 15,
            "drift_pool_cap": 5,
            "candidate_memory_runs": 2,
        },
        "gap_weights": {
            "sentiment_mean": 1.0,
            "velocity": 0.5,
            "delta": 0.5,
            "momentum_blend": [0.6, 0.4],
            "recent_move": 0.3,
        },
        "archetypes": {
            "lagged_gap_z": 1.0,
            "inflection_delta": 0.3,
            "avoid_gap_z": -1.0,
        },
        "fingerprints": {
            "pullback_min": 0.02,
            "pullback_max": 0.10,
            "uptrend_slope_min": 0.01,
            "base_range_ratio_max": 0.8,
            "base_vol_ratio_max": 0.8,
            "down_volume_ratio_max": 0.8,
            "exhaustion_min": 0.05,
            "exhaustion_max": 0.15,
        },
        "schedule": {"feature_hour_et": 20, "rank_day": "sun", "rank_hour_et": 20},
    },
    "exits": {
        "enabled": True,
        "breakeven_ratchet_pct": 0.05,
        "breakeven_lock_gain_pct": 0.005,
        "chandelier": {"activate_at_pct": 0.10, "atr_multiple": 3.0, "atr_period": 14},
        "take_profit_mode": "graduate",
        "take_profit_pct": 0.20,
        "climax": {
            "rsi_min": 80,
            "sigma_above_20dma": 3.0,
            "tighten_atr_multiple": 2.0,
        },
        "trend_break": {
            "ma_days": 20,
            "consecutive_closes": 2,
            "only_if_was_profitable": True,
        },
        "sentiment_reversal": {"delta_z": -1.0, "news_velocity_multiple": 2.0},
        "gap_closure_z": 0.3,
        "dead_money": {
            "trading_days": 10,
            "unrealized_band_pct": 0.02,
            "gap_progress_min": 0.25,
        },
        "rotation_margin_z": 0.8,
        "partial_take": {"enabled": False, "at_pct": 0.15, "fraction": 0.5},
        "specialized_review": {
            "drop_pct_threshold": 0.05,
            "vol_aware": True,
            "drop_atr_multiple": 1.5,
            "fifty_two_week_high_review": True,
            "review_cooldown_hours": 24,
            "new_high_rearm_atr_multiple": 1.0,
        },
        "pead": {
            "enabled": True,
            "min_sessions_after_earnings": 5,
            "capture_fraction_threshold": 0.8,
            "max_drift_sessions": 15,
            "min_comparable_events": 30,
        },
    },
    "trading": {
        "max_new_entries_per_day": 2,
        "sector_cap_pct": 0.30,
        "earnings_window_discount_notches": 1,
        "add_to_position": {
            "enabled": True,
            "max_adds_per_position": 1,
            "max_increment_pct_of_position_value": 0.25,
            "max_incremental_risk_fraction_of_equity": 0.0025,
            "cooldown_hours": 24,
            "require_valid_existing_stop": True,
        },
    },
    "execution": {
        "live_release_approved": False,
        "quote_max_age_seconds": 60,
        "reconciliation_max_age_seconds": 120,
        "manual_stop_file": "data/STOP_TRADING",
        "extended_hours_enabled": False,
    },
    "cost_tracking": {
        "pricing_version": "anthropic-2026-11-07",
        "pricing": {
            "claude-sonnet-5-5": {
                "input": 2.0,
                "output": 10.0,
                "cache_write": 2.5,
                "cache_write_1h": 4.0,
                "cache_read": 0.1,
            }
        },
    },
}


def validate(settings, defaults=DEFAULTS):
    from urllib.parse import urlsplit
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    errors = []

    def walk(expected, actual, prefix):
        if not isinstance(actual, dict):
            errors.append(prefix + ": object required")
            return
        for key, default in expected.items():
            value = actual.get(key)
            path = f"{prefix}.{key}"
            if isinstance(default, dict):
                walk(default, value, path)
            elif isinstance(default, bool) and not isinstance(value, bool):
                errors.append(path + ": boolean required")
            elif (
                isinstance(default, (int, float)) and not isinstance(default, bool)
            ) and not _finite_number(value):
                errors.append(path + ": finite number required")
            elif isinstance(default, str) and (
                not isinstance(value, str) or not value.strip()
            ):
                errors.append(path + ": nonempty string required")
            elif isinstance(default, list) and not isinstance(value, list):
                errors.append(path + ": array required")

    walk(defaults, settings, "config")
    if errors:
        return errors
    d = settings["discovery"]
    e = settings["exits"]
    a = settings["trading"]["add_to_position"]
    for key in (
        "new_candidate_count",
        "divergence_pool_cap",
        "drift_pool_cap",
        "candidate_memory_runs",
    ):
        if type(d["shortlist"][key]) is not int or d["shortlist"][key] < 1:
            errors.append("shortlist." + key)
    if (
        not 0
        < e["breakeven_lock_gain_pct"]
        < e["breakeven_ratchet_pct"]
        < e["chandelier"]["activate_at_pct"]
        < 1
    ):
        errors.append("exit activation ordering")
    if e["chandelier"]["atr_period"] != 14 or e["chandelier"]["atr_multiple"] <= 0:
        errors.append("ATR configuration")
    if (
        not 0 < a["max_increment_pct_of_position_value"] <= 1
        or not 0 < a["max_incremental_risk_fraction_of_equity"] < 1
    ):
        errors.append("add risk limits")
    for key in ("max_adds_per_position", "cooldown_hours"):
        if a[key] < 0:
            errors.append("add." + key)
    if type(a["max_adds_per_position"]) is not int:
        errors.append("trading.add_to_position.max_adds_per_position: integer required")
    for key in (
        "min_dollar_volume",
        "min_price",
        "max_vol_30d_pct_annualized",
        "min_bars",
    ):
        if d["gates"][key] <= 0:
            errors.append("discovery.gates." + key)
    if not 0 < e["partial_take"]["fraction"] <= 1:
        errors.append("partial exit fraction")
    if e["partial_take"]["at_pct"] < 0:
        errors.append("exits.partial_take.at_pct: nonnegative gain fraction required")
    if e["take_profit_pct"] <= 0:
        errors.append("exits.take_profit_pct: positive gain fraction required")
    if settings["execution"]["extended_hours_enabled"]:
        errors.append("extended-hours MCP contract is unverified")
    execution = settings["execution"]
    for key in ("quote_max_age_seconds", "reconciliation_max_age_seconds"):
        if execution[key] <= 0:
            errors.append("execution." + key + ": positive seconds required")
    if e["take_profit_mode"] != "graduate":
        errors.append("exits.take_profit_mode: only graduate is supported")
    if a["require_valid_existing_stop"] is not True:
        errors.append(
            "trading.add_to_position.require_valid_existing_stop must be true"
        )
    blend = d["gap_weights"]["momentum_blend"]
    if len(blend) != 2 or not all(_finite_number(weight) for weight in blend):
        errors.append(
            "discovery.gap_weights.momentum_blend: two finite weights required"
        )

    if "schedule" not in defaults:
        return errors

    if settings["trading"]["market_hours"]["calendar"] != "NYSE":
        errors.append("trading.market_hours.calendar: only NYSE is supported")
    if settings["news"]["provider"] != "finnhub":
        errors.append("news.provider: only finnhub is supported")
    if execution["min_dollar_volume"] <= 0:
        errors.append("execution.min_dollar_volume: positive dollar amount required")
    if execution["tradability_fail_mode"].lower() not in ("closed", "open"):
        errors.append("execution.tradability_fail_mode: expected closed or open")
    try:
        url = urlsplit(execution["mcp_url"])
        if url.scheme not in ("https", "http") or not url.hostname or url.username:
            raise ValueError
        _ = url.port
    except ValueError:
        errors.append("execution.mcp_url: valid HTTP(S) URL required")

    for section, key in (
        ("schedule", "timezone"),
        ("sentiment_selection", "schedule_timezone"),
    ):
        try:
            ZoneInfo(settings[section][key])
        except (ValueError, ZoneInfoNotFoundError):
            errors.append(f"{section}.{key}: IANA time zone required")

    schedule_groups = {
        "schedule": settings["schedule"],
        "reporting": settings["reporting"],
        "sentiment_selection": settings["sentiment_selection"],
        "discovery.schedule": d["schedule"],
    }
    for section, group in schedule_groups.items():
        for key, value in group.items():
            if key.endswith(("_hour", "_hour_et", "_minute")):
                limit = 59 if key.endswith("_minute") else 23
                if type(value) is not int or not 0 <= value <= limit:
                    errors.append(
                        f"{section}.{key}: integer between 0 and {limit} required"
                    )
    from apscheduler.triggers.cron import CronTrigger

    for day in (
        d["schedule"]["rank_day"],
        settings["sentiment_selection"]["schedule_day"],
    ):
        try:
            CronTrigger(day_of_week=day)
        except ValueError:
            errors.append("Invalid discovery or sentiment selection day-of-week")

    monitor = settings["monitor"]
    if type(monitor["interval_seconds"]) is not int or monitor["interval_seconds"] < 5:
        errors.append("monitor.interval_seconds: integer of at least 5 required")
    for key in ("profit_review_cooldown_min", "profit_target_cooldown_min"):
        if monitor[key] < 0:
            errors.append(f"monitor.{key}: nonnegative minutes required")
    if monitor["historicals_span"] not in (
        "day",
        "week",
        "month",
        "3month",
        "6month",
        "year",
    ):
        errors.append("monitor.historicals_span: unsupported span")
    for key, minimum, maximum in (
        ("stop_loss_bands", -1, 0),
        ("size_ladder", 0, 1),
        ("vol_sizing", 0, 1),
    ):
        rows = settings["trading"][key]
        if not rows or any(
            not isinstance(row, list)
            or len(row) != 2
            or not all(_finite_number(value) for value in row)
            or row[0] < 0
            or not minimum < row[1] <= maximum
            or (key == "stop_loss_bands" and row[1] == 0)
            for row in rows
        ):
            errors.append(f"trading.{key}: invalid threshold/value pairs")
    for section, group, key, minimum in (
        ("models", settings["models"], "max_tokens", 1),
        ("trading", settings["trading"], "max_positions", 1),
        ("trading", settings["trading"], "max_new_entries_per_day", 0),
        ("trading", settings["trading"], "earnings_window_discount_notches", 0),
        ("trading", settings["trading"], "max_holding_days", 1),
        ("news", settings["news"], "lookback_days", 1),
        ("news", settings["news"], "max_per_ticker", 1),
        ("news", settings["news"], "fresh_window_hours", 0),
        ("news.general_market", settings["news"]["general_market"], "count", 0),
        ("discovery", d, "min_cohort", 2),
        ("discovery.gates", d["gates"], "min_bars", 1),
        ("sentiment_selection", settings["sentiment_selection"], "lookback_days", 1),
        ("sentiment_selection", settings["sentiment_selection"], "universe_size", 1),
        ("sentiment_selection", settings["sentiment_selection"], "batch_size", 1),
    ):
        value = group[key]
        if type(value) is not int or value < minimum:
            errors.append(f"{section}.{key}: integer of at least {minimum} required")
    if settings["sentiment_selection"]["cache_max_age_hours"] < 0:
        errors.append(
            "sentiment_selection.cache_max_age_hours: nonnegative hours required"
        )
    for key in ("universe_max_age_days", "shortlist_max_age_days"):
        if d[key] <= 0:
            errors.append(f"discovery.{key}: positive days required")
    if not -1 <= settings["sentiment_selection"]["min_sentiment_score"] <= 1:
        errors.append(
            "sentiment_selection.min_sentiment_score: expected score in [-1, 1]"
        )
    if not 0 < settings["trading"]["max_position_pct"] <= 1:
        errors.append("trading.max_position_pct: expected fraction in (0, 1]")
    if not 0 <= settings["trading"]["sector_cap_pct"] <= 1:
        errors.append("trading.sector_cap_pct: expected fraction in [0, 1]")
    if not all(
        isinstance(ticker, str) and ticker.strip()
        for ticker in settings["watchlist"]["default_tickers"]
    ):
        errors.append("watchlist.default_tickers: expected nonempty ticker strings")
    return errors


def _finite_number(value):
    import math

    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False
