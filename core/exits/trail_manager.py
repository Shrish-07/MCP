"""Long-position stops: Wilder ATR-14, bid-based peaks and monotonic ratchets."""

import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from utils.freshness import fresh_timestamp


def completed_bars(bars, now=None, *, require_fresh=True):
    """Daily bars labeled by session date; require every expected session.

    A daily bar is completed only after its NYSE close. No current intraday bar
    can enter ATR/trend features. Dates are session labels from source timestamps.
    """
    from utils.market_calendar import _NYSE

    if _NYSE is None:
        raise ValueError("exchange calendar unavailable")
    now = now or datetime.now(timezone.utc)
    if not bars:
        raise ValueError("daily history missing")
    dated = []
    for bar in bars:
        stamp = datetime.fromisoformat(bar.timestamp.replace("Z", "+00:00"))
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
        raise ValueError("missing exchange sessions")
    return [b for d, b in completed]


def wilder_atr(bars, period=14):
    if len(bars) < period + 1:
        return None
    tr = [
        max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
        for a, b in zip(bars, bars[1:])
    ]
    if not all(math.isfinite(v) and v >= 0 for v in tr):
        return None
    atr = sum(tr[:period]) / period
    for value in tr[period:]:
        atr = ((period - 1) * atr + value) / period
    return atr if atr > 0 else None


def update_trail(record, quote, settings, *, now=None):
    now = now or datetime.now(timezone.utc)
    if not fresh_timestamp(quote.timestamp, 60, now):
        raise ValueError("stale/malformed quote timestamp")
    price = quote.bid if quote.bid > 0 else quote.price
    if not math.isfinite(price) or price <= 0:
        raise ValueError("invalid executable mark")
    if record.quote_timestamp and datetime.fromisoformat(
        quote.timestamp.replace("Z", "+00:00")
    ) < datetime.fromisoformat(record.quote_timestamp.replace("Z", "+00:00")):
        raise ValueError("out-of-order quote")
    if record.exit_state == "RECONCILIATION_REQUIRED":
        raise ValueError("corporate action/position reconciliation required")
    if price > record.peak_price:
        record.peak_price, record.peak_timestamp = price, quote.timestamp
    record.low_price = min(record.low_price, price) if record.low_price else price
    for lot in record.lots:
        lot["peak_price"]=max(lot.get("peak_price",lot["price"]),price)
        lot["low_price"]=min(lot.get("low_price",lot["price"]),price)
    record.quote_timestamp = quote.timestamp
    record.initial_stop_px = max(
        record.initial_stop_px, record.entry_price * (1 + record.stop_loss_pct)
    )
    candidate = record.initial_stop_px
    exits = settings.get("exits", {})
    if exits.get("enabled", True):
        if record.peak_price + 1e-9 >= record.entry_price * (
            1 + exits.get("breakeven_ratchet_pct", 0.05)
        ):
            candidate = max(
                candidate,
                record.entry_price * (1 + exits.get("breakeven_lock_gain_pct", 0.005)),
            )
            record.trail_mode, record.stop_reason = "PROFIT_LOCK", "PROFIT_LOCK"
        chandelier = exits.get("chandelier", {})
        atr_valid = False
        if (
            record.atr_date
            and record.atr14
            and math.isfinite(record.atr14)
            and record.atr14 > 0
        ):
            age = (now.date() - datetime.fromisoformat(record.atr_date).date()).days
            atr_valid = 0 <= age <= 5
        if (
            record.peak_price + 1e-9
            >= record.entry_price * (1 + chandelier.get("activate_at_pct", 0.10))
            and atr_valid
        ):
            candidate = max(
                candidate,
                record.peak_price - chandelier.get("atr_multiple", 3.0) * record.atr14,
            )
            record.trail_mode = (
                "TRAILING_AFTER_GRADUATION"
                if record.take_profit_review_state == "COMPLETED"
                else "CHANDELIER"
            )
            record.stop_reason = "CHANDELIER"
    record.trail_stop_px = max(record.trail_stop_px, candidate)
    record.effective_stop_px = max(
        record.effective_stop_px, record.initial_stop_px, record.trail_stop_px
    )
    if record.exit_state != "EXIT_PENDING":
        record.exit_state = (
            "OPEN_PROTECTED"
            if record.trail_mode != "INITIAL_STOP"
            else "OPEN_INITIAL_STOP"
        )
    return price <= record.effective_stop_px
