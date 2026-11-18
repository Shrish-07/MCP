from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.data.daily import completed_bars
from core.data.metrics import MetricsEngine
from core.data.news import NewsFetcher
from core.discovery.news_features import NewsHistory
from core.exits.exit_evidence import due_reviews
from core.exits.trail_manager import executable_mark
from models.market_data import OHLCVBar


@pytest.mark.asyncio
async def test_news_retry_ordering_intraday_and_general_failure(tmp_path, monkeypatch):
    from test_discovery import story

    monkeypatch.setattr("core.data.news.os.getenv", lambda *_: "")
    fetcher = NewsFetcher(settings={"max_per_ticker": 1})
    fetcher._api_key = "fixture-only"
    fetcher._seen_path = tmp_path / "seen.json"
    fetcher._rate_limiter = SimpleNamespace(acquire=AsyncMock())
    http = SimpleNamespace(
        get=AsyncMock(
            side_effect=[
                RuntimeError(),
                SimpleNamespace(raise_for_status=lambda: None, json=list),
            ]
        )
    )
    monkeypatch.setattr("core.data.news.asyncio.sleep", AsyncMock())
    assert (
        await fetcher._fetch_with_retry(http, "https://fixture.invalid", {}, "test")
        == []
    )
    assert http.get.await_count == 2
    assert fetcher.max_retries == fetcher._max_retries
    items = [story(1, 0.4), story(2, 0.1)]
    fetcher._fetch_company_news = AsyncMock(return_value=[])
    fetcher._parse_items = Mock(return_value=items)
    fetcher._fetch_general_news = AsyncMock(side_effect=RuntimeError())
    assert (await fetcher.fetch_news(["ABC"], "MONITOR"))["ABC"][0].id == 2
    assert not fetcher._seen_path.exists()
    assert (await fetcher.fetch_news(["ABC"], "OPEN"))["ABC"][0].id == 2
    assert (await fetcher.fetch_news(["ABC"], "MID"))["ABC"][0].id == 2
    import json

    assert json.loads(fetcher._seen_path.read_text())["ids"] == [2]


@pytest.mark.asyncio
async def test_chronological_drawdown_and_completed_volume(monkeypatch):
    import core.data.daily as trail
    from utils.market_calendar import _NYSE

    now = datetime(2026, 11, 16, 15, tzinfo=UTC)
    sessions = _NYSE.schedule(start_date="2026-06-01", end_date="2026-11-16")
    bars = [
        OHLCVBar(
            timestamp=str(day.date()),
            open=100 + i,
            close=100 + i,
            high=102 + i,
            low=99 + i,
            volume=1000,
        )
        for i, day in enumerate(sessions.index)
    ]
    bars[-1].volume = 1
    original = trail.completed_bars
    monkeypatch.setattr(
        trail, "completed_bars", lambda data, **kw: original(data, now=now, **kw)
    )
    client = AsyncMock()
    client.get_technical_indicator.return_value = {}
    client.get_equity_fundamentals.return_value = {
        "ABC": {"average_volume_30_days": 1000}
    }
    metrics = await MetricsEngine().compute("ABC", bars, client)
    assert metrics.drawdown_30d == 0
    assert metrics.volume_spike_pct == 0
    assert metrics.window_end == bars[-2].timestamp
    assert metrics.return_bases["90d"] == bars[-92].close


def test_old_gap_keeps_recent_contiguous_atr_window():
    from utils.market_calendar import _NYSE

    sessions = _NYSE.schedule(start_date="2026-06-01", end_date="2026-11-16")
    bars = [
        OHLCVBar(
            timestamp=str(day.date()),
            open=100,
            close=100,
            high=102,
            low=98,
            volume=1000,
        )
        for day in sessions.index
    ]
    del bars[3]
    now = datetime(2026, 11, 17, tzinfo=UTC)
    with pytest.raises(ValueError, match="missing exchange"):
        completed_bars(bars, now=now)
    assert len(completed_bars(bars, now=now, trailing_only=True)) > 90


def test_failed_review_short_retry_and_spread():
    from test_exits import quote, record

    r = record()
    now = datetime.now(UTC)
    r.review_events = {
        "x": {
            "id": "x",
            "state": "RETRY",
            "created_at": (now - timedelta(minutes=16)).isoformat(),
        }
    }
    assert due_reviews(r, now=now)
    r.review_events["x"]["state"] = "APPROVED"
    assert not due_reviews(r, now=now)
    q = quote(100)
    q.bid = 80
    q.ask = 101
    assert executable_mark(q) == 100
    q.bid = 99.9
    q.ask = 100.1
    assert executable_mark(q) == 99.9


