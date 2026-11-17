import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from main import TradingAgent
from utils.config import load_config


class Scheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, callback, trigger, **options):
        self.jobs[options["id"]] = (callback, trigger, options)

    def start(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("discovery,seconds", [(False, 15), (True, 15), (True, 60)])
async def test_real_startup_registration_is_complete_without_external_process(
    monkeypatch, discovery, seconds
):
    import main

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = cls(2026, 11, 16, 13, tzinfo=UTC)
            return instant.astimezone(tz) if tz else instant

    monkeypatch.setattr(main, "datetime", Clock)
    monkeypatch.setattr(main, "configure_logging", Mock())
    agent = TradingAgent.__new__(TradingAgent)
    agent.config = load_config(include_secrets=False)
    agent.config["monitor"]["interval_seconds"] = seconds
    agent.config["discovery"]["enabled"] = discovery
    agent.scheduler = Scheduler()
    agent.logger = Mock()
    agent.backend = SimpleNamespace(
        discovery=object() if discovery else None,
        reconcile=AsyncMock(return_value=True),
        monitor=AsyncMock(),
        nightly=AsyncMock(),
        weekly=AsyncMock(),
        heartbeat=Mock(),
        ensure_discovery=AsyncMock(return_value=True),
        ensure_nightly=AsyncMock(return_value=True),
        attribution=AsyncMock(),
    )

    async def selector():
        assert "attribution" in agent.scheduler.jobs
        return SimpleNamespace(enabled=False)

    agent._get_weekly_selector = AsyncMock(side_effect=selector)
    agent._stop_event = asyncio.Event()
    agent._stop_event.set()
    await agent._start_jobs()
    await agent._nightly_task
    agent.backend.ensure_nightly.assert_awaited_once()
    assert {
        "heartbeat",
        "nightly",
        "decision_open",
        "decision_mid",
        "decision_close",
        "position_monitor",
        "attribution",
    } <= agent.scheduler.jobs.keys()
    monitor, trigger, options = agent.scheduler.jobs["position_monitor"]
    assert trigger == "interval" and options["seconds"] == seconds
    await monitor()
    agent.backend.monitor.assert_awaited_once()
    nightly, _, options = agent.scheduler.jobs["nightly"]
    assert options["hour"] == 20 and options["timezone"] == "America/New_York"
    await nightly()
    agent.backend.nightly.assert_awaited_once()
    if discovery:
        await agent._discovery_task
        agent._get_weekly_selector.assert_not_awaited()
        agent.backend.ensure_discovery.assert_awaited_once()
        assert "discovery_weekly" in agent.scheduler.jobs
    else:
        agent._get_weekly_selector.assert_awaited_once()


@pytest.mark.asyncio
async def test_enabled_discovery_never_silently_uses_legacy(monkeypatch):
    from core.discovery import shortlist

    monkeypatch.setattr(
        shortlist, "load_shortlist", Mock(side_effect=ValueError("stale"))
    )
    agent = SimpleNamespace(
        config={
            "discovery": {
                "enabled": True,
                "shortlist_file": "missing.json",
                "shortlist_max_age_days": 8,
            }
        },
        logger=Mock(),
        _get_weekly_selector=AsyncMock(),
    )
    assert await TradingAgent._resolve_watchlist(agent) == []
    agent._get_weekly_selector.assert_not_awaited()


@pytest.mark.asyncio
async def test_discovery_bootstrap_and_reconciliation_failure(tmp_path, monkeypatch):
    from test_backend_safety import build

    backend, _, _, _ = build(tmp_path, monkeypatch)
    backend.discovery = SimpleNamespace(
        path=tmp_path / "shortlist.json",
        cfg={"shortlist_max_age_days": 8},
        rank=AsyncMock(),
    )
    backend.reconcile = AsyncMock(return_value=False)
    assert not await backend.ensure_discovery()
    backend.discovery.rank.assert_not_awaited()
    backend.reconcile.return_value = True

    async def publish():
        backend.discovery.path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "status": "SUCCESS",
                    "new_candidates": [],
                }
            )
        )

    backend.discovery.rank.side_effect = publish
    assert await backend.ensure_discovery()
    assert await backend.ensure_discovery()
    backend.discovery.rank.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "seconds,expected", [(30, "REJECTED_STALE_QUOTE"), (60, "SUBMITTED")]
)
async def test_execution_reads_configured_quote_age(
    tmp_path, monkeypatch, seconds, expected
):
    from datetime import timedelta

    from test_backend_safety import build
    from test_p0_safety import decision

    backend, client, _, quote = build(tmp_path, monkeypatch)
    assert await backend.reconcile()
    backend.agent.config["execution"]["quote_max_age_seconds"] = seconds
    quote.timestamp = (datetime.now(UTC) - timedelta(seconds=45)).isoformat()
    result = await backend.agent.executor.execute(
        decision("SELL"),
        backend.last_portfolio,
        100,
        False,
        quote_timestamp=quote.timestamp,
    )
    assert result.status == expected
    assert client.place_equity_order.await_count == (
        1 if expected == "SUBMITTED" else 0
    )


