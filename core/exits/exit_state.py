"""Durable one-time graduation and debounced advisory events."""

from datetime import datetime, timezone, timedelta
from uuid import uuid4


def queue_review(record, kind, evidence, *, cooldown_hours=24, now=None, rearm=False):
    now = now or datetime.now(timezone.utc)
    previous = record.review_events.get(kind)
    if previous:
        if previous["state"] in ("DUE", "IN_PROGRESS", "APPROVED", "SUBMITTED", "PARTIALLY_FILLED", "UNKNOWN_REQUIRES_RECONCILIATION"):
            return False
        if not rearm or now - datetime.fromisoformat(
            previous["created_at"]
        ) < timedelta(hours=cooldown_hours):
            return False
    event = {
        "id": str(uuid4()),
        "kind": kind,
        "state": "DUE",
        "created_at": now.isoformat(),
        "evidence": evidence,
    }
    record.review_events[kind] = event
    record.last_trigger_ids.append(event["id"])
    return True


def graduate(record, price, settings):
    threshold = settings.get("exits", {}).get("take_profit_pct", 0.20)
    if (
        record.take_profit_review_state == "NOT_DUE"
        and price + 1e-9 >= record.entry_price * (1 + threshold)
    ):
        record.take_profit_review_state = "DUE"
        record.exit_state = "GRADUATION_REVIEW_DUE"
        return queue_review(
            record,
            "GRADUATION",
            {"price": price, "entry": record.entry_price, "gain_pct": threshold},
        )
    return False


def finish_review(record, event_ids, decision, *, model_failed=False, now=None):
    now = now or datetime.now(timezone.utc)
    for event in record.review_events.values():
        if not isinstance(event, dict) or event.get("id") not in event_ids:
            continue
        event.update(
            state="RETRY" if model_failed else "APPROVED" if decision == "SELL" else "COMPLETED",
            decision=decision,
            finished_at=now.isoformat(),
        )
        if event["kind"] == "GRADUATION" and not model_failed and decision == "HOLD":
            record.take_profit_review_state = "COMPLETED"
            if decision == "HOLD":
                record.trail_mode = "TRAILING_AFTER_GRADUATION"
                record.exit_state = "TRAILING_AFTER_GRADUATION"
