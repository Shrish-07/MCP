from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone, time, timedelta
from pathlib import Path

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.data.ingest import DataIngestLayer
from core.decision.engine import DecisionEngine
from core.execution.engine import ExecutionEngine
from core.data.metrics import MetricsEngine
from core.data.news import NewsFetcher
from models.news import NewsItem
from core.position.pnl_tracker import PnLTracker
from core.position.manager import PositionManager, ExitSignal, position_snapshot
from robinhood_mcp.robinhood_client import RobinhoodMCPClient
from models.decisions import (
    DecisionType, CycleType, JournalEntry, TradeDecision, StructuredEdgeScore,
    SUCCESS_STATUSES,
)
from models.market_data import MarketMetrics
from models.portfolio import Portfolio, PositionRecord
from utils.config import load_config
from utils.market_calendar import is_market_open_at
from utils.journal_manager import JournalManager
from utils.earnings_calendar import EarningsCalendar
from utils.anthropic_client import AnthropicClient
from utils.cost_tracker import CostTracker
from core.weekly.selector import WeeklySelector, get_weekly_selector
from utils.logger import configure_logging
from core.backend import Backend


# Canonical exit-review rubric now lives in models.decisions (shared with
# core/decision/engine.py) — imported above as EXIT_REVIEW_RULES.


# Compact monitor-only system prompt for the 1-minute threshold path (~300 tokens).
# Replaces the long DecisionEngine system prompt on monitor complete() calls so
# repeated STOP_LOSS / PROFIT_TARGET / PROFIT_REVIEW reviews bill far less input.
_MONITOR_SYSTEM_PROMPT_TEXT = """\
You are reviewing an EXISTING open equity position after a price threshold fired.
Your only job: decide HOLD or SELL for this one ticker. Do not recommend BUY, IGNORE, or ROTATE.

Always ground reasoning in entry price vs current price and unrealized P&L %.
SELL requires explicit evidence (thesis broken, technical failure vs plan, or capital better deployed).
HOLD is valid when the thesis still holds and the move is noise or still unfolding.

For EXIT decisions, score edge as EXIT CONVICTION (not buy quality):
- catalyst_strength (0-2): strength of the exit reason
- technical_confirmation (0-2): price action supports exit
- portfolio_fit (0-1): value of freeing capital / cutting risk
Code will sum these for observability; SELL does not use an entry-score gate.
STOP_LOSS is a hard risk rule: decision must be SELL.

Earnings proximity is informational only — not an automatic SELL.
Cite only metrics present in the user message. Do not invent numbers.

Return ONLY valid JSON (no markdown):
{
  "bull_thesis": "string",
  "bear_thesis": "string",
  "failure_conditions": ["string"],
  "decision": "HOLD|SELL",
  "action_type": "NONE|CLOSE",
  "rotate_from_ticker": null,
  "edge": {"catalyst_strength": 0.0, "technical_confirmation": 0.0, "portfolio_fit": 0.0},
  "risk_notes": "string",
  "reasoning_summary": "string — must mention entry vs current and unrealized %"
}
"""

_MONITOR_SYSTEM_PROMPT = [
    {
        "type": "text",
        "text": _MONITOR_SYSTEM_PROMPT_TEXT,
        "cache_control": {"type": "ephemeral"},
    }
]


# Per-ticker cooldowns for SOFT monitor signals so a stuck >=15% / >=20% gain
# does not re-query the LLM between ticks after a HOLD. STOP_LOSS is a hard
# risk rule and is NEVER throttled. Values come from settings.json
# (monitor.profit_review_cooldown_min / monitor.profit_target_cooldown_min);
# these module constants only seed fallbacks if config keys are removed,
# and self._monitor_locks was folded into PositionManager's shared registry.


