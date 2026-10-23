from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from models.decisions import DecisionType, ExecutionResult, TradeDecision
from models.portfolio import Portfolio, Position, PositionRecord
from models.market_data import EquityQuote


@dataclass
class ExitSignal:
    """Signal to exit an open position."""

    ticker: str
    reason: str
    current_price: float
    unrealized_pct: float
    quantity: float


def position_snapshot(record: PositionRecord, current_price: float) -> str:
    """One-line deterministic P&L block for prompts. No LLM."""
    entry = float(record.entry_price or 0.0)
    qty = float(record.quantity or 0.0)
    if entry <= 0 or current_price <= 0:
        return f"POSITION: ticker={getattr(record, 'ticker', '?')} entry=unknown current={current_price:.2f}"
    unreal = (current_price - entry) / entry
    try:
        days = (datetime.now(timezone.utc).date() - datetime.fromisoformat(str(record.entry_date)).date()).days
    except Exception:
        days = 0
    stop = float(getattr(record, "stop_loss_pct", None) or -0.07)
    tp = float(getattr(record, "take_profit_pct", None) or 0.20)
    stop_px = entry * (1.0 + stop)
    tp_px = entry * (1.0 + tp)
    dist_stop = (current_price - stop_px) / entry
    dist_tp = (tp_px - current_price) / entry
    return (
        f"POSITION: entry={entry:.2f} current={current_price:.2f} "
        f"unrealized={unreal*100:+.2f}% days_held={days} qty={qty:.6f} | "
        f"STOP={stop*100:.1f}% (px {stop_px:.2f}, dist {dist_stop*100:+.1f}%) "
        f"TP={tp*100:.1f}% (px {tp_px:.2f}, dist {dist_tp*100:+.1f}%)"
    )


