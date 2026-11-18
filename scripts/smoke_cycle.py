"""One-shot smoke test: run a single full trading cycle against live APIs
(Robinhood MCP + Finnhub + Anthropic) in whatever mode settings.json says
and under the same instance lock as the scheduler.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Same bootstrap pattern as the other scripts: repo root first on sys.path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import main as m
from models.decisions import CycleType
from utils.instance_lock import InstanceLock
from utils.logger import configure_logging


async def _go() -> int:
    agent = m.TradingAgent()
    print(
        f"SMOKE: dry_run={agent.config['env']['DRY_RUN']} model={agent.config['models']['decision']}"
    )
    try:
        await asyncio.wait_for(agent.run_cycle(CycleType.OPEN), timeout=600)
    except TimeoutError:
        print("SMOKE: cycle TIMED OUT at 600s")
        return 2
    except Exception as exc:
        logging.getLogger(__name__).exception(
            "Check failed type=%s", type(exc).__name__, exc_info=False
        )
        print(f"SMOKE: cycle CRASHED: {type(exc).__name__}: {exc}")
        return 2
    print("SMOKE: cycle completed cleanly")
    # cheap sanity views (no prints of secrets)
    import json

    trades = _REPO_ROOT / "logs/trades.json"
    if trades.exists():
        data = json.loads(trades.read_text(encoding="utf-8"))
        newest = data[-6:] if len(data) >= 6 else data
        print("SMOKE: last trade log rows:")
        for row in newest:
            print(
                "   ",
                row.get("timestamp"),
                row.get("ticker"),
                row.get("decision"),
                row.get("status"),
                f"edge={row.get('edge_score', '')}",
            )
    return 0


if __name__ == "__main__":
    with InstanceLock(_REPO_ROOT / "logs/agent.lock"):
        configure_logging()
        sys.exit(asyncio.run(_go()))