class TradingAgent:
    """Anthropic-only trading agent orchestrating ingest, metrics, news, decisions,
    and execution.  Provides a three-cycle schedule plus a 60-second price monitor
    for real-time stop-loss / take-profit enforcement on held positions."""

    def __init__(self) -> None:
        self.logger = logging.getLogger("robinhood-agent.main")
        self.scheduler = AsyncIOScheduler(timezone="America/New_York")
        self.config = load_config()  # settings.json is the single source of truth
        self.cost_tracker = CostTracker()
        self.cost_tracker.set_pricing(self.config.get("cost_tracking", {}).get("pricing"),
                                     self.config.get("cost_tracking", {}).get("pricing_version"))
        self.mcp_client = RobinhoodMCPClient(url=self.config["execution"]["mcp_url"])
        self.ingest = DataIngestLayer(self.mcp_client)
        self.metrics_engine = MetricsEngine()
        self.news_fetcher = NewsFetcher(
            finnhub_api_key=self.config["env"]["FINNHUB_API_KEY"],
            settings=self.config.get("news", {}),
        )
        self.journal_manager = JournalManager()
        self.earnings_calendar = EarningsCalendar()
        self.earnings_calendar.set_client(self.mcp_client)
        self.decision_engine = DecisionEngine(
            AnthropicClient(
                model=self.config["models"]["decision"],
                max_tokens=int(self.config["models"].get("max_tokens", 4096)),
                send_temperature=bool(self.config["models"].get("send_temperature", False)),
            ),
            self.journal_manager,
            self.earnings_calendar,
            settings=self.config,
        )
        self.position_manager = PositionManager(config=self.config)
        self.pnl_tracker = PnLTracker(self.mcp_client)
        self.executor = ExecutionEngine(self.mcp_client, self.position_manager, self.config)
        self._cached_watchlist: list[str] = []
        self._cached_watchlist_date: str = ""

        # Weekly sentiment selector (lazy-loaded on first use)
        self._weekly_selector: WeeklySelector | None = None
        self._weekly_selector_task=None

        # ---- Price monitor state (sections 3 / 5 / 6) ----
        # Per-ticker locks now live in PositionManager.lock_for() so cycles
        # and the monitor can never race the same holding.
        self._monitor_cache: dict[str, tuple[float, MarketMetrics, list[str]]] = {}
        self._exit_in_progress: set[str] = set()
        # Per-ticker last soft-signal evaluation timestamp (UTC). Used to
        # throttle PROFIT_REVIEW / PROFIT_TARGET after a HOLD so the LLM is
        # not re-queried every tick. STOP_LOSS ignores this entirely.
        self._last_monitor_review: dict[str, datetime] = {}

        # Config-driven monitor behaviour (settings.json → monitor.*)
        _mon = self.config.get("monitor", {})
        self._monitor_soft_cd = timedelta(minutes=float(_mon.get("profit_review_cooldown_min", 15)))
        self._monitor_target_cd = timedelta(minutes=float(_mon.get("profit_target_cooldown_min", 5)))
        self._monitor_span = str(_mon.get("historicals_span", "3month"))
        self._monitor_max_hold = bool(_mon.get("check_max_holding", True))
        self.backend = Backend(self)

    # ==================================================================
    #  CONFIG
    # ==================================================================

    def _load_config(self) -> dict:
        """Thin shim: the canonical loader lives in utils.config.
        Kept so any external callers/tests keep working."""
        return load_config()

    async def _get_weekly_selector(self) -> WeeklySelector:
        """Lazy initialization of weekly selector."""
        if self._weekly_selector is None:
            if self._weekly_selector_task is None:
                self._weekly_selector_task=asyncio.create_task(get_weekly_selector(self.config,position_manager=self.position_manager))
            self._weekly_selector = await self._weekly_selector_task
            self._weekly_selector.position_manager = self.position_manager
        return self._weekly_selector

    async def _resolve_watchlist(self) -> list[str]:
        """Resolve the trading watchlist, preferring the sentiment-selected universe."""
        discovery=self.config.get("discovery",{})
        if discovery.get("enabled"):
            from core.discovery.shortlist import load_shortlist
            try:
                snapshot=load_shortlist(Path(__file__).resolve().parent/discovery["shortlist_file"],discovery["shortlist_max_age_days"])
                held={r.ticker for r in self.position_manager.get_open_records()} if self.position_manager.reconciliation_ok else set()
                return [r["ticker"] for r in snapshot["new_candidates"] if r["ticker"] not in held][:discovery["shortlist"]["new_candidate_count"]]
            except (OSError,ValueError,KeyError) as exc:
                self.logger.warning("discovery_fallback_to_existing_selector error=%s",exc)
        # Prefer the weekly sentiment-selected universe when enabled
        try:
            selector = await self._get_weekly_selector()
            if selector.enabled:
                selected = selector.get_selected_universe()
                if selected:
                    self.logger.info("watchlist_source=weekly_selector count=%d", len(selected))
                    return selected
        except Exception as exc:
            self.logger.warning("weekly_selector unavailable, falling back to config: %s", exc)

        # Fallback: Robinhood MCP watchlist
        watchlist_name = self.config.get("watchlist", {}).get("robinhood_watchlist_name", "Default")
        live_tickers = await self.mcp_client.get_watchlist_tickers(watchlist_name)
        if live_tickers:
            tickers = live_tickers[:20]
            self.logger.info("watchlist_source=robinhood_mcp count=%d name=%s", len(tickers), watchlist_name)
            return tickers
        fallback = list(self.config["watchlist"]["default_tickers"])
        self.logger.info("watchlist_source=settings_json count=%d (MCP returned empty)", len(fallback))
        return fallback

    def _is_market_open(self) -> bool:
        """Real NYSE calendar (holidays + half-days handled); fails SAFE
        (closed) if the calendar itself errors. See utils/market_calendar."""
        return is_market_open_at()

    def _earnings_flag(self, ticker: str) -> bool:
        try:
            return self.earnings_calendar.is_in_earnings_window(ticker)
        except Exception:
            return False

    def _earnings_window_block(self, ticker: str) -> str:
        try:
            return self.earnings_calendar.earnings_window_for_prompt(ticker)
        except Exception:
            return ""

    # ==================================================================
    #  PRICE MONITOR — config-speed loop (settings.json → monitor.*)
    # ==================================================================

    async def _fetch_monitor_metrics(self, ticker: str) -> MarketMetrics:
        """Fetch current metrics for a monitor threshold review."""
        try:
            bars = await self.mcp_client.get_equity_historicals(
                ticker, span=self._monitor_span, interval="day",
            )
            if bars:
                return await self.metrics_engine.compute(ticker, bars, self.mcp_client)
        except Exception:
            pass
        if ticker in self._monitor_cache:
            _, cached_m, _ = self._monitor_cache[ticker]
            return cached_m
        return MarketMetrics(ticker=ticker)

    async def _fetch_monitor_news(self, ticker: str) -> list[str]:
        """Fetch Finnhub news for a monitor review; falls back empty."""
        try:
            fetched = await self.news_fetcher.fetch_news([ticker], cycle_type="MONITOR")
            raw_items = fetched.get(ticker, [])
            if raw_items and isinstance(raw_items[0], NewsItem):
                return [
                    f"[{n.source}] {n.headline}"
                    + (f" — {n.summary}" if n.summary else "")
                    for n in raw_items
                ]
        except Exception:
            pass
        if ticker in self._monitor_cache:
            _, _, cached_news = self._monitor_cache[ticker]
            return cached_news
        return []

    async def _monitor_refresh_data(
        self, ticker: str, window: str,
    ) -> tuple[MarketMetrics, list[str]]:
        """Return (metrics, news) for a monitor trigger. A 15-minute in-memory
        cache keeps repeated threshold hits cheap; anything older is refetched
        fresh. The monitor only ever runs while the market is open, so no
        PRE/POST branching exists — the window arg is kept for log context
        only and never gates freshness."""
        now_ts = datetime.now(timezone.utc).timestamp()

        if ticker in self._monitor_cache:
            ts, cached_m, cached_news = self._monitor_cache[ticker]
            if now_ts - ts < 900:  # 15 minutes
                return cached_m, cached_news

        m = await self._fetch_monitor_metrics(ticker)
        nl = await self._fetch_monitor_news(ticker)

        self._monitor_cache[ticker] = (now_ts, m, nl)
        return m, nl

    async def _evaluate_threshold_signal(
        self,
        signal: ExitSignal,
        record: PositionRecord,
        cycle_label: str,
        metrics: MarketMetrics,
        news: list[str],
        portfolio: Portfolio,
        spy_context: str,
        open_positions_pnl: dict[str, float] | None = None,
    ) -> TradeDecision:
        """Compact re-evaluation prompt for a threshold hit (STOP_LOSS,
        PROFIT_TARGET, PROFIT_REVIEW). STOP_LOSS returns a deterministic
        SELL immediately without a model or optional data request.

        The other two are soft — model decides HOLD vs SELL.
        """
        if signal.reason == "STOP_LOSS":
            return TradeDecision(ticker=signal.ticker, decision=DecisionType.SELL,
                action_type="CLOSE", reasoning_summary=f"HARD_STOP price={signal.current_price} stop={record.effective_stop_px}",
                cycle_type="MONITOR", timestamp=datetime.now(timezone.utc).isoformat())
        journal_block = ""
        try:
            journal_block = self.journal_manager.summarise_for_prompt(signal.ticker, n=15)
        except Exception:
            journal_block = ""

        try:
            days_held = (datetime.now(timezone.utc).date()
                         - datetime.fromisoformat(record.entry_date).date()).days
        except Exception:
            days_held = 0

        unrealized_pct = signal.unrealized_pct

        # One-line POSITION snapshot + EARNINGS WINDOW block + EXIT REVIEW RULES
        pos_block = ""
        try:
            pos_block = position_snapshot(record, signal.current_price) + "\n"
        except Exception:
            pos_block = ""
        earnings_block = self._earnings_window_block(signal.ticker)

        # Compact account context: this ticker's full detail is already in
        # pos_block above; positions.json (via PositionManager) is the
        # single source of truth for everything else — name + live P&L only.
        pnl_map = open_positions_pnl or {}
        roster_line = PositionManager.other_positions_line(pnl_map, signal.ticker)
        account_line = f"ACCOUNT: total_value={portfolio.total_value:.2f} cash_pct={portfolio.cash_pct:.1f}%\n{roster_line}\n"

        user_prompt = (
            f"CYCLE: {cycle_label}\n"
            + pos_block
            + (earnings_block + "\n" if earnings_block else "")
            + f"TRIGGER: {signal.reason}\n"
            f"TICKER: {signal.ticker}\n"
            f"ENTRY_PRICE: {record.entry_price:.2f}\n"
            f"CURRENT_PRICE: {signal.current_price:.2f}\n"
            f"UNREALIZED_PNL_PCT: {unrealized_pct * 100:.2f}%\n"
            f"DAYS_HELD: {days_held}\n"
            f"STOP_LOSS_PCT: {record.stop_loss_pct * 100:.1f}%\n"
            f"TAKE_PROFIT_PCT: {record.take_profit_pct * 100:.1f}%\n"
            f"POSITION QUANTITY: {record.quantity:.6f}\n\n"
            f"JOURNAL HISTORY (last 15 entries - price & metric trajectory only):\n"
            f"{journal_block}\n\n"
            f"LATEST METRICS:\n{json.dumps(metrics.model_dump(mode='json'), indent=2)}\n\n"
            f"LATEST NEWS:\n{json.dumps(news, indent=2)}\n\n"
            f"{account_line}\n"
            + "You previously entered this position. A threshold has been crossed.\n"
            "Re-evaluate using ALL prior journal context plus the fresh data above.\n"
            "Decide strictly HOLD or SELL. Return only the JSON schema required by the system prompt.\n"
            + ("For STOP_LOSS the decision must be SELL.\n" if signal.reason == "STOP_LOSS" else "")
        )

        # ----- Soft threshold: model decides HOLD vs SELL -----
        try:
            response = await self.decision_engine.client.complete(
                _MONITOR_SYSTEM_PROMPT, user_prompt,
            )
            payload = json.loads(self.decision_engine._strip_fences(response))
            decision_type = self.decision_engine._parse_decision(
                payload.get("decision", "HOLD"), DecisionType.HOLD,
            )
            edge = self.decision_engine._parse_edge(payload, signal.ticker)

            return TradeDecision(
                ticker=signal.ticker,
                bull_thesis=str(payload.get("bull_thesis", "")),
                bear_thesis=str(payload.get("bear_thesis", "")),
                failure_conditions=list(payload.get("failure_conditions", [])) if payload.get("failure_conditions") else [],
                decision=decision_type,
                edge=edge,
                action_type="CLOSE" if decision_type == DecisionType.SELL else "NONE",
                replaced_ticker=None,
                rotate_from_ticker=None,
                risk_notes=str(payload.get("risk_notes", "")),
                reasoning_summary=f"MONITOR_{signal.reason}: {payload.get('reasoning_summary', '')}",
                timestamp=datetime.now(timezone.utc).isoformat(),
                cycle_type="MONITOR",
            )
        except Exception as exc:
            self.logger.warning("monitor_eval_failed ticker=%s error=%s", signal.ticker, exc)
            return TradeDecision(
                ticker=signal.ticker,
                bull_thesis="Fallback monitor decision.",
                bear_thesis="Fallback monitor decision.",
                failure_conditions=["Monitor model unavailable"],
                decision=DecisionType.HOLD,
                position_size_pct=0.0,
                action_type="NONE",
                replaced_ticker=None,
                edge=StructuredEdgeScore(),
                risk_notes="Monitor fallback due to API failure.",
                reasoning_summary="MONITOR_FALLBACK: model unavailable",
                timestamp=datetime.now(timezone.utc).isoformat(),
                cycle_type="MONITOR",
            )

    async def run_price_monitor(self) -> None:
        await self.backend.monitor()

    async def run_cycle(self, cycle_type: CycleType) -> None:
        """Serialize the three scheduled decision jobs and startup catch-up."""
        lock = getattr(self, "_decision_cycle_lock", None)
        if lock is None:
            lock = self._decision_cycle_lock = asyncio.Lock()
        if lock.locked():
            self.logger.info("cycle_skipped another decision cycle is active")
            return
        async with lock:
            await self._run_cycle(cycle_type)

    async def _run_cycle(self, cycle_type: CycleType) -> None:
        """Run one full trading cycle (OPEN / MID / CLOSE)."""
        cycle_str = cycle_type.value if isinstance(cycle_type, CycleType) else str(cycle_type)
        self.logger.info("=== cycle_start cycle=%s dry_run=%s ===", cycle_str, self.config["env"]["DRY_RUN"])

        # Cost tracking is OBSERVABILITY ONLY (see utils/cost_tracker.py):
        # totals are logged at cycle end, trading never halts for spend.

        if not self._is_market_open():
            self.logger.info("cycle_skipped market_closed")
            return

        await self.backend.monitor()
        if not await self.backend.reconcile():
            return
        today = datetime.now(timezone.utc).date().isoformat()
        if self._cached_watchlist_date == today and self._cached_watchlist:
            watchlist_tickers = self._cached_watchlist
        else:
            watchlist_tickers = await self._resolve_watchlist()
            self._cached_watchlist = watchlist_tickers
            self._cached_watchlist_date = today

        open_records = self.position_manager.get_open_records()
        existing_tickers = [r.ticker for r in open_records]

        all_fetch_tickers = list(dict.fromkeys(["SPY"] + watchlist_tickers + existing_tickers))

        # Ingest fresh data
        data = await self.ingest.ingest(all_fetch_tickers, [])
        portfolio = data["portfolio"]
        quotes = data["quotes"]
        historicals = data["historicals"]

        # Stale-data guard: if the calendar says the market is open but SPY
        # has no usable quote, the data path is unhealthy — skip the cycle
        # rather than let the model reason over phantom numbers.
        if bool(self.config.get("trading", {}).get("market_hours", {}).get("stale_data_guard", True)):
            spy_quote = quotes.get("SPY")
            if spy_quote is None or float(getattr(spy_quote, "price", 0.0) or 0.0) <= 0:
                self.logger.warning("cycle_skipped stale_data_guard: SPY quote unavailable while market open")
                return

        # ---- SECTION 1: reconcile live MCP portfolio ⟷ positions.json ----
        if portfolio.reconciliation_status != "SUCCESS" or not portfolio.positions_complete or not self.position_manager.account_matches(portfolio):
            self.position_manager.mark_unverified("decision-cycle portfolio unavailable or mismatched")
            return
        open_records = self.position_manager.get_open_records()
        existing_tickers = [r.ticker for r in open_records]

        self.logger.info(
            "ingest_complete cycle=%s tickers=%d quotes_fetched=%d historicals_filled=%d",
            cycle_str, len(all_fetch_tickers), len(quotes),
            sum(1 for bars in historicals.values() if bars),
        )
        if cycle_type == CycleType.OPEN:
            try:
                await self.earnings_calendar.refresh(watchlist_tickers)
            except Exception:
                pass

        # Compute metrics
        metrics = {}
        metrics_tasks = {
            t: self.metrics_engine.compute(t, historicals.get(t, []), self.mcp_client)
            for t in all_fetch_tickers
        }
        results_list = await asyncio.gather(*metrics_tasks.values(), return_exceptions=True)
        for ticker, result in zip(metrics_tasks.keys(), results_list):
            if isinstance(result, Exception):
                self.logger.warning("metrics_compute_failed ticker=%s error=%s", ticker, result)
                metrics[ticker] = MarketMetrics(ticker=ticker)
            else:
                metrics[ticker] = result

        # fix stale current_price from metrics with live quote price.
        # distance_from_20dma is recomputed from the REAL sma_20 value stored
        # by MetricsEngine — no more reconstructing an SMA from a percentage.
        for ticker, m in metrics.items():
            quote = quotes.get(ticker)
            if quote and quote.price > 0 and m.current_price > 0:
                m.current_price = quote.price
                if m.sma_20 > 0:
                    m.distance_from_20dma = ((quote.price - m.sma_20) / m.sma_20) * 100.0

        spy_m = metrics.get("SPY")
        for position in portfolio.positions:
            if position.ticker in metrics and metrics[position.ticker].sector:
                position.sector=metrics[position.ticker].sector
        spy_context = ""
        if spy_m and spy_m.current_price > 0:
            spy_context = (
                f"MARKET CONTEXT (SPY): "
                f"5d={spy_m.return_5d:+.2f}% "
                f"30d={spy_m.return_30d:+.2f}% "
                f"vs_20dma={spy_m.distance_from_20dma:+.2f}% "
                f"rsi={spy_m.rsi_14:.1f} "
                f"vol_30d={spy_m.realized_vol_30d:.1f}%"
            )

        raw_news = await self.news_fetcher.fetch_news(
            all_fetch_tickers, cycle_str,
        )
        news: dict[str, list[str]] = {}
        for ticker, items in raw_news.items():
            if isinstance(items, list) and items and isinstance(items[0], NewsItem):
                news[ticker] = [
                    f"[{n.source}] {n.headline}"
                    + (f" — {n.summary}" if n.summary else "")
                    for n in items
                ]
            else:
                news[ticker] = items  # type: ignore[assignment]

        exit_signals = self.position_manager.check_exits(portfolio, quotes)
        effective_buying_power = float(portfolio.buying_power)
        # positions.json (via PositionManager) is the single source of truth
        # for the position count against the 12-slot cap — not the raw live
        # portfolio object.
        effective_positions_count = len(open_records)
        # {ticker: unrealized_pct} for the whole open book, computed ONCE
        # per cycle here and reused across every review_exit/make_decision
        # call below, instead of re-sending full detail on every position
        # into every single ticker's prompt.
        pnl_map = self.position_manager.open_positions_pnl_map(quotes)

        # Process exit signals (hard stops) — under the SHARED per-ticker
        # locks so the 15-second monitor can never race this same holding.
        closed_this_cycle: set[str] = set()
        for signal in exit_signals:
            ticker = signal.ticker
            quote = quotes.get(ticker)
            current_price = quote.price if quote else signal.current_price
            held_record = next((r for r in open_records if r.ticker == ticker), None)
            exit_decision = await self.decision_engine.review_exit(
                signal,
                metrics.get(ticker, MarketMetrics(ticker=ticker)),
                news.get(ticker, []),
                portfolio,
                cycle_type,
                spy_context=spy_context,
                position_record=held_record,
                open_positions_pnl=pnl_map,
                effective_buying_power=effective_buying_power,
                evidence=self.backend.evidence_for(ticker,metrics.get(ticker),quote),
            )
            try:
                mm = metrics.get(ticker)
                key_metrics = mm.model_dump(mode="json") if mm and hasattr(mm, "model_dump") else {}
                entry = JournalEntry(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    cycle_type=cycle_str,
                    bull_thesis=exit_decision.bull_thesis,
                    bear_thesis=exit_decision.bear_thesis,
                    decision=exit_decision.decision.value,
                    decision_rationale=exit_decision.reasoning_summary,
                    key_metrics=key_metrics,
                    news_used=news.get(ticker, []),
                    fill_price=None,entry_price=held_record.entry_price if held_record else None,
                    decision_id=exit_decision.decision_id,mode="decision",
                    unrealized_pnl_pct=signal.unrealized_pct,
                    earnings_flag=self._earnings_flag(ticker),
                )
                self.journal_manager.append(ticker, entry)
            except Exception:
                pass
            if exit_decision.decision == DecisionType.SELL:
                async with self.position_manager.operation_lock:
                    result = await self.executor.execute(exit_decision, portfolio, current_price, dry_run=self.config["env"]["DRY_RUN"], quote_timestamp=getattr(quote, "timestamp", ""), vol_30d=metrics.get(ticker).realized_vol_30d if metrics.get(ticker) else 0.0, avg_volume_30d=metrics.get(ticker).avg_volume_30d if metrics.get(ticker) else 0.0, realized_vol_30d=metrics.get(ticker).realized_vol_30d if metrics.get(ticker) else 0.0)
                if result.status in SUCCESS_STATUSES:
                    effective_buying_power += signal.quantity * current_price
                    effective_positions_count = max(0, effective_positions_count - 1)  # clamp (never negative)
                    closed_this_cycle.add(ticker)
                    pnl_map.pop(ticker, None)

        # Refresh AFTER exits: Path A/B must never re-review a position that
        # was already closed this cycle (kills phantom double reviews,
        # wasted LLM calls, and double-sell windows).
        if closed_this_cycle:
            open_records = self.position_manager.get_open_records()
            existing_tickers = [r.ticker for r in open_records]

        # ---- SECTION 2: Path A (held) + Path B (non-held) ----
        #
        # Path A: every existing position gets a routine re-evaluation.
        # Path B: new watchlist positions evaluated normally.

        # ---- Path A: held tickers (never skipped) ----
        for ticker in existing_tickers:
            if ticker in self.backend.review_active:
                continue
            record = next((r for r in open_records if r.ticker == ticker), None)
            if record is None:
                continue
            quote = quotes.get(ticker)
            current_price = quote.price if quote else 0.0
            if current_price <= 0:
                continue

            unrealized_pct = ((current_price - record.entry_price)
                              / record.entry_price if record.entry_price else 0.0)
            routine_signal = ExitSignal(
                ticker=ticker,
                reason="ROUTINE",
                current_price=current_price,
                unrealized_pct=unrealized_pct,
                quantity=record.quantity,
            )
            routine_decision = await self.decision_engine.review_exit(
                routine_signal,
                metrics.get(ticker, MarketMetrics(ticker=ticker)),
                news.get(ticker, []),
                portfolio,
                cycle_type,
                spy_context=spy_context,
                position_record=record,
                open_positions_pnl=pnl_map,
                effective_buying_power=effective_buying_power,
                evidence=self.backend.evidence_for(ticker,metrics.get(ticker),quote),
            )

            # Journal the held review (numbers consistent with positions.json)
            try:
                mm = metrics.get(ticker)
                key_metrics = mm.model_dump(mode="json") if mm and hasattr(mm, "model_dump") else {}
                entry = JournalEntry(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    cycle_type=cycle_str,
                    bull_thesis=routine_decision.bull_thesis,
                    bear_thesis=routine_decision.bear_thesis,
                    decision=routine_decision.decision.value,
                    decision_rationale=routine_decision.reasoning_summary,
                    key_metrics=key_metrics,
                    news_used=news.get(ticker, []),
                    fill_price=None,entry_price=record.entry_price,
                    decision_id=routine_decision.decision_id,mode="decision",
                    unrealized_pnl_pct=unrealized_pct,
                    earnings_flag=self._earnings_flag(ticker),
                )
                self.journal_manager.append(ticker, entry)
            except Exception:
                pass

            if routine_decision.decision in (DecisionType.SELL,DecisionType.ADD_TO_POSITION):
                async with self.position_manager.operation_lock:
                    result = await self.executor.execute(routine_decision, portfolio, current_price, dry_run=self.config["env"]["DRY_RUN"], quote_timestamp=getattr(quote, "timestamp", ""), vol_30d=metrics.get(ticker).realized_vol_30d if metrics.get(ticker) else 0.0, avg_volume_30d=metrics.get(ticker).avg_volume_30d if metrics.get(ticker) else 0.0, realized_vol_30d=metrics.get(ticker).realized_vol_30d if metrics.get(ticker) else 0.0)
                if result.status in SUCCESS_STATUSES and routine_decision.decision == DecisionType.SELL:
                    effective_buying_power += record.quantity * current_price
                    effective_positions_count = max(0, effective_positions_count - 1)
                    closed_this_cycle.add(ticker)
                    pnl_map.pop(ticker, None)
                    # record_exit is already performed inside ExecutionEngine.execute on SELL fill.
                    # Do not call record_exit again here.

        # ---- Path B: non-held watchlist tickers (unchanged) ----
        for ticker in [t for t in watchlist_tickers if t != "SPY"]:
            if ticker in existing_tickers:
                continue
            quote = quotes.get(ticker)
            current_price = quote.price if quote else 0.0
            decision = await self.decision_engine.make_decision(
                ticker,
                metrics.get(ticker, MarketMetrics(ticker=ticker)),
                news.get(ticker, []),
                portfolio,
                current_price,
                effective_buying_power,
                effective_positions_count,
                cycle_type,
                spy_context=spy_context,
                open_positions_pnl=pnl_map,
                evidence=self.backend.evidence_for(ticker,metrics.get(ticker),quote),
            )
            mm = metrics.get(ticker)
            # ROTATE touches the HELD rotate_from position — take that ticker's
            # shared lock so the monitor can never fire mid-swap.
            if decision.decision == DecisionType.ROTATE and decision.rotate_from_ticker:
                async with self.position_manager.operation_lock:
                    result = await self.executor.execute(
                        decision,
                        portfolio,
                        current_price,
                        dry_run=self.config["env"]["DRY_RUN"],
                        quote_timestamp=getattr(quote, "timestamp", ""),
                        vol_30d=mm.realized_vol_30d if mm else 0.0,
                        avg_volume_30d=mm.avg_volume_30d if mm else 0.0,
                        realized_vol_30d=mm.realized_vol_30d if mm else 0.0,
                    )
            else:
                result = await self.executor.execute(
                    decision,
                    portfolio,
                    current_price,
                    dry_run=self.config["env"]["DRY_RUN"],
                    quote_timestamp=getattr(quote, "timestamp", ""),
                    vol_30d=mm.realized_vol_30d if mm else 0.0,
                    avg_volume_30d=mm.avg_volume_30d if mm else 0.0,
                    realized_vol_30d=mm.realized_vol_30d if mm else 0.0,
                )
            # Journal AFTER execute so fill_price can come from the fill
            # (matching positions.json). For IGNORE/HOLD fill_price is null.
            try:
                key_metrics = mm.model_dump(mode="json") if mm and hasattr(mm, "model_dump") else {}
                fill_price_for_journal = None
                if result.status in SUCCESS_STATUSES and result.fill_price:
                    fill_price_for_journal = float(result.fill_price)
                entry = JournalEntry(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    cycle_type=cycle_str,
                    bull_thesis=decision.bull_thesis,
                    bear_thesis=decision.bear_thesis,
                    decision=decision.decision.value,
                    decision_rationale=decision.reasoning_summary,
                    key_metrics=key_metrics,
                    news_used=news.get(ticker, []),
                    fill_price=fill_price_for_journal,
                    decision_id=decision.decision_id,order_id=result.order_id or None,
                    mode="paper_preview" if result.dry_run else "live_decision",
                    unrealized_pnl_pct=None,
                    earnings_flag=self._earnings_flag(ticker),
                    rotated_from=result.rotate_from_ticker if decision.decision == DecisionType.ROTATE else None,
                )
                self.journal_manager.append(ticker, entry)
                # Rotation writes a second, mirrored entry onto the OLD ticker
                # so its journal records exactly why it left the book.
                if (
                    decision.decision == DecisionType.ROTATE
                    and result.rotate_from_ticker
                    and result.status in SUCCESS_STATUSES
                ):
                    exit_entry = JournalEntry(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        cycle_type=cycle_str,
                        bull_thesis=decision.bull_thesis,
                        bear_thesis=decision.bear_thesis,
                        decision="ROTATED_OUT",
                        decision_rationale=decision.reasoning_summary,
                        key_metrics={},
                        news_used=news.get(result.rotate_from_ticker, []),
                        fill_price=result.rotate_sell_price or None,
                        unrealized_pnl_pct=None,
                        earnings_flag=self._earnings_flag(result.rotate_from_ticker),
                        rotated_to=ticker,
                    )
                    self.journal_manager.append(result.rotate_from_ticker, exit_entry)
            except Exception:
                pass
            if result.status in SUCCESS_STATUSES:
                if decision.decision == DecisionType.BUY:
                    size_pct = self.executor._size_from_edge(decision.edge.total)
                    effective_buying_power -= size_pct * portfolio.total_value
                    effective_positions_count += 1
                    # newly opened this cycle — 0% unrealized, so the very
                    # next Path B ticker's roster line already knows about it
                    pnl_map[ticker] = 0.0
                elif decision.decision == DecisionType.ROTATE:
                    # Rotation is COUNT-NEUTRAL: the sell leg freed the slot
                    # the buy leg consumed. Cash reflects both real legs:
                    # sell proceeds IN (rotate_sell_price × sold qty captured
                    # on the result) and the buy notional OUT.
                    effective_buying_power += (
                        (result.rotate_sell_price or 0.0) * (result.rotate_sell_quantity or 0.0)
                    )
                    effective_buying_power -= (result.fill_price or 0.0) * (result.quantity or 0.0)
                    if result.rotate_from_ticker:
                        pnl_map.pop(result.rotate_from_ticker, None)
                        pnl_map[ticker] = 0.0

        # ---- End of cycle: write open-position unrealized marks (no extra API) ----
        try:
            self.position_manager.write_performance_snapshot(quotes)
        except Exception as exc:
            self.logger.warning("end_of_cycle_performance_snapshot_failed: %s", exc)

        # Cost tracking is informational (never gating): log the totals.
        self.logger.info(
            "cycle_complete cycle=%s spend_today=$%.4f spend_month=$%.4f",
            cycle_str, self.cost_tracker.daily_total(), self.cost_tracker.monthly_total(),
        )

    async def _run_attribution_report(self) -> None:
        await self.backend.attribution()

    async def start(self) -> None:
        configure_logging(level=self.config.get("logging", {}).get("level", "INFO"))
        self.logger.info("agent_start dry_run=%s", self.config["env"]["DRY_RUN"])
        # Loud, unmistakable mode banner — the ONLY switch is settings.json.
        if self.config["env"]["DRY_RUN"]:
            self.logger.info("=" * 60)
            self.logger.info("*** DRY-RUN MODE — orders are simulated only ***")
            self.logger.info("*** Live activation requires owner release approval and all runtime safety gates ***")
            self.logger.info("=" * 60)
        else:
            self.logger.info("=" * 60)
            self.logger.info("*** LIVE TRADING MODE — execution.dry_run=false in settings.json ***")
            self.logger.info("*** Real orders WILL be placed through Robinhood. ***")
            self.logger.info("=" * 60)

        await self.backend.reconcile()
        self.scheduler.add_job(self.backend.heartbeat, "interval", seconds=30, max_instances=1)
        self.scheduler.add_job(self.backend.nightly,"cron",hour=20,minute=0,timezone="America/New_York",max_instances=1,coalesce=True)
        if self.backend.discovery:
            d=self.config["discovery"]["schedule"]
            self.scheduler.add_job(self.backend.weekly,"cron",day_of_week=d["rank_day"],hour=d["rank_hour_et"],minute=0,timezone="America/New_York",max_instances=1,coalesce=True)
        schedule = self.config.get("schedule", {})
        open_h = int(schedule.get("open_hour", 9))
        open_m = int(schedule.get("open_minute", 35))
        mid_h = int(schedule.get("mid_hour", 12))
        mid_m = int(schedule.get("mid_minute", 30))
        close_h = int(schedule.get("close_hour", 15))
        close_m = int(schedule.get("close_minute", 30))
        tz_name = schedule.get("timezone", "America/New_York")

        self.scheduler.add_job(
            self.run_cycle, "cron", args=[CycleType.OPEN],
            day_of_week="mon-fri", hour=open_h, minute=open_m, timezone=tz_name,
        )
        self.scheduler.add_job(
            self.run_cycle, "cron", args=[CycleType.MID],
            day_of_week="mon-fri", hour=mid_h, minute=mid_m, timezone=tz_name,
        )
        self.scheduler.add_job(
            self.run_cycle, "cron", args=[CycleType.CLOSE],
            day_of_week="mon-fri", hour=close_h, minute=close_m, timezone=tz_name,
        )

        # --- Config-speed price monitor (interval from settings.json) ---
        # Cron seconds field so the tick can be faster than 1 minute
        # (monitor.interval_seconds, default 15). Two jobs keep the tick
        # pinned to the 09:30–16:00 window; the NYSE-calendar gate inside
        # run_price_monitor pre-empts anything stale (holidays, half-days).
        mon_cfg = self.config.get("monitor", {})
        if bool(mon_cfg.get("enabled", True)):
            interval_s = max(5, int(mon_cfg.get("interval_seconds", 15)))
            second_spec = f"*/{interval_s}" if interval_s < 60 else "*"
            for hour_spec, minute_spec in (("9-15", "*"), ("16", "0")):
                self.scheduler.add_job(
                    self.run_price_monitor, "cron",
                    day_of_week="mon-fri", hour=hour_spec, minute=minute_spec,
                    second=second_spec, timezone=tz_name,
                    max_instances=1, coalesce=True,
                )
            self.logger.info("monitor_registered interval_seconds=%d", interval_s)
        # Safety jobs start before optional model initialization/bootstrap.
        self.scheduler.start()
        # --- Weekly sentiment ticker selection job (feature-flagged) ---
        try:
            selector = await self._get_weekly_selector()
            if selector.enabled:
                selector.start_scheduler(self.scheduler)
                self.logger.info("weekly_scheduler_registered")
                # Bootstrap: run once on startup if no previous selection exists
                if not selector.get_selected_universe():
                    self.logger.info("weekly_selection bootstrap: no previous selection, running now")
                    try:
                        await selector.run_now()
                    except Exception as sel_exc:
                        self.logger.error(
                            "weekly selection bootstrap failed, using config watchlist: %s", sel_exc
                        )
        except Exception as exc:
            self.logger.error("weekly_scheduler_setup_failed: %s", exc)

        # --- Daily attribution report (fix: previously defined but never scheduled) ---
        rep_cfg = self.config.get("reporting", {})
        if bool(rep_cfg.get("attribution_enabled", True)):
            self.scheduler.add_job(
                self._run_attribution_report, "cron",
                day_of_week="mon-fri",
                hour=int(rep_cfg.get("attribution_hour", 16)),
                minute=int(rep_cfg.get("attribution_minute", 5)),
                timezone=tz_name, max_instances=1, coalesce=True,
            )
            self.logger.info("attribution_report_registered at=%02d:%02d ET",
                             int(rep_cfg.get("attribution_hour", 16)),
                             int(rep_cfg.get("attribution_minute", 5)))

        # --- Smart startup catch-up ---
        tz = pytz.timezone(tz_name)
        now_et = datetime.now(tz)
        current_time = now_et.time()
        weekday = now_et.weekday()

        open_time = time(open_h, open_m)
        mid_time = time(mid_h, mid_m)
        close_time = time(close_h, close_m)
        after_hours = time(16, 0)

        if weekday >= 5:
            self.logger.info("outside market hours, waiting for next trading day (ET=%s weekday=%d)",
                             current_time.strftime("%H:%M"), weekday)
        elif current_time >= after_hours:
            self.logger.info("outside_after_hour waiting for next trading day (ET=%s)", current_time.strftime("%H:%M"))
        elif current_time < open_time:
            self.logger.info("waiting_for_open open_at=%s (ET=%s)", open_time.strftime("%H:%M"), current_time.strftime("%H:%M"))
        elif current_time < mid_time:
            self.logger.info("catch_up: running OPEN now (ET=%s)", current_time.strftime("%H:%M"))
            await self.run_cycle(CycleType.OPEN)
        elif current_time < close_time:
            self.logger.info("catch_up: running MID now (ET=%s)", current_time.strftime("%H:%M"))
            await self.run_cycle(CycleType.MID)
        else:
            self.logger.info("catch_up: running CLOSE now (ET=%s)", current_time.strftime("%H:%M"))
            await self.run_cycle(CycleType.CLOSE)

        try:
            while True:
                await asyncio.sleep(60)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            self.logger.info("agent_shutdown")
        finally:
            if self.backend.review_task and not self.backend.review_task.done():
                self.backend.review_task.cancel()
            self.scheduler.shutdown(wait=False)


if __name__ == "__main__":
    from utils.instance_lock import InstanceLock
    with InstanceLock(Path(__file__).resolve().parent/"logs/agent.lock"):
        agent = TradingAgent()
        asyncio.run(agent.start())
