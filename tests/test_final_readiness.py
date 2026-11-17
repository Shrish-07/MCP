import json
import logging
from logging.handlers import RotatingFileHandler

import pytest
from test_p0_safety import decision, result, seed

from core.position.manager import PositionManager, StateIntegrityError
from models.decisions import JournalEntry
from utils.journal_manager import JournalManager


def test_stale_position_snapshot_cannot_loosen_persisted_stops(pm):
    seed(pm)
    stale = pm.get_open_records()[0]
    current = pm.get_open_records()[0]
    current.peak_price = 120
    current.trail_stop_px = current.effective_stop_px = 110
    pm.update_record(current)
    stale.thesis = "Review returned after the monitor raised protection."
    pm.update_record(stale)
    restarted = PositionManager(pm._path).get_open_records()[0]
    assert restarted.peak_price == 120
    assert restarted.trail_stop_px == restarted.effective_stop_px == 110


def test_stale_position_snapshot_cannot_undo_partial_fill(pm):
    seed(pm)
    stale = pm.get_open_records()[0]
    key = pm.create_order(decision("SELL"), "sell", 2, 100)
    pm.update_order(key, result("PARTIALLY_FILLED", qty=1, side="sell"))
    with pytest.raises(StateIntegrityError, match="balance changed"):
        pm.update_record(stale)
    assert PositionManager(pm._path).get_open_records()[0].quantity == 1


def test_complete_empty_broker_snapshot_removes_only_isolated_legacy_holdings(tmp_path):
    from test_p0_safety import portfolio

    path = tmp_path / "positions.json"
    path.write_text(json.dumps([{"ticker": "ABC", "entry_price": 100, "quantity": 2}]))
    pm = PositionManager(path)
    assert len(pm.get_open_records()) == 1
    assert not pm.broker_ready
    assert pm.reconcile_with_mcp(portfolio())
    assert not pm.get_open_records()
    assert not pm.snapshot().get("account_binding")
    assert path.with_suffix(".json.bak").exists()


def test_new_journal_context_can_be_created_in_empty_store(tmp_path):
    journal = JournalManager(tmp_path / "journals")
    assert journal.get_all_tickers() == []
    entry = JournalEntry(
        timestamp="2026-11-12T15:00:00+00:00",
        cycle_type="OPEN",
        decision="HOLD",
        key_metrics={"current_price": 100},
    )
    assert journal.append("ABC", entry)
    assert journal.get_recent("ABC") == [entry]
    assert "price=100.00" in journal.summarise_for_prompt("ABC")


def test_diagnostic_trade_logging_preserves_execution_ledger(tmp_path, monkeypatch):
    import utils.logger as module

    monkeypatch.setattr(module, "__file__", str(tmp_path / "utils/logger.py"))
    logs = tmp_path / "logs"
    logs.mkdir()
    ledger = logs / "trades.json"
    ledger.write_text("[]")
    loggers = [
        logging.getLogger("robinhood-agent"),
        logging.getLogger("robinhood-agent.trades"),
    ]
    previous = [
        (list(logger.handlers), logger.level, logger.propagate) for logger in loggers
    ]
    try:
        module.configure_logging()
        logging.getLogger("robinhood-agent.trades").info("Synthetic diagnostic event")
        assert ledger.read_text() == "[]"
        assert (
            json.loads((logs / "trade_events.jsonl").read_text())["message"]
            == "Synthetic diagnostic event"
        )
        assert all(
            handler.maxBytes == 10 * 1024 * 1024 and handler.backupCount == 5
            for logger in loggers
            for handler in logger.handlers
            if isinstance(handler, RotatingFileHandler)
        )
    finally:
        for logger, (handlers, level, propagate) in zip(loggers, previous):
            for handler in logger.handlers:
                handler.close()
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
