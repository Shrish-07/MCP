"""Long-position stops: Wilder ATR-14, bid-based peaks and monotonic ratchets."""

import math
from datetime import UTC, datetime

from utils.freshness import fresh_quote


def update_trail(record, quote, settings, *, now=None):
    now = now or datetime.now(UTC)
    if not fresh_quote(quote.timestamp, settings, now):
        raise ValueError("stale/malformed quote timestamp")
    price = executable_mark(quote)
    if not math.isfinite(price) or price <= 0:
        raise ValueError("invalid executable mark")
    if record.quote_timestamp and datetime.fromisoformat(
        quote.timestamp
    ) < datetime.fromisoformat(record.quote_timestamp):
        raise ValueError("out-of-order quote")
    if record.exit_state == "RECONCILIATION_REQUIRED":
        raise ValueError("corporate action/position reconciliation required")
    if price > record.peak_price:
        record.peak_price, record.peak_timestamp = price, quote.timestamp
    record.low_price = min(record.low_price, price) if record.low_price else price
    for lot in record.lots:
        lot["peak_price"] = max(lot.get("peak_price", lot["price"]), price)
        lot["low_price"] = min(lot.get("low_price", lot["price"]), price)
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


def executable_mark(quote):
    if (
        quote.ask > quote.bid > 0
        and quote.price > 0
        and (quote.ask - quote.bid) / quote.price > 0.02
    ):
        return quote.price
    return quote.bid if quote.bid > 0 else quote.price
