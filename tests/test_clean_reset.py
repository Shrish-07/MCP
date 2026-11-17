import hashlib
import json
import signal
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_p0_safety import decision, result

from core.backend import Backend
from core.execution.engine import ExecutionEngine
from core.position.manager import PositionManager, StateIntegrityError
from main import TradingAgent
from models.portfolio import Portfolio
from robinhood_mcp.robinhood_client import (
    AuthenticationError,
    MCPError,
    RobinhoodMCPClient,
)
from utils.config import load_config
from utils.health import Health, inspect_health
from utils.journal_manager import JournalManager
from utils.state_reset import reset_local_state


def empty_portfolio():
    return Portfolio(
        account_id=hashlib.sha256(b"fixture-account").hexdigest(),
        cash=0,
        total_value=0,
        buying_power=0,
        reconciliation_status="SUCCESS",
        positions_complete=True,
    )


def test_reset_clears_context_and_preserves_execution_identity(tmp_path):
    pm = PositionManager(tmp_path / "logs/positions.json")
    key = pm.create_order(decision(), "buy", 2, 100)
    pm.update_order(key, result())
    original = pm.snapshot()
    stale = pm.get_open_records()[0]
    journal = tmp_path / "journals"
    journal.mkdir()
    (journal / "ABC.json").write_text(
        json.dumps([{"decision": "BUY", "fill_price": 100}])
    )
    data = tmp_path / "data"
    data.mkdir()
    (data / "selected_universe.json").write_text('{"tickers":["ABC"]}')
    summary = reset_local_state(tmp_path, empty_portfolio(), [])
    restarted = PositionManager(pm._path)
    restarted.update_record(stale)
    assert summary["journal_records_removed"] == 1
    assert summary["legacy_positions_removed"] == 1
    assert list(journal.iterdir()) == []
    assert not (data / "selected_universe.json").exists()
    assert restarted.snapshot()["orders"] == original["orders"]
    assert restarted.snapshot()["fills"] == original["fills"]
    assert restarted.get_open_records() == []
    assert restarted.snapshot()["account_binding"] == empty_portfolio().account_id
    agent = SimpleNamespace(
        position_manager=restarted, config={}, journal_manager=JournalManager(journal)
    )
    Backend(agent).sync_fill_journals()
    assert agent.journal_manager.get_all_tickers() == []


def test_reset_refuses_unresolved_intent_without_deleting_context(tmp_path):
    pm = PositionManager(tmp_path / "logs/positions.json")
    pm.create_order(decision(), "buy", 2, 100)
    journal = tmp_path / "journals"
    journal.mkdir()
    path = journal / "ABC.json"
    path.write_text("[]")
    before = pm._path.read_bytes()
    with pytest.raises(StateIntegrityError, match="Unresolved"):
        reset_local_state(tmp_path, empty_portfolio(), [])
    assert pm._path.read_bytes() == before
    assert path.exists()


@pytest.mark.parametrize("change", ["cash", "account", "broker_order"])
def test_reset_refuses_nonempty_or_mismatched_broker(tmp_path, change):
    pm = PositionManager(tmp_path / "logs/positions.json")
    portfolio = empty_portfolio()
    assert pm.reconcile_with_mcp(portfolio)
    orders = []
    if change == "cash":
        portfolio.cash = 1
    elif change == "account":
        portfolio.account_id = hashlib.sha256(b"other-account").hexdigest()
    else:
        orders = [result("SUBMITTED", qty=0)]
    before = pm._path.read_bytes()
    with pytest.raises(StateIntegrityError):
        reset_local_state(tmp_path, portfolio, orders)
    assert pm._path.read_bytes() == before


@pytest.mark.asyncio
async def test_clean_backend_reconciles_empty_book_and_keeps_live_gates(tmp_path):
    portfolio = empty_portfolio()
    pm = PositionManager(tmp_path / "logs/positions.json")
    pm.reset_strategy_context(portfolio, [])
    client = AsyncMock()
    client.get_portfolio.return_value = portfolio
    client.get_equity_orders.return_value = []
    config = {"env": {"DRY_RUN": True}, "execution": {"live_release_approved": False}}
    agent = SimpleNamespace(config=config, position_manager=pm, mcp_client=client)
    agent.executor = ExecutionEngine(client, pm, config)
    backend = Backend(agent)
    assert await backend.reconcile()
    assert PositionManager(pm._path).get_open_records() == []
    assert pm.entries_ready
    blocked = await agent.executor.execute(
        decision(), portfolio, 100, dry_run=False, avg_volume_30d=1e6
    )
    assert blocked.dry_run
    client.place_equity_order.assert_not_called()
    assert pm.snapshot()["orders"] == {}