@pytest.mark.parametrize("maximum", [False, 0, -1, float("nan"), "60"])
def test_invalid_quote_policy_fails_closed(maximum):
    from utils.freshness import fresh_quote

    assert not fresh_quote(
        datetime.now(UTC).isoformat(), {"execution": {"quote_max_age_seconds": maximum}}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["SELL", "ADD_TO_POSITION"])
async def test_late_review_cannot_act_on_a_different_position_lifecycle(
    tmp_path, monkeypatch, action
):
    from test_backend_safety import build
    from test_p0_safety import decision

    backend, client, _, quote = build(tmp_path, monkeypatch)
    assert await backend.reconcile()
    proposed = decision(action)
    proposed.evidence["position_id"] = "previous-closed-position"
    result = await backend.agent.executor.execute(
        proposed, backend.last_portfolio, 100, False, quote_timestamp=quote.timestamp
    )
    assert result.status == "REJECTED_POSITION_LIFECYCLE"
    client.place_equity_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_nightly_persistence_failure_is_not_reported_as_success(
    tmp_path, monkeypatch
):
    from test_backend_safety import build

    from core.position.manager import PositionManager
    from models.portfolio import Portfolio
    from utils import persistence

    backend, _, pm, _ = build(tmp_path, monkeypatch)
    backend.reconcile = AsyncMock(return_value=True)
    # Empty fixture book avoids unrelated historical/news work.
    assert pm.reconcile_with_mcp(
        Portfolio(positions_complete=True, reconciliation_status="SUCCESS")
    )
    original = persistence.atomic_json

    def fail_features(path, *args, **kwargs):
        if path.name == "held_features_latest.json":
            raise OSError("fixture disk unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(persistence, "atomic_json", fail_features)
    await backend.nightly()
    assert backend.health.state["nightly"]["error"] == "OSError"
    assert PositionManager(pm._path).get_open_records() == []


@pytest.mark.asyncio
async def test_attribution_failure_does_not_publish_a_current_report(
    tmp_path, monkeypatch
):
    from test_backend_safety import build

    backend, client, _, _ = build(tmp_path, monkeypatch)
    backend.reconcile = AsyncMock(return_value=False)
    assert await backend.attribution() is None
    assert backend.health.state["attribution"]["error"]
    client.get_equity_quotes.assert_not_awaited()


@pytest.mark.asyncio
async def test_nightly_does_not_process_unverified_holdings(tmp_path, monkeypatch):
    from test_backend_safety import build

    backend, client, _, _ = build(tmp_path, monkeypatch)
    backend.reconcile = AsyncMock(return_value=False)
    await backend.nightly()
    client.get_equity_historicals.assert_not_awaited()
    assert backend.health.state["nightly"]["error"]


@pytest.mark.asyncio
async def test_startup_cycle_failure_is_observable_and_next_cycle_can_run():
    from models.decisions import CycleType

    agent = TradingAgent.__new__(TradingAgent)
    agent.logger = Mock()
    agent.backend = SimpleNamespace(health=Mock())
    agent._run_cycle = AsyncMock(
        side_effect=[TimeoutError("provider unavailable"), None]
    )
    await agent.run_cycle(CycleType.OPEN)
    assert agent.backend.health.record.call_args.kwargs["success"] is False
    await agent.run_cycle(CycleType.MID)
    assert agent._run_cycle.await_count == 2


@pytest.mark.asyncio
async def test_restart_recovers_missing_nightly_maintenance_once(tmp_path, monkeypatch):
    from test_backend_safety import build

    backend, _, pm, _ = build(tmp_path, monkeypatch)

    async def complete():
        record = pm.get_open_records()[0]
        record.atr14 = 2
        record.atr_date = datetime.now(UTC).isoformat()
        pm.update_record(record)
        backend.health.record("nightly", success=True)

    backend.nightly = AsyncMock(side_effect=complete)
    assert await backend.ensure_nightly()
    assert await backend.ensure_nightly()
    backend.nightly.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "long_history,news_ok", [(False, True), (True, True), (True, False)]
)
async def test_nightly_production_callback_uses_frozen_gap_and_available_history(
    tmp_path, monkeypatch, long_history, news_ok
):
    from test_backend_safety import build
    from test_discovery import cohort

    from core.discovery.divergence import score_cohort, score_fixed_reference
    from models.market_data import OHLCVBar

    backend, client, pm, _ = build(tmp_path, monkeypatch)
    backend.agent.config = load_config(include_secrets=False)
    ranked = score_cohort(cohort(), backend.agent.config["discovery"], "entry")
    entry = ranked["T1"]
    candidate = dict(ranked["T29"], gap_reference="new-cohort", gap_z=999)
    record = pm.get_open_records()[0]
    record.lots = [{"decision": {"evidence": entry}, "price": 100, "quantity": 2}]
    pm.update_record(record)
    backend.reconcile = AsyncMock(return_value=True)
    backend.evidence_for = Mock(return_value={"z_components": {"sentiment_delta": -3}})
    news = {"sentiment_mean": 0.2, "sentiment_delta": -0.1, "news_velocity": 2}
    backend.discovery = SimpleNamespace(
        news_for=AsyncMock(return_value=news),
        features=AsyncMock(),
        path=tmp_path / "shortlist.json",
        cfg={"shortlist_max_age_days": 8},
    )
    if not news_ok:
        backend.discovery.news_for.side_effect = RuntimeError("news unavailable")
    from core.discovery.shortlist import publish

    publish(backend.discovery.path, [candidate], {})
    bars = [
        OHLCVBar(
            timestamp=datetime.now(UTC).isoformat(),
            open=100,
            high=102,
            low=98,
            close=100,
            volume=1e6,
        )
        for _ in range(250 if long_history else 20)
    ]
    client.get_equity_historicals.return_value = bars
    monkeypatch.setattr(
        "core.exits.trail_manager.completed_bars", lambda data, **_: data
    )
    technical = {"momentum_blend": 0.01, "r5": 0.01, "rsi14": 50}
    monkeypatch.setattr(
        "core.discovery.features.technical_features", lambda *_: technical
    )
    signals = Mock()
    monkeypatch.setattr("core.exits.triggers.nightly_signals", signals)
    await backend.nightly()
    signals.assert_called_once()
    assert not backend.health.state["nightly"]["error"]
    kwargs = signals.call_args.kwargs
    if long_history and news_ok:
        assert kwargs["comparable"]
        assert kwargs["best_candidate"]["gap_z"] == pytest.approx(
            score_fixed_reference(candidate, entry, backend.agent.config["discovery"])
        )
        assert kwargs["best_candidate"]["gap_reference"] == "entry"
        assert kwargs["best_candidate"]["gap_z"] != 999
    else:
        assert not kwargs["comparable"]
        assert kwargs["best_candidate"] is None
    if not news_ok:
        assert signals.call_args.args[3]["z_components"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["BUY", "ADD_TO_POSITION", "SELL", "ROTATE"])
