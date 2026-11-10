import json, math, statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest
from core.discovery.divergence import standardize, score_cohort, score_fixed_reference
from core.discovery.shortlist import select_new, publish, mark_failed, load_shortlist
from core.discovery.gates import eligibility
from core.discovery.news_features import deduplicate, NewsHistory
from core.discovery.universe import load_universe
from core.discovery.features import technical_features
from core.exits.trail_manager import completed_bars
from utils.strategy_config import DEFAULTS
from models.news import NewsItem
from models.market_data import EquityQuote, OHLCVBar


def cohort():
    return {
        f"T{i}": {
            "sentiment_mean": i / 25,
            "news_velocity": i * i / 20 + 1,
            "sentiment_delta": math.sin(i),
            "momentum_blend": math.cos(i),
            "r5": math.sin(i / 3),
            "fingerprints": {"F1": True, "F2_BASING": False},
            "high_runway": 0.1,
            "data_quality": "valid",
        }
        for i in range(30)
    }


def test_gap_is_separately_standardized_and_reproducible():
    a = score_cohort(cohort(), DEFAULTS["discovery"], "cohort-1")
    b = score_cohort(
        dict(reversed(list(cohort().items()))), DEFAULTS["discovery"], "cohort-1"
    )
    assert a == b
    assert statistics.mean([r["gap_z"] for r in a.values()]) == pytest.approx(
        0, abs=1e-12
    )
    assert statistics.pstdev([r["gap_z"] for r in a.values()]) == pytest.approx(1)
    for r in a.values():
        assert score_fixed_reference(r, r, DEFAULTS["discovery"]) == pytest.approx(
            r["gap_z"]
        )


@pytest.mark.parametrize("values", [[1] * 20, [1, 2, 3], [1] * 19 + [float("nan")]])
def test_invalid_standardization_unavailable(values):
    with pytest.raises(ValueError):
        standardize(values)


@pytest.mark.parametrize("n", [1, 10, 12])
def test_n_new_names_plus_holdings_without_consuming_slots(n):
    rows = {
        f"T{i}": {"ticker": f"T{i}", "gap_z": i / 10, "archetype": "A_LAGGED_GOOD_NEWS"}
        for i in range(25)
    }
    cfg = dict(DEFAULTS["discovery"]["shortlist"], new_candidate_count=n)
    selected = select_new(rows, ["T24", "T23"], cfg, [["T1", "T0"]])
    assert len(selected) == n and not {"T23", "T24"} & {r["ticker"] for r in selected}
    assert len(select_new({"T1": rows["T1"]}, [], cfg)) == 1


def test_failed_ranking_preserves_previous_shortlist(tmp_path):
    path = tmp_path / "shortlist.json"
    publish(path, [], {"data_cutoff": "test"})
    before = path.read_bytes()
    mark_failed(path, "model unavailable")
    assert path.read_bytes() == before and load_shortlist(path)["new_candidates"] == []
    assert (
        json.loads(path.with_suffix(".status.json").read_text())["status"] == "FAILED"
    )


def test_universe_source_hash_and_staleness():
    root = Path(__file__).resolve().parents[1]
    source=json.loads((root/"data/sp500_universe.json").read_text())
    v = load_universe(root / "data/sp500_universe.json",now=datetime.fromisoformat(source["as_of"]))
    assert 480 <= len(v["constituents"]) <= 520 and v["version"] and v["source"]
    with pytest.raises(ValueError):
        load_universe(
            root / "data/sp500_universe.json",
            now=datetime.fromisoformat(v["as_of"]) + timedelta(days=15),
        )


def test_liquidity_price_volatility_boundaries():
    q = EquityQuote(
        ticker="ABC",
        price=5,
        bid=5,
        ask=5,
        volume=1,
        prev_close=5,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    f = {"price": 5, "dollar_volume30": 75e6, "vol30_annual_pct": 120, "bar_count": 250}
    cfg = DEFAULTS["discovery"]["gates"]
    assert not eligibility(f, q, cfg)
    for key, value in [
        ("price", 4.999),
        ("dollar_volume30", 75e6 - 1),
        ("vol30_annual_pct", 120.001),
        ("bar_count", 249),
    ]:
        assert eligibility(dict(f, **{key: value}), q, cfg)


def test_technical_features_completed_sessions_and_missing_history():
    from utils.market_calendar import _NYSE

    schedule = _NYSE.schedule(start_date="2025-08-01", end_date="2026-11-06")
    bars = []
    for i, date in enumerate(schedule.index):
        px = 100 + i * 0.1 + math.sin(i / 5)
        bars.append(
            OHLCVBar(
                timestamp=date.date().isoformat(),
                open=px,
                high=px + 1,
                low=px - 1,
                close=px,
                volume=1e6,
            )
        )
    now = datetime(2026, 11, 7, tzinfo=timezone.utc)
    valid = completed_bars(bars, now)
    f = technical_features(valid, DEFAULTS["discovery"])
    assert (
        f["atr14"] > 0
        and f["high_252"] > f["price"]
        and set(f["fingerprints"])
        == {"F1_PULLBACK_UPTREND", "F2_BASING", "F3_DIP_EXHAUSTION"}
    )
    with pytest.raises(ValueError):
        completed_bars(bars[:-1], now)
    with pytest.raises(ValueError):
        completed_bars(bars[:50] + bars[51:], now)
    with pytest.raises(ValueError):
        technical_features(bars[:249], DEFAULTS["discovery"])


def story(i, days, headline=None):
    now = datetime.now(timezone.utc)
    return NewsItem(
        id=i,
        headline=headline or f"ABC measured event {i}",
        source="source",
        related="ABC",
        datetime=int((now - timedelta(days=days)).timestamp()),
        url=f"https://source.test/{i}?utm_source=x",
    )


def test_syndication_dedup_and_news_baseline(tmp_path):
    a = story(1, 1, "ABC wins contract")
    b = a.model_copy(update={"id": 2, "source": "other", "url": "https://other.test/1"})
    assert len(deduplicate([a, b])[0]) == 1
    items = [story(10, 1), story(11, 9), story(12, 20), story(13, 35)]
    h = NewsHistory(tmp_path / "news.json")
    unique, _ = deduplicate(items)
    scores = [0.2] * len(unique)
    f = h.update(
        "ABC",
        items,
        scores,
        {"model_name": "test-primary"},
        coverage_start=(datetime.now(timezone.utc) - timedelta(days=45)).isoformat(),
    )
    assert (
        f["sentiment_mean"] == 0.2
        and f["baseline_days"] == 30
        and f["baseline_headlines"] == 3
    )
    before = h.path.read_bytes()
    with pytest.raises(ValueError):
        h.update("ABC", items, [], {"model_name": "test-primary"})
    assert h.path.read_bytes() == before


def test_sparse_or_new_ticker_has_no_neutral_baseline(tmp_path):
    h = NewsHistory(tmp_path / "news.json")
    with pytest.raises(ValueError):
        h.update(
            "ABC",
            [],
            [],
            {"model_name": "test"},
            coverage_start=datetime.now(timezone.utc).isoformat(),
        )
