"""Generate the backend PEAD artifact from a point-in-time provider export."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.exits.pead_ingestion import build_pead_snapshot
from utils.persistence import atomic_json


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider_export", type=Path)
    args = parser.parse_args()
    snapshot = build_pead_snapshot(
        json.loads(args.provider_export.read_text(encoding="utf-8"))
    )
    if not snapshot["current"] and not snapshot["history"]:
        raise SystemExit("No usable observations. Existing PEAD artifact preserved.")
    atomic_json(ROOT / "data/pead_events.json", snapshot)
    print(
        f"Current symbols {len(snapshot['current'])}, mature events {len(snapshot['history'])}, rejected rows {snapshot['rejected_rows']}"
    )
