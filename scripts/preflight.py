"""Offline local startup validation. Never authenticates or submits orders."""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.position.manager import ACTIVE, PositionManager
from utils.config import load_config
from utils.persistence import atomic_json


def preflight(root=None):
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    config = load_config(include_secrets=False, root=root)
    # Invalid settings are still reported without passing them into state readers.
    if config["configuration_errors"]:
        return {
            "configuration_errors": config["configuration_errors"],
            "dry_run": True,
            "local_validation_passed": False,
            "live_ready": False,
        }
    manager = PositionManager(root / "logs/positions.json", config)
    state = manager.snapshot()
    dependencies = all(
        importlib.util.find_spec(name) is not None
        for name in ("torch", "transformers", "accelerate", "safetensors")
    )
    sentiment_enabled = (
        config["sentiment_selection"]["enabled"] or config["discovery"]["enabled"]
    )
    unresolved = sum(order["state"] in ACTIVE for order in state["orders"].values())
    report = {
        "python": sys.version.split()[0],
        "dry_run": config["execution"]["dry_run"],
        "discovery_enabled": config["discovery"]["enabled"],
        "legacy_sentiment_selection_enabled": config["sentiment_selection"]["enabled"],
        "live_release_approved": config["execution"]["live_release_approved"],
        "configuration_errors": config["configuration_errors"],
        "state_load_error": manager.load_error,
        "local_positions": len(manager.get_open_records()),
        "legacy_state_present": bool(state.get("legacy_migration")),
        "account_binding_present": bool(state.get("account_binding")),
        "unresolved_local_intents": unresolved,
        "sentiment_dependencies_installed": dependencies,
        "local_validation_passed": not config["configuration_errors"]
        and not manager.load_error
        and not unresolved
        and (not sentiment_enabled or dependencies),
        "live_ready": False,
        "provider_billing_reconciliation": "Pending after real usage; does not block local operation.",
        "requires_runtime_evidence": [
            "Broker reconciliation on startup",
            "Current process identity and heartbeat after startup",
            "Explicit owner-controlled live activation",
        ],
        "no_waiting_period": "No fixed trial period, minimum trade count or automatic exposure ramp.",
    }
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, help="Optionally save the nonsecret report"
    )
    args = parser.parse_args()
    result = preflight()
    if args.output:
        atomic_json(args.output, result, backup=False)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["local_validation_passed"] else 1)