@pytest.mark.parametrize("fallback", [False, True])
async def test_main_cycle_reaches_each_execution_action(
    tmp_path, monkeypatch, action, fallback
):
    from test_backend_safety import build
    from test_p0_safety import decision, portfolio

    from models.decisions import CycleType, ExecutionResult
    from models.market_data import EquityQuote, MarketMetrics

    backend, client, pm, _ = build(tmp_path, monkeypatch)
    agent = TradingAgent.__new__(TradingAgent)
    agent.__dict__.update(backend.agent.__dict__)
    backend.agent, agent.backend = agent, backend
    agent.config = load_config(include_secrets=False)
    agent.logger = Mock()
    agent._is_market_open = Mock(return_value=True)
    agent._discovery_task = agent._nightly_task = None
    agent._cached_watchlist_date, agent._cached_watchlist = "", []
    agent._resolve_watchlist = AsyncMock(return_value=["NEW"])
    quotes = {
        t: EquityQuote(
            ticker=t,
            price=100,
            bid=99,
            ask=101,
            volume=1e6,
            prev_close=100,
            timestamp=datetime.now(UTC).isoformat(),
        )
        for t in ["SPY", "ABC", "NEW"]
    }
    client.get_equity_quotes.side_effect = lambda names: [quotes[t] for t in names]
    agent.ingest = SimpleNamespace(
        ingest=AsyncMock(
            return_value={"portfolio": portfolio(), "quotes": quotes, "historicals": {}}
        )
    )
    agent.metrics_engine = SimpleNamespace(
        compute=AsyncMock(
            side_effect=lambda t, *_: MarketMetrics(
                ticker=t, current_price=100, sma_20=90
            )
        )
    )
    agent.news_fetcher = SimpleNamespace(fetch_news=AsyncMock(return_value={}))
    agent.earnings_calendar = SimpleNamespace(refresh=AsyncMock())
    agent._earnings_flag = Mock(return_value=False)
    agent.journal_manager = Mock()
    agent.cost_tracker = SimpleNamespace(
        daily_total=lambda: 0.0, monthly_total=lambda: 0.0
    )
    backend.monitor, backend.reconcile = AsyncMock(), AsyncMock(return_value=True)
    backend.evidence_for = Mock(return_value={})
    if action == "SELL":
        from core.position.manager import ExitSignal

        pm.check_exits = Mock(return_value=[ExitSignal("ABC", "STOP_LOSS", 100, 0, 2)])
    held_action = action if action in ("SELL", "ADD_TO_POSITION") else "HOLD"
    new_action = action if action in ("BUY", "ROTATE") else "IGNORE"
    agent.decision_engine = SimpleNamespace(
        review_exit=AsyncMock(return_value=decision(held_action)),
        make_decision=AsyncMock(
            return_value=decision(
                new_action,
                ticker="NEW",
                rotate_from_ticker="ABC" if action == "ROTATE" else None,
            )
        ),
    )
    agent.executor = SimpleNamespace(
        execute=AsyncMock(
            return_value=ExecutionResult(ticker="NEW", status="SKIPPED", dry_run=True)
        )
    )
    if fallback:
        agent.decision_engine.review_exit.return_value = decision(
            "HOLD", is_fallback=True
        )
        agent.decision_engine.make_decision.return_value = decision(
            "IGNORE", ticker="NEW", is_fallback=True
        )
    await agent._run_cycle(CycleType.MID)
    if action == "SELL":
        agent.decision_engine.review_exit.assert_awaited_once()
    if fallback:
        agent.journal_manager.append.assert_not_called()
        assert backend.health.state["decision_cycle:MID"]["error"] == "Model fallback"
        return
    dispatched = [
        call.args[0].decision.value for call in agent.executor.execute.await_args_list
    ]
    assert action in dispatched
    assert all(
        call.kwargs["dry_run"] for call in agent.executor.execute.await_args_list
    )
    assert [call.args[0] for call in client.get_equity_quotes.await_args_list] == [
        ["ABC"],
        ["NEW"],
    ]
    assert len(pm.get_open_records()) == 1


