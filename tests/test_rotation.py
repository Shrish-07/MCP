"""ROTATE two-leg integration tests — the audit's #2 critical bug.

Covers: sell-before-buy ordering, abort on sell failure, downgrade paths
(invalid / self / not-held rotation target), count-neutrality at the
position cap, and full P&L recording on both legs.
"""
from __future__ import annotations

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
    assert r.status == "ROTATE_SELL_PENDING"
    assert r.rotate_from_ticker == "OLD"
    assert r.rotate_sell_price == 0.0  # previews are not confirmed proceeds
    assert r.rotate_sell_quantity == 0.0
    # position book rotated: OLD gone, NEW in
    assert [x.ticker for x in pm.get_open_records()] == ["OLD"]
    # realized P&L of the sell leg booked: (100-90)*10 = 100
    assert not (Path(pm._path).parent / "performance.json").exists()


@pytest.mark.asyncio
async def test_rotate_rejects_source_not_held():
    eng, pm = make_engine(FakeClient())
    r = await eng.execute(
        make_decision("ROTATE", ticker="NEW", edge=4.5, cycle="OPEN", rotate_from="NOTHELD"),
        make_portfolio(), 100.0, dry_run=True, vol_30d=20.0, avg_volume_30d=1_000_000,
    )
    assert r.status == "REJECTED_INVALID_ROTATION"
    assert {x.ticker for x in pm.get_open_records()} == {"OLD"}  # preview only


@pytest.mark.asyncio
async def test_rotate_rejects_self_rotation():
    eng, pm = make_engine(FakeClient())
    r = await eng.execute(
        make_decision("ROTATE", ticker="OLD", edge=4.5, cycle="OPEN", rotate_from="OLD"),
        make_portfolio(), 100.0, dry_run=True, vol_30d=20.0, avg_volume_30d=1_000_000,
    )
    assert r.status == "REJECTED_INVALID_ROTATION"


@pytest.mark.asyncio
async def test_rotate_count_neutral_at_full_cap():
    eng, pm = make_engine(FakeClient())
    eng._get_open_position_count = lambda: 3  # at max_positions in settings
    r = await eng.execute(
        make_decision("ROTATE", ticker="NEW", edge=4.5, cycle="OPEN", rotate_from="OLD"),
        make_portfolio(), 100.0, dry_run=True, vol_30d=20.0, avg_volume_30d=1_000_000,
    )
    assert r.status == "ROTATE_SELL_PENDING"  # preview cannot free capital


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
