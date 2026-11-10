"""Build PEAD observations from timestamped earnings and adjusted-close exports."""

from datetime import datetime, timedelta, timezone
import math
from zoneinfo import ZoneInfo

from models.portfolio import PositionRecord
from utils.market_calendar import _NYSE


def _instant(value):
    stamp = datetime.fromisoformat(value)
    if not stamp.tzinfo:
        raise ValueError("timestamps must include a timezone")
    return stamp.astimezone(timezone.utc)


def build_pead_snapshot(payload, *, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = _instant(payload["data_cutoff"])
    if cutoff > now or _NYSE is None:
        raise ValueError("unusable cutoff or exchange calendar")
    source = payload["source"]
    if not isinstance(source, str) or not source.strip():
        raise ValueError("provider provenance is required")
    current, history, seen = {}, [], set()
    rejected = 0
    for event in payload["events"]:
        try:
            release = _instant(event["release_timestamp"])
            consensus_at = _instant(event["consensus_timestamp"])
            if not consensus_at < release <= cutoff:
                raise ValueError("consensus must predate the release")
            ticker = PositionRecord.valid_ticker(event["ticker"])
            actual, consensus = (
                float(event["actual_eps"]),
                float(event["consensus_eps"]),
            )
            if (
                not all(map(math.isfinite, (actual, consensus)))
                or abs(consensus) < 1e-6
            ):
                raise ValueError("invalid EPS")
            event_id = f"{ticker}:{release.isoformat()}"
            if event_id in seen:
                raise ValueError("duplicate earnings event")
            exchange_zone = ZoneInfo("America/New_York")
            release_day = release.astimezone(exchange_zone).date()
            schedule = _NYSE.schedule(
                start_date=release_day - timedelta(days=10),
                end_date=min(
                    cutoff.astimezone(exchange_zone).date(),
                    release_day + timedelta(days=45),
                ),
            )
            closes = [
                (idx.date().isoformat(), row["market_close"].to_pydatetime())
                for idx, row in schedule.iterrows()
            ]
            before = [day for day, stamp in closes if stamp < release]
            completed = [
                (day, stamp) for day, stamp in closes if release < stamp <= cutoff
            ]
            after = completed[:15]
            if not before or not after:
                raise ValueError("no completed benchmark window")
            stock, benchmark = (
                event["stock_adjusted_close"],
                event["benchmark_adjusted_close"],
            )
            days = [before[-1]] + [day for day, _ in after]
            prices = [(float(stock[day]), float(benchmark[day])) for day in days]
            if any(
                not math.isfinite(value) or value <= 0
                for pair in prices
                for value in pair
            ):
                raise ValueError("invalid price or incomplete window")
            first_stock, first_benchmark = prices[0]
            curve = {
                str(i): stock_close / first_stock - benchmark_close / first_benchmark
                for i, (stock_close, benchmark_close) in enumerate(prices[1:], 1)
            }
            row = {
                "event_id": event_id,
                "ticker": ticker,
                "source": source,
                "release_timestamp": release.isoformat(),
                "actual_eps": actual,
                "consensus_eps": consensus,
                "surprise": (actual - consensus) / abs(consensus),
                "data_cutoff": cutoff.isoformat(),
                "age_sessions": len(after),
                "car_by_session": curve,
            }
            seen.add(event_id)
            if len(after) == 15:
                # With only export-level availability, do not assume it was
                # possible to use a retrospectively supplied observation earlier.
                available = _instant(event.get("available_at", cutoff.isoformat()))
                if after[-1][1] <= available <= cutoff:
                    history.append({**row, "available_at": available.isoformat()})
            if len(completed) <= 15 and (
                ticker not in current
                or row["release_timestamp"] > current[ticker]["release_timestamp"]
            ):
                current[ticker] = row
        except (ValueError, TypeError, KeyError, IndexError):
            rejected += 1
    return {
        "data_cutoff": cutoff.isoformat(),
        "source": source,
        "current": current,
        "history": history,
        "rejected_rows": rejected,
    }
