"""Single-worker broker mirror. Positions, orders and fills commit atomically."""
from __future__ import annotations

import copy
import json
import logging
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from models.portfolio import Portfolio, PositionRecord
from models.orders import OrderRecord
from utils.locks import ReentrantAsyncLock
from utils.persistence import atomic_json

ACTIVE = {"CREATED", "SUBMITTED", "PARTIALLY_FILLED", "CANCEL_REQUESTED", "UNKNOWN_REQUIRES_RECONCILIATION"}
TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class StateIntegrityError(RuntimeError):
    pass


@dataclass
class ExitSignal:
    ticker: str
    reason: str
    current_price: float
    unrealized_pct: float
    quantity: float


def position_snapshot(record, current_price):
    pnl = (current_price / record.entry_price - 1) * 100 if record.entry_price else 0
    return (f"POSITION: ticker={record.ticker} entry={record.entry_price:.2f} current={current_price:.2f} "
            f"unrealized={pnl:+.2f}% qty={record.quantity:.6f} entry_date="
            f"{record.entry_date if record.entry_date_known else 'unknown'} "
            f"peak={record.peak_price:.2f} effective_stop={record.effective_stop_px:.2f} "
            f"exit_state={record.exit_state} pending_order={record.pending_exit_order_id}")


class PositionManager:
    DEFAULT_REVIEW_GAIN_PCT = .15
    DEFAULT_MAX_HOLDING_DAYS = 15
    DEFAULT_STOP_BANDS = [[0, -.07], [30, -.10], [60, -.13], [90, -.16]]
    DEFAULT_TAKE_PROFIT_PCT = .20
    DEFAULT_STOP_LOSS_PCT = -.07

    def __init__(self, path=None, config=None, *, mode="live"):
        self.config, self.mode = config or {}, mode
        self.logger = logging.getLogger("robinhood-agent.positions")
        self._path = Path(path) if path else Path(__file__).resolve().parents[2] / "logs/positions.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._mutex = threading.RLock()
        self.operation_lock = ReentrantAsyncLock()
        self._locks = {}
        self.reconciliation_ok = False
        self.orders_reconciled = mode == "paper"
        self.load_error = None
        self.last_reconciliation = None
        trading = self.config.get("trading", {})
        self._stop_bands = sorted(trading.get("stop_loss_bands", self.DEFAULT_STOP_BANDS))
        self._review_gain_pct = float(trading.get("profit_review_pct", .15))
        self._max_holding_days = int(trading.get("max_holding_days", 15))
        self._take_profit_pct = float(trading.get("take_profit_pct", .20))
        if not self._path.exists():
            atomic_json(self._path, self._empty())
        self.get_open_records()

    def _empty(self):
        return {"schema_version": 2, "mode": self.mode, "positions": [], "orders": {},
                "fills": {}, "events": [], "reconciliations": [], "rotations": {}}

    def _load(self):
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                state = self._empty()
                state["positions"] = raw
                state["legacy_migration"] = True
            elif isinstance(raw, dict) and raw.get("schema_version") == 2 and raw.get("mode") == self.mode:
                state = raw
            else:
                raise ValueError("unknown schema or mode mismatch")
            if not all(isinstance(state.get(k), t) for k, t in
                       (("positions", list), ("orders", dict), ("fills", dict), ("events", list))):
                raise ValueError("invalid state envelope")
            for key,order in state["orders"].items():
                validated=OrderRecord.model_validate(order)
                if validated.local_id!=key or validated.mode!=self.mode:raise ValueError("order key/mode mismatch")
            return state
        except (OSError, ValueError, TypeError) as exc:
            self.load_error = str(exc)
            self.reconciliation_ok = False
            self.logger.error("state_load_failed: %s", exc)
            raise StateIntegrityError(str(exc)) from exc

    def _records(self, state):
        records, errors, seen = [], [], set()
        for row in state["positions"]:
            try:
                rec = PositionRecord.model_validate(row)
                if rec.ticker in seen or rec.quantity <= 0 or rec.entry_price <= 0:
                    raise ValueError("duplicate ticker or invalid cost/quantity")
                seen.add(rec.ticker)
                rec.peak_price = max(rec.peak_price, rec.entry_price)
                if not rec.initial_stop_px:
                    rec.initial_stop_px = rec.entry_price * (1 + rec.stop_loss_pct)
                rec.trail_stop_px = max(rec.trail_stop_px, rec.initial_stop_px)
                rec.effective_stop_px = max(rec.trail_stop_px, rec.initial_stop_px, rec.effective_stop_px)
                records.append(rec)
            except (ValueError, TypeError) as exc:
                errors.append(str(exc))
        if errors:
            self.load_error = "; ".join(errors)
            self.reconciliation_ok = False
            self.logger.error("malformed_position_rows count=%d", len(errors))
        return records, errors

    def _save(self, state):
        try:
            for event in state["events"]:
                event.setdefault("event_id", str(uuid4()))
                event.setdefault("timestamp", utcnow())
            atomic_json(self._path, state)
        except (OSError, ValueError) as exc:
            self.load_error = str(exc)
            self.reconciliation_ok = False
            raise StateIntegrityError("state persistence failed") from exc

    def snapshot(self):
        with self._mutex:
            return copy.deepcopy(self._load())

    def get_open_records(self):
        with self._mutex:
            try:
                return self._records(self._load())[0]
            except StateIntegrityError:
                return []  # load_error blocks all orders; this is NEVER persisted as empty

    def lock_for(self, ticker):
        return self._locks.setdefault(ticker.upper(), ReentrantAsyncLock())

    @property
    def broker_ready(self):
        if self.load_error or not self.reconciliation_ok:
            return False
        if self.mode == "paper":
            return True
        return self.last_reconciliation is not None and (
            datetime.now(timezone.utc) - datetime.fromisoformat(self.last_reconciliation)
        ).total_seconds() <= float(self.config.get("execution", {}).get("reconciliation_max_age_seconds", 120))

    @property
    def entries_ready(self):
        if not self.broker_ready or not self.orders_reconciled:
            return False
        return not any(o["state"] in ("CREATED","UNKNOWN_REQUIRES_RECONCILIATION") for o in self.snapshot()["orders"].values())

    def mark_unverified(self, reason):
        self.reconciliation_ok = False
        self.logger.error("broker_reconciliation_unverified reason=%s", reason)

    def account_matches(self, portfolio):
        pinned = self.snapshot().get("account_binding")
        return not pinned or pinned == portfolio.account_id

    def reconcile_with_mcp(self, portfolio):
        with self._mutex:
            if self.mode != "live":
                raise StateIntegrityError("broker reconciliation cannot replace paper state")
            if not isinstance(portfolio, Portfolio) or portfolio.reconciliation_status != "SUCCESS" or not portfolio.positions_complete:
                self.mark_unverified("failed/incomplete broker portfolio")
                return False
            state = self._load()
            if not self.account_matches(portfolio):
                self.mark_unverified("broker account differs from persisted binding")
                return False
            if portfolio.account_id:
                state["account_binding"] = portfolio.account_id
            records, errors = self._records(state)
            if errors:
                return False
            old = {r.ticker: r for r in records}
            kept, seen = [], set()
            for pos in portfolio.positions:
                if not math.isfinite(pos.quantity) or pos.quantity < 0 or pos.ticker in seen:
                    self.mark_unverified("invalid broker quantity/duplicate ticker")
                    return False
                seen.add(pos.ticker)
                if pos.quantity == 0:
                    continue
                if not math.isfinite(pos.avg_cost) or pos.avg_cost <= 0:
                    self.mark_unverified("unknown broker cost basis")
                    return False
                rec = old.get(pos.ticker)
                if rec is None:
                    rec = PositionRecord(ticker=pos.ticker, entry_price=pos.avg_cost, quantity=pos.quantity,
                                         entry_date="", entry_date_known=False, reconciled=True)
                elif abs(rec.quantity-pos.quantity) > 1e-8 or abs(rec.entry_price-pos.avg_cost) > .005:
                    state["events"].append({"type": "BROKER_DISCREPANCY", "ticker": pos.ticker,
                                            "before": rec.model_dump(), "broker": pos.model_dump(), "timestamp": utcnow()})
                    # Possible split: block price comparisons until a verified adjustment.
                    if abs(rec.quantity*rec.entry_price-pos.quantity*pos.avg_cost) < .01 and abs(rec.quantity-pos.quantity) > 1e-8:
                        rec.exit_state = "RECONCILIATION_REQUIRED"
                    rec.quantity, rec.entry_price = pos.quantity, pos.avg_cost
                    # The discrepancy event retains the old lot ledger for audit.
                    # Without matching broker executions those lots cannot explain this balance.
                    rec.lots = []
                    rec.history_complete = False
                rec.broker_confirmed, rec.verified_at = True, utcnow()
                rec.reconciled = not rec.entry_date_known
                rec.peak_price = max(rec.peak_price, rec.entry_price)
                rec.initial_stop_px = max(rec.initial_stop_px, rec.entry_price*(1+rec.stop_loss_pct))
                rec.effective_stop_px = max(rec.effective_stop_px, rec.initial_stop_px, rec.trail_stop_px)
                kept.append(rec.model_dump())
            for ticker in old.keys()-seen:
                state["events"].append({"type": "BROKER_POSITION_REMOVED", "ticker": ticker,
                    "position_id": old[ticker].position_id, "timestamp": utcnow(),
                    "reason": "broker reconciliation discrepancy; realized P&L unknown"})
            state["positions"] = kept
            state["reconciliations"].append({"timestamp": utcnow(), "tickers": sorted(seen), "status": "SUCCESS"})
            self._save(state)
            self.load_error = None
            self.last_reconciliation, self.reconciliation_ok = utcnow(), True
            return True

    def _vol_adjusted_stop_loss(self, vol):
        stop = -.07
        for floor, value in self._stop_bands:
            if vol >= floor:
                stop = value
        return stop

    def pending(self, ticker):
        return [o for o in self.snapshot()["orders"].values() if o["ticker"] == ticker and o["state"] in ACTIVE]

    def conflicting_orders(self, decision):
        pending = self.pending(decision.ticker)
        if decision.decision.value == "SELL" and decision.cycle_type == "MONITOR":
            return [order for order in pending if order["side"] != "buy"]
        return pending

    def create_order(self, decision, side, quantity, reference_price, vol=0):
        with self._mutex:
            state = self._load()
            if self.load_error or self.conflicting_orders(decision):
                raise StateIntegrityError("unsafe state or pending ticker order")
            key = str(uuid4())
            state["orders"][key] = {"local_id": key, "broker_id": "", "ticker": decision.ticker,
                "side": side, "quantity": quantity, "filled_quantity": 0.0, "filled_notional": 0.0,
                "state": "CREATED", "decision": decision.model_dump(mode="json"), "vol": vol,
                "reference_price": reference_price, "timestamp": utcnow(), "mode": self.mode}
            if side == "sell":
                for rec in state["positions"]:
                    if rec["ticker"] == decision.ticker:
                        rec["pending_exit_order_id"], rec["exit_state"] = key, "EXIT_PENDING"
                        if decision.cycle_type == "MONITOR":
                            for pending in state["orders"].values():
                                if pending["ticker"] == decision.ticker and pending["side"] == "buy" and pending["state"] in ACTIVE:
                                    pending["protective_floor"] = rec.get("effective_stop_px", 0)
            self._save(state)  # durable intent BEFORE network call
            return key

    def update_order(self, key, result):
        with self._mutex:
            state = self._load()
            order = state["orders"][key]
            if result.ticker != order["ticker"] or (result.side and result.side != order["side"]):
                raise StateIntegrityError("order identity mismatch")
            if order["broker_id"] and result.order_id and order["broker_id"] != result.order_id:
                raise StateIntegrityError("broker order ID changed")
            order["broker_id"] = result.order_id or order["broker_id"]
            status = {"EXECUTED": "FILLED", "COMPLETED": "FILLED", "CANCELED": "CANCELLED"}.get(result.status.upper(), result.status.upper())
            if status not in ACTIVE | TERMINAL:
                status = "UNKNOWN_REQUIRES_RECONCILIATION"
            if not order["broker_id"] and status not in ("CREATED","REJECTED"):
                status="UNKNOWN_REQUIRES_RECONCILIATION"
            if not order["broker_id"] or status == "UNKNOWN_REQUIRES_RECONCILIATION":
                result=result.model_copy(update={"fills":[],"filled_quantity":None})
            fills = list(result.fills)
            if not fills and result.filled_quantity is not None:
                cumulative = float(result.filled_quantity)
                if not math.isfinite(cumulative) or cumulative < order["filled_quantity"]-1e-8:
                    raise StateIntegrityError("nonmonotonic fill total")
                delta = cumulative-order["filled_quantity"]
                if delta > 1e-8:
                    notional = cumulative*result.fill_price-order["filled_notional"]
                    fills = [{"id": f"cumulative:{cumulative:.10f}", "quantity": delta,
                        "price": notional/delta, "fees": result.fees, "timestamp": result.timestamp or utcnow()}]
            for fill in fills:
                fill_id = str(fill.get("id", ""))
                unique = f"{order['broker_id'] or key}:{fill_id}"
                if not fill_id:
                    raise StateIntegrityError("fill missing stable ID")
                if unique in state["fills"]:
                    previous = state["fills"][unique]
                    if previous["quantity"] != float(fill["quantity"]) or previous["price"] != float(fill["price"]):
                        raise StateIntegrityError("execution ID was reused with different values")
                    continue
                qty, price = float(fill["quantity"]), float(fill["price"])
                if not all(math.isfinite(x) and x > 0 for x in (qty, price)) or qty+order["filled_quantity"] > order["quantity"]+1e-6:
                    raise StateIntegrityError("invalid/overfilled order")
                event = self._apply_fill(state, order, unique, qty, price, fill)
                state["fills"][unique] = event
                state["events"].append(event)
                order["filled_quantity"] += qty
                order["filled_notional"] += qty*price
            if result.fills and result.filled_quantity is not None and (
                not math.isfinite(result.filled_quantity)
                or abs(order["filled_quantity"] - result.filled_quantity) > 1e-6
            ):
                raise StateIntegrityError("execution rows disagree with cumulative fills")
            if status == "FILLED" and abs(order["filled_quantity"]-order["quantity"]) > 1e-6:
                status = "UNKNOWN_REQUIRES_RECONCILIATION"
            elif status != "UNKNOWN_REQUIRES_RECONCILIATION" and order["filled_quantity"] >= order["quantity"]-1e-6:
                status = "FILLED"
            elif order["filled_quantity"] > 0 and status in ("SUBMITTED", "PARTIALLY_FILLED"):
                status = "PARTIALLY_FILLED"
            if order["state"] in TERMINAL and status in ACTIVE:
                status = order["state"]
            order["state"], order["updated_at"] = status, utcnow()
            review_ids = order["decision"].get("evidence", {}).get("exit_review_ids", [])
            if review_ids and order["side"] == "sell":
                state["events"].append({"type": "EXIT_REVIEW_EXECUTION", "ticker": order["ticker"],
                    "order_id": key, "review_ids": review_ids, "status": status, "timestamp": utcnow()})
                for rec in state["positions"]:
                    if rec["ticker"] != order["ticker"]:
                        continue
                    for review in rec.get("review_events", {}).values():
                        if isinstance(review, dict) and review.get("id") in review_ids:
                            review.update(state="RETRY" if status in TERMINAL else status,
                                          execution_status=status, finished_at=utcnow())
            if status in TERMINAL:
                for rec in state["positions"]:
                    if rec.get("pending_exit_order_id") == key:
                        rec["pending_exit_order_id"] = None
                        rec["exit_state"] = "OPEN_PROTECTED" if rec.get("trail_mode") != "INITIAL_STOP" else "OPEN_INITIAL_STOP"
            self._save(state)
            return copy.deepcopy(order)

    def _apply_fill(self, state, order, fill_id, qty, price, fill):
        decision = order["decision"]
        recs, errors = self._records(state)
        if errors:
            raise StateIntegrityError("cannot book fill over corrupt positions")
        rec = next((r for r in recs if r.ticker == order["ticker"]), None)
        stamp = fill.get("timestamp") or utcnow()
        event = {"event_id": fill_id, "type": "FILL", "mode": self.mode, "ticker": order["ticker"],
            "order_id": order["broker_id"], "local_order_id": order["local_id"], "fill_id": fill_id,
            "decision_id": decision.get("decision_id"), "side": order["side"], "quantity": qty,
            "price": price, "timestamp": stamp, "fees": fill.get("fees"), "decision": decision,
            "reference_price": order["reference_price"],
            "slippage": (price-order["reference_price"])*qty*(1 if order["side"] == "buy" else -1)}
        if order["side"] == "buy":
            if rec is None:
                stop = self._vol_adjusted_stop_loss(order["vol"])
                rec = PositionRecord(ticker=order["ticker"], entry_price=price, quantity=qty,
                    initial_entry_price=price,
                    entry_date=stamp, entry_date_known=True, order_id=order["broker_id"],
                    peak_price=price, low_price=price, peak_timestamp=stamp, peak_reconstructed=False,
                    history_complete=True, initial_stop_px=price*(1+stop), trail_stop_px=price*(1+stop),
                    effective_stop_px=price*(1+stop), stop_loss_pct=stop,
                    broker_confirmed=self.mode == "live", verified_at=stamp,
                    entry_snapshot_id=fill_id, thesis=decision.get("bull_thesis", ""),
                    invalidation=decision.get("failure_conditions", []))
                recs.append(rec)
                if order.get("protective_floor"):
                    rec.trail_stop_px = max(rec.trail_stop_px, order["protective_floor"])
                    rec.effective_stop_px = max(rec.effective_stop_px, rec.trail_stop_px)
                    rec.thesis_invalidated = True
                evidence = decision.get("evidence", {})
                rec.entry_gap_score = evidence.get("raw_gap_score")
                rec.entry_gap_z = evidence.get("gap_z")
                rec.gap_reference = evidence.get("gap_reference")
                rec.gap_progress = 0.0 if rec.entry_gap_z is not None and abs(rec.entry_gap_z) > 1e-6 else None
            else:
                rec.entry_price = (rec.entry_price*rec.quantity+price*qty)/(rec.quantity+qty)
                rec.quantity += qty
                rec.peak_price = max(rec.peak_price, price)
                rec.initial_stop_px = max(rec.initial_stop_px, rec.entry_price*(1+rec.stop_loss_pct))
                rec.effective_stop_px = max(rec.effective_stop_px, rec.initial_stop_px, rec.trail_stop_px)
            rec.lots.append({"lot_id": fill_id, "quantity": qty, "remaining": qty, "price": price,
                            "timestamp": stamp, "decision": decision, "fees": fill.get("fees"),"peak_price":price,"low_price":price})
            if decision.get("decision")=="ADD_TO_POSITION" and not order.get("add_counted"):
                rec.adds_count+=1
                rec.last_add_timestamp=stamp
                order["add_counted"]=True
        else:
            if rec is None or qty > rec.quantity+1e-6:
                raise StateIntegrityError("sell fill exceeds known holding; reconciliation required")
            event.update(position_id=rec.position_id, entry_price=rec.entry_price,
                realized_pnl=(price-rec.entry_price)*qty, position_closed=qty >= rec.quantity-1e-6,
                peak_price=rec.peak_price, low_price=rec.low_price, history_complete=rec.history_complete,
                initial_entry_price=rec.initial_entry_price,
                entry_snapshot_id=rec.entry_snapshot_id, entry_date=rec.entry_date if rec.entry_date_known else None,
                stop_price=rec.effective_stop_px, reason=decision.get("reasoning_summary"), entry_lots=copy.deepcopy(rec.lots))
            remaining = qty
            for lot in rec.lots:
                used = min(lot["remaining"], remaining)
                lot["remaining"] -= used
                remaining -= used
            rec.quantity = max(0, rec.quantity-qty)
            if rec.quantity <= 1e-6:
                recs.remove(rec)
        event["position_id"] = rec.position_id
        state["positions"] = [r.model_dump() for r in recs]
        return event

    def record_entry(self, result, decision, realized_vol_30d=0):
        """Compatibility: only explicit confirmed fills in the matching ledger."""
        if result.dry_run != (self.mode == "paper") or result.status not in ("FILLED", "EXECUTED") or result.filled_quantity is None:
            return
        key = next((k for k, o in self.snapshot()["orders"].items() if result.order_id and o["broker_id"] == result.order_id), None)
        if key is None:
            key = self.create_order(decision, "buy", result.quantity, result.fill_price, realized_vol_30d)
        self.update_order(key, result)

    def update_record(self, record):
        with self._mutex:
            state = self._load()
            state["positions"] = [record.model_dump() if r["ticker"] == record.ticker else r for r in state["positions"]]
            self._save(state)

    def apply_split(self,ticker,ratio,evidence):
        """Explicit verified corporate action; never infer ratio from a quote."""
        if not math.isfinite(ratio) or ratio<=0 or not evidence.get("source") or not evidence.get("timestamp"):
            raise ValueError("verified corporate-action evidence required")
        with self._mutex:
            if self.pending(ticker):
                raise StateIntegrityError("reconcile/cancel pending orders before price-basis adjustment")
            state=self._load()
            recs,errors=self._records(state)
            if errors:raise StateIntegrityError("corrupt state")
            rec=next(r for r in recs if r.ticker==ticker)
            # Ratio is new shares / old shares. Broker reconciliation may
            # already have updated quantity/cost; price-state still needs adjustment.
            if rec.exit_state!="RECONCILIATION_REQUIRED":
                rec.quantity*=ratio;rec.entry_price/=ratio
            for field in ("peak_price","low_price","initial_stop_px","trail_stop_px","effective_stop_px","atr14","initial_entry_price"):
                value=getattr(rec,field)
                if value is not None:setattr(rec,field,value/ratio)
            for lot in rec.lots:
                lot["quantity"]*=ratio;lot["remaining"]*=ratio;lot["price"]/=ratio
                for field in ("peak_price","low_price"):
                    if field in lot:lot[field]/=ratio
            rec.exit_state="OPEN_PROTECTED"
            state["positions"]=[r.model_dump() for r in recs]
            state["events"].append({"type":"CORPORATE_ACTION","ticker":ticker,"ratio":ratio,"evidence":evidence})
            self._save(state)

    def record_exit(self, ticker):
        raise StateIntegrityError("record_exit requires a confirmed sell fill; use update_order")

    def record_realized_pnl(self, *args, **kwargs):
        raise StateIntegrityError("realized P&L is derived exclusively from the atomic fill ledger")

    def _perf_path(self):
        return self._path.parent/("paper_performance.json" if self.mode == "paper" else "performance.json")

    def record_equity(self,portfolio):
        if not math.isfinite(portfolio.total_value) or portfolio.total_value<=0 or not portfolio.timestamp:return
        with self._mutex:
            state=self._load()
            history=state.setdefault("equity_history",[])
            if history and history[-1]["timestamp"]==portfolio.timestamp:return
            history.append({"timestamp":portfolio.timestamp,"equity":portfolio.total_value,"source":"broker account snapshot","cash_flow_adjusted":False})
            self._save(state)

    def write_performance_snapshot(self, quotes):
        sells = [f for f in self.snapshot()["fills"].values() if f["side"] == "sell"]
        marks = []
        for r in self.get_open_records():
            q = quotes.get(r.ticker)
            px = float(getattr(q, "price", q) or 0)
            if math.isfinite(px) and px > 0:
                marks.append({"ticker": r.ticker, "quantity": r.quantity, "unrealized_pnl": (px-r.entry_price)*r.quantity})
        atomic_json(self._perf_path(), {"mode": self.mode, "closed_trades": [f for f in sells if f["position_closed"]],
            "realized_fills": sells, "total_realized": sum(f["realized_pnl"] for f in sells),
            "open_marks": marks, "open_unrealized_total": sum(m["unrealized_pnl"] for m in marks), "last_updated": utcnow()})

    def open_positions_pnl_map(self, quotes):
        result = {}
        for r in self.get_open_records():
            q = quotes.get(r.ticker)
            px = float(getattr(q, "price", q) or 0)
            if px > 0 and r.entry_price > 0:
                result[r.ticker] = (px/r.entry_price-1)*100
        return result

    @staticmethod
    def other_positions_line(pnl_map, exclude_ticker, max_positions=12):
        others = [f"{t} {p:+.1f}%" for t, p in pnl_map.items() if t != exclude_ticker]
        return f"OTHER OPEN POSITIONS ({len(others)}/{max_positions} max): " + (", ".join(others) or "none")

    def check_exits(self, portfolio, quotes):
        signals = []
        for r in self.get_open_records():
            q = quotes.get(r.ticker)
            px = float(getattr(q, "price", 0) or 0)
            if px <= 0 or self.pending(r.ticker) or r.exit_state == "RECONCILIATION_REQUIRED":
                continue
            pnl = px/r.entry_price-1
            if px <= r.effective_stop_px:
                signals.append(ExitSignal(r.ticker, "STOP_LOSS", px, pnl, r.quantity))
        return signals
