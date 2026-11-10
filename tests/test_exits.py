from datetime import datetime, timezone, timedelta
import pytest
from core.exits.trail_manager import update_trail, wilder_atr
from core.exits.exit_state import graduate, finish_review
from models.market_data import EquityQuote, OHLCVBar
from models.portfolio import PositionRecord
from core.simulation import replay


def record():
    return PositionRecord(
        ticker="ABC",
        entry_price=100,
        quantity=10,
        peak_price=100,
        initial_stop_px=93,
        trail_stop_px=93,
        effective_stop_px=93,
        atr14=2,
        atr_date=datetime.now(timezone.utc).date().isoformat(),
    )


def quote(price, stamp=None):
    return EquityQuote(
        ticker="ABC",
        price=price,
        bid=price,
        ask=price,
        volume=1,
        prev_close=100,
        timestamp=stamp or datetime.now(timezone.utc).isoformat(),
    )


def test_profit_lock_chandelier_and_missing_atr_never_loosen():
    r = record()
    for px, expected in [(104, 93), (105, 100.5), (110, 104), (125, 119), (120, 119)]:
        update_trail(r, quote(px), {})
        assert r.effective_stop_px == pytest.approx(expected)
    r.atr14 = None
    update_trail(r, quote(121), {})
    assert r.effective_stop_px == 119
    r.atr14 = 10
    update_trail(r, quote(121), {})
    assert r.effective_stop_px == 119


def test_graduation_once_restart(pm):
    r = record()
    assert graduate(r, 120, {})
    assert not graduate(r, 125, {})
    ids = list(r.last_trigger_ids)
    finish_review(r, ids, "HOLD")
    assert r.trail_mode == "TRAILING_AFTER_GRADUATION"
    restored = PositionRecord.model_validate(r.model_dump())
    assert not graduate(restored, 130, {})


def test_graduation_failure_keeps_protection():
    r = record()
    graduate(r, 120, {})
    finish_review(r, r.last_trigger_ids, "HOLD", model_failed=True)
    assert r.take_profit_review_state == "DUE"
    assert r.effective_stop_px == 93


def test_bad_quote_does_not_mutate_peak():
    r = record()
    with pytest.raises(ValueError):
        update_trail(
            r,
            quote(120, (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()),
            {},
        )
    assert r.peak_price == 100


def test_wilder_atr_uses_true_range_and_smoothing():
    bars = [
        OHLCVBar(
            timestamp=f"2026-01-{i + 1:02d}",
            open=100,
            high=102,
            low=98,
            close=100,
            volume=1,
        )
        for i in range(16)
    ]
    assert wilder_atr(bars) == 4
    bars[-1].high = 116
    assert wilder_atr(bars) == pytest.approx((13 * 4 + 18) / 14)
    assert wilder_atr(bars[:14]) is None


def test_replay_partial_fills_gap_and_restart(tmp_path):
    now = datetime.now(timezone.utc)
    ticks = [
        ((now + timedelta(seconds=i * 15)).isoformat(), px)
        for i, px in enumerate([100, 106, 109, 98, 96, 95, 94])
    ]
    report, pm = replay(tmp_path / "paper.json", ticks, fill_capacity=3, restart_at=4)
    assert report["metrics"]["closed_positions"] == 1
    assert not pm.get_open_records()
    assert report["stop_path"] == sorted(report["stop_path"])
    assert (
        len([o for o in pm.snapshot()["orders"].values() if o["side"] == "sell"]) == 1
    )
    assert report["closed_positions"][0]["slippage_dollars"] > 0
    assert not (tmp_path / "positions.json").exists()


def test_split_adjusts_consistent_price_basis(pm):
    from test_p0_safety import seed

    seed(pm)
    pm.apply_split(
        "ABC",
        2,
        {
            "source": "test verified split",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
    r = pm.get_open_records()[0]
    assert r.quantity == 4 and r.entry_price == 50
    assert r.effective_stop_px == 46.5 and r.peak_price == 50


def test_graduation_sell_tracks_execution_until_fill(pm):
    from test_p0_safety import seed, decision, result
    seed(pm)
    record = pm.get_open_records()[0]
    graduate(record, 125, {})
    ids = [record.review_events["GRADUATION"]["id"]]
    finish_review(record, ids, "SELL")
    assert record.review_events["GRADUATION"]["state"] == "APPROVED"
    pm.update_record(record)
    sell = decision("SELL")
    sell.evidence["exit_review_ids"] = ids
    key = pm.create_order(sell, "sell", 2, 125)
    pm.update_order(key, result("SUBMITTED", qty=0, side="sell"))
    pm.update_order(key, result("PARTIALLY_FILLED", qty=1, side="sell"))
    assert pm.get_open_records()[0].review_events["GRADUATION"]["state"] == "PARTIALLY_FILLED"
    pm.update_order(key, result("REJECTED", qty=1, side="sell"))
    assert pm.get_open_records()[0].review_events["GRADUATION"]["state"] == "RETRY"
    assert pm.get_open_records()[0].quantity == 1


