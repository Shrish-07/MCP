"""Offline fill-aware price replay. Uses a separate paper ledger exclusively."""

from datetime import datetime
from math import inf
from models.decisions import TradeDecision, DecisionType, ExecutionResult
from models.market_data import EquityQuote
from core.position.manager import PositionManager
from core.exits.trail_manager import update_trail
from core.exits.exit_state import graduate
from core.attribution import build_report


def replay(
    path,
    ticks,
    *,
    settings=None,
    quantity=10,
    spread_bps=10,
    slippage_bps=10,
    fill_capacity=inf,
    restart_at=None,
    entry_price=100,
):
    if not ticks or quantity <= 0 or fill_capacity <= 0:
        raise ValueError("nonempty price path and positive quantities required")
    settings = settings or {}
    pm = PositionManager(str(path), settings, mode="paper")
    key = pm.create_order(
        TradeDecision(
            ticker="REPLAY",
            decision=DecisionType.BUY,
            bull_thesis="Replay of existing selector entry",
        ),
        "buy",
        quantity,
        entry_price,
    )
    fill_px = entry_price * (1 + (spread_bps / 2 + slippage_bps) / 1e4)
    pm.update_order(
        key,
        ExecutionResult(
            ticker="REPLAY",
            order_id="paper-entry",
            status="FILLED",
            quantity=quantity,
            filled_quantity=quantity,
            fill_price=fill_px,
            dry_run=True,
            side="buy",
            timestamp=ticks[0][0],
            fees=0,
        ),
    )
    stops = []
    pending = None
    for index, (stamp, mid) in enumerate(ticks):
        if restart_at == index:
            pm = PositionManager(str(path), settings, mode="paper")
        rec = next(iter(pm.get_open_records()), None)
        if not rec:
            break
        now = datetime.fromisoformat(stamp)
        quote = EquityQuote(
            ticker="REPLAY",
            price=mid,
            bid=mid * (1 - spread_bps / 2e4),
            ask=mid * (1 + spread_bps / 2e4),
            volume=1,
            prev_close=entry_price,
            timestamp=stamp,
        )
        crossed = update_trail(rec, quote, settings, now=now)
        graduate(rec, mid, settings)
        pm.update_record(rec)
        stops.append(rec.effective_stop_px)
        if crossed and pending is None:
            pending = pm.create_order(
                TradeDecision(
                    ticker="REPLAY",
                    decision=DecisionType.SELL,
                    reasoning_summary=f"HARD_STOP {rec.effective_stop_px}",
                ),
                "sell",
                rec.quantity,
                quote.bid,
            )
        if pending:
            order = pm.snapshot()["orders"][pending]
            delta = min(fill_capacity, order["quantity"] - order["filled_quantity"])
            px = quote.bid * (1 - slippage_bps / 1e4)
            cumulative = order["filled_quantity"] + delta
            avg = (order["filled_notional"] + px * delta) / cumulative
            pm.update_order(
                pending,
                ExecutionResult(
                    ticker="REPLAY",
                    order_id="paper-exit",
                    side="sell",
                    dry_run=True,
                    quantity=order["quantity"],
                    filled_quantity=cumulative,
                    fill_price=avg,
                    status="FILLED"
                    if cumulative >= order["quantity"]
                    else "PARTIALLY_FILLED",
                    timestamp=stamp,
                    fees=0,
                ),
            )
    report = build_report(pm)
    report["stop_path"] = stops
    return report, pm
