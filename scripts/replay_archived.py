"""Replay saved journal marks as sparse observations, never as a broker account."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.simulation import replay
from models.portfolio import PositionRecord
from utils.persistence import atomic_json

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--ticker", default="MU", type=PositionRecord.valid_ticker)
    p.add_argument("--output-dir", default="data/replays")
    a = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        rows = json.loads(
            (root / "journals" / f"{a.ticker}.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        p.error(
            "No valid saved journal is available. A clean reset intentionally removes old observations."
        )
    ticks = sorted(
        {
            (r["timestamp"], float(r.get("key_metrics", {}).get("current_price") or 0))
            for r in rows
        }
    )
    ticks = [t for t in ticks if t[1] > 0]
    if not ticks:
        p.error("No saved price observations are available for replay")
    out = root / a.output_dir
    out.mkdir(parents=True, exist_ok=True)
    # A new output folder is required to avoid merging replay lifecycles.
    state = out / f"{a.ticker}_paper.json"
    if state.exists():
        raise SystemExit("Replay state exists; choose a fresh --output-dir")
    report, pm = replay(
        state,
        ticks,
        entry_price=ticks[0][1],
        fill_capacity=3,
        restart_at=len(ticks) // 2,
    )
    report["limitations"] = (
        "Sparse archived journal observations, not continuous quotes, actual account holdings or a return forecast; ATR unavailable."
    )
    report["source"] = {
        "ticker": a.ticker,
        "observations": len(ticks),
        "first": ticks[0][0],
        "last": ticks[-1][0],
    }
    atomic_json(out / f"{a.ticker}_report.json", report)
    print(
        json.dumps(
            {
                "observations": len(ticks),
                "closed_positions": report["metrics"]["closed_positions"],
                "report": str(out / f"{a.ticker}_report.json"),
            }
        )
    )
