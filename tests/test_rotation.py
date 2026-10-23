"""ROTATE two-leg integration tests — the audit's #2 critical bug.

Covers: sell-before-buy ordering, abort on sell failure, downgrade paths
(invalid / self / not-held rotation target), count-neutrality at the
position cap, and full P&L recording on both legs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_execution_guards import FakeClient, make_decision, make_engine, make_portfolio


@pytest.mark.asyncio
async def test_rotate_sells_first_then_buys_and_records():
    eng, pm = make_engine(FakeClient())
    r = await eng.execute(
        make_decision("ROTATE", ticker="NEW", edge=4.5, cycle="OPEN", rotate_from="OLD"),
        make_portfolio(), 100.0, dry_run=True, vol_30d=20.0, avg_volume_30d=1_000_000,
    )
    assert r.status == "WOULD_EXECUTE"
    assert r.rotate_from_ticker == "OLD"
    assert r.rotate_sell_price == 100.0
    assert r.rotate_sell_quantity == 10.0  # full old position sold
    # position book rotated: OLD gone, NEW in
    assert [x.ticker for x in pm.get_open_records()] == ["NEW"]
    # realized P&L of the sell leg booked: (100-90)*10 = 100
    perf = json.loads((Path(pm._path).parent / "performance.json").read_text())
    assert round(perf["closed_trades"][-1]["realized_pnl"], 2) == 100.0
    assert round(r.rotate_realized_pnl, 2) == 100.0


@pytest.mark.asyncio
async def test_rotate_downgrades_to_buy_when_target_not_held():
    eng, pm = make_engine(FakeClient())
    r = await eng.execute(
        make_decision("ROTATE", ticker="NEW", edge=4.5, cycle="OPEN", rotate_from="NOTHELD"),
        make_portfolio(), 100.0, dry_run=True, vol_30d=20.0, avg_volume_30d=1_000_000,
    )
    assert r.status == "WOULD_EXECUTE"
    assert {x.ticker for x in pm.get_open_records()} == {"OLD", "NEW"}  # nothing sold


@pytest.mark.asyncio
async def test_rotate_self_target_downgrades_to_duplicate_buy():
    eng, pm = make_engine(FakeClient())
    r = await eng.execute(
        make_decision("ROTATE", ticker="OLD", edge=4.5, cycle="OPEN", rotate_from="OLD"),
        make_portfolio(), 100.0, dry_run=True, vol_30d=20.0, avg_volume_30d=1_000_000,
    )
    # self-rotation degrades to BUY of a held name → duplicate guard refuses
    assert r.status == "REJECTED_DUPLICATE"


@pytest.mark.asyncio
async def test_rotate_count_neutral_at_full_cap():
    eng, pm = make_engine(FakeClient())
    eng._get_open_position_count = lambda: 3  # at max_positions in settings
    r = await eng.execute(
        make_decision("ROTATE", ticker="NEW", edge=4.5, cycle="OPEN", rotate_from="OLD"),
        make_portfolio(), 100.0, dry_run=True, vol_30d=20.0, avg_volume_30d=1_000_000,
    )
    assert r.status == "WOULD_EXECUTE"  # rotation survives a full book


@pytest.mark.asyncio
async def test_rotate_aborts_buy_when_sell_fails():
    class FailingSellClient(FakeClient):
        async def review_equity_order(self, ticker, side, quantity, order_type="market"):
            if side == "sell":
                raise RuntimeError("broker rejected the sell")
            return {"estimated_price": 100.0}

    eng, pm = make_engine(FailingSellClient())
    r = await eng.execute(
        make_decision("ROTATE", ticker="NEW", edge=4.5, cycle="OPEN", rotate_from="OLD"),
        make_portfolio(), 100.0, dry_run=True, vol_30d=20.0, avg_volume_30d=1_000_000,
    )
    # Sell leg raised → outer execute() catches → REJECTED_EXCEPTION, and
    # critically: NOTHING was bought on the back of a failed sell, and OLD
    # is still in the book.
    assert r.status == "REJECTED_EXCEPTION"
    assert [x.ticker for x in pm.get_open_records()] == ["OLD"]


@pytest.mark.asyncio
async def test_rotate_refuses_blind_sell_without_price():
    class NoQuoteClient(FakeClient):
        async def get_equity_quotes(self, tickers):
            return []

    eng, pm = make_engine(NoQuoteClient())
    r = await eng.execute(
        make_decision("ROTATE", ticker="NEW", edge=4.5, cycle="OPEN", rotate_from="OLD"),
        make_portfolio(), 100.0, dry_run=True, vol_30d=20.0, avg_volume_30d=1_000_000,
    )
    assert r.status == "REJECTED_ROTATE_NO_PRICE"
    assert [x.ticker for x in pm.get_open_records()] == ["OLD"]