"""Read-only broker contract probe. Interactive OAuth and order mutation disabled."""

import asyncio, json, sys
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.config import load_config
from utils.persistence import atomic_json
from robinhood_mcp.robinhood_client import RobinhoodMCPClient
from core.position.manager import PositionManager
from core.execution.engine import ExecutionEngine
from core.backend import Backend


async def main():
    root = Path(__file__).resolve().parents[1]
    config = load_config()
    client = RobinhoodMCPClient(url=config["execution"]["mcp_url"])

    async def no_interaction(*args, **kwargs):
        raise RuntimeError(
            "Interactive broker authentication disabled during read-only probe"
        )

    client._run_browser_auth_flow = no_interaction
    client._register_client = no_interaction
    original = client._call_tool_with_retry

    async def read_only(name, args):
        if not name.startswith("get_"):
            raise RuntimeError("Read-only probe cannot mutate the broker")
        return await original(name, args)

    client._call_tool_with_retry = read_only
    report = {"live_orders_enabled": False, "status": "AUTHENTICATION_REQUIRED"}
    if client._token_is_fresh(client._load_token_cache()):
        with TemporaryDirectory(prefix="broker-probe-", dir=root / "logs") as directory:
            pm = PositionManager(path=Path(directory) / "positions.json", config=config)
            agent = SimpleNamespace(
                config=config, mcp_client=client, position_manager=pm
            )
            agent.executor = ExecutionEngine(client, pm, config)
            backend = Backend(agent)
            try:
                ok = await asyncio.wait_for(backend.reconcile(), 120)
                report.update(
                    status="RECONCILED" if ok else "UNVERIFIED",
                    positions_reconciled=pm.reconciliation_ok,
                    orders_reconciled=pm.orders_reconciled,
                    confirmed_positions=len(pm.get_open_records()),
                    entry_gates_ready=pm.entries_ready,
                )
            except Exception as exc:
                report.update(status="FAILED", error_type=type(exc).__name__)
    atomic_json(root / "logs/broker_contract_probe.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
