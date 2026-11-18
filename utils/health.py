"""Process identity, heartbeat and reconciliation health for the local backend."""

import json
import logging
import math
import os
from datetime import UTC, datetime
from pathlib import Path

import psutil

from utils.freshness import fresh_timestamp
from utils.persistence import atomic_json


def monitor_max_age(interval_seconds):
    return max(90, 2 * interval_seconds)


def _process_matches(pid, created):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if not isinstance(created, (int, float)) or not math.isfinite(created):
        return False
    try:
        process = psutil.Process(pid)
        return (
            process.is_running()
            and process.status() != psutil.STATUS_ZOMBIE
            and abs(process.create_time() - created) < 0.01
        )
    except (psutil.Error, OSError, ValueError):
        return False


def inspect_health(path, *, now=None, market_open=None, process_probe=None):
    """Read health without touching state or assuming an old PID is still ours."""
    from utils.market_calendar import is_market_open_at

    def result(status, reason):
        return {"status": status, "healthy": status == "HEALTHY", "reason": reason}

    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return result("PROCESS_ABSENT", "No backend health state")
    except (OSError, ValueError):
        return result("STATE_RECONCILIATION_FAILED", "Unreadable health state")
    if not isinstance(state, dict) or any(
        not isinstance(row, dict) for row in state.values()
    ):
        return result("STATE_RECONCILIATION_FAILED", "Invalid health schema")
    process = state.get("process", {})
    probe = process_probe or _process_matches
    if process.get("phase") == "STOPPED" or not probe(
        process.get("pid"), process.get("created_at")
    ):
        return result(
            "PROCESS_ABSENT", "Recorded backend process is absent or has changed"
        )
    now = now or datetime.now(UTC)
    if not fresh_timestamp(state.get("heartbeat", {}).get("last_success"), 90, now):
        return result("PROCESS_STALE", "Backend heartbeat is missing or stale")
    broker = state.get("broker", {})
    if broker.get("error"):
        kind = broker.get("failure_kind", "BROKER_UNAVAILABLE")
        if kind not in {
            "AUTHENTICATION_FAILED",
            "STATE_RECONCILIATION_FAILED",
            "BROKER_UNAVAILABLE",
        }:
            kind = "BROKER_UNAVAILABLE"
        return result(kind, "Broker reconciliation has not recovered")
    if not broker.get("last_success"):
        return result(
            "STATE_RECONCILIATION_FAILED", "No successful broker reconciliation"
        )
    market_open = is_market_open_at(now) if market_open is None else market_open
    monitor = state.get("monitor", {})
    interval = monitor.get("interval_seconds", 15)
    if type(interval) is not int or interval < 5:
        return result("STATE_RECONCILIATION_FAILED", "Invalid monitor interval")
    if market_open and not fresh_timestamp(
        monitor.get("last_success"), monitor_max_age(interval), now
    ):
        return result("PROCESS_STALE", "Protection monitor is stale")
    if market_open and state.get("quotes", {}).get("error"):
        return result("MARKET_DATA_UNAVAILABLE", "Protection quotes are unavailable")
    return result(
        "HEALTHY", "Process identity, heartbeat and applicable safety checks passed"
    )


class Health:
    def __init__(self, path):
        self.path = Path(path)
        self.logger = logging.getLogger("robinhood-agent.health")
        try:
            self.state = (
                json.loads(self.path.read_text(encoding="utf-8"))
                if self.path.exists()
                else {}
            )
            if not isinstance(self.state, dict) or any(
                not isinstance(v, dict) for v in self.state.values()
            ):
                raise ValueError("invalid health state")
        except (OSError, ValueError) as exc:
            self.logger.error(
                "OPERATOR_ALERT health_state_unavailable type=%s", type(exc).__name__
            )
            self.state = {}

    def start_process(self):
        self.state["process"] = {
            "pid": os.getpid(),
            "created_at": psutil.Process().create_time(),
            "started_at": datetime.now(UTC).isoformat(),
            "phase": "RUNNING",
        }
        self.record("heartbeat", success=True)

    def stop_process(self):
        self.state.setdefault("process", {})["phase"] = "STOPPED"
        self.record("shutdown", success=True)

    def record(self, job, *, success=None, error=None, **details):
        now = datetime.now(UTC).isoformat()
        row = self.state.setdefault(job, {})
        row.update(last_attempt=now, **details)
        if success is True:
            row.update(
                last_success=now, error=None, failure_kind=None, consecutive_failures=0
            )
        elif success is False:
            label = (
                type(error).__name__ if isinstance(error, BaseException) else str(error)
            )
            row.update(
                error=label, consecutive_failures=row.get("consecutive_failures", 0) + 1
            )
            self.logger.error("OPERATOR_ALERT job=%s error=%s", job, label)
        try:
            atomic_json(self.path, self.state)
        except OSError as exc:
            self.logger.error(
                "OPERATOR_ALERT health_persist_failed type=%s", type(exc).__name__
            )

    def stale(self, job, seconds):
        return not fresh_timestamp(self.state.get(job, {}).get("last_success"), seconds)
