from datetime import datetime, timezone, timedelta
import copy
import pytest
from core.exits.triggers import (
    gap_progress,
    trend_break,
    climax,
    pead,
    nightly_signals,
    monitor_advisories,
    abnormal_curve,
)
from core.exits.exit_state import finish_review
from models.market_data import OHLCVBar
from test_exits import record
from utils.strategy_config import DEFAULTS


@pytest.mark.parametrize(
    "entry,current,progress,inverted",
    [
        (2, 1, 0.5, False),
        (2, -1, 1, True),
        (-2, -1, 0.5, False),
        (-2, 1, 1, True),
        (2, 3, 0, False),
        (0, 1, None, False),
        (None, 1, None, False),
    ],
)
def test_signed_gap_progress(entry, current, progress, inverted):
    p, i = gap_progress(entry, current)
    assert p == progress and i == inverted
    assert gap_progress(entry, current, comparable=False) == (None, False)


def bars(prices):
    return [
        OHLCVBar(
            timestamp=f"2026-10-{i + 1:02d}",
            open=p,
            high=p + 1,
            low=p - 1,
            close=p,
            volume=1e6,
        )
        for i, p in enumerate(prices)
    ]


def test_trend_requires_two_completed_closes_and_prior_profit():
    data = bars([100] * 20 + [90, 89])
    assert trend_break(data, True, DEFAULTS["exits"]["trend_break"])
    assert not trend_break(data, False, DEFAULTS["exits"]["trend_break"])
    assert not trend_break(
        bars([100] * 20 + [101, 89]), True, DEFAULTS["exits"]["trend_break"]
    )


@pytest.mark.parametrize(
    "rsi,late_spike,expected", [(79, True, False), (80, True, True), (90, False, False)]
)
def test_climax_and_rule(rsi, late_spike, expected):
    prices = ([100] * 20 + [110]) if late_spike else ([110] + [100] * 20)
    assert bool(climax(bars(prices), rsi, DEFAULTS["exits"]["climax"])) == expected


def test_pead_unavailable_small_negative_and_no_lookahead():
    cfg = DEFAULTS["exits"]["pead"]
    assert not pead(None, [], cfg)["available"]
    event = {
        "actual_eps": 1.1,
        "consensus_eps": 1.0,
        "release_timestamp": "2026-10-01T20:00:00+00:00",
        "source": "verified",
        "age_sessions": 5,
        "data_cutoff": "2026-10-09T20:00:00+00:00",
        "car_by_session": {str(i): 0.08 for i in (1, 3, 5, 10, 15)},
    }
    history = [
        {
            "event_id":f"historical-{j}",
            "available_at": "2026-09-01T00:00:00+00:00",
            "source": "verified",
            "surprise": 0.11,
            "car_by_session": {str(i): 0.1 for i in (1, 3, 5, 10, 15)},
        }
        for j in range(30)
    ]
    assert pead(event, history, cfg)["trigger"]
    assert not pead(event, history[:29], cfg)["available"]
    negative = copy.deepcopy(history)
    for h in negative:
        h["car_by_session"]["15"] = -0.1
    assert not pead(event, negative, cfg)["available"]
    for h in history:
        h["available_at"] = "2026-10-02T00:00:00+00:00"
    assert not pead(event, history, cfg)["available"]
    assert abnormal_curve([0.01] * 15, [0.0] * 15)["15"] == pytest.approx(1.01**15 - 1)


def test_drop_and_new_high_debounce_recovery_rearm():
    r = record()
    r.peak_price = 110
    r.atr14 = 2
    now = datetime.now(timezone.utc)
    settings = copy.deepcopy(DEFAULTS)
    monitor_advisories(r, 103, settings, now=now)
    first = len(r.last_trigger_ids)
    assert first >= 1
    finish_review(r, r.last_trigger_ids, "HOLD", now=now)
    monitor_advisories(r, 102, settings, now=now + timedelta(hours=25))
    assert (
        len(r.last_trigger_ids) == first
    )  # sustained drawdown is not a fresh crossing
    monitor_advisories(r, 110, settings, now=now + timedelta(hours=26), prior_high=109)
    monitor_advisories(r, 103, settings, now=now + timedelta(hours=27))
    assert len(r.last_trigger_ids) > first


def test_missing_baselines_disable_gap_and_sentiment_rules():
    r = record()
    r.entry_date_known = False
    signal, unavailable = nightly_signals(r, bars([100] * 22), {}, None, DEFAULTS)
    assert "DEAD_MONEY" not in signal and "SENTIMENT_REVERSAL" not in signal
    assert "gap_rules" in unavailable and "PEAD" in unavailable
