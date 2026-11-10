from datetime import datetime, timezone
import math


def fresh_timestamp(value, max_age_seconds, now=None):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return False
        age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
        return math.isfinite(age) and -5 <= age <= max_age_seconds
    except (ValueError, TypeError):
        return False
