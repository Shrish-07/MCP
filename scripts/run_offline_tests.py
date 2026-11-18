"""Guarded test entry point with isolated cost accounting and loopback-only I/O."""

import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["FINNHUB_API_KEY"] = ""
scratch = root / ".test-tmp" / "offline-audit-runtime"
scratch.mkdir(parents=True, exist_ok=True)
os.environ["TEMP"] = os.environ["TMP"] = str(scratch)
os.environ["ROBINHOOD_TOKEN_CACHE"] = str(scratch / ".rh_oauth_cache.json")


def guard(event, args):
    if event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple) and address[0] not in (
            "127.0.0.1",
            "::1",
            "localhost",
        ):
            raise PermissionError(
                "External connections disabled for this offline audit"
            )
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).resolve()
        if (
            path.name in (".env", ".rh_oauth_cache.json")
            or path.name.startswith(".env.")
            or path.is_relative_to(root / "oauth")
        ):
            raise PermissionError("Secret files disabled for this offline audit")
        flags = args[2] or 0
        writing = flags & (
            os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        )
        if any(path.is_relative_to(root / base) for base in ("logs", "journals")):
            raise PermissionError("Runtime state disabled for this offline audit")
        if path.is_relative_to(root / "data") and (
            writing
            or path
            not in (
                root / "data/candidate_universe.json",
                root / "data/sp500_universe.json",
            )
        ):
            raise PermissionError(
                "Only public data fixtures are available in this offline audit"
            )
        if writing and path.is_relative_to(root / "config"):
            raise PermissionError("Config writes disabled for this offline audit")


sys.addaudithook(guard)
from utils.cost_tracker import CostTracker

costs = scratch / "costs"
costs.mkdir(exist_ok=True)
CostTracker._logs_dir = lambda self: costs
import pytest

raise SystemExit(
    pytest.main(
        [
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(scratch / "pytest"),
            *sys.argv[1:],
        ]
    )
)
