"""One application lifecycle; model/news jobs cannot hold the exit monitor lock."""

import asyncio
from datetime import datetime, timezone
from models.decisions import DecisionType, TradeDecision
from models.portfolio import Portfolio
from utils.freshness import fresh_timestamp
from utils.health import Health
from utils.market_calendar import is_market_open_at
from core.exits.trail_manager import update_trail
from core.exits.exit_state import graduate


class Backend:
    def __init__(self, agent):
        self.agent = agent
        self.pm = agent.position_manager
        self.health = Health(self.pm._path.parent / "health.json")
        self.monitor_lock = asyncio.Lock()
        self.review_task = None
        self.review_active = set()
        self.held_features = {}
        self.held_news_fetcher=None
        self.last_portfolio = Portfolio()
        self.discovery = None
        if agent.config.get("discovery", {}).get("enabled"):
            from core.discovery.pipeline import Discovery
            from core.weekly.news import WeeklyNewsFetcher
            from core.data.sentiment import get_sentiment_model

            self.discovery = Discovery(
                self.pm._path.parent.parent,
                agent.config,
                agent.mcp_client,
                self.pm,
                WeeklyNewsFetcher(),
                get_sentiment_model,
            )

    def evidence_for(self, ticker, metrics=None, quote=None):
        import hashlib, json

        measured = metrics.model_dump(mode="json") if metrics else {}
        fact_id = hashlib.sha256(
            json.dumps(measured, sort_keys=True).encode()
        ).hexdigest()
        rec = next((r for r in self.pm.get_open_records() if r.ticker == ticker), None)
        facts = {
            "evidence_ids": [fact_id],
            "metrics_snapshot": measured,
            "quote_timestamp": getattr(quote, "timestamp", None),
            "feature_timestamp": measured.get("window_end"),
            "daily_new_entry_count":self.agent.executor.daily_entry_count(),
            "earnings_window":bool(self.agent.earnings_calendar.get_upcoming(ticker)) if hasattr(self.agent,"earnings_calendar") else None,
            "sector": measured.get("sector"),
            "config_revision": self.agent.config.get("config_revision"),
            "incremental_opportunity_confirmed": bool(
                metrics
                and metrics.sma_20 > 0
                and metrics.current_price > metrics.sma_20
                and metrics.macd_histogram > 0
            ),
            "existing_thesis": rec.thesis if rec else None,
            "invalidation": rec.invalidation if rec else [],
            "entry_date_known": rec.entry_date_known if rec else None,
            "peak": rec.peak_price if rec else None,
            "effective_stop": rec.effective_stop_px if rec else None,
            "exit_state": rec.exit_state if rec else None,
            "pending_orders": self.pm.pending(ticker),
            "interpretation": "Measured facts only. Orientation, not a recommendation.",
        }
        if self.discovery:
            import json
            from core.discovery.shortlist import load_shortlist

            try:
                snapshot = load_shortlist(
                    self.discovery.path, self.discovery.cfg["shortlist_max_age_days"]
                )
                row = snapshot.get("all_ranked", {}).get(ticker)
                if row:
                    facts.update(row)
                    facts["evidence_ids"].append(snapshot["run_id"] + ":" + ticker)
                    facts["incremental_opportunity_confirmed"] = bool(
                        any(row["fingerprints"].values()) and row["sentiment_mean"] > 0
                    )
            except (OSError, ValueError, KeyError):
                facts["discovery_quality"] = "unavailable"
        return facts

    async def reconcile(self):
        async with self.pm.operation_lock:
            try:
                self.pm.orders_reconciled = False
                pending = any(order["state"] in {"CREATED", "SUBMITTED", "PARTIALLY_FILLED", "CANCEL_REQUESTED", "UNKNOWN_REQUIRES_RECONCILIATION"}
                              for order in self.pm.snapshot()["orders"].values())
                before = await self.agent.mcp_client.get_equity_orders() if pending else None
                portfolio = await self.agent.mcp_client.get_portfolio()
                if (portfolio.reconciliation_status != "SUCCESS" or not portfolio.positions_complete
                    or not self.pm.account_matches(portfolio)):
                    raise ValueError("incomplete or mismatched account snapshot")
                orders = await self.agent.mcp_client.get_equity_orders()
                if before is not None:
                    def fingerprint(rows):
                        return {r.order_id: (r.status, r.filled_quantity, r.fill_price, r.fills) for r in rows}
                    if fingerprint(before) != fingerprint(orders):
                        raise ValueError("orders changed during holdings snapshot")
                orders_ok = await self.agent.executor.reconcile_orders(orders)
                if not orders_ok:
                    raise ValueError("order state could not be reconciled")
                positions_ok = self.pm.reconcile_with_mcp(portfolio)
                if positions_ok:self.pm.record_equity(portfolio)
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
                self.pm.mark_unverified(str(exc))
                self.health.record("broker", success=False, error=exc)
                return False

    async def monitor(self):
        if self.monitor_lock.locked() or not is_market_open_at():
            return
        async with self.monitor_lock:
            self.health.record("monitor")
            if self.health.stale("broker", 60) and not await self.reconcile():
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
                quotes_valid=True
                for record in records:
                    q = quote_map.get(record.ticker)
                    if q is None or not fresh_timestamp(q.timestamp, 60):
                        quotes_valid=False
                        self.health.record(
                            "quotes",
                            success=False,
                            error=f"stale/missing quote: {record.ticker}",
                        )
                        continue
                    price = q.bid if q.bid > 0 else q.price
                    if (
                        price <= 0
                        or not record.broker_confirmed
                        or record.exit_state == "RECONCILIATION_REQUIRED"
                    ):
                        continue
                    async with self.pm.operation_lock:
                        async with self.pm.lock_for(record.ticker):
                            # Reload after acquiring locks; a cycle may have sold it.
                            record = next(
                                (
                                    r
                                    for r in self.pm.get_open_records()
                                    if r.ticker == record.ticker
                                ),
                                None,
                            )
                            if record is None:
                                continue
                            stop_crossed = update_trail(record, q, self.agent.config)
                            self.pm.update_record(record)
                            if stop_crossed:
                                record.thesis_invalidated = True
                                self.pm.update_record(record)
                                if any(order["side"] == "sell" for order in self.pm.pending(record.ticker)):
                                    continue
                                if (
                                    self.agent.config["env"]["DRY_RUN"]
                                    and record.review_events.get("dry_stop")
                                    == record.effective_stop_px
                                ):
                                    continue
                                record.review_events["dry_stop"] = (
                                    record.effective_stop_px
                                )
                                self.pm.update_record(record)
                                decision = TradeDecision(
                                    ticker=record.ticker,
                                    decision=DecisionType.SELL,
                                    action_type="CLOSE",
                                    cycle_type="MONITOR",
                                    timestamp=datetime.now(timezone.utc).isoformat(),
                                    reasoning_summary=f"HARD_STOP stop={record.effective_stop_px} bid_or_last={price} quote={q.timestamp}",
                                )
                                await self.agent.executor.execute(
                                    decision,
                                    self.last_portfolio,
                                    price,
                                    dry_run=self.agent.config["env"]["DRY_RUN"],
                                    quote_timestamp=q.timestamp,
                                )
                            elif self.agent.config.get("exits",{}).get("enabled",True) and graduate(record, price, self.agent.config):
                                self.pm.update_record(record)
                            if not stop_crossed and self.agent.config.get(
                                "exits", {}
                            ).get("enabled", True):
                                from core.exits.triggers import monitor_advisories
                                from utils.strategy_config import DEFAULTS

                                settings = dict(
                                    self.agent.config,
                                    exits=self.agent.config.get(
                                        "exits", DEFAULTS["exits"]
                                    ),
                                )
                                monitor_advisories(
                                    record,
                                    price,
                                    settings,
                                    prior_high=record.trigger_memory.get(
                                        "prior_high_252"
                                    ),
                                )
                                self.pm.update_record(record)
                self.health.record("monitor", success=True, positions=len(records))
                if quotes_valid:self.health.record("quotes",success=True)
                if self.review_task is None or self.review_task.done():
                    self.review_task = asyncio.create_task(self.drain_reviews())
            except Exception as exc:
                self.health.record("monitor", success=False, error=exc)

    async def drain_reviews(self):
        """One model call per affected ticker; never hold execution locks during it."""
        from core.exits.exit_evidence import due_reviews
        from core.exits.exit_state import finish_review
        from core.position.manager import ExitSignal
        from models.decisions import CycleType

        for record in self.pm.get_open_records():
            events = due_reviews(record)
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
                                started_at=datetime.now(timezone.utc).isoformat(),
                            )
                    self.pm.update_record(current)
                metrics, news = await self.agent._monitor_refresh_data(
                    ticker, "SPECIALIZED"
                )
                quotes = await self.agent.mcp_client.get_equity_quotes([ticker])
                quote = next((q for q in quotes if q.ticker == ticker), None)
                if quote is None or not fresh_timestamp(quote.timestamp, 60):
                    raise ValueError("review quote stale")
                price = quote.bid if quote.bid > 0 else quote.price
                facts = self.evidence_for(ticker, metrics, quote)
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
                    evidence=facts,
                )
                failed = "fallback" in decision.reasoning_summary.lower()
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
                if decision.decision == DecisionType.SELL:
                    decision.evidence["exit_review_ids"] = ids
                if not failed and decision.decision in (
                    DecisionType.SELL,
                    DecisionType.ADD_TO_POSITION,
                ):
                    quotes = await self.agent.mcp_client.get_equity_quotes([ticker])
                    q = quotes[0]
                    result = await self.agent.executor.execute(
                        decision,
                        self.last_portfolio,
                        q.bid if q.bid > 0 else q.price,
                        dry_run=self.agent.config["env"]["DRY_RUN"],
                        quote_timestamp=q.timestamp,
                        vol_30d=metrics.realized_vol_30d,
                        avg_volume_30d=metrics.avg_volume_30d,
                    )
                    if decision.decision == DecisionType.SELL and (
                        result.dry_run or result.status.startswith("REJECTED")
                    ):
                        async with self.pm.operation_lock:
                            current = next((r for r in self.pm.get_open_records()
                                            if r.position_id == record.position_id), None)
                            if current and not self.pm.pending(ticker):
                                finish_review(current, ids, "SELL", model_failed=True)
                                self.pm.update_record(current)
                self.health.record(
                    "reviews",
                    success=not failed,
                    error="model/defended hold unavailable" if failed else None,
                )
            except Exception as exc:
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

    def heartbeat(self):
        if is_market_open_at() and self.health.stale("monitor", 90):
            self.health.record(
                "heartbeat", success=False, error="monitor heartbeat overdue"
            )

    async def nightly(self):
        from core.exits.trail_manager import completed_bars, wilder_atr

        self.health.record("nightly")
        await self.reconcile()
        failures = []
        for record in self.pm.get_open_records():
            try:
                bars = await self.agent.mcp_client.get_equity_historicals(
                    record.ticker, span="year"
                )
                bars = completed_bars(bars)
                atr = wilder_atr(bars)
                if atr is None:
                    raise ValueError("ATR14 unavailable")
                fresh_news={}
                if self.agent.config.get("sentiment_selection",{}).get("enabled") or self.discovery:
                    try:
                        if self.discovery:fresh_news=await self.discovery.news_for(record.ticker)
                        else:
                            from core.weekly.news import WeeklyNewsFetcher
                            from core.data.sentiment import get_sentiment_model
                            from core.discovery.news_features import collect_news_features,NewsHistory
                            if self.held_news_fetcher is None:self.held_news_fetcher=WeeklyNewsFetcher()
                            model=await asyncio.to_thread(get_sentiment_model)
                            fresh_news=await collect_news_features(NewsHistory(self.pm._path.parent.parent/"data/sentiment_history.json"),self.held_news_fetcher,model,record.ticker)
                        self.health.record("held_news:"+record.ticker,success=True)
                    except Exception as exc:self.health.record("held_news:"+record.ticker,success=False,error=exc)
                async with self.pm.operation_lock:
                    current = next(
                        (
                            r
                            for r in self.pm.get_open_records()
                            if r.ticker == record.ticker
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
                    from core.discovery.features import technical_features
                    from core.exits.triggers import nightly_signals
                    from utils.strategy_config import DEFAULTS
                    import json

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
                    facts = self.evidence_for(record.ticker)
                    if fresh_news:facts.update(fresh_news)
                    comparable_gap = None
                    entry = (
                        current.lots[0].get("decision", {}).get("evidence", {})
                        if current.lots
                        else {}
                    )
                    if entry.get("cohort") and fresh_news:
                        from core.discovery.divergence import score_fixed_reference

                        comparable_gap = score_fixed_reference(
                            dict(facts, **feature), entry, cfg
                        )
                    pead_path = self.pm._path.parent.parent / "data/pead_events.json"
                    try:pead_data = json.loads(pead_path.read_text()) if pead_path.exists() else {}
                    except (OSError,ValueError) as exc:
                        pead_data={}
                        self.health.record("pead_data",success=False,error=exc)
                    best = None
                    if self.discovery:
                        from core.discovery.shortlist import load_shortlist

                        try:
                            snapshot = load_shortlist(self.discovery.path,self.discovery.cfg["shortlist_max_age_days"])
                            best = next(iter(snapshot["new_candidates"]), None)
                        except (OSError,ValueError) as exc:self.health.record("discovery_review_evidence",success=False,error=exc)
                    if settings["exits"]["enabled"]:nightly_signals(
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
                failures.append(f"{record.ticker}: {exc}")
        self.health.record(
            "nightly",
            success=not failures,
            error="; ".join(failures) if failures else None,
        )
        from utils.persistence import atomic_json

        atomic_json(
            self.pm._path.parent.parent / "data/held_features_latest.json",
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "rows": self.held_features,
                "errors": failures,
            },
        )
        if self.discovery:
            try:
                await self.discovery.features()
                self.health.record("discovery_features", success=True)
            except Exception as exc:
                self.health.record("discovery_features", success=False, error=exc)

    async def weekly(self):
        if not self.discovery:
            return
        await self.reconcile()
        try:
            await self.discovery.rank()
            self.health.record("discovery_ranking", success=True)
        except Exception as exc:
            self.health.record("discovery_ranking", success=False, error=exc)

    async def attribution(self):
        from core.attribution import build_report
        try:
            await self.reconcile()
            tickers=[r.ticker for r in self.pm.get_open_records()]
            quotes={}
            for start in range(0,len(tickers),20):
                for q in await self.agent.mcp_client.get_equity_quotes(tickers[start:start+20]):
                    if fresh_timestamp(q.timestamp,5*86400):quotes[q.ticker]=q
            report=build_report(self.pm,quotes=quotes,equity=self.last_portfolio.total_value)
            build_report(self.agent.executor.paper_manager)
            self.pm.write_performance_snapshot(quotes)
            self.health.record("attribution",success=True,closed_positions=report["metrics"]["closed_positions"])
            return report
        except Exception as exc:
            self.health.record("attribution",success=False,error=exc)

    def sync_fill_journals(self):
        if not hasattr(self.agent,"journal_manager"):return
        import json
        from models.decisions import JournalEntry
        from utils.persistence import atomic_json
        path=self.pm._path.parent/"journal_sync.json"
        try:
            synced=set(json.loads(path.read_text())) if path.exists() else set()
            for fill_id,event in self.pm.snapshot()["fills"].items():
                if fill_id in synced:continue
                d=event["decision"]
                entry=JournalEntry(timestamp=event["timestamp"],cycle_type="CONFIRMED_FILL",
                    event_id=fill_id,mode=self.pm.mode,decision_id=event.get("decision_id"),
                    position_id=event["position_id"],order_id=event["order_id"],
                    decision=d.get("decision",event["side"].upper()),decision_rationale=d.get("reasoning_summary",""),
                    bull_thesis=d.get("bull_thesis",""),bear_thesis=d.get("bear_thesis",""),fill_price=event["price"])
                if self.agent.journal_manager.append(event["ticker"],entry):synced.add(fill_id)
            atomic_json(path,sorted(synced))
        except Exception as exc:self.health.record("journal_sync",success=False,error=exc)
