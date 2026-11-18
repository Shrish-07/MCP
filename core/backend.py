"""One application lifecycle; model/news jobs cannot hold the exit monitor lock."""

import asyncio
import logging
from datetime import UTC, datetime

from core.data.company_news import CompanyNewsFetcher
from core.discovery.evidence import ranked_evidence
from core.discovery.shortlist import load_shortlist
from core.exits.exit_state import graduate
from core.exits.trail_manager import executable_mark, update_trail
from core.position.manager import StateIntegrityError
from models.decisions import DecisionType, TradeDecision
from models.portfolio import Portfolio
from robinhood_mcp.robinhood_client import AccountResolutionError, AuthenticationError
from utils.freshness import fresh_quote, fresh_timestamp
from utils.health import Health, monitor_max_age
from utils.market_calendar import is_market_open_at


class Backend:
    def __init__(self, agent):
        self.agent = agent
        self.pm = agent.position_manager
        self.health = Health(self.pm._path.parent / "health.json")
        self.monitor_lock = asyncio.Lock()
        self.discovery_lock = asyncio.Lock()
        self.nightly_lock = asyncio.Lock()
        self.review_task = None
        self.review_active = set()
        self.held_features = {}
        self.held_news_fetcher = None
        self.last_portfolio = Portfolio()
        self.news_cache_hours = agent.config.get("sentiment_selection", {}).get(
            "cache_max_age_hours", 24
        )
        self.discovery = None
        if agent.config.get("discovery", {}).get("enabled"):
            from core.data.sentiment import get_sentiment_model
            from core.discovery.pipeline import Discovery

            self.discovery = Discovery(
                self.pm._path.parent.parent,
                agent.config,
                agent.mcp_client,
                self.pm,
                CompanyNewsFetcher(cache_max_age_hours=self.news_cache_hours),
                get_sentiment_model,
            )

    def read_discovery_shortlist(self):
        if self.discovery is None:
            return None
        try:
            return load_shortlist(
                self.discovery.path, self.discovery.cfg["shortlist_max_age_days"]
            )
        except (OSError, ValueError, OverflowError) as exc:
            logging.getLogger(__name__).debug(
                "discovery_shortlist_unavailable type=%s", type(exc).__name__
            )
            return None

    def evidence_for(self, ticker, metrics=None, quote=None, *, snapshot=None):
        import hashlib
        import json

        measured = metrics.model_dump(mode="json") if metrics else {}
        fact_id = hashlib.sha256(
            json.dumps(measured, sort_keys=True).encode()
        ).hexdigest()
        rec = next((r for r in self.pm.get_open_records() if r.ticker == ticker), None)
        sector = measured.get("sector") or (rec.sector if rec else "")
        known_sectors = {r.ticker: r.sector for r in self.pm.get_open_records()}
        sector_value = sum(
            p.market_value
            for p in self.last_portfolio.positions
            if (p.sector or known_sectors.get(p.ticker)) == sector
        )
        facts = {
            "sector_exposure_pct": sector_value / self.last_portfolio.total_value
            if self.last_portfolio.total_value > 0
            else None,
            "evidence_ids": [fact_id],
            "metrics_snapshot": measured,
            "quote_timestamp": getattr(quote, "timestamp", None),
            "feature_timestamp": measured.get("window_end"),
            "daily_new_entry_count": self.agent.executor.daily_entry_count(),
            "earnings_window": bool(self.agent.earnings_calendar.get_upcoming(ticker))
            if hasattr(self.agent, "earnings_calendar")
            else None,
            "sector": sector,
            "config_revision": self.agent.config.get("config_revision"),
            "incremental_opportunity_confirmed": bool(
                metrics
                and metrics.sma_20 > 0
                and metrics.current_price > metrics.sma_20
                and metrics.macd_histogram > 0
                or metrics
                and metrics.sma_20 > 0
                and metrics.current_price >= metrics.sma_20 * 0.97
                and metrics.return_30d > 0
                and 30 <= metrics.rsi_14 <= 55
                and metrics.volume_spike_pct < 0
            ),
            "existing_thesis": rec.thesis if rec else None,
            "position_id": rec.position_id if rec else None,
            "invalidation": rec.invalidation if rec else [],
            "entry_date_known": rec.entry_date_known if rec else None,
            "peak": rec.peak_price if rec else None,
            "effective_stop": rec.effective_stop_px if rec else None,
            "exit_state": rec.exit_state if rec else None,
            "pending_orders": self.pm.pending(ticker),
            "interpretation": "Measured facts only. Orientation, not a recommendation.",
        }
        row = ranked_evidence(snapshot, ticker)
        if row:
            facts["evidence_ids"].append(row["discovery_run_id"] + ":" + ticker)
            facts["incremental_opportunity_confirmed"] |= bool(
                any(row["fingerprints"].values()) and row["sentiment_mean"] > 0
            )
            facts = row | facts
        elif self.discovery:
            facts["discovery_quality"] = "unavailable"
        return facts

    async def reconcile(self):
        async with self.pm.operation_lock:
            try:
                self.pm.orders_reconciled = False
                pending = any(
                    order["state"]
                    in {
                        "CREATED",
                        "SUBMITTED",
                        "PARTIALLY_FILLED",
                        "CANCEL_REQUESTED",
                        "UNKNOWN_REQUIRES_RECONCILIATION",
                    }
                    for order in self.pm.snapshot()["orders"].values()
                )
                before = (
                    await self.agent.mcp_client.get_equity_orders() if pending else None
                )
                portfolio = await self.agent.mcp_client.get_portfolio()
                if (
                    portfolio.reconciliation_status != "SUCCESS"
                    or not portfolio.positions_complete
                    or not self.pm.account_matches(portfolio)
                ):
                    raise ValueError("incomplete or mismatched account snapshot")
                orders = await self.agent.mcp_client.get_equity_orders()
                if before is not None:

                    def fingerprint(rows):
                        return {
                            r.order_id: (
                                r.status,
                                r.filled_quantity,
                                r.fill_price,
                                r.fills,
                            )
                            for r in rows
                        }

                    if fingerprint(before) != fingerprint(orders):
                        raise ValueError("orders changed during holdings snapshot")
                orders_ok = await self.agent.executor.reconcile_orders(orders)
                if not orders_ok:
                    raise ValueError("order state could not be reconciled")
                positions_ok = self.pm.reconcile_with_mcp(portfolio)
                if positions_ok:
                    self.pm.record_equity(portfolio)
                self.sync_fill_journals()
                self.last_portfolio = portfolio
                ok = orders_ok and positions_ok
                self.health.record(
                    "broker",
                    success=ok,
                    error=None if ok else "positions/orders unverified",
                )
                return ok
            except Exception as exc:
                logging.getLogger(__name__).exception(
                    "Handled failure type=%s", type(exc).__name__, exc_info=False
                )
                self.pm.mark_unverified(type(exc).__name__)
                kind = (
                    "AUTHENTICATION_FAILED"
                    if isinstance(exc, AuthenticationError)
                    else "STATE_RECONCILIATION_FAILED"
                    if isinstance(
                        exc, (StateIntegrityError, ValueError, AccountResolutionError)
                    )
                    else "BROKER_UNAVAILABLE"
                )
                self.health.record(
                    "broker", success=False, error=exc, failure_kind=kind
                )
                return False

    async def monitor(self):
        if self.monitor_lock.locked() or not is_market_open_at():
            return
        async with self.monitor_lock:
            interval = self.agent.config.get("monitor", {}).get("interval_seconds", 15)
            self.health.record("monitor", interval_seconds=interval)
            reconciliation_age = self.agent.config.get("execution", {}).get(
                "reconciliation_max_age_seconds", 120
            )
            if (
                self.health.stale("broker", min(60, reconciliation_age))
                and not await self.reconcile()
            ):
                self.health.record(
                    "monitor",
                    success=False,
                    error="broker unavailable; stops cannot be guaranteed",
                )
                return
            records = self.pm.get_open_records()
            if self.pm.load_error:
                self.health.record("monitor", success=False, error=self.pm.load_error)
                return
            try:
                quotes = []
                tickers = [r.ticker for r in records if r.broker_confirmed]
                for start in range(0, len(tickers), 20):
                    quotes.extend(
                        await self.agent.mcp_client.get_equity_quotes(
                            tickers[start : start + 20]
                        )
                    )
                quote_map = {q.ticker: q for q in quotes}
                quotes_valid = True
                for record in records:
                    q = quote_map.get(record.ticker)
                    if q is None or not fresh_quote(q.timestamp, self.agent.config):
                        quotes_valid = False
                        self.health.record(
                            "quotes",
                            success=False,
                            error=f"stale/missing quote: {record.ticker}",
                        )
                        continue
                    price = executable_mark(q)
                    if (
                        price <= 0
                        or not record.broker_confirmed
                        or record.exit_state == "RECONCILIATION_REQUIRED"
                    ):
                        continue
                    async with self.pm.operation_lock, self.pm.lock_for(record.ticker):
                        # Reload after acquiring locks; a cycle may have sold it.
                        record = next(
                            (
                                r
                                for r in self.pm.get_open_records()
                                if r.position_id == record.position_id
                            ),
                            None,
                        )
                        if record is None:
                            continue
                        try:
                            stop_crossed = update_trail(record, q, self.agent.config)
                        except ValueError as exc:
                            quotes_valid = False
                            self.health.record("quotes", success=False, error=exc)
                            continue
                        self.pm.update_record(record)
                        if stop_crossed:
                            record.thesis_invalidated = True
                            self.pm.update_record(record)
                            if any(
                                order["side"] == "sell"
                                for order in self.pm.pending(record.ticker)
                            ):
                                continue
                            if (
                                self.agent.config["env"]["DRY_RUN"]
                                and record.review_events.get("dry_stop")
                                == record.effective_stop_px
                            ):
                                continue
                            record.review_events["dry_stop"] = record.effective_stop_px
                            self.pm.update_record(record)
                            decision = TradeDecision(
                                ticker=record.ticker,
                                decision=DecisionType.SELL,
                                action_type="CLOSE",
                                cycle_type="MONITOR",
                                timestamp=datetime.now(UTC).isoformat(),
                                reasoning_summary=f"HARD_STOP stop={record.effective_stop_px} bid_or_last={price} quote={q.timestamp}",
                                evidence={"position_id": record.position_id},
                            )
                            await self.agent.executor.execute(
                                decision,
                                self.last_portfolio,
                                price,
                                dry_run=self.agent.config["env"]["DRY_RUN"],
                                quote_timestamp=q.timestamp,
                            )
                        elif self.agent.config.get("exits", {}).get(
                            "enabled", True
                        ) and graduate(record, price, self.agent.config):
                            self.pm.update_record(record)
                        if not stop_crossed and self.agent.config.get("exits", {}).get(
                            "enabled", True
                        ):
                            from core.exits.triggers import monitor_advisories
                            from utils.strategy_config import DEFAULTS

                            settings = dict(
                                self.agent.config,
                                exits=self.agent.config.get("exits", DEFAULTS["exits"]),
                            )
                            monitor_advisories(
                                record,
                                price,
                                settings,
                                prior_high=record.trigger_memory.get("prior_high_252"),
                            )
                            self.pm.update_record(record)
                self.health.record("monitor", success=True, positions=len(records))
                if quotes_valid:
                    self.health.record("quotes", success=True)
                if self.review_task is None or self.review_task.done():
                    self.review_task = asyncio.create_task(self.drain_reviews())
            except Exception as exc:
                logging.getLogger(__name__).exception(
                    "Handled failure type=%s", type(exc).__name__, exc_info=False
                )
                self.health.record("monitor", success=False, error=exc)

    async def drain_reviews(self):
        """One model call per affected ticker; never hold execution locks during it."""
        if not self.agent.config.get("exits", {}).get("enabled", True):
            return
        from core.exits.exit_evidence import due_reviews
        from core.exits.exit_state import finish_review
        from core.position.manager import ExitSignal
        from models.decisions import CycleType

        monitor = self.agent.config.get("monitor", {})
        snapshot = self.read_discovery_shortlist()
        for record in self.pm.get_open_records():
            events = due_reviews(
                record,
                retry_minutes=monitor.get("profit_review_cooldown_min", 15),
                graduation_retry_minutes=monitor.get("profit_target_cooldown_min", 5),
            )
            if (
                not events
                or record.ticker in self.review_active
                or self.pm.pending(record.ticker)
            ):
                continue
            ticker = record.ticker
            self.review_active.add(ticker)
            ids = [e["id"] for e in events]
            try:
                async with self.pm.operation_lock:
                    current = next(
                        (
                            r
                            for r in self.pm.get_open_records()
                            if r.position_id == record.position_id
                        ),
                        None,
                    )
                    if current is None:
                        continue
                    for event in current.review_events.values():
                        if isinstance(event, dict) and event.get("id") in ids:
                            event.update(
                                state="IN_PROGRESS",
                                started_at=datetime.now(UTC).isoformat(),
                            )
                    self.pm.update_record(current)
                metrics, news = await self.agent._monitor_refresh_data(
                    ticker, "SPECIALIZED"
                )
                quotes = await self.agent.mcp_client.get_equity_quotes([ticker])
                quote = next((q for q in quotes if q.ticker == ticker), None)
                if quote is None or not fresh_quote(quote.timestamp, self.agent.config):
                    raise ValueError("review quote stale")
                price = executable_mark(quote)
                facts = self.evidence_for(ticker, metrics, quote, snapshot=snapshot)
                facts["review_events"] = events
                signal = ExitSignal(
                    ticker,
                    "+".join(e["kind"] for e in events),
                    price,
                    price / record.entry_price - 1,
                    record.quantity,
                )
                decision = await self.agent.decision_engine.review_exit(
                    signal,
                    metrics,
                    news,
                    self.last_portfolio,
                    CycleType.MID,
                    position_record=record,
                    spy_context=getattr(self.agent, "_latest_spy_context", ""),
                    open_positions_pnl=self.pm.open_positions_pnl_map(
                        {
                            item.ticker: item
                            for item in await self.agent.mcp_client.get_equity_quotes(
                                [r.ticker for r in self.pm.get_open_records()]
                            )
                        }
                    ),
                    evidence=facts,
                )
                failed = (
                    decision.is_fallback
                    or "fallback" in decision.reasoning_summary.lower()
                )
                if (
                    any(e["kind"] == "GRADUATION" for e in events)
                    and decision.decision == DecisionType.HOLD
                ):
                    failed = (
                        failed
                        or len(decision.bull_thesis) < 20
                        or not decision.failure_conditions
                    )
                async with self.pm.operation_lock:
                    current = next(
                        (
                            r
                            for r in self.pm.get_open_records()
                            if r.position_id == record.position_id
                        ),
                        None,
                    )
                    if current is None or self.pm.pending(ticker):
                        continue
                    finish_review(
                        current, ids, decision.decision.value, model_failed=failed
                    )
                    self.pm.update_record(current)
                if not failed and hasattr(self.agent, "journal_manager"):
                    from models.decisions import JournalEntry

                    self.agent.journal_manager.append(
                        ticker,
                        JournalEntry(
                            timestamp=datetime.now(UTC).isoformat(),
                            cycle_type="SPECIALIZED",
                            decision=decision.decision.value,
                            decision_rationale=decision.reasoning_summary,
                            bull_thesis=decision.bull_thesis,
                            bear_thesis=decision.bear_thesis,
                            decision_id=decision.decision_id,
                            mode="decision",
                            key_metrics=metrics.model_dump(mode="json"),
                        ),
                    )
                if decision.decision == DecisionType.SELL:
                    decision.evidence["exit_review_ids"] = ids
                if not failed and decision.decision in (
                    DecisionType.SELL,
                    DecisionType.ADD_TO_POSITION,
                ):
                    quotes = await self.agent.mcp_client.get_equity_quotes([ticker])
                    q = next((item for item in quotes if item.ticker == ticker), None)
                    if q is None or not fresh_quote(q.timestamp, self.agent.config):
                        raise ValueError("review execution quote unavailable")
                    result = await self.agent.executor.execute(
                        decision,
                        self.last_portfolio,
                        executable_mark(q),
                        dry_run=self.agent.config["env"]["DRY_RUN"],
                        quote_timestamp=q.timestamp,
                        vol_30d=metrics.realized_vol_30d,
                        avg_volume_30d=metrics.avg_volume_30d,
                    )
                    if decision.decision == DecisionType.SELL and (
                        result.dry_run or result.status.startswith("REJECTED")
                    ):
                        async with self.pm.operation_lock:
                            current = next(
                                (
                                    r
                                    for r in self.pm.get_open_records()
                                    if r.position_id == record.position_id
                                ),
                                None,
                            )
                            if current and not self.pm.pending(ticker):
                                finish_review(current, ids, "SELL", model_failed=True)
                                self.pm.update_record(current)
                self.health.record(
                    "reviews",
                    success=not failed,
                    error="model/defended hold unavailable" if failed else None,
                )
            except Exception as exc:
                logging.getLogger(__name__).exception(
                    "Handled failure type=%s", type(exc).__name__, exc_info=False
                )
                async with self.pm.operation_lock:
                    current = next(
                        (
                            r
                            for r in self.pm.get_open_records()
                            if r.position_id == record.position_id
                        ),
                        None,
                    )
                    if current:
                        finish_review(current, ids, "HOLD", model_failed=True)
                        self.pm.update_record(current)
                self.health.record("reviews", success=False, error=exc)
            finally:
                self.review_active.discard(ticker)

    async def heartbeat(self):
        self.health.record("heartbeat", success=True)
        interval = self.agent.config.get("monitor", {}).get("interval_seconds", 15)
        if is_market_open_at() and self.health.stale(
            "monitor", monitor_max_age(interval)
        ):
            self.health.record(
                "watchdog", success=False, error="monitor heartbeat overdue"
            )
        else:
            self.health.record("watchdog", success=True)

    async def ensure_nightly(self):
        """Recover missed held-position maintenance after a process restart."""
        from datetime import timedelta

        from utils.market_calendar import _NYSE

        records = self.pm.get_open_records()
        if not records:
            return True
        now = datetime.now(UTC)
        if _NYSE is None:
            self.health.record("nightly", success=False, error="Calendar unavailable")
            return False
        schedule = _NYSE.schedule(
            start_date=now.date() - timedelta(days=10), end_date=now.date()
        )
        completed = [
            row["market_close"].to_pydatetime()
            for _, row in schedule.iterrows()
            if row["market_close"].to_pydatetime() <= now
        ]
        latest = max(completed) if completed else None
        last = self.health.state.get("nightly", {}).get("last_success")
        try:
            fresh = (
                latest is not None
                and last is not None
                and datetime.fromisoformat(last) >= latest
            )
            fresh = fresh and all(
                r.atr14
                and r.atr_date
                and datetime.fromisoformat(r.atr_date).date() >= latest.date()
                for r in records
            )
        except (TypeError, ValueError):
            fresh = False
        if not fresh:
            await self.nightly()
        return not self.health.state.get("nightly", {}).get("error")

    async def nightly(self):
        async with self.nightly_lock:
            await self._nightly()

    async def _nightly(self):
        from core.data.daily import completed_bars, wilder_atr

        self.health.record("nightly")
        if not await self.reconcile():
            self.health.record(
                "nightly", success=False, error="Broker reconciliation required"
            )
            return
        self.held_features = {}
        failures = []
        snapshot = self.read_discovery_shortlist()
        records = self.pm.get_open_records()
        held = {record.ticker for record in records}
        for record in records:
            try:
                bars = await self.agent.mcp_client.get_equity_historicals(
                    record.ticker, span="14month"
                )
                bars = completed_bars(bars, trailing_only=True)
                atr = wilder_atr(bars)
                if atr is None:
                    raise ValueError("ATR14 unavailable")
                fresh_news = {}
                if (
                    self.agent.config.get("sentiment_selection", {}).get("enabled")
                    or self.discovery
                ):
                    try:
                        if self.discovery:
                            fresh_news = await self.discovery.news_for(record.ticker)
                        else:
                            from core.data.sentiment import get_sentiment_model
                            from core.discovery.news_features import (
                                NewsHistory,
                                collect_news_features,
                            )

                            if self.held_news_fetcher is None:
                                self.held_news_fetcher = CompanyNewsFetcher(
                                    cache_max_age_hours=self.news_cache_hours
                                )
                            model = await asyncio.to_thread(get_sentiment_model)
                            fresh_news = await collect_news_features(
                                NewsHistory(
                                    self.pm._path.parent.parent
                                    / "data/sentiment_history.json"
                                ),
                                self.held_news_fetcher,
                                model,
                                record.ticker,
                            )
                        self.health.record("held_news:" + record.ticker, success=True)
                    except Exception as exc:
                        logging.getLogger(__name__).exception(
                            "Handled failure type=%s",
                            type(exc).__name__,
                            exc_info=False,
                        )
                        self.health.record(
                            "held_news:" + record.ticker, success=False, error=exc
                        )
                async with self.pm.operation_lock:
                    current = next(
                        (
                            r
                            for r in self.pm.get_open_records()
                            if r.position_id == record.position_id
                        ),
                        None,
                    )
                    if current is None:
                        continue
                    current.atr14, current.atr_date, current.atr_source = (
                        atr,
                        bars[-1].timestamp,
                        "Robinhood MCP daily bars; Wilder14",
                    )
                    self.pm.update_record(current)
                    import json

                    from core.discovery.features import technical_features
                    from core.exits.triggers import nightly_signals
                    from utils.strategy_config import DEFAULTS

                    cfg = self.agent.config.get("discovery", DEFAULTS["discovery"])
                    feature = (
                        technical_features(bars, cfg)
                        if len(bars) >= 250
                        else {"rsi14": None}
                    )
                    self.held_features[record.ticker] = feature
                    if feature.get("high_252"):
                        current.trigger_memory["prior_high_252"] = feature["high_252"]
                    settings = dict(
                        self.agent.config,
                        exits=self.agent.config.get("exits", DEFAULTS["exits"]),
                        trading=self.agent.config.get("trading", {}),
                    )
                    facts = self.evidence_for(record.ticker, snapshot=snapshot)
                    # A weekly cross section cannot stand in for fresh held news.
                    facts["z_components"] = fresh_news.get("z_components", {})
                    if fresh_news:
                        facts.update(fresh_news)
                    comparable_gap = None
                    entry = (
                        current.lots[0].get("decision", {}).get("evidence", {})
                        if current.lots
                        else {}
                    )
                    if (
                        entry.get("cohort")
                        and fresh_news
                        and "momentum_blend" in feature
                    ):
                        from core.discovery.divergence import score_fixed_reference

                        try:
                            comparable_gap = score_fixed_reference(
                                dict(facts, **feature), entry, cfg
                            )
                        except (
                            KeyError,
                            TypeError,
                            ValueError,
                            ArithmeticError,
                        ) as exc:
                            self.health.record(
                                "discovery_review_evidence", success=False, error=exc
                            )
                    pead_path = self.pm._path.parent.parent / "data/pead_events.json"
                    try:
                        pead_data = (
                            json.loads(pead_path.read_text())
                            if pead_path.exists()
                            else {}
                        )
                    except (OSError, ValueError) as exc:
                        pead_data = {}
                        self.health.record("pead_data", success=False, error=exc)
                    best = None
                    if comparable_gap is not None and entry.get("gap_reference"):
                        facts.update(
                            gap_z=comparable_gap, gap_reference=entry["gap_reference"]
                        )
                    if (
                        snapshot
                        and comparable_gap is not None
                        and entry.get("gap_reference")
                    ):
                        candidate = next(
                            (
                                row
                                for row in snapshot["new_candidates"]
                                if row["ticker"] not in held
                            ),
                            None,
                        )
                        try:
                            if candidate:
                                best = dict(
                                    candidate,
                                    gap_z=score_fixed_reference(candidate, entry, cfg),
                                    gap_reference=entry["gap_reference"],
                                )
                        except (
                            KeyError,
                            TypeError,
                            ValueError,
                            ArithmeticError,
                        ) as exc:
                            self.health.record(
                                "discovery_review_evidence", success=False, error=exc
                            )
                    if settings["exits"]["enabled"]:
                        nightly_signals(
                            current,
                            bars,
                            feature,
                            facts,
                            settings,
                            current_gap=comparable_gap,
                            comparable=comparable_gap is not None,
                            pead_event=pead_data.get("current", {}).get(record.ticker),
                            pead_history=pead_data.get("history", []),
                            best_candidate=best,
                        )
                    self.pm.update_record(current)
            except Exception as exc:
                logging.getLogger(__name__).exception(
                    "Handled failure type=%s", type(exc).__name__, exc_info=False
                )
                failures.append(f"{record.ticker}: {exc}")
        self.health.record(
            "nightly",
            success=not failures,
            error="; ".join(failures) if failures else None,
        )
        from utils.persistence import atomic_json

        try:
            atomic_json(
                self.pm._path.parent.parent / "data/held_features_latest.json",
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "rows": self.held_features,
                    "errors": failures,
                },
            )
        except (OSError, ValueError) as exc:
            self.health.record("nightly", success=False, error=exc)

    async def refresh_discovery_features(self):
        """Refresh only discovery market inputs at the configured feature hour."""
        if self.discovery:
            try:
                await self.discovery.features()
                self.health.record("discovery_features", success=True)
            except Exception as exc:
                logging.getLogger(__name__).exception(
                    "Handled failure type=%s", type(exc).__name__, exc_info=False
                )
                self.health.record("discovery_features", success=False, error=exc)

    async def ensure_discovery(self):
        """Bootstrap a missing/stale shortlist without starting legacy research."""
        from datetime import timedelta

        from apscheduler.triggers.cron import CronTrigger

        from utils.strategy_config import DEFAULTS

        if not self.discovery:
            return False
        try:
            snapshot = load_shortlist(
                self.discovery.path, self.discovery.cfg["shortlist_max_age_days"]
            )
            schedule = self.discovery.cfg.get(
                "schedule", DEFAULTS["discovery"]["schedule"]
            )
            trigger = CronTrigger(
                day_of_week=schedule["rank_day"],
                hour=schedule["rank_hour_et"],
                minute=0,
                timezone="America/New_York",
            )
            now = datetime.now(UTC)
            due = trigger.get_next_fire_time(None, now - timedelta(days=8))
            latest = None
            while due is not None and due <= now:
                latest = due
                due = trigger.get_next_fire_time(due, due)
            if latest and datetime.fromisoformat(snapshot["generated_at"]) < latest:
                raise ValueError("Missed weekly discovery refresh")
            return True
        except (OSError, ValueError, KeyError, TypeError):
            await self.weekly()
        try:
            load_shortlist(
                self.discovery.path, self.discovery.cfg["shortlist_max_age_days"]
            )
            return True
        except (OSError, ValueError, KeyError, TypeError):
            self.health.record(
                "discovery_ranking", success=False, error="No valid discovery shortlist"
            )
            return False

    async def weekly(self):
        if not self.discovery:
            return
        async with self.discovery_lock:
            if not await self.reconcile():
                self.health.record(
                    "discovery_ranking",
                    success=False,
                    error="Broker reconciliation required",
                )
                return
            try:
                await self.discovery.rank()
                self.health.record("discovery_ranking", success=True)
            except Exception as exc:
                logging.getLogger(__name__).exception(
                    "Discovery failed type=%s", type(exc).__name__, exc_info=False
                )
                self.health.record("discovery_ranking", success=False, error=exc)

    async def attribution(self):
        from core.attribution import build_report

        try:
            if not await self.reconcile():
                self.health.record(
                    "attribution", success=False, error="Broker reconciliation required"
                )
                return None
            tickers = [r.ticker for r in self.pm.get_open_records()]
            quotes = {}
            for start in range(0, len(tickers), 20):
                for q in await self.agent.mcp_client.get_equity_quotes(
                    tickers[start : start + 20]
                ):
                    if fresh_timestamp(q.timestamp, 5 * 86400):
                        quotes[q.ticker] = q
            report = build_report(
                self.pm, quotes=quotes, equity=self.last_portfolio.total_value
            )
            build_report(self.agent.executor.paper_manager)
            self.pm.write_performance_snapshot(quotes)
            self.health.record(
                "attribution",
                success=True,
                closed_positions=report["metrics"]["closed_positions"],
            )
            return report
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "Handled failure type=%s", type(exc).__name__, exc_info=False
            )
            self.health.record("attribution", success=False, error=exc)

    def sync_fill_journals(self):
        if not hasattr(self.agent, "journal_manager"):
            return
        import json

        from models.decisions import JournalEntry
        from utils.persistence import atomic_json

        path = self.pm._path.parent / "journal_sync.json"
        try:
            synced = set(json.loads(path.read_text())) if path.exists() else set()
            state = self.pm.snapshot()
            excluded = set(state.get("journal_context_excluded_fill_ids", []))
            for fill_id, event in state["fills"].items():
                if fill_id in synced or fill_id in excluded:
                    continue
                d = event["decision"]
                entry = JournalEntry(
                    timestamp=event["timestamp"],
                    cycle_type="CONFIRMED_FILL",
                    event_id=fill_id,
                    mode=self.pm.mode,
                    decision_id=event.get("decision_id"),
                    position_id=event["position_id"],
                    order_id=event["order_id"],
                    decision=d.get("decision", event["side"].upper()),
                    decision_rationale=d.get("reasoning_summary", ""),
                    bull_thesis=d.get("bull_thesis", ""),
                    bear_thesis=d.get("bear_thesis", ""),
                    fill_price=event["price"],
                )
                if self.agent.journal_manager.append(event["ticker"], entry):
                    synced.add(fill_id)
            atomic_json(path, sorted(synced))
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "Handled failure type=%s", type(exc).__name__, exc_info=False
            )
            self.health.record("journal_sync", success=False, error=exc)
