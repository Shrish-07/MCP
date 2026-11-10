"""Read confirmed fills only. Unknown history/costs remain unknown."""

from __future__ import annotations

import json
import math
import os
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from utils.persistence import atomic_json


def export_events(pm):
    path = pm._path.parent / (
        "paper_exit_ledger.jsonl" if pm.mode == "paper" else "exit_ledger.jsonl"
    )
    existing = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            existing[row["event_id"]] = row
    with path.open("a", encoding="utf-8") as stream:
        for row in pm.snapshot()["events"]:
            if row["event_id"] in existing:
                if existing[row["event_id"]] != row:
                    raise ValueError("immutable event changed")
                continue
            stream.write(json.dumps(row, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _metrics(trades):
    values = [r["gross_realized_pnl"] for r in trades]
    net = [r["net_realized_pnl"] for r in trades if r["net_realized_pnl"] is not None]
    n = len(values)
    wins = sum(v > 0 for v in values)
    p = wins / n if n else None
    z = 1.96
    interval = None
    if n:
        center = (p + z * z / (2 * n)) / (1 + z * z / n)
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        interval = [center - half, center + half]
    return {
        "closed_positions": n,
        "known_cost_positions": len(net),
        "unknown_cost_positions": n - len(net),
        "gross_expectancy": statistics.mean(values) if values else None,
        "net_expectancy_known_cost_subset": statistics.mean(net) if net else None,
        "gross_win_rate": p,
        "win_rate_wilson_95pct": interval,
        "average_gross_win": statistics.mean([v for v in values if v > 0])
        if wins
        else None,
        "average_gross_loss": statistics.mean([v for v in values if v < 0])
        if any(v < 0 for v in values)
        else None,
        "gross_expectancy_approx_95pct": [
            statistics.mean(values) - z * statistics.stdev(values) / math.sqrt(n),
            statistics.mean(values) + z * statistics.stdev(values) / math.sqrt(n),
        ]
        if n >= 2
        else None,
    }


def build_report(pm, *, day=None, quotes=None, equity=None):
    day = day or datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    state = pm.snapshot()
    export_events(pm)
    fills = list(state["fills"].values())
    by_position = defaultdict(list)
    for fill in fills:
        by_position[fill["position_id"]].append(fill)
    trades = []
    for pid, events in by_position.items():
        sells = [e for e in events if e["side"] == "sell"]
        final = next((e for e in reversed(sells) if e["position_closed"]), None)
        if not final:
            continue
        buys = [e for e in events if e["side"] == "buy"]
        fees_known = all(e.get("fees") is not None for e in events)
        gross = sum(e["realized_pnl"] for e in sells)
        first = buys[0] if buys else None
        evidence = first["decision"].get("evidence", {}) if first else {}
        trade = {
            "position_id": pid,
            "ticker": final["ticker"],
            "closed_at": final["timestamp"],
            "entry_at": first["timestamp"] if first else final.get("entry_date"),
            "entry_snapshot_id": final.get("entry_snapshot_id"),
            "fill_ids": [e["fill_id"] for e in events],
            "gross_realized_pnl": gross,
            "net_realized_pnl": gross - sum(e["fees"] for e in events)
            if fees_known
            else None,
            "fees_status": "known" if fees_known else "unknown",
            "slippage_dollars": sum(e["slippage"] for e in events),
            "mfe_pct": (final["peak_price"] / final["initial_entry_price"] - 1) * 100
            if final["history_complete"] and final.get("initial_entry_price")
            else None,
            "mae_pct": (final["low_price"] / final["initial_entry_price"] - 1) * 100
            if final["history_complete"] and final.get("low_price") and final.get("initial_entry_price")
            else None,
            "history_complete": final["history_complete"],
            "lot_excursions":[{"lot_id":lot["lot_id"],"mfe_pct":(lot["peak_price"]/lot["price"]-1)*100,
                "mae_pct":(lot["low_price"]/lot["price"]-1)*100} for lot in final.get("entry_lots",[]) if lot.get("peak_price") and lot.get("low_price")],
            "exit_reason": final.get("reason"),
            "entry_archetype": evidence.get("archetype"),
            "gap_decile": evidence.get("gap_decile"),
            "entry_features": evidence,
            "adds": sum(
                e["decision"].get("decision") == "ADD_TO_POSITION" for e in buys
            ),
        }
        if trade["entry_at"]:
            start, end = (
                datetime.fromisoformat(trade["entry_at"]),
                datetime.fromisoformat(trade["closed_at"]),
            )
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            trade["holding_calendar_days"] = (end - start).total_seconds() / 86400
            from core.exits.triggers import trading_sessions
            trade["holding_trading_sessions"]=trading_sessions(start.isoformat(),end)
        trades.append(trade)
    today_closed = [
        t
        for t in trades
        if datetime.fromisoformat(t["closed_at"])
        .astimezone(ZoneInfo("America/New_York"))
        .date()
        .isoformat()
        == day
    ]
    groups = {}
    for field in ("entry_archetype", "gap_decile", "exit_reason"):
        grouped = defaultdict(list)
        for trade in trades:
            grouped[str(trade.get(field))].append(trade)
        groups[field] = {key: _metrics(rows) for key, rows in grouped.items()}
    exposure = (
        sum(
            r.quantity * float(getattr((quotes or {}).get(r.ticker), "price", 0) or 0)
            for r in pm.get_open_records()
        )
        if quotes and all(r.ticker in quotes for r in pm.get_open_records())
        else None
    )
    history=state.get("equity_history",[])
    peak=0.;drawdown=0.
    for mark in history:
        peak=max(peak,mark["equity"])
        drawdown=max(drawdown,1-mark["equity"]/peak)
    day_fills=[e for e in fills if datetime.fromisoformat(e["timestamp"]).astimezone(ZoneInfo("America/New_York")).date().isoformat()==day]
    report = {
        "schema_version": 2,
        "mode": pm.mode,
        "day_et": day,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": _metrics(trades),
        "closed_positions": trades,
        "today_closed_positions": today_closed,
        "confirmed_fill_count": len(fills),
        "active_orders": [
            o
            for o in state["orders"].values()
            if o["state"] not in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
        ],
        "gross_realized_fill_pnl": sum(e.get("realized_pnl", 0) for e in fills),
        "by_entry_and_exit": groups,
        "exposure_dollars": exposure,
        "turnover_fraction": sum(e["price"] * e["quantity"] for e in day_fills) / equity
        if equity and equity > 0
        else None,
        "drawdown": drawdown if len(history)>=2 else None,
        "drawdown_basis":"observed broker account equity; unadjusted for deposits/withdrawals; incomplete history",
        "exposure_fraction":exposure/equity if exposure is not None and equity and equity>0 else None,
        "marked_open_pnl":sum((quotes[r.ticker].price-r.entry_price)*r.quantity for r in pm.get_open_records()) if quotes and all(r.ticker in quotes for r in pm.get_open_records()) else None,
        "benchmark_relative_performance": None,
        "data_quality": {
            "legacy_history": "incomplete unless confirmed fill and valid marks exist",
            "drawdown": "requires complete equity history",
            "benchmark": "requires aligned benchmark and cash-flow history",
        },
        "interpretation": "Available sample and uncertainty only; profitability and discovery lift are unestablished.",
    }
    prefix = "paper_" if pm.mode == "paper" else ""
    atomic_json(pm._path.parent / f"{prefix}attribution_report_{day}.json", report)
    return report