@pytest.mark.parametrize(
    "kind",
    ["AUTHENTICATION_FAILED", "STATE_RECONCILIATION_FAILED", "BROKER_UNAVAILABLE"],
)
def test_health_classifies_broker_failures(tmp_path, kind):
    now = datetime.now(UTC)
    path = tmp_path / "health.json"
    state = {
        "process": {"pid": 1, "created_at": 1, "phase": "RUNNING"},
        "heartbeat": {"last_success": now.isoformat()},
        "broker": {"error": "fixture", "failure_kind": kind},
    }
    path.write_text(json.dumps(state))
    assert (
        inspect_health(path, now=now, market_open=False, process_probe=lambda *_: True)[
            "status"
        ]
        == kind
    )


def test_health_requires_current_process_and_heartbeat_outside_market(tmp_path):
    now = datetime.now(UTC)
    path = tmp_path / "health.json"
    assert inspect_health(path)["status"] == "PROCESS_ABSENT"
    health = Health(path)
    health.start_process()
    health.record("broker", success=True)
    assert inspect_health(path, now=now, market_open=False)["healthy"]
    assert (
        inspect_health(
            path, now=now, market_open=False, process_probe=lambda *_: False
        )["status"]
        == "PROCESS_ABSENT"
    )
    health.state["heartbeat"]["last_success"] = (now - timedelta(minutes=5)).isoformat()
    path.write_text(json.dumps(health.state))
    assert inspect_health(path, now=now, market_open=False)["status"] == "PROCESS_STALE"
    health.stop_process()
    assert inspect_health(path, market_open=False)["status"] == "PROCESS_ABSENT"


@pytest.mark.asyncio
async def test_startup_failure_always_marks_process_stopped(tmp_path):
    agent = TradingAgent.__new__(TradingAgent)
    health = Health(tmp_path / "health.json")
    agent.backend = SimpleNamespace(health=health, review_task=None)
    agent.scheduler = SimpleNamespace(running=False)
    agent._start_jobs = AsyncMock(side_effect=ValueError("fixture startup failure"))
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(ValueError, match="fixture"):
        await agent.start()
    assert health.state["process"]["phase"] == "STOPPED"
    assert signal.getsignal(signal.SIGTERM) == before


def test_config_inspection_does_not_read_secret_sources(monkeypatch):
    import utils.config as module

    monkeypatch.setattr(
        module,
        "load_dotenv",
        Mock(side_effect=AssertionError("Secret source accessed")),
    )
    config = load_config(include_secrets=False)
    assert config["env"]["ANTHROPIC_API_KEY"] == config["env"]["FINNHUB_API_KEY"] == ""
    assert config["execution"]["dry_run"] is True
    assert config["execution"]["live_release_approved"] is False


def test_zero_buying_power_is_not_replaced_by_unleveraged_power():
    value = RobinhoodMCPClient._coerce_portfolio_fields(
        {
            "cash": "0",
            "total_value": "100",
            "buying_power": {"buying_power": "0", "unleveraged_buying_power": "50"},
        }
    )
    assert value["buying_power"] == 0
    assert value["cash_pct"] == 0


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"total_value": "bad", "buying_power": 0},
        {"total_value": 0, "buying_power": {"buying_power": None}},
        {"total_value": 0, "buying_power": 0, "cash": "NaN"},
    ],
)
def test_unknown_balance_cannot_become_verified_zero(data):
    with pytest.raises(MCPError):
        RobinhoodMCPClient._coerce_portfolio_fields(data)


@pytest.mark.asyncio
async def test_authentication_failure_survives_account_resolution():
    client = RobinhoodMCPClient()
    client._call_tool_with_retry = AsyncMock(side_effect=AuthenticationError("fixture"))
    with pytest.raises(AuthenticationError):
        await client._get_account_number()


def test_journal_rejects_paths_outside_store(tmp_path):
    journal = JournalManager(tmp_path)
    with pytest.raises(ValueError):
        journal.get_recent("../outside")


def test_failure_logs_do_not_expose_exception_content(tmp_path, caplog):
    health = Health(tmp_path / "health.json")
    health.record(
        "broker", success=False, error=RuntimeError("sensitive-fixture-value")
    )
    assert "sensitive-fixture-value" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_failed_cache_persistence_is_reported_without_exposing_values(
    monkeypatch, caplog
):
    from utils import persistence

    monkeypatch.setattr(
        persistence,
        "atomic_json",
        Mock(side_effect=PermissionError("sensitive-fixture")),
    )
    with pytest.raises(AuthenticationError, match="persisted"):
        RobinhoodMCPClient()._save_token_cache({"access_token": "fixture-only"})
    assert "sensitive-fixture" not in caplog.text