def test_batch_news_and_model_change(tmp_path):
    from test_discovery import story

    h = NewsHistory(tmp_path / "news.json")
    items = [story(10, 1), story(11, 9), story(12, 20), story(13, 35)]
    start = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    for ticker in ["ABC", "DEF"]:
        h.update(
            ticker,
            items,
            [0.1] * 4,
            {"model_name": "first"},
            coverage_start=start,
            persist=False,
        )
    assert not h.path.exists()
    h.flush()
    h.update("ABC", items, [0.2] * 4, {"model_name": "second"}, coverage_start=start)
    import json

    state = json.loads(h.path.read_text())
    assert all(
        s["model"]["model_name"] == "second" for s in state["tickers"]["ABC"]["stories"]
    )
    assert "DEF" in state["tickers"]


@pytest.mark.asyncio
async def test_partial_sell_fifo_basis_and_metadata(tmp_path, monkeypatch):
    from test_backend_safety import build
    from test_p0_safety import decision, portfolio, result

    from models.portfolio import Position

    backend, _client, pm, _ = build(tmp_path, monkeypatch)
    # Explicit fills establish known lots rather than reconstructed broker history.
    assert pm.reconcile_with_mcp(portfolio())
    for price in [100, 120]:
        key = pm.create_order(decision(), "buy", 2, price)
        pm.update_order(key, result(qty=2, price=price, order_id=str(price)))
    record = pm.get_open_records()[0]
    assert record.entry_price == 110
    key = pm.create_order(decision("SELL"), "sell", 1, 130)
    pm.update_order(key, result(qty=1, price=130, side="sell", order_id="sell-fixture"))
    assert (
        next(f for f in pm.snapshot()["fills"].values() if f["side"] == "sell")[
            "realized_pnl"
        ]
        == 30
    )
    record = pm.get_open_records()[0]
    assert record.entry_price == pytest.approx(340 / 3)
    lots = record.lots
    assert pm.reconcile_with_mcp(
        portfolio(Position(ticker="ABC", quantity=3, avg_cost=340 / 3))
    )
    assert pm.get_open_records()[0].lots == lots
    backend.agent.executor.settings["exits"] = {
        "partial_take": {"enabled": True, "fraction": 0.5}
    }
    d = decision("SELL")
    d.action_type = "REDUCE"
    backend.agent.executor._submit = AsyncMock(
        return_value=result("WOULD_EXECUTE", qty=0, side="sell")
    )
    rejected = await backend.agent.executor._sell_leg(d, portfolio(), 130, True, record)
    assert rejected.status == "REJECTED_PARTIAL_TAKE_THRESHOLD"
    backend.agent.executor._submit.assert_not_awaited()
    await backend.agent.executor._sell_leg(d, portfolio(), 131, True, record)
    assert backend.agent.executor._submit.await_args.args[2] == 1.5
    backend.agent.executor.settings["exits"]["partial_take"]["enabled"] = False
    backend.agent.executor._submit.reset_mock()
    rejected = await backend.agent.executor._sell_leg(d, portfolio(), 131, True, record)
    assert rejected.status == "REJECTED_PARTIAL_TAKE_DISABLED"
    backend.agent.executor._submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_sector_allocation_and_pullback_evidence(tmp_path, monkeypatch):
    from test_backend_safety import build
    from test_p0_safety import decision, portfolio

    from models.market_data import MarketMetrics
    from models.portfolio import Position

    backend, client, _, _ = build(tmp_path, monkeypatch)
    client.get_equity_tradability.return_value = {"tradeable": True}
    d = decision(ticker="NEW")
    d.edge.total = 3
    book = portfolio(
        Position(
            ticker="KEEP",
            quantity=23,
            avg_cost=100,
            current_price=100,
            market_value=2300,
            sector="Technology",
        )
    )
    assert (
        await backend.agent.executor._check_buy_gates(
            "NEW", 100, 0, 1e6, 0, book, d, False
        )
        is None
    )
    metrics = MarketMetrics(
        ticker="ABC",
        current_price=97,
        sma_20=100,
        return_30d=5,
        rsi_14=40,
        volume_spike_pct=-20,
    )
    assert backend.evidence_for("ABC", metrics)["incremental_opportunity_confirmed"]