class PositionManager:
    """Manage position entry/exit persistence and exit checks.

    Every threshold is read from config/settings.json (trading.*).
    Also owns the SHARED per-ticker asyncio lock registry used by BOTH the
    scheduled cycles and the price monitor, so two paths can never race the
    same holding.
    """

    DEFAULT_REVIEW_GAIN_PCT = 0.15  # fallback if trading.profit_review_pct absent
    DEFAULT_MAX_HOLDING_DAYS = 15
    DEFAULT_STOP_BANDS = [[0, -0.07], [30, -0.10], [60, -0.13], [90, -0.16]]
    DEFAULT_TAKE_PROFIT_PCT = 0.20
    DEFAULT_STOP_LOSS_PCT = -0.07

    def __init__(self, path: str | None = None, config: dict | None = None) -> None:
        self.logger = logging.getLogger("robinhood-agent.core.position.manager")
        self._path = Path(path) if path else Path(__file__).resolve().parent.parent.parent / "logs" / "positions.json"
        self._path.parent.mkdir(exist_ok=True)
        if not self._path.exists():
            self._path.write_text("[]", encoding="utf-8")

        trading = (config or {}).get("trading") or {}
        try:
            self._review_gain_pct = float(trading.get("profit_review_pct", self.DEFAULT_REVIEW_GAIN_PCT))
            self._max_holding_days = int(trading.get("max_holding_days", self.DEFAULT_MAX_HOLDING_DAYS))
            self._take_profit_pct = float(trading.get("take_profit_pct", self.DEFAULT_TAKE_PROFIT_PCT))
            bands = [[float(t), float(p)] for t, p in (trading.get("stop_loss_bands") or self.DEFAULT_STOP_BANDS)]
            self._stop_bands = sorted(bands, key=lambda row: row[0])
        except (TypeError, ValueError) as exc:
            self.logger.error("position_manager_config_invalid (%s) — using safe defaults", exc)
            self._review_gain_pct = self.DEFAULT_REVIEW_GAIN_PCT
            self._max_holding_days = self.DEFAULT_MAX_HOLDING_DAYS
            self._take_profit_pct = self.DEFAULT_TAKE_PROFIT_PCT
            self._stop_bands = list(self.DEFAULT_STOP_BANDS)

        # Shared per-ticker locks — ONE registry for cycles AND monitor.
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, ticker: str) -> asyncio.Lock:
        """Return the shared per-ticker lock (cycles + monitor both use it)."""
        key = ticker.upper()
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    # ------------------------------------------------------------------ #
    #  Live ↔ local reconciliation
    # ------------------------------------------------------------------ #

    def reconcile_with_mcp(self, portfolio: Portfolio) -> None:
        """Synchronise positions.json with live MCP holdings.

        SAFETY RULE: if the MCP portfolio is empty or None, do nothing.
        A single failed/empty pull must never wipe local records.
        Only remove a record when the MCP portfolio is non-empty AND
        that ticker is absent (or quantity <= 0) in the live response.
        """
        records = self.get_open_records()
        record_by_ticker: dict[str, PositionRecord] = {r.ticker: r for r in records}

        mcp_list = getattr(portfolio, "positions", None) or []
        if not mcp_list:
            self.logger.warning(
                "reconcile_skipped_empty_portfolio local_records=%d",
                len(records),
            )
            return

        mcp_positions: dict[str, Position] = {
            p.ticker: p for p in mcp_list if getattr(p, "ticker", None)
        }

        kept: list[PositionRecord] = []
        added: list[str] = []
        removed: list[str] = []
        qty_adjusted: list[str] = []

        # Added: broker shows a real holding we have no local record for at
        # all (e.g. a manual trade placed outside the bot, or a local record
        # lost to a crash before record_entry() ran). This is exactly the
        # "doesn't realize it has stocks it does have" failure mode.
        for ticker, mcp_pos in mcp_positions.items():
            if ticker not in record_by_ticker:
                if mcp_pos.quantity <= 0:
                    continue
                entry_price = mcp_pos.avg_cost if mcp_pos.avg_cost else mcp_pos.current_price
                new_record = PositionRecord(
                    ticker=ticker,
                    entry_price=entry_price,
                    entry_date=datetime.now(timezone.utc).date().isoformat(),
                    entry_edge_score=0.0,
                    stop_loss_pct=self.DEFAULT_STOP_LOSS_PCT,
                    take_profit_pct=self._take_profit_pct,
                    quantity=mcp_pos.quantity,
                    order_id="",
                    reconciled=True,  # not from our own fill — see PositionRecord.reconciled
                )
                kept.append(new_record)
                added.append(f"{ticker}(qty={mcp_pos.quantity:.6f},avg_cost={entry_price:.2f})")

        # Kept / removed for tickers we already track locally. Removing a
        # local record here means the broker no longer shows any quantity
        # for it — this is exactly the "thinks it has positions it doesn't
        # have" failure mode (e.g. a SELL was recorded locally but the
        # order never actually filled, or vice versa).
        for rec in records:
            mcp = mcp_positions.get(rec.ticker)
            if mcp is None or mcp.quantity <= 0:
                removed.append(rec.ticker)
                continue
            if abs(float(mcp.quantity) - float(rec.quantity or 0.0)) > 1e-6:
                qty_adjusted.append(f"{rec.ticker}({rec.quantity:.6f}->{mcp.quantity:.6f})")
            rec.quantity = mcp.quantity
            kept.append(rec)

        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps([r.model_dump(mode="json") for r in kept], indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as exc:
            self.logger.warning("reconcile_write_failed: %s", exc)
            return

        self.logger.info(
            "position_reconcile kept=%d added=%d removed=%d qty_adjusted=%d",
            len(kept) - len(added), len(added), len(removed), len(qty_adjusted),
        )
        # Loud, name-level logging for any real drift so it's auditable
        # instead of silently self-healing (or silently staying wrong).
        if added:
            self.logger.warning("position_reconcile_added_untracked_holdings %s", ", ".join(added))
        if removed:
            self.logger.warning("position_reconcile_removed_phantom_holdings %s", ", ".join(removed))
        if qty_adjusted:
            self.logger.warning("position_reconcile_quantity_drift %s", ", ".join(qty_adjusted))

    def _vol_adjusted_stop_loss(self, realized_vol_30d: float) -> float:
        """Widen the stop-loss for higher-volatility names instead of a
        flat stop that gets triggered by routine noise on growth stocks.

        Bands are config-driven (trading.stop_loss_bands: [[vol_floor, stop], ...]
        in ascending vol_floor order; the last band the vol clears wins).
        """
        try:
            vol = float(realized_vol_30d or 0.0)
        except (TypeError, ValueError):
            vol = 0.0
        stop = self.DEFAULT_STOP_LOSS_PCT
        for floor, band_stop in self._stop_bands:
            if vol >= floor:
                stop = band_stop
        return stop

    def record_entry(self, result: ExecutionResult, decision: TradeDecision, realized_vol_30d: float = 0.0) -> None:
        """Persist a position entry record for executed BUY/ROTATE orders.

        If a record for the same ticker already exists, it is MERGED with a
        weighted-average entry price and summed quantity (broker-style average
        cost; original entry_date kept) — never silently overwritten.
        """
        try:
            if decision.decision not in (DecisionType.BUY, DecisionType.ROTATE):
                return
        except Exception:
            pass

        try:
            entry_price = float(getattr(result, "fill_price", 0.0))
            quantity = float(getattr(result, "quantity", 0.0))
        except (TypeError, ValueError):
            entry_price = 0.0
            quantity = 0.0

        record = PositionRecord(
            ticker=decision.ticker,
            entry_price=entry_price,
            entry_date=datetime.now(timezone.utc).date().isoformat(),
            entry_edge_score=decision.edge_score if hasattr(decision, "edge_score") else 0.0,
            stop_loss_pct=self._vol_adjusted_stop_loss(realized_vol_30d),
            take_profit_pct=self._take_profit_pct,
            quantity=quantity,
            order_id=getattr(result, "order_id", "") or "",
        )

        # ---- weighted-average merge (defensive: overwrites corrupt P&L) ----
        entries = self.get_open_records()
        existing = next((item for item in entries if item.ticker == record.ticker), None)
        if existing is not None:
            old_qty = float(existing.quantity or 0.0)
            old_px = float(existing.entry_price or 0.0)
            total_qty = old_qty + record.quantity
            if total_qty > 0 and record.entry_price > 0:
                merged_px = (old_px * old_qty + record.entry_price * record.quantity) / total_qty
                self.logger.warning(
                    "record_entry_merge ticker=%s old_px=%.4f old_qty=%.6f new_px=%.4f new_qty=%.6f merged_px=%.4f merged_qty=%.6f",
                    record.ticker, old_px, old_qty, record.entry_price, record.quantity, merged_px, total_qty,
                )
                record.entry_price = merged_px
                record.quantity = total_qty
                # Keep the ORIGINAL entry date and the WIDER (looser) stop —
                # we never let a re-entry tighten an existing risk envelope.
                record.entry_date = existing.entry_date or record.entry_date
                record.stop_loss_pct = min(existing.stop_loss_pct, record.stop_loss_pct)
                record.take_profit_pct = max(existing.take_profit_pct, record.take_profit_pct)
                record.reconciled = existing.reconciled
                record.order_id = record.order_id or existing.order_id
                entries = [item for item in entries if item.ticker != record.ticker]

        self.logger.info(
            "record_entry ticker=%s entry_price=%.4f qty=%.6f vol_30d=%.1f stop_loss_pct=%.2f",
            record.ticker, record.entry_price, record.quantity,
            realized_vol_30d, record.stop_loss_pct,
        )
        entries.append(record)
        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps([item.model_dump(mode="json") for item in entries], indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except Exception as exc:
            self.logger.warning("position_write_failed: %s", exc)

    def record_exit(self, ticker: str) -> None:
        """Remove a ticker from the open-position log."""
        entries = [item for item in self.get_open_records() if item.ticker != ticker]
        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps([item.model_dump(mode="json") for item in entries], indent=2), encoding="utf-8")
            tmp.replace(self._path)
            self.logger.info("position_record_exit ticker=%s", ticker)
        except Exception as exc:
            self.logger.warning("position_write_failed: %s", exc)

    # ------------------------------------------------------------------ #
    #  performance.json — pure-python realized/unrealized writer (no LLM)
    # ------------------------------------------------------------------ #
    def _perf_path(self) -> Path:
        return self._path.parent / "performance.json"

    def record_realized_pnl(self, ticker: str, entry_price: float, exit_price: float, quantity: float) -> float:
        """Append a closed trade to performance.json and recompute totals.

        Pure-python math; no LLM, no extra API call. Atomic write.
        Returns the realized dollar P&L booked (0.0 if nothing was recorded).
        """
        try:
            entry = float(entry_price or 0.0)
            exit_px = float(exit_price or 0.0)
            qty = float(quantity or 0.0)
            if entry <= 0 or exit_px <= 0 or qty <= 0:
                self.logger.info("record_realized_pnl_skipped ticker=%s invalid entry/exit/qty", ticker)
                return 0.0
            realized = (exit_px - entry) * qty
            realized_pct = (exit_px - entry) / entry
        except Exception as exc:
            self.logger.warning("record_realized_pnl_math_failed ticker=%s: %s", ticker, exc)
            return 0.0

        path = self._perf_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        closed = data.get("closed_trades", [])
        closed.append({
            "ticker": ticker,
            "entry_price": entry,
            "exit_price": exit_px,
            "quantity": qty,
            "realized_pnl": realized,
            "realized_pct": realized_pct,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        })
        data["closed_trades"] = closed
        try:
            data["total_realized"] = sum(float(t.get("realized_pnl", 0.0)) for t in closed)
        except Exception:
            data["total_realized"] = 0.0
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(path)
            self.logger.info(
                "performance_updated ticker=%s realized_pnl=%.4f realized_pct=%.4f total_realized=%.4f",
                ticker, realized, realized_pct, data.get("total_realized", 0.0),
            )
        except Exception as exc:
            self.logger.warning("performance_write_failed ticker=%s: %s", ticker, exc)
        return realized

    def write_performance_snapshot(self, quotes: dict) -> None:
        """Write open-position unrealized marks from already-fetched quotes.

        No extra API call, no LLM. One atomic write. Called at end of run_cycle.
        `quotes` may be dict[str, EquityQuote] or dict[str, float].
        """
        path = self._perf_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        try:
            data.setdefault("closed_trades", [])
        except Exception:
            data["closed_trades"] = []

        open_marks = []
        for rec in self.get_open_records():
            q = quotes.get(rec.ticker)
            cur = 0.0
            try:
                cur = float(getattr(q, "price", q)) if q else 0.0
            except Exception:
                cur = 0.0
            if rec.entry_price > 0 and cur > 0:
                open_marks.append({
                    "ticker": rec.ticker,
                    "entry_price": rec.entry_price,
                    "current_price": cur,
                    "quantity": rec.quantity,
                    "unrealized_pnl": (cur - rec.entry_price) * rec.quantity,
                    "unrealized_pnl_pct": (cur - rec.entry_price) / rec.entry_price,
                })
        data["open_marks"] = open_marks
        try:
            data["open_unrealized_total"] = sum(float(m.get("unrealized_pnl", 0.0)) for m in open_marks)
        except Exception:
            data["open_unrealized_total"] = 0.0
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(path)
            self.logger.info("performance_snapshot_written open_marks=%d", len(open_marks))
        except Exception as exc:
            self.logger.warning("performance_snapshot_write_failed: %s", exc)

    # ------------------------------------------------------------------ #
    #  Compact per-ticker prompt context — positions.json is the single
    #  source of truth for WHICH tickers are held / entry / stop / tp.
    #  Each decision-engine call gets full detail on ONLY the ticker it is
    #  evaluating, plus a one-line name+P&L roster of every OTHER open
    #  position — never the full payload for all of them.
    # ------------------------------------------------------------------ #

    def open_positions_pnl_map(self, quotes: dict) -> dict[str, float]:
        """Compute {ticker: unrealized_pct} for every open record, using
        already-fetched live quotes. Compute ONCE per cycle (not per
        ticker call) and hand the resulting map to every decision-engine
        call that cycle — this is what keeps the per-call context cheap.

        `quotes` may be dict[str, EquityQuote] or dict[str, float].
        """
        pnl_map: dict[str, float] = {}
        for rec in self.get_open_records():
            q = quotes.get(rec.ticker) if quotes else None
            try:
                price = float(getattr(q, "price", q)) if q is not None else 0.0
            except Exception:
                price = 0.0
            if rec.entry_price > 0 and price > 0:
                pnl_map[rec.ticker] = (price - rec.entry_price) / rec.entry_price * 100.0
        return pnl_map

    @staticmethod
    def other_positions_line(pnl_map: dict[str, float], exclude_ticker: str, max_positions: int = 12) -> str:
        """One compact line naming every OTHER open position + its live P&L.

        This is the mechanism that lets a single-ticker call be "aware" of
        the rest of the book by name/P&L without re-sending each of those
        positions' full metrics/news/journal payload — that stays scoped
        to whichever ticker is actually being evaluated this call.
        """
        others = {t: p for t, p in pnl_map.items() if t != exclude_ticker}
        if not others:
            return f"OTHER OPEN POSITIONS: none (0/{max_positions} max)"
        parts = [f"{t} {p:+.1f}%" for t, p in others.items()]
        return f"OTHER OPEN POSITIONS ({len(others)}/{max_positions} max): " + ", ".join(parts)

    def get_open_records(self) -> List[PositionRecord]:
        """Return all persisted open positions."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return [PositionRecord.model_validate(item) for item in data]
        except Exception as exc:
            self.logger.warning("position_load_failed: %s", exc)
            return []

    def check_exits(self, portfolio: Portfolio, quotes: dict[str, EquityQuote]) -> List[ExitSignal]:
        """Check open records against current price and holding rules.

        Emits signals with reason types:
          - STOP_LOSS: forced exit (hard rule)
          - PROFIT_TARGET: soft take-profit when gain >= record.take_profit_pct
          - PROFIT_REVIEW: advisory review when gain exceeds REVIEW_GAIN_PCT
          - MAX_HOLDING_PERIOD: advisory when days held > configured max
        """
        signals: List[ExitSignal] = []
        for record in self.get_open_records():
            # Prefer live quote; fall back to MCP position price if present.
            quote = quotes.get(record.ticker) if quotes else None
            current_price = 0.0
            if quote is not None:
                try:
                    current_price = float(getattr(quote, "price", 0.0) or 0.0)
                except Exception:
                    current_price = 0.0
            mcp_pos = next((item for item in (getattr(portfolio, "positions", None) or []) if item.ticker == record.ticker), None)
            if current_price <= 0 and mcp_pos is not None:
                try:
                    current_price = float(getattr(mcp_pos, "current_price", 0.0) or 0.0)
                except Exception:
                    current_price = 0.0
            if current_price <= 0 or record.entry_price <= 0:
                continue

            unrealized_pct = (current_price - record.entry_price) / record.entry_price if record.entry_price else 0.0
            quantity = float(record.quantity or 0.0)
            if mcp_pos is not None and getattr(mcp_pos, "quantity", 0) > 0:
                quantity = float(mcp_pos.quantity)

            # Hard stop-loss exit
            if unrealized_pct <= record.stop_loss_pct:
                signals.append(ExitSignal(record.ticker, "STOP_LOSS", current_price, unrealized_pct, quantity))
                continue

            # Soft take-profit target (uses the per-record take_profit_pct, default 20%)
            if unrealized_pct >= record.take_profit_pct:
                signals.append(ExitSignal(record.ticker, "PROFIT_TARGET", current_price, unrealized_pct, quantity))
                continue

            # Soft profit review (advisory only) and max holding period advisory.
            # Both thresholds come from config — never hard-coded.
            profit_review = unrealized_pct >= self._review_gain_pct
            try:
                days_held = (datetime.now(timezone.utc).date() - datetime.fromisoformat(record.entry_date).date()).days
            except Exception:
                days_held = 0
            # Reconciled records (added from the broker, not from our own fill)
            # skip the max-hold advisory — their true entry date is unknown.
            max_holding = (not record.reconciled) and (days_held > self._max_holding_days)

            # Combine signals: if both conditions apply, emit a single combined signal.
            if profit_review and max_holding:
                signals.append(ExitSignal(record.ticker, "PROFIT_REVIEW+MAX_HOLDING_PERIOD", current_price, unrealized_pct, quantity))
            elif profit_review:
                signals.append(ExitSignal(record.ticker, "PROFIT_REVIEW", current_price, unrealized_pct, quantity))
            elif max_holding:
                signals.append(ExitSignal(record.ticker, "MAX_HOLDING_PERIOD", current_price, unrealized_pct, quantity))

        return signals