@pytest.mark.asyncio
async def test_unsaved_credentials_cannot_silently_be_used_after_write_failure():
    client = RobinhoodMCPClient()
    client._token_cache = {"access_token": "synthetic-fixture"}
    client._token_cache_unsaved = True
    client._token_is_fresh = Mock(return_value=True)
    client._save_token_cache = Mock(
        side_effect=[AuthenticationError("fixture persistence failure"), None]
    )
    client._load_token_cache = Mock(side_effect=AssertionError("Unexpected cache read"))
    with pytest.raises(AuthenticationError):
        await client._ensure_valid_token()
    assert client._token_cache_unsaved
    assert await client._ensure_valid_token() == "synthetic-fixture"
    assert not client._token_cache_unsaved
    assert client._save_token_cache.call_count == 2


def test_instance_lock_does_not_grow_or_rewrite_existing_lock_file(tmp_path):
    from utils.instance_lock import InstanceLock

    path = tmp_path / "agent.lock"
    with InstanceLock(path):
        pass
    before = path.read_bytes()
    with InstanceLock(path) as owner:
        owner.stream.seek(0)
        assert owner.stream.read() == before
    assert path.read_bytes() == before == b"0"


def test_model_initialization_is_serialized(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from time import sleep

    from core.data import sentiment

    calls = []

    def load(model):
        sleep(0.02)
        calls.append(True)

    monkeypatch.setattr(sentiment.SentimentModel, "_instance", None)
    monkeypatch.setattr(sentiment.SentimentModel, "_load_model", load)
    with ThreadPoolExecutor(max_workers=4) as pool:
        models = list(pool.map(lambda _: sentiment.SentimentModel(), range(4)))
    assert all(model is models[0] for model in models)
    assert calls == [True]


@pytest.mark.asyncio
async def test_holdings_authentication_failure_reaches_health_classification():
    client = RobinhoodMCPClient()
    client._get_account_number = AsyncMock(return_value="fixture-account")
    client._call_tool_with_retry = AsyncMock(
        return_value=[
            {
                "type": "text",
                "text": '{"data":{"cash":0,"total_value":0,"buying_power":0}}',
            }
        ]
    )
    client.get_equity_positions = AsyncMock(side_effect=AuthenticationError("fixture"))
    with pytest.raises(AuthenticationError):
        await client.get_portfolio()


def test_replay_refuses_non_symbol_paths(monkeypatch):
    import runpy
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts/replay_archived.py"
    monkeypatch.setattr(sys, "argv", [str(script), "--ticker", "../outside"])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(script), run_name="__main__")
    assert error.value.code == 2


def test_oauth_callback_listener_closes_after_one_request(monkeypatch):
    from unittest.mock import MagicMock

    import robinhood_mcp.robinhood_client as module

    server = MagicMock()
    monkeypatch.setattr(module, "HTTPServer", Mock(return_value=server))
    module._run_callback_server_blocking()
    server.__enter__.return_value.handle_request.assert_called_once()
    server.__exit__.assert_called_once()


@pytest.mark.asyncio
async def test_async_response_recording_keeps_each_json_line(tmp_path, monkeypatch):
    import asyncio

    import robinhood_mcp.response_recorder as module

    path = tmp_path / "trace.jsonl"
    monkeypatch.setattr(module, "_write_lock", asyncio.Lock())
    await asyncio.gather(
        *(
            module.write_record({"fixture_index": index}, log_path=path)
            for index in range(10)
        )
    )
    assert sorted(
        json.loads(line)["fixture_index"] for line in path.read_text().splitlines()
    ) == list(range(10))


@pytest.mark.parametrize(
    "compatibility,canonical",
    [
        ("data_ingest", "core.data.ingest"),
        ("decision_engine", "core.decision.engine"),
        ("metrics", "core.data.metrics"),
        ("news_fetch", "core.data.news"),
        ("pnl_tracker", "core.position.pnl_tracker"),
        ("position_manager", "core.position.manager"),
        ("sentiment_model", "core.data.sentiment"),
        ("ticker_selector", "core.weekly.ticker_selector"),
        ("weekly_news", "core.weekly.news"),
        ("weekly_selector", "core.weekly.selector"),
    ],
)
def test_compatibility_modules_preserve_public_symbol_identity(
    compatibility, canonical
):
    import importlib

    implementation = importlib.import_module(canonical)
    wrapper = importlib.import_module("core." + compatibility)
    names = getattr(
        implementation,
        "__all__",
        [name for name in vars(implementation) if not name.startswith("_")],
    )
    assert set(wrapper.__all__) == set(names)
    assert all(
        getattr(wrapper, name) is getattr(implementation, name) for name in names
    )
