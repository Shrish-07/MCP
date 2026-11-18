"""Run the production funnel using synthetic provider inputs and local artifacts."""

import json
import math
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from core.decision.engine import DecisionEngine
from core.discovery.evidence import candidate_tickers, ranked_evidence
from core.discovery.pipeline import Discovery
from core.discovery.shortlist import load_shortlist
from models.decisions import CycleType, DecisionType
from models.market_data import EquityQuote, MarketMetrics, OHLCVBar
from models.news import NewsItem
from models.portfolio import Portfolio
from utils.market_calendar import _NYSE
from utils.strategy_config import DEFAULTS


@pytest.fixture
def research(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    cfg = deepcopy(DEFAULTS["discovery"])
    cfg.update(enabled=True, min_cohort=5, features_dir="data/features")
    cfg["shortlist"]["new_candidate_count"] = 2
    cfg["archetypes"].update(lagged_gap_z=-10, avoid_gap_z=-20)
    cfg["fingerprints"].update(
        exhaustion_min=0, exhaustion_max=1, down_volume_ratio_max=10
    )
    cfg["gates"]["min_dollar_volume"] = 1
    settings = {"config_revision": "fixture-config", "discovery": cfg}

    symbols = [f"T{i:03}" for i in range(480)]
    eligible = symbols[:7]
    held = eligible[-2]
    universe_path = tmp_path / cfg["universe_file"]
    universe_path.parent.mkdir()
    universe = {
        "source": "fixture",
        "version": "fixture-universe",
        "as_of": now.isoformat(),
        "constituents": [{"ticker": ticker} for ticker in symbols],
    }
    universe_path.write_text(json.dumps(universe))
    refresh = AsyncMock(side_effect=httpx.ConnectError("fixture upstream offline"))
    monkeypatch.setattr("core.discovery.pipeline.refresh", refresh)
    sessions = _NYSE.schedule(
        start_date=now.date() - timedelta(days=420), end_date=now.date()
    )
    sessions = sessions[sessions["market_close"] <= now]
    bars = {}
    for i, ticker in enumerate(eligible):
        history = []
        for j, day in enumerate(sessions.index):
            px = 100 + j * (i + 1) * 0.025 + math.sin(j / (3 + i / 2))
            history.append(
                OHLCVBar(
                    timestamp=day.date().isoformat(),
                    open=px,
                    high=px + 0.5,
                    low=px - 0.5,
                    close=px,
                    volume=1e6,
                )
            )
        bars[ticker] = history

    async def historicals(chunk, *, lookback_days):
        assert lookback_days > 365
        return {ticker: bars[ticker] for ticker in chunk if ticker in bars}

    async def quotes(chunk):
        return [
            EquityQuote(
                ticker=ticker,
                price=100,
                bid=99,
                ask=101,
                volume=1e6,
                prev_close=100,
                timestamp=now.isoformat(),
            )
            for ticker in chunk
            if ticker in bars
        ]

    async def stories(ticker, *, from_date, to_date, strict):
        assert strict and (to_date - from_date).days == 45
        if ticker == eligible[-1]:
            return []
        index = eligible.index(ticker)
        days = [1 + k / 10 for k in range(index + 1)] + [9, 18, 28, 38]
        return [
            NewsItem(
                id=j + 1,
                headline=f"{index} {day}",
                source="fixture",
                related=ticker,
                datetime=int((now - timedelta(days=day)).timestamp()),
            )
            for j, day in enumerate(days)
        ]

    def score(headlines):
        inputs = [list(map(float, headline.split())) for headline in headlines]
        return [
            index / 10 if day < 7 else index**2 / 125 - 0.3 for index, day in inputs
        ]

    broker = SimpleNamespace(
        get_equity_historicals_batch=AsyncMock(side_effect=historicals),
        get_equity_quotes=AsyncMock(side_effect=quotes),
        get_earnings_calendar_window=AsyncMock(return_value={"data": {"results": []}}),
    )
    positions = SimpleNamespace(
        reconciliation_ok=True, get_open_records=lambda: [SimpleNamespace(ticker=held)]
    )
    model = SimpleNamespace(
        get_model_info=lambda: {"model_name": "fixture"}, score_headlines=score
    )
    news = SimpleNamespace(fetch_company_news=AsyncMock(side_effect=stories))
    discovery = Discovery(tmp_path, settings, broker, positions, news, lambda: model)
    return SimpleNamespace(
        discovery=discovery,
        broker=broker,
        refresh=refresh,
        news=news,
        held=held,
        sparse=eligible[-1],
        universe=universe,
        universe_path=universe_path,
    )


@pytest.mark.asyncio
async def test_funnel_reuses_scheduled_features_and_publishes_versioned_evidence(
    research,
):
    discovery = research.discovery
    features = await discovery.features()
    assert len(features["rows"]) == 7
    assert len(features["excluded"]) == 473
    assert (
        discovery.root / "data/features" / f"features_{features['session']}.json"
    ).is_file()
    research.broker.get_equity_historicals_batch.reset_mock()

    published = await discovery.rank()
    research.refresh.assert_awaited_once()
    research.broker.get_equity_historicals_batch.assert_not_awaited()
    assert research.news.fetch_company_news.await_count == 7
    assert published["excluded"][research.sparse] == ["sparse_news"]
    assert published["cohort_size"] == 6
    assert research.held in published["all_ranked"]
    snapshot = load_shortlist(discovery.path)
    candidates = candidate_tickers(snapshot, {research.held}, 2)
    assert len(candidates) == 2 and research.held not in candidates
    evidence = ranked_evidence(snapshot, candidates[0])
    assert evidence["discovery_run_id"] == published["run_id"]
    assert evidence["discovery_universe_version"] == "fixture-universe"
    assert (
        datetime.fromisoformat(evidence["discovery_data_cutoff"]).utcoffset()
        is not None
    )
    assert math.isfinite(evidence["gap_z"])
    model = SimpleNamespace(complete=AsyncMock(return_value='{"decision":"IGNORE"}'))
    engine = DecisionEngine(
        model,
        SimpleNamespace(summarise_for_prompt=lambda *_args, **_kwargs: ""),
        SimpleNamespace(earnings_window_for_prompt=lambda *_args: ""),
    )
    decision = await engine.make_decision(
        candidates[0],
        MarketMetrics(ticker=candidates[0]),
        [],
        Portfolio(),
        100,
        1000,
        1,
        CycleType.OPEN,
        evidence=evidence,
    )
    assert decision.decision is DecisionType.IGNORE and not decision.is_fallback
    assert decision.evidence["discovery_run_id"] == published["run_id"]
    assert published["run_id"] in model.complete.await_args.args[1]


@pytest.mark.asyncio
async def test_bad_features_rebuild_and_bad_universe_preserves_shortlist(research):
    discovery = research.discovery
    await discovery.features()
    discovery.feature_path.write_text("[]")
    research.broker.get_equity_historicals_batch.reset_mock()
    await discovery.rank()
    research.broker.get_equity_historicals_batch.assert_awaited()
    saved = discovery.path.read_bytes()

    research.universe["as_of"] = (datetime.now(UTC) - timedelta(days=15)).isoformat()
    research.universe_path.write_text(json.dumps(research.universe))
    with pytest.raises(ValueError, match="stale"):
        await discovery.rank()
    assert discovery.path.read_bytes() == saved
    assert (
        json.loads(discovery.path.with_suffix(".status.json").read_text())["status"]
        == "FAILED"
    )


@pytest.mark.asyncio
async def test_disabled_discovery_skips_every_provider(research):
    research.discovery.cfg["enabled"] = False
    assert await research.discovery.features() is None
    assert await research.discovery.rank() is None
    research.refresh.assert_not_called()
    research.broker.get_equity_historicals_batch.assert_not_awaited()
    research.news.fetch_company_news.assert_not_awaited()