@pytest.mark.asyncio
async def test_stop_during_startup_cancels_active_cycle_and_workers(tmp_path):
    from utils.health import Health

    agent = TradingAgent.__new__(TradingAgent)
    agent.backend = SimpleNamespace(
        health=Health(tmp_path / "health.json"), review_task=None
    )
    agent.scheduler = SimpleNamespace(running=True, shutdown=Mock())
    cancelled = asyncio.Event()

    async def startup():
        agent._discovery_task = asyncio.create_task(asyncio.Event().wait())
        agent.backend.review_task = asyncio.create_task(asyncio.Event().wait())
        agent._stop_event.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    agent._start_jobs = startup
    await asyncio.wait_for(agent.start(), 2)
    assert cancelled.is_set()
    assert agent._discovery_task.cancelled()
    assert agent.backend.review_task.cancelled()
    agent.scheduler.shutdown.assert_called_once_with(wait=False)
    assert agent.backend.health.state["process"]["phase"] == "STOPPED"


@pytest.mark.asyncio
async def test_heartbeat_runs_on_event_loop(tmp_path, monkeypatch):
    import inspect

    from test_backend_safety import build

    backend, _, _, _ = build(tmp_path, monkeypatch)
    assert inspect.iscoroutinefunction(backend.heartbeat)
    await backend.heartbeat()
    assert backend.health.state["heartbeat"]["last_success"]


@pytest.mark.asyncio
async def test_weekly_bootstrap_detects_missed_run_inside_age_limit(
    tmp_path, monkeypatch
):
    from test_backend_safety import build

    import core.backend as module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = cls(2026, 11, 16, 13, tzinfo=UTC)
            return instant.astimezone(tz) if tz else instant

    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr("core.discovery.shortlist.datetime", Clock)
    backend, _, _, _ = build(tmp_path, monkeypatch)
    backend.discovery = SimpleNamespace(
        path=tmp_path / "shortlist.json",
        cfg={"shortlist_max_age_days": 8},
        rank=AsyncMock(),
    )
    backend.reconcile = AsyncMock(return_value=True)
    payload = {
        "generated_at": "2026-11-09T13:00:00+00:00",
        "status": "SUCCESS",
        "new_candidates": [],
    }
    backend.discovery.path.write_text(json.dumps(payload))

    async def rank():
        payload["generated_at"] = Clock.now(UTC).isoformat()
        backend.discovery.path.write_text(json.dumps(payload))

    backend.discovery.rank.side_effect = rank
    assert await backend.ensure_discovery()
    backend.discovery.rank.assert_awaited_once()
