"""Validate reported earnings catalysts from the configured broker calendar."""

from datetime import date, datetime, timedelta, timezone
import math

from models.portfolio import PositionRecord
from utils.persistence import atomic_json


async def refresh_catalysts(client, path, symbols, *, now=None):
    now = now or datetime.now(timezone.utc)
    start = now.date() - timedelta(days=7)
    payload = await client.get_earnings_calendar_window(start.isoformat(), 8)
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("earnings calendar results unavailable")
    if data.get("next") or data.get("next_cursor") or data.get("has_more"):
        raise ValueError("earnings calendar response incomplete")
    catalysts = {}
    for row in data["results"]:
        if not isinstance(row, dict) or row.get("symbol") not in symbols:
            continue
        report, eps = row.get("report"), row.get("eps")
        if (
            not isinstance(report, dict)
            or report.get("verified") is not True
            or not isinstance(eps, dict)
        ):
            continue
        try:
            ticker = PositionRecord.valid_ticker(row["symbol"])
            released = date.fromisoformat(report["date"])
            actual = float(eps["actual"])
            estimate = float(eps["estimate"])
        except (ValueError, TypeError, KeyError):
            continue
        if not start <= released <= now.date() or not all(
            map(math.isfinite, (actual, estimate))
        ):
            continue
        if (
            ticker in catalysts
            and catalysts[ticker]["report_date"] >= released.isoformat()
        ):
            continue
        catalysts[ticker] = {
            "source": "robinhood_mcp.get_earnings_calendar",
            "confirmed": True,
            "timestamp": now.isoformat(),
            "timestamp_basis": "observation_time",
            "report_date": released.isoformat(),
            "report_timing": report.get("timing"),
            "eps_actual": actual,
            "eps_estimate": estimate,
        }
    atomic_json(path, catalysts)
    return catalysts
