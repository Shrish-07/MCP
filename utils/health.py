"""Durable job status and operator-visible alerts; no model dependency."""

import logging
from datetime import datetime, timezone
from pathlib import Path
import json
from utils.persistence import atomic_json


class Health:
    def __init__(self, path):
        self.path = Path(path)
        self.logger = logging.getLogger("robinhood-agent.health")
        try:
            self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
            if not isinstance(self.state,dict) or any(not isinstance(v,dict) for v in self.state.values()):raise ValueError("invalid health state")
        except (OSError,ValueError) as exc:
            self.logger.error("OPERATOR_ALERT health_state_unavailable error=%s",exc)
            self.state={}

    def record(self, job, *, success=None, error=None, **details):
        now = datetime.now(timezone.utc).isoformat()
        row = self.state.setdefault(job, {})
        row.update(last_attempt=now, **details)
        if success is True:
            row.update(last_success=now, error=None, consecutive_failures=0)
        elif success is False:
            row.update(
                error=str(error),
                consecutive_failures=row.get("consecutive_failures", 0) + 1,
            )
            self.logger.error("OPERATOR_ALERT job=%s error=%s", job, error)
        try:atomic_json(self.path, self.state)
        except OSError as exc:self.logger.error("OPERATOR_ALERT health_persist_failed error=%s",exc)

    def stale(self, job, seconds):
        stamp = self.state.get(job, {}).get("last_success")
        from utils.freshness import fresh_timestamp
        return not fresh_timestamp(stamp,seconds)
