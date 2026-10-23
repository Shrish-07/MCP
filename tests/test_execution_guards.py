"""Unit tests for ExecutionEngine risk gates — fully offline, no API calls.

Covers: edge bar + per-cycle bonus, vol-targeted sizing, fail-closed
tradability, duplicate/low-edge rejection, hold/ignore skips.
ROTATE two-leg tests live in test_rotation.py.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from core.execution.engine import ExecutionEngine
from core.position.manager import PositionManager
from models.decisions import DecisionType, StructuredEdgeScore, TradeDecision
from models.portfolio import Portfolio


def _settings(dry_run: bool = True, fail_mode: str = "closed") -> dict:
    return {
        "env": {"DRY_RUN": dry_run},
        "execution": {
            "dry_run": dry_run,
            "tradability_fail_mode": fail_mode,
            "min_dollar_volume": 75_000_000,
        },
        "trading": {
            "max_positions": 3,
            "max_position_pct": 0.08,
            "edge_score_threshold": 3.0,
            "cycle_edge_bonus": {"OPEN": 0.0, "MID": 0.25, "CLOSE": 0.5},
            "size_ladder": [[3.0, 0.03], [3.5, 0.04], [4.0, 0.06], [4.5, 0.08]],
            "vol_sizing": [[30, 0.8], [60, 0.6], [90, 0.45]],
            "extreme_vol_reject_30d": 120.0,
        },
    }


class FakeClient:
    """Offline stand-in for RobinhoodMCPClient."""

    def __init__(self, tradability: Any = None, tradability_err: Exception | None = None):
        self._tradability = tradability if tradability is not None else {"tradeable": True}
        self._tradability_err = tradability_err
        self.reviewed: list[dict] = []
        self.placed: list[dict] = []
        self.quotes_requested: list[list[str]] = []

    async def get_equity_tradability(self, ticker: str):
        if self._tradability_err is not None:
            raise self._tradability_err
        return self._tradability

    async def review_equity_order(self, ticker: str, side: str, quantity: float, order_type: str = "market"):
        self.reviewed.append({"ticker": ticker, "side": side, "quantity": quantity})
        return {"estimated_price": 100.0}

    async def place_equity_order(self, ticker, side, quantity, order_type="market", dry_run=True):
        from models.decisions import ExecutionResult
        self.placed.append({"ticker": ticker, "side": side, "quantity": quantity})
        return ExecutionResult(ticker=ticker, order_id="ord-1", status="EXECUTED",
                               fill_price=100.0, quantity=quantity, dry_run=False, side=side)

    async def get_equity_quotes(self, tickers: list[str]):
        from models.market_data import EquityQuote
        self.quotes_requested.append(list(tickers))
        return [EquityQuote(ticker=t, price=100.0, bid=99.9, ask=100.1, volume=1_000_000, prev_close=99.5)
                for t in tickers]


def make_engine(client: FakeClient, **overrides):
    """Shared harness used by this module and test_rotation.py."""
    settings = _settings(**{k: v for k, v in overrides.items() if k in ("dry_run", "fail_mode")})
    pm_path = Path(tempfile.mkdtemp())
    pm = PositionManager(path=str(pm_path / "positions.json"), config=settings)
    pm._path.write_text(json.dumps([
        {"ticker": "OLD", "entry_price": 90.0, "entry_date": "2026-07-01",
         "entry_edge_score": 0.0, "stop_loss_pct": -0.10, "take_profit_pct": 0.20,
         "quantity": 10.0, "order_id": "", "reconciled": False}
    ]))
    eng = ExecutionEngine(client=client, position_manager=pm, settings=settings)
    return eng, pm


def make_decision(kind: str, ticker: str = "NEW", edge: float = 4.0,
                  cycle: str = "OPEN", rotate_from: str | None = None) -> TradeDecision:
    e = StructuredEdgeScore(catalyst_strength=2.0, technical_confirmation=2.0, portfolio_fit=0.0)
    e.total = edge
    return TradeDecision(
        ticker=ticker, decision=DecisionType(kind), edge=e,
        rotate_from_ticker=rotate_from, cycle_type=cycle,
        reasoning_summary="test reasoning",
    )


def make_portfolio() -> Portfolio:
    return Portfolio(account_id="acc", total_value=10_000.0, buying_power=9_000.0, positions=[], cash_pct=90.0)

# --------------------------------------------------------------------- #
#  Edge bar
# --------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_low_edge_rejected():
    eng, pm = make_engine(FakeClient())
    r = await eng.execute(make_decision("BUY", edge=2.5), make_portfolio(), 100.0, dry_run=True,
                          vol_30d=20.0, avg_volume_30d=1_000_000)
    assert r.status == "REJECTED_LOW_EDGE"


@pytest.mark.asyncio
async def test_close_cycle_needs_higher_bar_but_is_allowed():
    eng, pm = make_engine(FakeClient())
    mk = make_portfolio
    kwargs = dict(dry_run=True, vol_30d=20.0, avg_volume_30d=1_000_000)
    # edge 3.2 passes OPEN (3.0) but not CLOSE (3.5) — CLOSE no longer forbids trades
    r_open = await eng.execute(make_decision("BUY", edge=3.2, cycle="OPEN"), mk(), 100.0, **kwargs)
    r_close = await eng.execute(make_decision("BUY", edge=3.2, cycle="CLOSE"), mk(), 100.0, **kwargs)
    r_close_hi = await eng.execute(make_decision("BUY", ticker="NEW2", edge=4.0, cycle="CLOSE"), mk(), 100.0, **kwargs)
    assert r_open.status == "WOULD_EXECUTE"
    assert r_close.status == "REJECTED_LOW_EDGE"
    assert r_close_hi.status == "WOULD_EXECUTE"


# --------------------------------------------------------------------- #
#  Vol-targeted sizing
# --------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_vol_scaling_shrinks_but_allows():
    eng, pm = make_engine(FakeClient())
    eng._get_open_position_count = lambda: 0
    r = await eng.execute(make_decision("BUY", edge=4.6), make_portfolio(), 100.0, dry_run=True,
                          vol_30d=70.0, avg_volume_30d=1_000_000)
    # edge 4.6 → ladder 8%; vol 70 → mult 0.6 → 4.8% of $10k = $480 → 4.8 shares
    assert r.status == "WOULD_EXECUTE"
    assert abs(r.quantity - 4.8) < 1e-6


@pytest.mark.asyncio
async def test_extreme_vol_rejected():
    eng, pm = make_engine(FakeClient())
    r = await eng.execute(make_decision("BUY", edge=4.6), make_portfolio(), 100.0, dry_run=True,
                          vol_30d=150.0, avg_volume_30d=1_000_000)
    assert r.status == "REJECTED_EXTREME_VOLATILITY"


# --------------------------------------------------------------------- #
#  Tradability fail-closed
# --------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_tradability_error_fails_closed():
    eng, pm = make_engine(FakeClient(tradability_err=RuntimeError("mcp down")))
    r = await eng.execute(make_decision("BUY"), make_portfolio(), 100.0, dry_run=True,
                          vol_30d=20.0, avg_volume_30d=1_000_000)
    assert r.status == "REJECTED_TRADABILITY_UNKNOWN"


@pytest.mark.asyncio
async def test_tradability_unparseable_fails_closed():
    eng, pm = make_engine(FakeClient(tradability={}))
    r = await eng.execute(make_decision("BUY"), make_portfolio(), 100.0, dry_run=True,
                          vol_30d=20.0, avg_volume_30d=1_000_000)
    assert r.status == "REJECTED_TRADABILITY_UNKNOWN"


@pytest.mark.asyncio
async def test_explicitly_untradeable_rejected():
    eng, pm = make_engine(FakeClient(tradability={"tradeable": False}))
    r = await eng.execute(make_decision("BUY"), make_portfolio(), 100.0, dry_run=True,
                          vol_30d=20.0, avg_volume_30d=1_000_000)
    assert r.status == "REJECTED_UNTRADEABLE"


# --------------------------------------------------------------------- #
#  Miscellaneous gates
# --------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_duplicate_buy_rejected():
    eng, pm = make_engine(FakeClient())
    r = await eng.execute(make_decision("BUY", ticker="OLD"), make_portfolio(), 100.0, dry_run=True,
                          vol_30d=20.0, avg_volume_30d=1_000_000)
    assert r.status == "REJECTED_DUPLICATE"


@pytest.mark.asyncio
async def test_position_cap_enforced():
    eng, pm = make_engine(FakeClient())
    eng._get_open_position_count = lambda: 3  # == max_positions in _settings
    r = await eng.execute(make_decision("BUY"), make_portfolio(), 100.0, dry_run=True,
                          vol_30d=20.0, avg_volume_30d=2_000_000)
    assert r.status == "REJECTED_MAX_POSITIONS"


@pytest.mark.asyncio
async def test_hold_and_ignore_are_skipped():
    eng, pm = make_engine(FakeClient())
    for kind in ("HOLD", "IGNORE"):
        r = await eng.execute(make_decision(kind), make_portfolio(), 100.0, dry_run=True)
        assert r.status == "SKIPPED"


@pytest.mark.asyncio
async def test_sell_without_position_rejected():
    eng, pm = make_engine(FakeClient())
    r = await eng.execute(make_decision("SELL", ticker="GHOST", edge=4.0), make_portfolio(), 100.0, dry_run=True)
    assert r.status == "REJECTED_NO_POSITION"


@pytest.mark.asyncio
async def test_sell_exits_position_with_pnl_dry_run():
    """Exits are world-agnostic: never blocked, P&L recorded, record closed."""
    eng, pm = make_engine(FakeClient())
    r = await eng.execute(make_decision("SELL", ticker="OLD", edge=5.0), make_portfolio(), 100.0, dry_run=True)
    assert r.status == "WOULD_EXECUTE"
    assert pm.get_open_records() == []  # position actually exited
    perf_path = Path(pm._path).parent / "performance.json"
    assert perf_path.exists()
    perf = json.loads(perf_path.read_text())
    assert round(perf["closed_trades"][-1]["realized_pnl"], 2) == 100.0  # (100-90)*10