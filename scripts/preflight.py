"""Read-only offline release evidence; never authenticates or submits an order."""
import json,sys,importlib.util
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from utils.config import load_config
from core.position.manager import PositionManager
from utils.persistence import atomic_json


def preflight(root=None):
    root=Path(root) if root else Path(__file__).resolve().parents[1]
    config=load_config();pm=PositionManager(str(root/"logs/positions.json"),config)
    report={"python":sys.version.split()[0],"dry_run":config["execution"]["dry_run"],
        "discovery_enabled":config["discovery"]["enabled"],"live_release_approved":config["execution"]["live_release_approved"],
        "configuration_errors":config["configuration_errors"],"state_load_error":pm.load_error,
        "archived_positions_count_not_current_account":len(pm.get_open_records()),
        "sentiment_dependencies_installed":all(importlib.util.find_spec(m) for m in ("torch","transformers")),
        "live_ready":False,"requires_runtime_evidence":["fresh actual broker holdings/open orders and source quote timestamps",
            "exact-scope Anthropic dashboard reconciliation","operator review of release checklist and deployment restart/outage evidence",
            "explicit owner-controlled live activation"],
        "no_waiting_period":"No fixed calendar trial, minimum trade count, or automatic exposure ramp."}
    atomic_json(root/"logs/preflight.json",report)
    return report

if __name__=="__main__":print(json.dumps(preflight(),indent=2))
