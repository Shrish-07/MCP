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


def validate(settings):
    import math

    errors = []

    def walk(expected, actual, prefix):
        if not isinstance(actual, dict):
            errors.append(prefix + ": object required")
            return
        for key, default in expected.items():
            value = actual.get(key)
            path = prefix + "." + key
            if isinstance(default, dict):
                walk(default, value, path)
            elif isinstance(default, bool) and not isinstance(value, bool):
                errors.append(path + ": boolean required")
            elif isinstance(default, (int, float)) and not isinstance(default, bool):
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                ):
                    errors.append(path + ": finite number required")

    walk(DEFAULTS, settings, "config")
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
    for key in (
        "min_dollar_volume",
        "min_price",
        "max_vol_30d_pct_annualized",
        "min_bars",
    ):
        if d["gates"][key] <= 0:
            errors.append("discovery.gates." + key)
    if e["partial_take"]["enabled"]:
        errors.append("partial exits are not released")
    if settings["execution"]["extended_hours_enabled"]:
        errors.append("extended-hours MCP contract is unverified")
    return errors
