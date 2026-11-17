"""Read-only local process and trading-backend health check."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.health import inspect_health

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "logs/health.json",
    )
    args = parser.parse_args()
    report = inspect_health(args.path)
    print(json.dumps(report))
    raise SystemExit(0 if report["healthy"] else 1)
