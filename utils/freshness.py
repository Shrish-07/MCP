import math
from datetime import UTC, datetime


def fresh_timestamp(value, max_age_seconds, now=None):
    try:
        stamp = datetime.fromisoformat(str(value))
        if stamp.tzinfo is None:
            return False
        age = ((now or datetime.now(UTC)) - stamp).total_seconds()
        return math.isfinite(age) and -5 <= age <= max_age_seconds
    except (ValueError, TypeError):
        return False


def fresh_quote(value, settings, now=None):
    """One quote-age policy shared by research, protection and execution."""
    maximum = settings.get("execution", {}).get("quote_max_age_seconds", 60)
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, (int, float))
        or not math.isfinite(maximum)
        or maximum <= 0
    ):
        return False
    return fresh_timestamp(value, maximum, now)
