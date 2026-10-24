"""Real NYSE market-hours / holiday calendar for the trading agent.

Source of truth: `pandas_market_calendars` ships the authoritative NYSE
calendar — every holiday AND every early-close half-day, years into the
future, zero manual upkeep. Robindhood's MCP exposes no market-status
tool, so this replaces the old naive "Mon-Fri 09:30-16:00 ET" check.

Falls back to the naive weekday+time rule (with a loud warning) if the
library is unavailable, and never crashes — a calendar failure means
"assume closed" (fail-safe: we skip trading rather than trade blind).

Early closes (13:00 ET on half-days) are handled automatically because
the close time comes from the calendar, not a hard-coded 16:00.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone

import pytz

logger = logging.getLogger("robinhood-agent.utils.market_calendar")

try:
    import pandas_market_calendars as mcal  # type: ignore

    _NYSE = mcal.get_calendar("NYSE")
    _HAS_CALENDAR = True
except Exception as exc:  # ImportError or anything else
    _NYSE = None
    _HAS_CALENDAR = False
    logger.warning(
        "market_calendar_unavailable pandas_market_calendars import failed "
        "(%s) — falling back to naive weekday 09:30-16:00 ET rule",
        exc,
    )

_ET = pytz.timezone("America/New_York")


def _naive_open_check(now_et: datetime) -> bool:
    """Naive fallback: weekday + 09:30-16:00 ET (no holidays, no half-days)."""
    if now_et.weekday() >= 5:
        return False
    return time(9, 30) <= now_et.time() <= time(16, 0)


def is_market_open_at(now_utc: datetime | None = None) -> bool:
    """True if the NYSE is open for trading at the given moment (or now)."""
    try:
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        now_et = now_utc.astimezone(_ET)

        if not _HAS_CALENDAR:
            return _naive_open_check(now_et)

        today = now_et.date()
        schedule = _NYSE.schedule(start_date=today.isoformat(), end_date=today.isoformat())
        if schedule.empty:
            # Weekend or NYSE holiday — market is closed.
            return False

        row = schedule.iloc[0]
        open_at = row["market_open"].to_pydatetime().astimezone(timezone.utc)
        close_at = row["market_close"].to_pydatetime().astimezone(timezone.utc)
        return open_at <= now_utc <= close_at
    except Exception as exc:
        # FAIL-SAFE: if we cannot prove the market is open, refuse to trade.
        logger.error("market_calendar_error (fail-safe CLOSED): %s", exc)
        return False


def is_trading_day_today(now_utc: datetime | None = None) -> bool:
    """True if today (in ET) is an NYSE trading day at all."""
    try:
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        now_et = now_utc.astimezone(_ET)
        if not _HAS_CALENDAR:
            return now_et.weekday() < 5
        return not _NYSE.schedule(
            start_date=now_et.date().isoformat(), end_date=now_et.date().isoformat()
        ).empty
    except Exception as exc:
        logger.error("market_calendar_trading_day_error (fail-safe False): %s", exc)
        return False
