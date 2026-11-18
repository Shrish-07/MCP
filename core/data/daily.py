"""Completed exchange sessions and daily-bar indicators shared by research and exits."""

import itertools
import math
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def completed_bars(bars, now=None, *, require_fresh=True, trailing_only=False):
    """Daily bars labeled by session date; require every expected session.

    A daily bar is completed only after its NYSE close. No current intraday bar
    can enter ATR/trend features. Dates are session labels from source timestamps.
    """
    from utils.market_calendar import _NYSE

    if _NYSE is None:
        raise ValueError("exchange calendar unavailable")
    now = now or datetime.now(UTC)
    if not bars:
        raise ValueError("daily history missing")
    dated = []
    for bar in bars:
        stamp = datetime.fromisoformat(bar.timestamp)
        values = [bar.open, bar.high, bar.low, bar.close, bar.volume]
        if (
            not all(math.isfinite(v) for v in values)
            or min(values[:4]) <= 0
            or bar.volume < 0
            or bar.high < max(bar.open, bar.close, bar.low)
            or bar.low > min(bar.open, bar.close)
        ):
            raise ValueError("invalid OHLCV bar")
        dated.append((stamp.date(), bar))
    dated.sort(key=lambda item: item[0])
    if len({d for d, b in dated}) != len(dated):
        raise ValueError("duplicate daily session")
    schedule = _NYSE.schedule(
        start_date=dated[0][0],
        end_date=now.astimezone(ZoneInfo("America/New_York")).date(),
    )
    expected = [
        i.date()
        for i, row in schedule.iterrows()
        if row["market_close"].to_pydatetime() <= now
    ]
    allowed = set(expected)
    completed = [(d, b) for d, b in dated if d in allowed]
    if not completed:
        raise ValueError("no completed bars")
    if require_fresh and (not expected or completed[-1][0] != expected[-1]):
        raise ValueError("stale completed history")
    expected_in_range = {
        d for d in expected if completed[0][0] <= d <= completed[-1][0]
    }
    if {d for d, b in completed} != expected_in_range:
        if not trailing_only:
            raise ValueError("missing exchange sessions")
        present = dict(completed)
        suffix = []
        for day in reversed([d for d in expected if d <= completed[-1][0]]):
            if day not in present:
                break
            suffix.append((day, present[day]))
        completed = list(reversed(suffix))
    return [b for d, b in completed]


def wilder_atr(bars, period=14):
    if len(bars) < period + 1:
        return None
    tr = [
        max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
        for a, b in itertools.pairwise(bars)
    ]
    if not all(math.isfinite(v) and v >= 0 for v in tr):
        return None
    atr = sum(tr[:period]) / period
    for value in tr[period:]:
        atr = ((period - 1) * atr + value) / period
    return atr if atr > 0 else None
