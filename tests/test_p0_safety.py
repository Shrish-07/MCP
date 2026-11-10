import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from core.position.manager import PositionManager, StateIntegrityError
from core.execution.engine import ExecutionEngine
from models.decisions import (
    TradeDecision,
    DecisionType,
    ExecutionResult,
    StructuredEdgeScore,
)
from models.portfolio import Portfolio, Position


def portfolio(*positions):
    return Portfolio(
        total_value=10000,
        buying_power=9000,
        positions=list(positions),
        reconciliation_status="SUCCESS",
        positions_complete=True,
    )


def decision(side="BUY", ticker="ABC", **kwargs):
    return TradeDecision(
        ticker=ticker,
        evidence={"feature_timestamp":datetime.now(timezone.utc).isoformat(),"sector":"Technology"},
        decision=DecisionType(side),
        edge=StructuredEdgeScore(total=4.5),
        **kwargs,
    )


def result(
    status="FILLED", qty=2, price=100, side="buy", order_id="broker-1", **kwargs
):
    return ExecutionResult(
        ticker="ABC",
        order_id=order_id,
        status=status,
        quantity=2,
        filled_quantity=qty,
        fill_price=price,
        dry_run=False,
        side=side,
        **kwargs,
    )


@pytest.fixture
def pm(tmp_path):
    return PositionManager(str(tmp_path / "logs/positions.json"))


def seed(pm):
    pm.reconcile_with_mcp(portfolio(Position(ticker="ABC", quantity=2, avg_cost=100)))


def test_empty_success_clears_but_failure_preserves(pm):
    seed(pm)
    assert not pm.reconcile_with_mcp(Portfolio())
    assert len(pm.get_open_records()) == 1
    assert pm.reconcile_with_mcp(portfolio())
    assert pm.get_open_records() == []
    assert any(e["type"] == "BROKER_POSITION_REMOVED" for e in pm.snapshot()["events"])


def test_bad_row_preserves_good_and_blocks(pm):
    pm._path.write_text(
        json.dumps(
            [{"ticker": "ABC", "quantity": 1, "entry_price": 100}, {"ticker": "???"}]
        )
    )
    assert [r.ticker for r in pm.get_open_records()] == ["ABC"]
    assert pm.load_error
    assert not pm.entries_ready
    assert not pm.reconcile_with_mcp(portfolio())


def test_corrupt_state_is_not_overwritten(pm):
    pm._path.write_text("{broken")
    assert pm.get_open_records() == []
    assert pm.load_error
    with pytest.raises(StateIntegrityError):
        pm.reconcile_with_mcp(portfolio())
    assert pm._path.read_text() == "{broken"


def test_submitted_partial_duplicate_restart_and_final(pm):
    key = pm.create_order(decision(), "buy", 2, 100)
    pm.update_order(key, result("SUBMITTED", qty=0))
    assert pm.get_open_records() == []
    pm.update_order(key, result("PARTIALLY_FILLED", qty=0.5, price=98))
    pm.update_order(key, result("PARTIALLY_FILLED", qty=0.5, price=98))
    assert pm.get_open_records()[0].quantity == 0.5
    restarted = PositionManager(str(pm._path))
    assert not restarted.entries_ready
    restarted.update_order(key, result(qty=2, price=101))
    assert restarted.get_open_records()[0].quantity == 2
    assert restarted.get_open_records()[0].entry_price == pytest.approx(101)
    assert len(restarted.snapshot()["fills"]) == 2


@pytest.mark.parametrize("terminal", ["CANCELLED", "REJECTED", "EXPIRED"])
def test_partial_sell_terminal_keeps_remaining(pm, terminal):
    seed(pm)
    key = pm.create_order(decision("SELL"), "sell", 2, 90)
    pm.update_order(key, result("SUBMITTED", qty=0, side="sell"))
    assert pm.get_open_records()[0].quantity == 2
    pm.update_order(key, result("PARTIALLY_FILLED", qty=0.5, price=90, side="sell"))
    pm.update_order(key, result(terminal, qty=0.5, price=90, side="sell"))
    assert pm.get_open_records()[0].quantity == 1.5
    assert pm.get_open_records()[0].pending_exit_order_id is None
    assert sum(f.get("realized_pnl", 0) for f in pm.snapshot()["fills"].values()) == -5


def test_atomic_failure_retains_previous_file(pm, monkeypatch):
    original = pm._path.read_bytes()
    import utils.persistence as persistence

    monkeypatch.setattr(
        persistence.os,
        "replace",
        lambda *args: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    with pytest.raises(StateIntegrityError):
        pm.create_order(decision(), "buy", 2, 100)
    assert pm._path.read_bytes() == original
    assert pm.load_error


@pytest.mark.asyncio
async def test_live_requires_reconciliation_and_duplicate_is_persisted(pm):
    config = {"env": {"DRY_RUN": False}, "execution": {"live_release_approved": True}}
    client = AsyncMock()
    client.get_equity_tradability.return_value = {"tradeable": True}
    client.place_equity_order.return_value = result("SUBMITTED", qty=0)
    eng = ExecutionEngine(client, pm, config)
    kw = dict(
        dry_run=False,
        quote_timestamp=datetime.now(timezone.utc).isoformat(),
        avg_volume_30d=1e6,
    )
    r = await eng.execute(decision(), portfolio(), 100, **kw)
    assert r.status == "REJECTED_RECONCILIATION_REQUIRED"
    pm.reconcile_with_mcp(portfolio())
    pm.orders_reconciled = True
    r = await eng.execute(decision(), portfolio(), 100, **kw)
    assert r.status == "SUBMITTED"
    assert pm.get_open_records() == []
    r = await eng.execute(decision(), portfolio(), 100, **kw)
    assert r.status == "REJECTED_PENDING_ORDER"
    assert client.place_equity_order.await_count == 1


@pytest.mark.asyncio
async def test_low_edge_sell_and_paper_isolation(pm):
    seed(pm)
    eng = ExecutionEngine(AsyncMock(), pm, {"env": {"DRY_RUN": True}})
    d = decision("SELL")
    d.edge.total = 0
    before = pm._path.read_bytes()
    r = await eng.execute(d, portfolio(), 90, dry_run=True)
    assert r.status == "WOULD_EXECUTE"
    assert pm._path.read_bytes() == before
    assert eng.paper_manager.get_open_records() == []


@pytest.mark.asyncio
async def test_hard_stop_never_calls_model(pm):
    from main import TradingAgent
    from core.position.manager import ExitSignal

    seed(pm)
    agent = TradingAgent.__new__(TradingAgent)
    agent.decision_engine = AsyncMock()
    rec = pm.get_open_records()[0]
    d = await agent._evaluate_threshold_signal(
        ExitSignal("ABC", "STOP_LOSS", 80, -0.2, 2),
        rec,
        "MONITOR",
        None,
        [],
        portfolio(),
        "",
    )
    assert d.decision == DecisionType.SELL
    agent.decision_engine.client.complete.assert_not_called()
