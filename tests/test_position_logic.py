"""PositionManager lifecycle tests: config-driven bands, weighted-average
merge, reconciled-flag max-hold skip, shared lock registry."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.position.manager import PositionManager
from models.decisions import DecisionType, ExecutionResult, StructuredEdgeScore, TradeDecision
from models.market_data import EquityQuote
from models.portfolio import Portfolio, Position


def _pm(config_trading: dict | None = None) -> PositionManager:
    tmp = Path(tempfile.mkdtemp())
    return PositionManager(path=str(tmp / "positions.json"),
                           config={"trading": config_trading} if config_trading else {})


def _entry_result(ticker: str, price: float, qty: float) -> tuple[ExecutionResult, TradeDecision]:
    e = StructuredEdgeScore(); e.total = 4.0
    result = ExecutionResult(ticker=ticker, status="WOULD_EXECUTE", fill_price=price,
                             quantity=qty, dry_run=True, side="buy")
    decision = TradeDecision(ticker=ticker, decision=DecisionType.BUY, edge=e)
    return result, decision


def test_config_driven_stop_bands():
    pm = _pm({"stop_loss_bands": [[0, -0.05], [50, -0.11]]})
    assert pm._vol_adjusted_stop_loss(10.0) == -0.05
    assert pm._vol_adjusted_stop_loss(75.0) == -0.11


def test_default_stop_bands_without_config():
    pm = _pm(None)
    assert pm._vol_adjusted_stop_loss(10.0) == -0.07
    assert pm._vol_adjusted_stop_loss(45.0) == -0.10
    assert pm._vol_adjusted_stop_loss(70.0) == -0.13
    assert pm._vol_adjusted_stop_loss(95.0) == -0.16


def test_record_entry_merge_weighted_average():
    pm = _pm(None)
    r1, d1 = _entry_result("AAA", 100.0, 1.0)   # $100 of exposure
    pm.record_entry(r1, d1, realized_vol_30d=20.0)
    r2, d2 = _entry_result("AAA", 140.0, 1.0)
    pm.record_entry(r2, d2, realized_vol_30d=20.0)
    recs = pm.get_open_records()
    assert len(recs) == 1                                    # merged, not overwritten
    assert recs[0].quantity == 2.0
    assert abs(recs[0].entry_price - 120.0) < 1e-9           # weighted avg
    assert recs[0].entry_date != ""                           # original date kept
    assert recs[0].stop_loss_pct == -0.07                    # looser stop kept


def test_shared_lock_registry_is_the_same_object():
    pm = _pm(None)
    a = pm.lock_for("nvda")
    b = pm.lock_for("NVDA")
    assert a is b  # case-insensitive, shared between cycle + monitor


def test_check_exits_uses_config_and_skips_reconciled_max_hold():
    pm = _pm({"profit_review_pct": 0.10, "max_holding_days": 10})
    old_date = (datetime.now(timezone.utc) - timedelta(days=40)).date().isoformat()
    # reconciled holding, 40 days in reality — max-hold advisory must NOT fire
    pm._path.write_text(json.dumps([{
        "ticker": "RECON", "entry_price": 100.0, "entry_date": old_date,
        "entry_edge_score": 0.0, "stop_loss_pct": -0.07, "take_profit_pct": 0.20,
        "quantity": 1.0, "order_id": "", "reconciled": True,
    }, {
        "ticker": "OWNED", "entry_price": 100.0, "entry_date": old_date,
        "entry_edge_score": 0.0, "stop_loss_pct": -0.07, "take_profit_pct": 0.20,
        "quantity": 1.0, "order_id": "", "reconciled": False,
    }]))
    quotes = {
        "RECON": EquityQuote(ticker="RECON", price=105.0, bid=105, ask=105, volume=1, prev_close=100),
        "OWNED": EquityQuote(ticker="OWNED", price=105.0, bid=105, ask=105, volume=1, prev_close=100),
    }
    signals = pm.check_exits(Portfolio(positions=[]), quotes)
    reasons = {s.ticker: s.reason for s in signals}
    # OWNED: +5% < 10% review bar, 40d > 10d hold → MAX_HOLDING_PERIOD fires
    assert reasons.get("OWNED") == "MAX_HOLDING_PERIOD"
    # RECON: reconciled → no max-hold advisory
    assert "RECON" not in reasons


def test_review_gain_pct_comes_from_config():
    pm = _pm({"profit_review_pct": 0.05})
    rec_date = datetime.now(timezone.utc).date().isoformat()
    pm._path.write_text(json.dumps([{
        "ticker": "UP", "entry_price": 100.0, "entry_date": rec_date,
        "entry_edge_score": 0.0, "stop_loss_pct": -0.07, "take_profit_pct": 0.20,
        "quantity": 1.0, "order_id": "", "reconciled": False,
    }]))
    quotes = {"UP": EquityQuote(ticker="UP", price=106.0, bid=106, ask=106, volume=1, prev_close=100)}
    reasons = {s.ticker: s.reason for s in pm.check_exits(Portfolio(positions=[]), quotes)}
    assert reasons.get("UP") == "PROFIT_REVIEW"  # 6% ≥ 5% config bar (was hard 15%)
