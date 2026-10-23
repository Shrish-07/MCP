"""Execution engine — deterministic risk gates around every order.

The LLM proposes; this module disposes. All enforcement is plain Python:

  * Dry-run is the ONLY live/dry switch: config/settings.json → execution.dry_run
    (fail-safe: anything but an explicit `false` is treated as dry-run).
  * Edge bar: trading.edge_score_threshold + trading.cycle_edge_bonus[cycle]
    for entries (BUY/ROTATE). CLOSE carries the highest bar by default — an
    overnight position you cannot react to must be your strongest conviction,
    NOT a banned decision. Exits (SELL) clear only the base threshold.
  * Vol-targeted sizing (trading.vol_sizing): high-vol names get smaller
    positions paired with PositionManager's wider stops, keeping the maximum
    dollar-risk per trade roughly constant across volatility regimes.
    Anything past trading.extreme_vol_reject_30d is refused outright.
  * Liquidity floor: execution.min_dollar_volume (30d avg dollar volume).
  * Tradability gate: execution.tradability_fail_mode=closed — if the check
    ERRORS or returns nothing parseable, the trade is REJECTED_TRADABILITY_UNKNOWN.
  * ROTATE is a first-class two-leg decision: sell the old holding first,
    then buy the new one, with full P&L recording for both legs.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from models.decisions import (
    SUCCESS_STATUSES,
    DecisionType,
    ExecutionResult,
    TradeDecision,
)
from models.portfolio import Portfolio, PositionRecord
from core.position.manager import PositionManager


def _table(raw) -> list[list[float]]:
    """Coerce a config [[threshold, value], ...] table into sorted float rows."""
    rows = []
    for item in raw or []:
        try:
            rows.append([float(item[0]), float(item[1])])
        except (TypeError, ValueError, IndexError):
            continue
    rows.sort(key=lambda row: row[0])
    return rows or []


class ExecutionEngine:
    """Execution engine with dry-run enforcement and logging."""

    def __init__(self, client, position_manager: PositionManager, settings: dict) -> None:
        self.client = client
        self.position_manager = position_manager
        self.settings = settings

        t = settings.get("trading") or {}
        e = settings.get("execution") or {}
        self._edge_threshold = float(t.get("edge_score_threshold", 3.0))
        self._cycle_bonus = {str(k).upper(): float(v) for k, v in dict(t.get("cycle_edge_bonus") or {}).items()}
        self._max_positions = int(t.get("max_positions", 12))
        self._max_position_pct = float(t.get("max_position_pct", 0.08))
        self._size_ladder = _table(t.get("size_ladder")) or [[3.0, 0.03], [3.5, 0.04], [4.0, 0.06], [4.5, 0.08]]
        self._vol_sizing = _table(t.get("vol_sizing")) or [[30, 0.8], [60, 0.6], [90, 0.45]]
        self._extreme_vol = float(t.get("extreme_vol_reject_30d", 120.0))
        self._min_dollar_volume = float(e.get("min_dollar_volume", 75_000_000))
        self._fail_closed = str(e.get("tradability_fail_mode", "closed")).lower() == "closed"

        self.logger = logging.getLogger("robinhood-agent.core.execution")
        # trades.json lives next to positions.json — in production that's
        # logs/, and in tests it's whatever temp dir the harness supplies,
        # so unit tests can never pollute the real trade ledger.
        self._trades_path = Path(position_manager._path).parent / "trades.json"
        self._trades_path.parent.mkdir(exist_ok=True)
        if not self._trades_path.exists():
            self._trades_path.write_text("[]", encoding="utf-8")

    # ------------------------------------------------------------------ #
    #  Config-derived helpers
    # ------------------------------------------------------------------ #

    def _size_from_edge(self, total: float) -> float:
        """Map an edge total to a base position-size fraction (pre-vol scaling)."""
        try:
            total = float(total or 0.0)
        except (TypeError, ValueError):
            return 0.0
        chosen = 0.0
        for threshold, pct in self._size_ladder:
            if total >= threshold:
                chosen = pct
        return chosen

    def _vol_size_multiplier(self, vol_30d: float) -> float:
        """Vol-targeted size scaling: calm names trade full size, hot names shrink."""
        try:
            vol = float(vol_30d or 0.0)
        except (TypeError, ValueError):
            vol = 0.0
        if vol <= 0.0:
            return 1.0  # unknown vol → full table default (data, not panic, governs)
        multiplier = 1.0
        for threshold, mult in self._vol_sizing:
            if vol >= threshold:
                multiplier = mult
        return multiplier

    def _edge_bar(self, decision: TradeDecision) -> float:
        """The edge total this specific decision must clear."""
        if decision.decision in (DecisionType.BUY, DecisionType.ROTATE):
            bonus = self._cycle_bonus.get(str(decision.cycle_type or "").upper(), 0.0)
            return self._edge_threshold + bonus
        return self._edge_threshold

    def _get_open_position_count(self) -> int:
        """positions.json (via PositionManager) is the single source of truth."""
        return len(self.position_manager.get_open_records())

    def _result(self, decision: TradeDecision, status: str, dry_run: bool,
                side: str = "", fill_price: float = 0.0, quantity: float = 0.0,
                order_id: str = "", **rotation_meta) -> ExecutionResult:
        return ExecutionResult(
            ticker=decision.ticker, order_id=order_id, status=status,
            fill_price=float(fill_price or 0.0), quantity=float(quantity or 0.0),
            timestamp=datetime.now(timezone.utc).isoformat(),
            dry_run=dry_run, side=side, **rotation_meta,
        )

    # ------------------------------------------------------------------ #
    #  Entry gates (shared by BUY and the buy-leg of ROTATE)
    # ------------------------------------------------------------------ #

    async def _check_buy_gates(self, ticker: str, current_price: float,
                               vol_30d: float, avg_volume_30d: float,
                               extra_buying_power: float, portfolio: Portfolio,
                               decision: TradeDecision, dry_run: bool,
                               skip_position_cap: bool = False) -> ExecutionResult | None:
        """Run every entry gate. Returns an error ExecutionResult, or None if all pass."""
        # G1: extreme-volatility refusal — the only hard vol guard. Below the
        # refuse line, vol merely SHRINKS the position (see _vol_size_multiplier).
        v = float(vol_30d or 0.0)
        if v > 0 and v > self._extreme_vol:
            self.logger.warning("reject_extreme_volatility ticker=%s vol=%.1f cap=%.1f",
                                ticker, v, self._extreme_vol)
            return self._result(decision, "REJECTED_EXTREME_VOLATILITY", dry_run, "buy")

        # G2: duplicate entry guard — a held name is never re-bought as a
        # fresh entry (PositionManager merges qty/avg-cost if it ever slips past).
        if any(r.ticker == ticker for r in self.position_manager.get_open_records()):
            return self._result(decision, "REJECTED_DUPLICATE", dry_run, "buy")

        # G3: position-count cap (skipped for the buy leg of a rotation: the
        # sell leg already freed a slot — rotation is count-neutral).
        if not skip_position_cap and self._get_open_position_count() >= self._max_positions:
            return self._result(decision, "REJECTED_MAX_POSITIONS", dry_run, "buy")

        # G4: tradability — FAIL-CLOSED by design. An errored or unparseable
        # check must refuse the trade (the very failure mode this gate guards
        # is an API that can no longer answer correctly).
        try:
            tradability = await self.client.get_equity_tradability(ticker)
        except Exception as exc:
            self.logger.error("tradability_check_failed ticker=%s error=%s", ticker, exc)
            if self._fail_closed:
                return self._result(decision, "REJECTED_TRADABILITY_UNKNOWN", dry_run, "buy")
            tradability = {"tradeable": None}
        if not isinstance(tradability, dict) or "tradeable" not in tradability:
            if self._fail_closed:
                self.logger.error("tradability_unparseable ticker=%s raw=%s — refusing trade (fail-closed)",
                                  ticker, tradability)
                return self._result(decision, "REJECTED_TRADABILITY_UNKNOWN", dry_run, "buy")
        elif tradability.get("tradeable") is False:
            return self._result(decision, "REJECTED_UNTRADEABLE", dry_run, "buy")

        # G5: liquidity floor (30d average dollar volume).
        dollar_volume = float(avg_volume_30d or 0.0) * float(current_price or 0.0)
        if dollar_volume > 0 and dollar_volume < self._min_dollar_volume:
            return self._result(decision, "REJECTED_LOW_LIQUIDITY", dry_run, "buy")

        # G6: sizing must be possible — total_value AND price AND buying power.
        if float(portfolio.total_value or 0.0) <= 0:
            # Fail-safe: never size an order against an unknown account value.
            self.logger.error("reject_no_portfolio_value ticker=%s — total_value unavailable", ticker)
            return self._result(decision, "REJECTED_NO_PORTFOLIO_VALUE", dry_run, "buy")
        if float(current_price or 0.0) <= 0:
            return self._result(decision, "REJECTED_NO_PRICE", dry_run, "buy")
        if float(portfolio.buying_power or 0.0) + extra_buying_power <= 0:
            return self._result(decision, "REJECTED_INSUFFICIENT_BUYING_POWER", dry_run, "buy")
        return None

    # ------------------------------------------------------------------ #
    #  Main entry point
    # ------------------------------------------------------------------ #

    async def execute(self, decision: TradeDecision, portfolio: Portfolio, current_price: float,
                      dry_run: bool, vol_30d: float = 0.0, avg_volume_30d: float = 0.0,
                      realized_vol_30d: float = 0.0) -> ExecutionResult:
        """Execute or simulate a decision after validating all risk gates."""
        effective_dry_run = bool(self.settings.get("env", {}).get("DRY_RUN", True)) or bool(dry_run)

        try:
            # ---- No-op decisions ----
            if decision.decision in (DecisionType.IGNORE, DecisionType.HOLD):
                return self._result(decision, "SKIPPED", True)

            # ---- Edge bar (base threshold; entries add the per-cycle bonus) ----
            bar = self._edge_bar(decision)
            if float(decision.edge.total or 0.0) < bar:
                self.logger.info(
                    "reject_low_edge ticker=%s decision=%s edge=%.2f bar=%.2f cycle=%s",
                    decision.ticker, decision.decision.value, decision.edge.total, bar, decision.cycle_type,
                )
                return self._result(decision, "REJECTED_LOW_EDGE", effective_dry_run)

            # ---- Dispatch ----
            if decision.decision == DecisionType.SELL:
                sell_record = next(
                    (r for r in self.position_manager.get_open_records() if r.ticker == decision.ticker),
                    None,
                )
                if sell_record is None:
                    return self._result(decision, "REJECTED_NO_POSITION", effective_dry_run)
                return await self._sell_leg(decision, portfolio, current_price,
                                            effective_dry_run, sell_record)

            if decision.decision == DecisionType.BUY:
                return await self._buy_leg(decision, portfolio, current_price,
                                           effective_dry_run, vol_30d, avg_volume_30d,
                                           realized_vol_30d)

            if decision.decision == DecisionType.ROTATE:
                return await self._rotate(decision, portfolio, current_price,
                                          effective_dry_run, vol_30d, avg_volume_30d,
                                          realized_vol_30d)

            return self._result(decision, "SKIPPED", True)
        except Exception as exc:
            self.logger.warning("execution_failed ticker=%s error=%s", decision.ticker, exc)
            return self._result(decision, "REJECTED_EXCEPTION", effective_dry_run)

    # ------------------------------------------------------------------ #
    #  Sell leg (shared by SELL and the sell-leg of ROTATE)
    # ------------------------------------------------------------------ #

    async def _sell_leg(self, decision: TradeDecision, portfolio: Portfolio,
                        current_price: float, dry_run: bool,
                        sell_record: PositionRecord) -> ExecutionResult:
        """Sell the full position behind sell_record. Records realized P&L and
        exits the record on success. Never blocked by anything except reality."""
        ticker = decision.ticker
        quantity = float(sell_record.quantity or 0.0)

        if dry_run:
            preview = await self.client.review_equity_order(
                ticker=ticker, side="sell", quantity=quantity,
            )
            result = self._result(
                decision, "WOULD_EXECUTE", True, "sell",
                fill_price=current_price, quantity=quantity,
            )
        else:
            placed = await self.client.place_equity_order(
                ticker, side="sell", quantity=quantity, order_type="market", dry_run=False,
            )
            result = ExecutionResult(
                ticker=ticker,
                order_id=getattr(placed, "order_id", "") or "",
                status=getattr(placed, "status", "SUBMITTED") or "SUBMITTED",
                fill_price=getattr(placed, "fill_price", current_price) or current_price,
                quantity=getattr(placed, "quantity", quantity) or quantity,
                timestamp=datetime.now(timezone.utc).isoformat(),
                dry_run=False, side="sell",
            )

        self._log_trade(decision, result, 0.0)

        if result.status in SUCCESS_STATUSES:
            # Book the realized P&L BEFORE the record is dropped.
            realized = 0.0
            if sell_record.entry_price > 0 and result.fill_price > 0:
                realized = self.position_manager.record_realized_pnl(
                    ticker, sell_record.entry_price, result.fill_price,
                    quantity=result.quantity or quantity,
                )
            self.position_manager.record_exit(ticker)
            self.logger.info("sell_leg status=%s ticker=%s fill=%.4f realized_pnl=%.4f",
                             result.status, ticker, result.fill_price, realized)
            result.rotate_realized_pnl = float(realized or 0.0)
        return result

    # ------------------------------------------------------------------ #
    #  Buy leg (shared by BUY and the buy-leg of ROTATE)
    # ------------------------------------------------------------------ #

    async def _buy_leg(self, decision: TradeDecision, portfolio: Portfolio,
                       current_price: float, dry_run: bool, vol_30d: float,
                       avg_volume_30d: float, realized_vol_30d: float,
                       extra_buying_power: float = 0.0,
                       skip_position_cap: bool = False) -> ExecutionResult:
        """Validate every entry gate, then execute/simulate the buy with
        vol-targeted sizing."""
        ticker = decision.ticker
        blocked = await self._check_buy_gates(
            ticker, current_price, vol_30d, avg_volume_30d, extra_buying_power,
            portfolio, decision, dry_run, skip_position_cap=skip_position_cap,
        )
        if blocked is not None:
            self._log_trade(decision, blocked, 0.0)
            return blocked

        # ---- Vol-targeted, ladder-sized, capped allocation ----
        base_pct = self._size_from_edge(decision.edge.total)
        vol_mult = self._vol_size_multiplier(vol_30d or realized_vol_30d)
        size_pct = min(base_pct * vol_mult, self._max_position_pct)
        notional = size_pct * float(portfolio.total_value)
        available = float(portfolio.buying_power) + extra_buying_power
        if notional > available * 0.99:  # leave a 1% cash buffer to crowd out
            notional = available * 0.99  #       partial-fill rejections
        quantity = notional / float(current_price) if current_price > 0 else 0.0
        if quantity <= 0:
            result = self._result(decision, "REJECTED_INSUFFICIENT_BUYING_POWER", dry_run, "buy")
            self._log_trade(decision, result, size_pct)
            return result

        self.logger.info(
            "sizing ticker=%s edge=%.2f ladder=%.1f%% vol_mult=%.2f final=%.2f%% notional=%.2f qty=%.6f",
            ticker, decision.edge.total, base_pct * 100, vol_mult, size_pct * 100, notional, quantity,
        )

        if dry_run:
            preview = await self.client.review_equity_order(
                ticker=ticker, side="buy", quantity=quantity,
            )
            result = self._result(
                decision, "WOULD_EXECUTE", True, "buy",
                fill_price=float(preview.get("estimated_price", current_price) or current_price),
                quantity=quantity,
            )
        else:
            placed = await self.client.place_equity_order(
                ticker, side="buy", quantity=quantity, order_type="market", dry_run=False,
            )
            result = ExecutionResult(
                ticker=ticker,  # always inject — never trust the payload
                order_id=getattr(placed, "order_id", "") or "",
                status=getattr(placed, "status", "SUBMITTED") or "SUBMITTED",
                fill_price=getattr(placed, "fill_price", current_price) or current_price,
                quantity=getattr(placed, "quantity", quantity) or quantity,
                timestamp=datetime.now(timezone.utc).isoformat(),
                dry_run=False, side="buy",
            )

        self._log_trade(decision, result, size_pct)

        # Treat EXECUTED / SUBMITTED / WOULD_EXECUTE (dry-run) as ownership.
        if result.status in SUCCESS_STATUSES:
            self.position_manager.record_entry(result, decision, realized_vol_30d)
        return result

    # ------------------------------------------------------------------ #
    #  Two-leg ROTATE — sell the named old holding, then buy the new one
    # ------------------------------------------------------------------ #

    async def _rotate(self, decision: TradeDecision, portfolio: Portfolio,
                      current_price: float, dry_run: bool, vol_30d: float,
                      avg_volume_30d: float, realized_vol_30d: float) -> ExecutionResult:
        """Execute a ROTATE as two real legs with full bookkeeping.

        Order of operations (fail-safe):
          1. Validate rotate_from_ticker (must be HELD, must differ from target).
             Invalid target → degrade to a plain BUY (the model intent for the
             new name is preserved; nothing is sold).
          2. Pre-flight the NEW name's entry gates BEFORE touching the old
             position — we never sell into a blocked buy.
          3. Leg 1: sell the full old position at its live quote; record
             realized P&L + exit. If the sell does not fill, abort the buy.
          4. Leg 2: buy the new name with the freed capital. If the buy fails
             after the sell filled, the rotation degrades to a plain exit —
             cash sits; reconciliation sees the account correctly next cycle.
        """
        from_ticker = str(decision.rotate_from_ticker or "").strip().upper()

        # ---- 1. Validate the rotation target ----
        sell_record = None
        if from_ticker:
            sell_record = next(
                (r for r in self.position_manager.get_open_records()
                 if r.ticker.upper() == from_ticker), None,
            )
        if not from_ticker or from_ticker == decision.ticker.upper() or sell_record is None:
            reason = (
                "rotate_from_missing" if not from_ticker
                else "rotate_from_is_self" if from_ticker == decision.ticker.upper()
                else "rotate_from_not_held"
            )
            self.logger.warning(
                "rotate_downgraded_to_buy ticker=%s rotate_from=%s reason=%s",
                decision.ticker, from_ticker or "?", reason,
            )
            return await self._buy_leg(decision, portfolio, current_price, dry_run,
                                       vol_30d, avg_volume_30d, realized_vol_30d)

        # ---- 2. Pre-flight entry gates on the NEW name (position cap skipped:
        # rotation is count-neutral — the sell frees the slot it consumes) ----
        blocked = await self._check_buy_gates(
            decision.ticker, current_price, vol_30d, avg_volume_30d,
            extra_buying_power=float(sell_record.quantity) * float(sell_record.entry_price),
            portfolio=portfolio, decision=decision, dry_run=dry_run,
            skip_position_cap=True,
        )
        if blocked is not None:
            self._log_trade(decision, blocked, 0.0)
            return blocked

        # ---- 3. Leg 1: sell the old holding at a live price (never blind) ----
        sell_price = 0.0
        try:
            quotes = await self.client.get_equity_quotes([from_ticker])
            if quotes:
                sell_price = float(getattr(quotes[0], "price", 0.0) or 0.0)
        except Exception as exc:
            self.logger.error("rotate_quote_fetch_failed ticker=%s error=%s", from_ticker, exc)
        if sell_price <= 0:
            self.logger.error("rotate_aborted_no_price ticker=%s rotate_from=%s — refusing blind sell",
                              decision.ticker, from_ticker)
            result = self._result(decision, "REJECTED_ROTATE_NO_PRICE", dry_run, "sell")
            self._log_trade(decision, result, 0.0)
            return result

        sell_decision = TradeDecision(
            ticker=from_ticker,
            bull_thesis=decision.bull_thesis,
            bear_thesis=decision.bear_thesis,
            decision=DecisionType.SELL,
            edge=decision.edge,
            action_type="CLOSE",
            risk_notes=decision.risk_notes,
            reasoning_summary=f"ROTATE-OUT into {decision.ticker}: {decision.reasoning_summary}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            cycle_type=decision.cycle_type,
        )
        sell_result = await self._sell_leg(sell_decision, portfolio, sell_price, dry_run, sell_record)

        if sell_result.status not in SUCCESS_STATUSES:
            self.logger.error(
                "rotate_aborted_sell_failed ticker=%s rotate_from=%s sell_status=%s — buy leg skipped",
                decision.ticker, from_ticker, sell_result.status,
            )
            result = self._result(decision, "REJECTED_ROTATE_SELL_FAILED", dry_run, "sell",
                                  rotate_from_ticker=from_ticker)
            self._log_trade(decision, result, 0.0)
            return result

        # ---- 4. Leg 2: buy the new name with the freed capital ----
        freed = float(sell_result.fill_price or sell_price) * float(sell_result.quantity or 0.0)
        self.logger.info("rotate_leg2 ticker=%s rotate_from=%s freed=%.2f",
                         decision.ticker, from_ticker, freed)
        buy_result = await self._buy_leg(
            decision, portfolio, current_price, dry_run,
            vol_30d, avg_volume_30d, realized_vol_30d,
            extra_buying_power=freed, skip_position_cap=True,
        )
        buy_result.rotate_from_ticker = from_ticker
        buy_result.rotate_sell_price = float(sell_result.fill_price or sell_price)
        buy_result.rotate_sell_quantity = float(sell_result.quantity or 0.0)
        buy_result.rotate_realized_pnl = float(sell_result.rotate_realized_pnl or 0.0)

        if buy_result.status not in SUCCESS_STATUSES:
            self.logger.warning(
                "rotate_degraded_to_sell ticker=%s rotate_from=%s buy_status=%s — sold but buy failed; cash sits",
                decision.ticker, from_ticker, buy_result.status,
            )
        return buy_result

    # ------------------------------------------------------------------ #
    #  Trade logging
    # ------------------------------------------------------------------ #

    def _log_trade(self, decision: TradeDecision, result: ExecutionResult,
                   position_size_pct: float) -> None:
        """Append a trade result to logs/trades.json."""
        try:
            existing = json.loads(self._trades_path.read_text(encoding="utf-8"))
            existing.append({
                "timestamp": result.timestamp,
                "ticker": decision.ticker,
                "decision": decision.decision.value,
                "edge_score": decision.edge_score,
                "position_size_pct": position_size_pct,
                "action_type": decision.action_type,
                "status": result.status,
                "fill_price": result.fill_price,
                "quantity": result.quantity,
                "dry_run": result.dry_run,
                "side": result.side,
                "rotate_from_ticker": result.rotate_from_ticker,
                "rotate_realized_pnl": result.rotate_realized_pnl,
                "reasoning_summary": decision.reasoning_summary,
            })
            self._trades_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        except Exception as exc:
            self.logger.warning("trade_logging_failed: %s", exc)
