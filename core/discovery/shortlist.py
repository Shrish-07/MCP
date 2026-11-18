"""N new names, never a holdings quota. Retain last good output on failure."""

import json
import logging
import math
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from models.portfolio import PositionRecord
from utils.persistence import atomic_json

logger = logging.getLogger(__name__)


def select_new(rows, holdings, cfg, history=()):
    held = set(holdings)
    eligible = [
        r
        for r in rows.values()
        if r.get("archetype")
        in ("A_LAGGED_GOOD_NEWS", "B_EARLY_INFLECTION", "C_CONFIRMED_DRIFT")
        and r["ticker"] not in held
    ]
    memory = {t for run in list(history)[-cfg["candidate_memory_runs"] :] for t in run}
    key = lambda r: (-r["gap_z"], -(r["ticker"] in memory), r["ticker"])
    ab = sorted(
        [r for r in eligible if r["archetype"].startswith(("A_", "B_"))], key=key
    )[: cfg["divergence_pool_cap"]]
    c = sorted([r for r in eligible if r["archetype"].startswith("C_")], key=key)[
        : cfg["drift_pool_cap"]
    ]
    return [
        dict(
            r,
            selection_reason="eligible pool; gap_z ranking; memory tie break",
            candidate_memory=r["ticker"] in memory,
        )
        for r in sorted(ab + c, key=key)[: cfg["new_candidate_count"]]
    ]


def prior_history(path):
    try:
        previous = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    history = previous.get("history", []) if isinstance(previous, dict) else []
    if not isinstance(history, list):
        return []
    return [
        run
        for run in history
        if isinstance(run, list) and all(isinstance(ticker, str) for ticker in run)
    ]


def publish(path, candidates, metadata, *, candidate_memory_runs=2):
    path = Path(path)
    payload = {
        **metadata,
        "run_id": str(uuid4()),
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "SUCCESS",
        "new_candidates": candidates,
        "history": (prior_history(path) + [[c["ticker"] for c in candidates]])[
            -candidate_memory_runs:
        ],
    }
    _validate_shortlist(payload, max_age_days=1)
    atomic_json(path, payload)
    try:
        atomic_json(
            path.with_suffix(".status.json"),
            {"status": "SUCCESS", "run_id": payload["run_id"]},
        )
    except OSError:
        logger.warning("discovery_status_write_failed")
    return payload


def mark_failed(path, error):
    atomic_json(
        Path(path).with_suffix(".status.json"),
        {
            "status": "FAILED",
            "error": str(error),
            "timestamp": datetime.now(UTC).isoformat(),
            "previous_shortlist_retained": Path(path).exists(),
        },
    )


def load_shortlist(path, max_age_days=8):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate_shortlist(value, max_age_days)
    return value


def _validate_shortlist(value, max_age_days):
    if (
        not isinstance(value, dict)
        or value.get("status") != "SUCCESS"
        or not isinstance(value.get("run_id"), str)
        or not value["run_id"].strip()
    ):
        raise ValueError("invalid shortlist")
    try:
        age = (
            datetime.now(UTC) - datetime.fromisoformat(value["generated_at"])
        ).total_seconds() / 86400
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid shortlist timestamp") from exc
    candidates = value.get("new_candidates")
    ranked = value.get("all_ranked", {})
    if (
        not 0 <= age <= max_age_days
        or not isinstance(candidates, list)
        or not isinstance(ranked, dict)
    ):
        raise ValueError("stale/invalid shortlist")
    for ticker, row in ranked.items():
        if (
            not isinstance(row, dict)
            or ticker != PositionRecord.valid_ticker(ticker)
            or row.get("ticker") != ticker
            or not isinstance(row.get("fingerprints"), dict)
            or not all(type(flag) is bool for flag in row["fingerprints"].values())
            or not all(
                type(row.get(key)) in (int, float) and math.isfinite(row[key])
                for key in (
                    "gap_z",
                    "sentiment_mean",
                    "news_velocity",
                    "sentiment_delta",
                    "momentum_blend",
                    "r5",
                )
            )
        ):
            raise ValueError("invalid ranked shortlist row")
    symbols = set()
    for row in candidates:
        ticker = row.get("ticker") if isinstance(row, dict) else None
        if (
            not isinstance(ticker, str)
            or ticker in symbols
            or ticker not in ranked
            or any(row.get(key) != value for key, value in ranked[ticker].items())
        ):
            raise ValueError("shortlist candidate does not match the ranked cohort")
        symbols.add(ticker)
