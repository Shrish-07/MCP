import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.position.manager import PositionManager
from main import TradingAgent
from models.portfolio import Position
from test_backend_safety import build
from test_p0_safety import decision, portfolio, result


@pytest.mark.asyncio
async def test_moving_order_snapshot_does_not_apply_holdings_or_fills(
    tmp_path, monkeypatch
):
    backend, client, pm, _ = build(tmp_path, monkeypatch)
    key = pm.create_order(decision("ADD_TO_POSITION"), "buy", 2, 100)
    before = result("SUBMITTED", qty=0)
    pm.update_order(key, before)
    client.get_equity_orders.side_effect = [
        [before],
        [result("PARTIALLY_FILLED", qty=1)],
    ]
    client.get_portfolio.return_value = portfolio(
        Position(ticker="ABC", quantity=3, avg_cost=100)
    )
    assert not await backend.reconcile()
    assert pm.get_open_records()[0].quantity == 2
    assert not pm.snapshot()["fills"]
    assert not pm.entries_ready


def test_account_binding_survives_restart(tmp_path):
    path = tmp_path / "positions.json"
    pm = PositionManager(path)
    first = portfolio(Position(ticker="ABC", quantity=2, avg_cost=100))
    first.account_id = "account-a"
    assert pm.reconcile_with_mcp(first)
    restarted = PositionManager(path)
    other = portfolio()
    other.account_id = "account-b"
    assert not restarted.reconcile_with_mcp(other)
    assert restarted.get_open_records()[0].quantity == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_ambiguous_submission_is_reported_and_persists(
    tmp_path, monkeypatch, side
):
    backend, client, pm, _ = build(tmp_path, monkeypatch)
    client.place_equity_order.side_effect = TimeoutError("fixture timeout")
    out = await backend.agent.executor._submit(
        decision(side.upper()), side, 2, 100, False
    )
    assert out.status == "UNKNOWN_REQUIRES_RECONCILIATION"
    restarted = PositionManager(pm._path)
    assert restarted.pending("ABC")[0]["state"] == out.status
    assert restarted.get_open_records()[0].quantity == 2
    assert client.place_equity_order.await_count == 1


@pytest.mark.asyncio
async def test_decision_cycles_do_not_overlap():
    entered, finish = asyncio.Event(), asyncio.Event()

    async def slow_cycle(_):
        entered.set()
        await finish.wait()

    agent = SimpleNamespace(logger=Mock(), _run_cycle=AsyncMock(side_effect=slow_cycle))
    running = asyncio.create_task(TradingAgent.run_cycle(agent, "OPEN"))
    await entered.wait()
    await TradingAgent.run_cycle(agent, "MID")
    finish.set()
    await running
    agent._run_cycle.assert_awaited_once_with("OPEN")
