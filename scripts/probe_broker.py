"""Verify current empty-account state without renewal, writes or local reconciliation."""

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.reset_local_context import run

if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        report = asyncio.run(run(Path(__file__).resolve().parents[1]))
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        report = {"status": "BLOCKED", "error_type": type(error).__name__}
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["status"] == "VERIFIED_EMPTY" else 1)
