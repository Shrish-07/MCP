"""N new names, never a holdings quota. Retain last good output on failure."""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from utils.persistence import atomic_json


def select_new(rows, holdings, cfg, history=()):
    eligible = [
        r
        for r in rows.values()
        if r.get("archetype")
        in ("A_LAGGED_GOOD_NEWS", "B_EARLY_INFLECTION", "C_CONFIRMED_DRIFT")
        and r["ticker"] not in set(holdings)
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


def publish(path, candidates, metadata):
    path = Path(path)
    previous = json.loads(path.read_text()) if path.exists() else {}
    payload = {
        "run_id": str(uuid4()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "SUCCESS",
        "new_candidates": candidates,
        "history": (previous.get("history", []) + [[c["ticker"] for c in candidates]])[
            -2:
        ],
        **metadata,
    }
    atomic_json(path, payload)
    atomic_json(
        path.with_suffix(".status.json"),
        {"status": "SUCCESS", "run_id": payload["run_id"]},
    )
    return payload


def mark_failed(path, error):
    atomic_json(
        Path(path).with_suffix(".status.json"),
        {
            "status": "FAILED",
            "error": str(error),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "previous_shortlist_retained": Path(path).exists(),
        },
    )


def load_shortlist(path, max_age_days=8):
    value = json.loads(Path(path).read_text())
    age = (
        datetime.now(timezone.utc) - datetime.fromisoformat(value["generated_at"])
    ).total_seconds() / 86400
    if not 0 <= age <= max_age_days or value["status"] != "SUCCESS":
        raise ValueError("stale/invalid shortlist")
    return value
