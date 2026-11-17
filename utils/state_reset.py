"""Explicit local strategy reset using a verified empty broker observation."""

import json
from pathlib import Path

from core.position.manager import ACTIVE, PositionManager, StateIntegrityError
from utils.instance_lock import InstanceLock

DATA_CONTEXT_PATTERNS = (
    "selected_universe.json*",
    "weekly_news_cache.json*",
    "seen_headlines.json*",
    "earnings_calendar.json*",
    "sentiment_history.json*",
    "discovery_shortlist*.json*",
    "discovery_features_latest.json*",
    "discovery_ranked_latest.json*",
    "features_*.json*",
    "catalyst_events.json*",
    "pead_events.json*",
)
LOG_CONTEXT_PATTERNS = (
    "performance.json*",
    "paper_performance.json*",
    "attribution_report_*.json*",
)


def _checked_files(base, patterns):
    files = set()
    for pattern in patterns:
        for path in base.glob(pattern):
            if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
                raise StateIntegrityError("Reset store escapes its declared directory")
            if path.is_file():
                files.add(path)
    return sorted(files)


def reset_local_state(root, portfolio, broker_orders):
    root = Path(root).resolve()
    with InstanceLock(root / "logs/agent.lock"):
        pm = PositionManager(root / "logs/positions.json")
        state = pm.snapshot()
        if any(order["state"] in ACTIVE for order in state["orders"].values()):
            raise StateIntegrityError("Unresolved local intent prevents reset")
        backup = pm._path.with_suffix(".json.bak")
        if backup.exists():
            old = json.loads(backup.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                for identifier, value in old.get("fills", {}).items():
                    if value != state["fills"].get(identifier):
                        raise StateIntegrityError(
                            "State backup contains unmatched execution history"
                        )
                for identifier, previous in old.get("orders", {}).items():
                    current = state["orders"].get(identifier)
                    if (
                        current is None
                        or previous.get("filled_quantity", 0)
                        > current["filled_quantity"]
                        or previous.get("broker_id")
                        and previous["broker_id"] != current["broker_id"]
                    ):
                        raise StateIntegrityError(
                            "State backup contains an unresolved order"
                        )
                if old.get("account_binding") and old["account_binding"] != state.get(
                    "account_binding"
                ):
                    raise StateIntegrityError(
                        "State backup has a different account binding"
                    )
            elif not isinstance(old, list):
                raise StateIntegrityError("Unknown state backup schema")
        journals = root / "journals"
        journals.mkdir(parents=True, exist_ok=True)
        journal_files = _checked_files(journals, ("*.json", "*.json.bak", "*.tmp"))
        records = 0
        for path in journal_files:
            if path.suffix == ".tmp":
                continue
            rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise StateIntegrityError("Unknown journal schema")
            records += len(rows)
        derived = _checked_files(root / "data", DATA_CONTEXT_PATTERNS)
        derived += _checked_files(root / "logs", LOG_CONTEXT_PATTERNS)
        # Feature snapshots are derived; static constituent/candidate inputs remain.
        derived += _checked_files(root / "data/features", ("*.json*",))
        removed_positions = pm.reset_strategy_context(portfolio, broker_orders)
        if backup.exists():
            backup.unlink()
        for path in journal_files + derived:
            path.unlink()
        if _checked_files(journals, ("*.json", "*.json.bak", "*.tmp")):
            raise StateIntegrityError("Journal context remains after reset")
        current = pm.snapshot()
        if current["positions"] or any(
            order["state"] in ACTIVE for order in current["orders"].values()
        ):
            raise StateIntegrityError("Active state remains after reset")
        return {
            "journal_files_removed": len(journal_files),
            "journal_records_removed": records,
            "legacy_positions_removed": removed_positions,
            "derived_strategy_files_removed": len(derived),
            "local_positions": 0,
            "journal_records_remaining": 0,
            "unresolved_local_intents": 0,
            "account_binding_matches": current["account_binding"]
            == portfolio.account_id,
            "canonical_orders_preserved": current["orders"] == state["orders"],
            "canonical_fills_preserved": current["fills"] == state["fills"],
            "prior_fill_prompt_context_excluded": set(current["fills"])
            <= set(current["journal_context_excluded_fill_ids"]),
        }
