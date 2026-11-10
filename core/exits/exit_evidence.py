"""Group due evidence into one single-ticker review."""

from datetime import datetime, timezone, timedelta


def due_reviews(record, cooldown_hours=24, now=None):
    now = now or datetime.now(timezone.utc)
    result = []
    for event in record.review_events.values():
        if not isinstance(event, dict) or "id" not in event:
            continue
        state = event.get("state")
        last = event.get("finished_at", event.get("started_at", event["created_at"]))
        if (
            state == "DUE"
            or state in ("RETRY", "IN_PROGRESS", "APPROVED")
            and now - datetime.fromisoformat(last) >= timedelta(hours=cooldown_hours)
        ):
            result.append(event)
    return result
