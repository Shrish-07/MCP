from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime, timezone, timedelta
import pytest

from core.backend import Backend
from core.execution.engine import ExecutionEngine
from core.position.manager import PositionManager
from models.market_data import EquityQuote
from models.decisions import ExecutionResult
from test_p0_safety import portfolio, seed, decision, result


def build(tmp_path, monkeypatch):
    monkeypatch.setattr("core.backend.is_market_open_at", lambda: True)
    config = {"env": {"DRY_RUN": False}, "execution": {"live_release_approved": True}}
    pm = PositionManager(str(tmp_path / "logs/positions.json"), config)
    seed(pm)
    client = AsyncMock()
    client.get_portfolio.return_value = portfolio(
        *[
            __import__("models.portfolio", fromlist=["Position"]).Position(
                ticker="ABC", quantity=2, avg_cost=100
            )
        ]
    )
    client.get_equity_orders.return_value = []
    client.place_equity_order.return_value = result("SUBMITTED", qty=0, side="sell")
    q = EquityQuote(
        ticker="ABC",
        price=80,
        bid=80,
        ask=81,
        volume=1,
        prev_close=100,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    client.get_equity_quotes.return_value = [q]
    agent = SimpleNamespace(
        position_manager=pm,
        mcp_client=client,
        config=config,
        decision_engine=AsyncMock(),
    )
    agent.executor = ExecutionEngine(client, pm, config)
    return Backend(agent), client, pm, q


@pytest.mark.asyncio
async def test_monitor_submission_no_model_and_no_duplicate(tmp_path, monkeypatch):
    b, c, pm, q = build(tmp_path, monkeypatch)
    await b.monitor()
    await b.monitor()
    c.place_equity_order.assert_awaited_once()
    b.agent.decision_engine.assert_not_called()
    assert pm.get_open_records()[0].quantity == 2
    assert pm.get_open_records()[0].exit_state == "EXIT_PENDING"


@pytest.mark.asyncio
async def test_pending_add_does_not_suspend_stop(tmp_path, monkeypatch):
    b, c, pm, quote = build(tmp_path, monkeypatch)
    add = pm.create_order(decision("ADD_TO_POSITION"), "buy", 2, 100)
    update = result("SUBMITTED", qty=0, order_id="buy-in-flight")
    pm.update_order(add, update)
    c.get_equity_orders.return_value = [update]
    await b.monitor()
    assert c.place_equity_order.await_args.kwargs["side"] == "sell"
    assert c.place_equity_order.await_args.kwargs["quantity"] == 2
    assert pm.get_open_records()[0].thesis_invalidated


@pytest.mark.asyncio
async def test_stale_quote_or_broker_down_does_not_submit(tmp_path, monkeypatch):
    b, c, pm, q = build(tmp_path, monkeypatch)
    q.timestamp = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    await b.monitor()
    c.place_equity_order.assert_not_called()
    assert b.health.state["quotes"]["error"]
    c.get_portfolio.side_effect = TimeoutError("broker down")
    assert not await b.reconcile()
    assert len(pm.get_open_records()) == 1


@pytest.mark.asyncio
async def test_rotation_partial_sell_never_buys_and_recovers(tmp_path, monkeypatch):
    from models.portfolio import Position

    b, c, pm, q = build(tmp_path, monkeypatch)
    await b.reconcile()
    c.get_equity_tradability.return_value = {"tradeable": True}
    c.place_equity_order.return_value = result(
        "PARTIALLY_FILLED", qty=0.5, price=80, side="sell"
    )
    d = decision("ROTATE", ticker="NEW", rotate_from_ticker="ABC")
    r = await b.agent.executor.execute(
        d,
        portfolio(Position(ticker="ABC", quantity=2, avg_cost=100,current_price=100,market_value=200,sector="Technology")),
        100,
        False,
        avg_volume_30d=1e6,
        quote_timestamp=q.timestamp,
    )
    assert r.status == "ROTATE_SELL_PENDING"
    c.place_equity_order.assert_awaited_once()
    assert pm.get_open_records()[0].quantity == 1.5
    assert pm.snapshot()["rotations"]
    restarted = PositionManager(str(pm._path))
    assert restarted.pending("ABC")


@pytest.mark.asyncio
async def test_concurrent_duplicate_buys_submit_once(tmp_path):
    import asyncio
    from test_p0_safety import portfolio

    pm = PositionManager(str(tmp_path / "positions.json"))
    pm.reconcile_with_mcp(portfolio())
    pm.orders_reconciled = True
    c = AsyncMock()
    c.get_equity_tradability.return_value = {"tradeable": True}
    c.place_equity_order.return_value = result("SUBMITTED", qty=0)
    e = ExecutionEngine(
        c, pm, {"env": {"DRY_RUN": False}, "execution": {"live_release_approved": True}}
    )
    kwargs = dict(
        dry_run=False,
        avg_volume_30d=1e6,
        quote_timestamp=datetime.now(timezone.utc).isoformat(),
    )
    await asyncio.gather(
        e.execute(decision(), portfolio(), 100, **kwargs),
        e.execute(decision(), portfolio(), 100, **kwargs),
    )
    c.place_equity_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_hung_specialized_model_does_not_block_hard_stop(tmp_path,monkeypatch):
    import asyncio
    from models.market_data import MarketMetrics
    b,c,pm,q=build(tmp_path,monkeypatch)
    b.agent._monitor_refresh_data=AsyncMock(return_value=(MarketMetrics(ticker="ABC"),[]))
    started=asyncio.Event();release=asyncio.Event()
    async def hung(*args,**kwargs):
        started.set();await release.wait()
    b.agent.decision_engine.review_exit.side_effect=hung
    q.price=q.bid=120
    await b.monitor()
    await asyncio.wait_for(started.wait(),2)
    q.price=q.bid=80
    await b.monitor()
    c.place_equity_order.assert_awaited_once()
    b.review_task.cancel()
    with pytest.raises(asyncio.CancelledError):await b.review_task


@pytest.mark.parametrize("second_leg_fails",[False,True])
@pytest.mark.asyncio
async def test_rotation_fill_confirmation_and_second_leg_failure(tmp_path,monkeypatch,second_leg_fails):
    from models.portfolio import Position
    from models.market_data import EquityQuote
    b,c,pm,q=build(tmp_path,monkeypatch)
    await b.reconcile()
    c.get_equity_tradability.return_value={"tradeable":True}
    async def quotes(tickers):
        return [EquityQuote(ticker=t,price=100,bid=100,ask=100,volume=1e6,prev_close=100,timestamp=q.timestamp) for t in tickers]
    async def place(ticker,side,quantity,**kw):
        failed=second_leg_fails and side=="buy"
        return ExecutionResult(ticker=ticker,side=side,order_id=side+"-order",status="REJECTED" if failed else "FILLED",
            quantity=quantity,filled_quantity=0 if failed else quantity,fill_price=100,dry_run=False)
    c.get_equity_quotes.side_effect=quotes
    c.place_equity_order.side_effect=place
    c.get_portfolio.return_value=portfolio()
    before=portfolio(Position(ticker="ABC",quantity=2,avg_cost=100,current_price=100,market_value=200,sector="Technology"))
    out=await b.agent.executor.execute(decision("ROTATE",ticker="NEW",rotate_from_ticker="ABC"),before,100,False,
        avg_volume_30d=1e6,quote_timestamp=q.timestamp)
    assert out.status==("REJECTED" if second_leg_fails else "FILLED")
    assert [r.ticker for r in pm.get_open_records()]==([] if second_leg_fails else ["NEW"])
    assert c.place_equity_order.await_count==2
