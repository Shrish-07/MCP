"""L1/L2 snapshot validation for rank-time reuse of completed sessions."""

import hashlib
import json
import math
from datetime import timedelta

from utils.freshness import fresh_timestamp
from utils.market_calendar import _ET, _NYSE

from .gates import feature_gate_reasons


def feature_revision(cfg):
    inputs = {
        "gates": cfg["gates"],
        "fingerprints": cfg["fingerprints"],
        "momentum_blend": cfg["gap_weights"]["momentum_blend"],
    }
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def latest_completed_session(now):
    if _NYSE is None:
        raise ValueError("NYSE calendar unavailable")
    end = now.astimezone(_ET).date()
    schedule = _NYSE.schedule(start_date=end - timedelta(days=14), end_date=end)
    completed = schedule[schedule["market_close"] <= now]
    if completed.empty:
        raise ValueError("no recent completed NYSE session")
    return completed.index[-1].date().isoformat()


def load_features(path, universe, cfg, now):
    """Reject unversioned, stale, cross-universe or incomplete saved features."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("universe_version") != universe["version"]
        or value.get("feature_revision") != feature_revision(cfg)
        or value.get("session") != latest_completed_session(now)
        or not fresh_timestamp(value.get("generated_at"), 5 * 86400, now)
    ):
        raise ValueError("stale or incompatible feature snapshot")
    rows = value.get("rows")
    excluded = value.get("excluded")
    if (
        not isinstance(rows, dict)
        or not isinstance(excluded, dict)
        or len(rows) < cfg["min_cohort"]
        or rows.keys() & excluded.keys()
        or (rows.keys() | excluded.keys())
        != {row["ticker"] for row in universe["constituents"]}
    ):
        raise ValueError("incomplete feature snapshot")
    for row in rows.values():
        if (
            not isinstance(row, dict)
            or str(row.get("data_cutoff", ""))[:10] != value["session"]
            or not isinstance(row.get("fingerprints"), dict)
            or not all(type(flag) is bool for flag in row["fingerprints"].values())
            or feature_gate_reasons(row, cfg["gates"])
            or not fresh_timestamp(row.get("quote_timestamp"), 5 * 86400, now)
            or not all(
                type(row.get(key)) in (int, float) and math.isfinite(row[key])
                for key in ("r5", "r21", "r63", "momentum_blend")
            )
            or (
                row.get("high_runway") is not None
                and (
                    type(row["high_runway"]) not in (int, float)
                    or not math.isfinite(row["high_runway"])
                )
            )
        ):
            raise ValueError("invalid feature row")
    return value
