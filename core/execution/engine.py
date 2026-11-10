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
import math
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from contextlib import AsyncExitStack

from models.decisions import (
    SUCCESS_STATUSES,
    DecisionType,
    ExecutionResult,
    TradeDecision,
)
from models.portfolio import Portfolio, PositionRecord
from core.position.manager import PositionManager
from utils.persistence import atomic_json
from utils.market_calendar import is_market_open_at


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
        self.paper_manager = PositionManager(path=str(position_manager._path.parent / "paper_positions.json"),
                                             config=settings, mode="paper")

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
        held={r.ticker for r in self.position_manager.get_open_records()}
        held.update(o["ticker"] for o in self.position_manager.snapshot()["orders"].values()
                    if o["side"]=="buy" and o["state"] in ("CREATED","SUBMITTED","PARTIALLY_FILLED","UNKNOWN_REQUIRES_RECONCILIATION"))
        return len(held)

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
                               skip_position_cap: bool = False, allow_existing: bool = False) -> ExecutionResult | None:
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
        if not allow_existing and any(r.ticker == ticker for r in self.position_manager.get_open_records()):
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
        if not math.isfinite(dollar_volume) or dollar_volume<=0:
            return self._result(decision,"REJECTED_LIQUIDITY_UNKNOWN",dry_run,"buy")
        if dollar_volume < self._min_dollar_volume:
            return self._result(decision, "REJECTED_LOW_LIQUIDITY", dry_run, "buy")

        # G6: sizing must be possible — total_value AND price AND buying power.
        if not dry_run and decision.decision!=DecisionType.ADD_TO_POSITION:
            sector=decision.evidence.get("sector")
            if not sector or any(not p.sector or p.market_value<=0 for p in portfolio.positions if p.quantity>0):
                return self._result(decision,"REJECTED_SECTOR_EXPOSURE_UNKNOWN",False,"buy")
            fraction=sum(p.market_value for p in portfolio.positions if p.sector==sector)/portfolio.total_value if portfolio.total_value>0 else 1
            if fraction+self._max_position_pct>self.settings.get("trading",{}).get("sector_cap_pct",.3) and len(decision.portfolio_review.get("sector_review",""))<20:
                return self._result(decision,"REJECTED_SECTOR_REVIEW_REQUIRED",False,"buy")
        if not math.isfinite(portfolio.total_value) or float(portfolio.total_value or 0.0) <= 0:
            # Fail-safe: never size an order against an unknown account value.
            self.logger.error("reject_no_portfolio_value ticker=%s — total_value unavailable", ticker)
            return self._result(decision, "REJECTED_NO_PORTFOLIO_VALUE", dry_run, "buy")
        if float(current_price or 0.0) <= 0:
            return self._result(decision, "REJECTED_NO_PRICE", dry_run, "buy")
        if not math.isfinite(portfolio.buying_power) or float(portfolio.buying_power or 0.0) + extra_buying_power <= 0:
            return self._result(decision, "REJECTED_INSUFFICIENT_BUYING_POWER", dry_run, "buy")
        return None

    def daily_entry_count(self):
        from zoneinfo import ZoneInfo
        today=datetime.now(ZoneInfo("America/New_York")).date()
        count=0
        for o in self.position_manager.snapshot()["orders"].values():
            if o["side"]!="buy" or o.get("decision",{}).get("decision") not in ("BUY","ROTATE") or o["state"] in ("REJECTED","CANCELLED","EXPIRED"):continue
            if datetime.fromisoformat(o["timestamp"]).astimezone(ZoneInfo("America/New_York")).date()==today:count+=1
        return count

    # ------------------------------------------------------------------ #
    #  Main entry point
    # ------------------------------------------------------------------ #

    async def execute(self, decision: TradeDecision, portfolio: Portfolio, current_price: float,
                      dry_run: bool, vol_30d: float = 0.0, avg_volume_30d: float = 0.0,
                      realized_vol_30d: float = 0.0, quote_timestamp: str = "") -> ExecutionResult:
        # One lock order everywhere: global operation lock, then sorted tickers.
        # Existing cycle callers may already own a ticker lock; those callers
        # are removed during coordinated-backend integration.
        async with self.position_manager.operation_lock:
            async with AsyncExitStack() as stack:
                for ticker in sorted({decision.ticker, decision.rotate_from_ticker or decision.ticker}):
                    await stack.enter_async_context(self.position_manager.lock_for(ticker))
                result=await self._execute_locked(decision, portfolio, current_price, dry_run,
                    vol_30d, avg_volume_30d, realized_vol_30d, quote_timestamp)
                self._log_trade(decision,result,0)
                return result

    async def _execute_locked(self, decision, portfolio, current_price, dry_run,
                              vol_30d, avg_volume_30d, realized_vol_30d, quote_timestamp):
        """Execute or simulate a decision after validating all risk gates."""
        effective_dry_run = bool(self.settings.get("env", {}).get("DRY_RUN", True)) or bool(dry_run)

        try:
            # ---- No-op decisions ----
            if decision.decision in (DecisionType.IGNORE, DecisionType.HOLD):
                return self._result(decision, "SKIPPED", True)

            if self.position_manager.load_error:
                return self._result(decision, "REJECTED_STATE_INTEGRITY", effective_dry_run)
            if self.position_manager.conflicting_orders(decision):
                return self._result(decision, "REJECTED_PENDING_ORDER", effective_dry_run)
            if not math.isfinite(current_price) or current_price <= 0:
                return self._result(decision, "REJECTED_NO_PRICE", effective_dry_run)
            if not effective_dry_run:
                stop_file=self.position_manager._path.parent.parent/self.settings.get("execution",{}).get("manual_stop_file","data/STOP_TRADING")
                if stop_file.exists():return self._result(decision,"REJECTED_MANUAL_STOP",False)
                if not is_market_open_at():return self._result(decision,"REJECTED_MARKET_CLOSED",False)
                if self.settings.get("execution", {}).get("live_release_approved") is not True:
                    return self._result(decision, "REJECTED_LIVE_RELEASE_GATE", False)
                ready=(self.position_manager.broker_ready and self.position_manager.orders_reconciled) if decision.decision==DecisionType.SELL else self.position_manager.entries_ready
                if not ready:
                    return self._result(decision, "REJECTED_RECONCILIATION_REQUIRED", False)
                from utils.freshness import fresh_timestamp
                if not fresh_timestamp(quote_timestamp, 60):
                    return self._result(decision, "REJECTED_STALE_QUOTE", False)
                if decision.decision != DecisionType.SELL:
                    if not fresh_timestamp(decision.evidence.get("feature_timestamp"),5*86400):
                        return self._result(decision,"REJECTED_STALE_FEATURES",False)
                    if self.daily_entry_count()>=self.settings.get("trading",{}).get("max_new_entries_per_day",2) and decision.decision!=DecisionType.ADD_TO_POSITION and len(decision.portfolio_review.get("daily_entry_count_review",""))<20:
                        return self._result(decision,"REJECTED_ENTRY_REVIEW_REQUIRED",False)

            # ---- Edge bar (base threshold; entries add the per-cycle bonus) ----
            bar = self._edge_bar(decision)
            if decision.decision != DecisionType.SELL and float(decision.edge.total or 0.0) < bar:
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

            if decision.decision == DecisionType.ADD_TO_POSITION:
                return await self._add(decision,portfolio,current_price,effective_dry_run,vol_30d,avg_volume_30d)

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
        quantity = float(sell_record.quantity or 0.0)

        result = await self._submit(decision, "sell", quantity, current_price, dry_run)
        if result.filled_quantity:
            result.rotate_realized_pnl = (result.fill_price-sell_record.entry_price)*result.filled_quantity

        self._log_trade(decision, result, 0.0)

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
        if decision.evidence.get("earnings_window"):
            if len(decision.portfolio_review.get("earnings_review",""))<20:
                return self._result(decision,"REJECTED_EARNINGS_REVIEW_REQUIRED",dry_run,"buy")
            notches=self.settings.get("trading",{}).get("earnings_window_discount_notches",1)
            ladder=[row[1] for row in self._size_ladder]
            index=max(i for i,value in enumerate(ladder) if value<=base_pct)
            base_pct=ladder[max(0,index-notches)]
        vol_mult = self._vol_size_multiplier(vol_30d or realized_vol_30d)
        size_pct = min(base_pct * vol_mult, self._max_position_pct)
        notional = size_pct * float(portfolio.total_value)
        available = self._available_cash(portfolio) + extra_buying_power
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

        result = await self._submit(decision, "buy", quantity, current_price, dry_run, realized_vol_30d)

        self._log_trade(decision, result, size_pct)

        return result

    async def _submit(self, decision, side, quantity, price, dry_run, vol=0):
        if not math.isfinite(quantity) or quantity <= 0:
            return self._result(decision, "REJECTED_QUANTITY", dry_run, side)
        if dry_run:
            await self.client.review_equity_order(ticker=decision.ticker, side=side, quantity=quantity)
            # Preview is a decision record, never a fill in either holdings book.
            return self._result(decision, "WOULD_EXECUTE", True, side, quantity=quantity)
        pm = self.position_manager
        key = pm.create_order(decision, side, quantity, price, vol)
        placed = None
        try:
            placed = await self.client.place_equity_order(decision.ticker, side=side,
                quantity=quantity, order_type="market", dry_run=False)
            order = pm.update_order(key, placed)
            placed.status = order["state"]
            placed.quantity = quantity
            placed.filled_quantity = order["filled_quantity"]
            placed.fill_price = order["filled_notional"]/order["filled_quantity"] if order["filled_quantity"] else 0
            return placed
        except Exception:
            # A timeout may have accepted the order at the broker. Persist its
            # ambiguity and never submit a replacement without reconciliation.
            unresolved = self._result(decision, "UNKNOWN_REQUIRES_RECONCILIATION", False, side)
            if placed is not None:
                unresolved.order_id = placed.order_id
            pm.update_order(key, unresolved)
            return unresolved

    async def _add(self,decision,portfolio,price,dry_run,vol,volume):
        from utils.strategy_config import DEFAULTS
        from utils.freshness import fresh_timestamp
        cfg=self.settings.get("trading",{}).get("add_to_position",DEFAULTS["trading"]["add_to_position"])
        def reject(reason):return self._result(decision,"REJECTED_ADD_"+reason,dry_run,"buy")
        thesis=decision.add_thesis
        if not cfg.get("enabled",False):return reject("DISABLED")
        if thesis is None or not thesis.existing_position_confirmed or not thesis.thesis_still_valid:return reject("THESIS")
        rec=next((r for r in self.position_manager.get_open_records() if r.ticker==decision.ticker),None)
        if not self.position_manager.entries_ready or rec is None or not rec.broker_confirmed:return reject("UNVERIFIED_HOLDING")
        if rec.thesis_invalidated or rec.exit_state in ("EXIT_PENDING","RECONCILIATION_REQUIRED") or self.position_manager.pending(rec.ticker):return reject("EXIT_PRIORITY")
        if rec.trigger_memory.get("trend_break_active") or rec.trigger_memory.get("sentiment_reversal_active"):
            return reject("EXIT_PRIORITY")
        if not rec.thesis or thesis.original_thesis.strip()!=rec.thesis.strip():return reject("ORIGINAL_THESIS_MISMATCH")
        if not rec.invalidation or thesis.original_invalidation != rec.invalidation:
            return reject("ORIGINAL_INVALIDATION_MISMATCH")
        facts=decision.evidence
        if not set(thesis.evidence_ids)<=set(facts.get("evidence_ids",[])) or not facts.get("incremental_opportunity_confirmed"):
            return reject("UNSUPPORTED_EVIDENCE")
        if not fresh_timestamp(thesis.data_timestamps.get("quote"),60) or not fresh_timestamp(thesis.data_timestamps.get("features"),5*86400):return reject("STALE_DATA")
        if thesis.data_timestamps.get("quote")!=facts.get("quote_timestamp") or thesis.data_timestamps.get("features")!=facts.get("feature_timestamp"):
            return reject("TIMESTAMP_MISMATCH")
        if rec.effective_stop_px<=0 or rec.effective_stop_px>=price:return reject("STOP_CONFLICT")
        if rec.adds_count>=cfg["max_adds_per_position"]:return reject("COUNT")
        if rec.last_add_timestamp and (datetime.now(timezone.utc)-datetime.fromisoformat(rec.last_add_timestamp)).total_seconds()<cfg["cooldown_hours"]*3600:return reject("COOLDOWN")
        if decision.edge.total<max(self._edge_bar(decision),3.5):return reject("CONVICTION")
        blocked=await self._check_buy_gates(rec.ticker,price,vol,volume,0,portfolio,decision,dry_run,skip_position_cap=True,allow_existing=True)
        if blocked:return blocked
        increment=min(thesis.requested_increment_pct,cfg["max_increment_pct_of_position_value"])*rec.quantity*price
        cap=self._max_position_pct*portfolio.total_value-rec.quantity*price
        risk_qty=cfg["max_incremental_risk_fraction_of_equity"]*portfolio.total_value/(price-rec.effective_stop_px)
        qty=min(increment/price,cap/price,risk_qty,self._available_cash(portfolio)*.99/price)
        if not math.isfinite(qty) or qty<=0:return reject("RISK_CAP")
        sector=facts.get("sector")
        if not sector:return reject("UNKNOWN_SECTOR")
        if any(not p.sector for p in portfolio.positions):return reject("UNKNOWN_SECTOR_EXPOSURE")
        sector_value=sum(p.market_value for p in portfolio.positions if p.sector==sector)
        if (sector_value+qty*price)/portfolio.total_value>self.settings.get("trading",{}).get("sector_cap_pct",.3) and len(decision.portfolio_review.get("sector_review",""))<20:
            return reject("SECTOR_REVIEW_REQUIRED")
        decision.evidence["accepted_sizing"]={"quantity":qty,"incremental_risk":qty*(price-rec.effective_stop_px),
            "aggregate_risk":(rec.quantity+qty)*(price-rec.effective_stop_px),"stop":rec.effective_stop_px}
        return await self._submit(decision,"buy",qty,price,dry_run,vol)

    def _available_cash(self,portfolio):
        # Reserve unfilled buys. Confirmed fills after the account snapshot
        # also consume cash until the next authoritative account refresh.
        reserved=0
        state = self.position_manager.snapshot()
        for order in state["orders"].values():
            if order["side"]!="buy":continue
            if order["state"] in ("CREATED","SUBMITTED","PARTIALLY_FILLED","CANCEL_REQUESTED","UNKNOWN_REQUIRES_RECONCILIATION"):
                reserved+=(order["quantity"]-order["filled_quantity"])*order.get("reference_price",0)
        if portfolio.timestamp:
            reserved += sum(fill["price"] * fill["quantity"] for fill in state["fills"].values()
                            if fill["side"] == "buy" and fill["timestamp"] >= portfolio.timestamp)
        return max(0,portfolio.buying_power-reserved)

    async def reconcile_orders(self, updates=None):
        """Recover owned orders by stable broker ID. Missing/ambiguous IDs stay blocked."""
        pm = self.position_manager
        async with pm.operation_lock:
            pm.orders_reconciled = False
            try:
                if updates is None:
                    updates = await self.client.get_equity_orders()
                by_id = {u.order_id: u for u in updates}
                if len(by_id) != len(updates) or "" in by_id:
                    raise ValueError("duplicate/missing broker order IDs")
                state = pm.snapshot()
                known = {o["broker_id"] for o in state["orders"].values() if o["broker_id"]}
                complete = True
                for key, order in state["orders"].items():
                    if order["state"] not in ("FILLED", "CANCELLED", "REJECTED", "EXPIRED"):
                        update = by_id.get(order["broker_id"])
                        if update is None:
                            complete = False
                            pm.update_order(key,ExecutionResult(ticker=order["ticker"],order_id=order["broker_id"],
                                side=order["side"],status="UNKNOWN_REQUIRES_RECONCILIATION",dry_run=False))
                            continue
                        if order.get("external"):
                            external=pm.snapshot()
                            external_order=external["orders"][key]
                            external_order.update(state=update.status,filled_quantity=update.filled_quantity or 0,
                                filled_notional=(update.filled_quantity or 0)*update.fill_price,updated_at=update.timestamp)
                            pm._save(external)
                            continue
                        pm.update_order(key, update)
                # Adopt external open orders to reserve the ticker. Historical
                # fills are established by holdings reconciliation, not replayed.
                from core.position.manager import ACTIVE
                for update in updates:
                    if update.order_id not in known and update.status in ACTIVE:
                        state = pm.snapshot()
                        key = str(uuid4())
                        state["orders"][key] = {"local_id": key, "broker_id": update.order_id,
                            "ticker": update.ticker, "side": update.side, "quantity": update.quantity,
                            "filled_quantity": update.filled_quantity or 0, "filled_notional": (update.filled_quantity or 0)*update.fill_price,
                            "state": update.status, "decision": {}, "external": True,
                            "timestamp": update.timestamp, "mode": "live"}
                        pm._save(state)
                # A complete account order listing is distinct from unresolved
                # local submission ambiguity, which entries_ready also checks.
                pm.orders_reconciled = True
                if not complete:self.logger.error("OPERATOR_ALERT unresolved submission/order IDs; entries blocked")
                return True
            except Exception as exc:
                self.logger.error("order_reconciliation_failed error=%s", exc)
                return False

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
        if from_ticker and self.position_manager.pending(from_ticker):
            return self._result(decision, "REJECTED_PENDING_ORDER", dry_run)

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
            self.logger.warning("rotation_rejected reason=%s", reason)
            return self._result(decision, "REJECTED_INVALID_ROTATION", dry_run)

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
            from utils.freshness import fresh_timestamp
            quote = next((item for item in quotes if item.ticker == from_ticker), None)
            if quote and (dry_run or fresh_timestamp(quote.timestamp, 60)):
                sell_price = quote.bid if quote.bid > 0 else quote.price
        except Exception as exc:
            self.logger.error("rotate_quote_fetch_failed ticker=%s error=%s", from_ticker, exc)
        if not math.isfinite(sell_price) or sell_price <= 0:
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
        if not dry_run:
            state = self.position_manager.snapshot()
            rotation_id=str(uuid4())
            state["rotations"][rotation_id] = {"decision": decision.model_dump(mode="json"),
                "from_ticker": from_ticker, "to_ticker": decision.ticker, "sell_order_id": sell_result.order_id,
                "state": "SELL_FILLED" if sell_result.status in SUCCESS_STATUSES else "AWAITING_SELL_FILL",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "recovery": "Reconcile sell; require a fresh decision and risk checks before any deferred buy"}
            self.position_manager._save(state)

        if sell_result.status not in SUCCESS_STATUSES:
            self.logger.error(
                "rotate_aborted_sell_failed ticker=%s rotate_from=%s sell_status=%s — buy leg skipped",
                decision.ticker, from_ticker, sell_result.status,
            )
            result = self._result(decision, "ROTATE_SELL_PENDING" if sell_result.status in ("SUBMITTED", "PARTIALLY_FILLED", "UNKNOWN_REQUIRES_RECONCILIATION", "WOULD_EXECUTE") else "REJECTED_ROTATE_SELL_FAILED", dry_run, "sell",
                                  rotate_from_ticker=from_ticker)
            self._log_trade(decision, result, 0.0)
            return result

        # ---- 4. Leg 2: buy the new name with the freed capital ----
        freed = float(sell_result.fill_price or sell_price) * float(sell_result.quantity or 0.0)
        self.logger.info("rotate_leg2 ticker=%s rotate_from=%s freed=%.2f",
                         decision.ticker, from_ticker, freed)
        refreshed=await self.client.get_portfolio()
        if (refreshed.reconciliation_status != "SUCCESS" or not refreshed.positions_complete
            or not self.position_manager.account_matches(refreshed)
            or any(pos.ticker == from_ticker and pos.quantity > 0 for pos in refreshed.positions)):
            return self._result(decision,"ROTATE_CASH_RECONCILIATION_REQUIRED",dry_run)
        target_quotes=await self.client.get_equity_quotes([decision.ticker])
        target=next((q for q in target_quotes if q.ticker==decision.ticker),None)
        from utils.freshness import fresh_timestamp
        if target is None or not fresh_timestamp(target.timestamp,60):
            return self._result(decision,"ROTATE_TARGET_QUOTE_STALE",dry_run)
        current_price=target.ask if target.ask>0 else target.price
        buy_result = await self._buy_leg(
            decision, refreshed, current_price, dry_run,
            vol_30d, avg_volume_30d, realized_vol_30d,
            extra_buying_power=0, skip_position_cap=True,
        )
        state=self.position_manager.snapshot()
        state["rotations"][rotation_id].update(buy_order_id=buy_result.order_id,
            state="COMPLETED" if buy_result.status in SUCCESS_STATUSES else "BUY_PENDING_OR_FAILED",buy_status=buy_result.status)
        self.position_manager._save(state)
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
            if any(r.get("decision_id")==decision.decision_id and r.get("order_id")==result.order_id and r.get("status")==result.status for r in existing):return
            existing.append({
                "record_kind":"execution_result" if result.filled_quantity else "decision_or_submission",
                "decision_id":decision.decision_id,"order_id":result.order_id,"filled_quantity":result.filled_quantity,
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
            atomic_json(self._trades_path, existing)
        except Exception as exc:
            self.logger.warning("trade_logging_failed: %s", exc)
