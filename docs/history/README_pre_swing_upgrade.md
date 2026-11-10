# robinhood-agent

An **Anthropic-only, MCP-native, autonomous equity trading agent** for a Robinhood **Agentic** brokerage account. The system runs scheduled decision cycles throughout the trading day, reads live portfolio/quotes/technical data directly from Robinhood's MCP server over Streamable HTTP, drives all judgment through Claude (`claude-sonnet-5-5`), enforces hard risk rules deterministically in code, and executes orders through the same MCP server. Built for research, experimentation, and education — **not** for unsupervised live capital.

---

## What It Is

`robinhood-agent` is a scheduled, agentic trading loop. Three times per day (at market open, midday, and near close) it wakes up, pulls the current account state and live market data, feeds each watchlist ticker to an LLM portfolio manager (Claude), turns the model's structured JSON verdict into sizing + risk checks, and then either simulates or places the trade. A separate **config-speed price monitor** (`monitor.interval_seconds`, default **15s**) continuously checks already-open positions against hard stop-loss / take-profit / profit-review / max-hold thresholds so risk is enforced even between the three scheduled reviews. Market hours come from the real **NYSE calendar** (`pandas-market-calendars`) — holidays *and* early-close half-days are handled.

Key design decisions baked into the code:

- **Anthropic only, single model.** Every model call is `claude-sonnet-5-5` via `utils/anthropic_client.py`. The cheap Haiku tier was removed (it served two disabled features; no code calls it).
- **MCP-native data.** Market data and broker actions come from Robinhood's official trading MCP server via the `mcp` Python SDK — a direct connection that replaced an earlier, expensive `claude --print` shelling approach. No LLM tokens are spent fetching data.
- **Deterministic risk guardrails.** Stop-loss, take-profit, max positions, vol-targeted sizing, and liquidity floors are enforced in plain Python (`core/execution/engine.py`, `core/position/manager.py`), never left to the model. There are **no time-window order blocks** — exits can never be deferred, and entries are gated by evidence quality (edge bar), not the clock.
- **DRY_RUN enforced in code, single switch.** The ONLY live/dry toggle is `execution.dry_run` in `config/settings.json`; anything other than an explicit `false` degrades to dry-run (fail-safe). `.env` carries secrets only.
- **LLM cost: perfect accounting, zero enforcement.** Every call is metered from the API's own `usage` object (append-only record at `logs/cost_records.jsonl`, aggregates in `logs/cost_tracker.json`). The bot never halts or changes behaviour because of spend — the numbers exist to match your Anthropic dashboard exactly.
- **settings.json is the single source of truth** for every tunable. See also `config/robinhood_mcp_tools.json` — a reference catalog of every Robinhood MCP tool (which are live, which are dormant; nothing reads it at runtime).

---

## High-Level Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │                  main.py                    │
                    │     (TradingAgent — APScheduler loop)       │
                    │  OPEN / MID / CLOSE  +  15s monitor         │
                    └─────────────────────────────────────────────┘
                          │                │                │
                          ▼                ▼                ▼
             ┌──────────────┐       ┌──────────────┐   ┌──────────────┐
             │DataIngestLayer│       │ NewsFetcher  │   │MetricsEngine  │
             └──────────────┘       └──────────────┘   └──────────────┘
                          │                │                │
                          └──────┬─────────┴───────┬────────┘
                                 ▼                 ▼
                    ┌───────────────────┐  ┌──────────────────┐
                    │   DecisionEngine   │◄─ │  AnthropicClient │
                    └───────────────────┘  └──────────────────┘
                                 │ TradeDecision + edge score
                                 ▼
                    ┌───────────────────┐
                    │   ExecutionEngine  │  edge→size, windows, dry-run
                    └───────────────────┘
                                 │
                    ┌────────────▼─────────────┐
                    │ PositionManager + MCP     │
                    │  (positions.json, exits)  │
                    └──────────────────────────┘
```

**Data flow summary:** ingest → enrich (metrics, news) → decide (LLM) → validate/size (code) → execute (MCP) → journal + persist. Exit signals from the monitor or the cycle flow back through `review_exit` (a monitor-style HOLD/SELL prompt) and the same execution path.

---

## Directory Structure

```
robinhood-agent/
├── main.py                     # Entry point. TradingAgent: config, scheduler, cycles, monitor.
├── README.md                   # This file.
├── requirements.txt            # Core runtime dependency pins.
├── requirements-ml.txt         # Optional ML extra (torch/transformers) — only for sentiment selection.
├── requirements-dev.txt        # Dev/test tooling (pytest, pytest-asyncio, ruff).
├── CONTRIBUTING.md             # Contribution rules, dev setup, test commands.
├── SECURITY.md                 # Security policy + local-data inventory.
├── .env                        # Secrets ONLY (ANTHROPIC_API_KEY, FINNHUB_API_KEY). Not committed.
├── .env.example                # Template for the secrets above.
│
├── config/
│   ├── settings.json             # THE single source of truth for every
│   │                             # tunable/toggle (incl. execution.dry_run).
│   └── robinhood_mcp_tools.json  # Reference catalog of every RH MCP tool.
│                                 # (docs only — not read at runtime)
│
├── core/                       # Business logic layer — one package per job.
│   ├── data/                   # Data pipeline: ingest → metrics → news → sentiment.
│   │   ├── ingest.py           #   Phase-1 ingestion: portfolio + quotes + historical bars.
│   │   ├── metrics.py          #   RSI/MACD/BB/ATR/SMA/fundamentals (RH MCP) + local returns/vol.
│   │   ├── news.py             #   Finnhub news fetch, dedup, freshness (pure data — no LLM).
│   │   └── sentiment.py        #   ModernBERT sentiment scorer (singleton, lazy, fail-safe).
│   ├── decision/
│   │   └── engine.py           # LLM prompts + JSON parsing → TradeDecision / ExitDecision.
│   ├── execution/
│   │   └── engine.py           # edge→size ladder, vol-targeted sizing, fail-closed gates.
│   ├── position/
│   │   ├── manager.py         # positions.json, reconcile, exit signals, realized P&L,
│   │   │                      #   shared per-ticker locks, vol-adjusted stop bands.
│   │   └── pnl_tracker.py     # Realized P&L + trade-history report builder.
│   ├── weekly/                 # Weekly universe selection pipeline.
│   │   ├── selector.py        # Sunday-evening orchestrator (feature-flagged).
│   │   ├── news.py            # Weekly Finnhub news fetch with caching.
│   │   └── ticker_selector.py # Selection logic with absolute holding protection.
│   ├── discovery/              # RESERVED: flag-gated discovery funnel (L0–L6). Empty for now.
│   └── exits/                  # RESERVED: code-owned exit stack (trails/triggers). Empty for now.
│
├── models/                     # Pydantic data contracts (shared across layers).
│   ├── decisions.py            # DecisionType, CycleType, EdgeScore, TradeDecision,
│   │                           #   ExecutionResult (+rotation fields), SUCCESS_STATUSES.
│   ├── market_data.py          # OHLCVBar, MarketMetrics (incl. real sma_20), EquityQuote.
│   ├── news.py                 # NewsItem (Finnhub schema).
│   └── portfolio.py            # PositionRecord (+reconciled flag), Position, Portfolio.
│
├── robinhood_mcp/
│   ├── robinhood_client.py     # Direct MCP client (OAuth) — ACTIVE.
│   └── response_recorder.py    # Observability: traces/filters MCP responses.
│
├── utils/
│   ├── anthropic_client.py     # Async Anthropic wrapper (retry + exact metering) + strip_fences.
│   ├── config.py               # Central settings.json loader with fail-safe defaults.
│   ├── cost_tracker.py         # Singleton cost METER (JSONL records) — observability only.
│   ├── market_calendar.py      # NYSE holidays/half-days via pandas_market_calendars.
│   ├── rate_limit.py           # Shared sliding-window rate limiter (Finnhub).
│   ├── earnings_calendar.py    # Cached earnings-window lookups (informational only).
│   ├── journal_manager.py      # Per-ticker rolling journals for model memory.
│   └── logger.py               # UTC console/file logs + JSON trade log config.
│
├── scripts/                    # Development & verification harnesses.
│   ├── smoke_cycle.py          # One-shot live cycle smoke test (honors dry-run).
│   └── verify_weekly_layer.py  # Offline-deterministic weekly-layer verification.
│
├── tests/                      # Offline test suite (55 by default / 57 including the 2
│                               #   heavy model-download tests): config loader, cost tracker,
│                               #   execution guards, parsing, position logic, rotation,
│                               #   sentiment, ticker selector, weekly e2e.
│
├── .github/                    # CI (tests + gitleaks secret scan), CODEOWNERS, PR template.
│
├── archive/                    # Quarantined legacy/one-off artifacts (not on the import path).
│   ├── mcp_deep_schema_dump.py # One-time MCP tool-schema enumeration (dev aid).
│   ├── mcp_diagnostic_v2.py    # Standalone OAuth+MCP connectivity probe.
│   └── smoke_out.txt           # Raw output of the one-shot smoke run.
│
├── docs/
│   └── history/                # Superseded documents (kept for provenance).
│       └── AUDIT.md            # Pre-overhaul codebase audit (HISTORICAL).
│
├── data/
│   ├── earnings_calendar.json  # Cached upcoming RH earnings.
│   └── seen_headlines.json     # Finnhub article-id dedup state for the day.
│
├── journals/                   # Per-ticker history (TICKER.json) + Inactive/.
└── logs/                       # Runtime output & state (auto-created).
    ├── agent.log               # Main DEBUG log (UTC).
    ├── trades.json             # JSON array of every trade attempt.
    ├── positions.json          # Open + closed records with realized P&L.
    ├── cost_tracker.json       # Persisted daily/monthly cost aggregates.
    ├── cost_records.jsonl      # Append-only per-call LLM usage records.
    └── direct_mcp_trace.jsonl  # Recorded direct MCP calls (observability).
```

---

## The Trading Day: Cycles + Monitor

The agent is driven by an APScheduler (`AsyncIOScheduler`) with cron jobs on **US/Eastern** market hours. All times below are Eastern.

| Job | Time | What happens |
|-----|------|--------------|
| **OPEN** | 09:35 | Highest-conviction entries/rotations. Full weight given to the historical price/metric trajectory (journal) + earnings context. Also reviews clear exit signals. |
| **MID** | 12:30 | Intraday reassessment. The entry bar is slightly higher than OPEN (`cycle_edge_bonus.MID`), enforced in code. |
| **CLOSE** | 15:30 | Trades ARE allowed — entries/rotations just need the strongest evidence bar of the day (`cycle_edge_bonus.CLOSE`, default +0.5) because you cannot react until tomorrow's session. |
| **Monitor** | every 15s (in window) | Price-threshold monitoring of already-open positions: stop-loss / take-profit / profit-review / max-hold days. |

The three cycle types are modelled as `CycleType` (`OPEN`, `MID`, `CLOSE`) in `models/decisions.py`.

### What happens each cycle (`run_cycle`)

1. **Load config (`utils/config.load_config`) + resolve watchlist.** settings.json is validated with fail-safe defaults (a malformed file ⇒ dry-run). Watchlist prefers the weekly sentiment-selected universe, then the Robinhood watchlist named in config, then `watchlist.default_tickers`.
2. **Ingest** (`DataIngestLayer.ingest`): fetch the live `Portfolio`, equity quotes, and ~3 months of daily historical bars for the union of watchlist + open positions (deduped, capped at 20 symbols). **Stale-data guard:** if SPY has no usable quote while the calendar says the market is open, the cycle is skipped.
3. **Enrich:**
   - `MetricsEngine.compute` per ticker — RSI, MACD histogram, Bollinger z-score, ATR, SMA-overrun, fundamentals (market cap / P/E / dividend / sector / industry) directly from Robinhood MCP, plus locally-computed 5d/30d/90d returns, 30d drawdown, and 7d/30d realized volatility.
   - `NewsFetcher.fetch_news` — Finnhub company news per ticker + general-market news (attached to `SPY`/`MARKET`), ID-deduplicated for the day, freshness-filtered, capped per ticker.
   - `EarningsCalendar.get_upcoming` — informational earnings-window flag.
4. **Decide** (`DecisionEngine`): for each non-held watchlist ticker → `make_decision(...)` (LLM → `TradeDecision`). For held positions that tripped an exit signal → `review_exit(...)` (LLM → HOLD/SELL).
5. **Establish portfolio context:** effective buying power is "freed" as SELLs execute, positions counts and P&L maps are updated in-loop so later tickers in the same cycle see the effect of earlier fills.
6. **Execute** (`ExecutionEngine.execute`): validate against safe windows, size the position from edge, respect DRY_RUN, place/simulate the order.
7. **Journal:** append a `JournalEntry` to the ticker's journal for the cycle (`JournalManager.append`), recording bull/bear thesis, decision, rationale, key metrics, news, fill price, unrealized P&L, and earnings flag.

### The price monitor (`run_price_monitor`)

Every `monitor.interval_seconds` (default 15s, adjustable in settings.json) while the NYSE calendar says the market is open, the monitor:

1. Loads open position records (`PositionManager.get_open_records`).
2. Fetches quotes only for held tickers (no historical bars).
3. Checks thresholds against each record (all config-driven):
   - `STOP_LOSS` — hard: unrealized ≤ stop_loss_pct (vol-adjusted bands, default −7% base). **Forced SELL, never deferred, never blocked by any window.**
   - `PROFIT_TARGET` — soft: unrealized ≥ take_profit_pct (default 20%).
   - `PROFIT_REVIEW` — advisory: unrealized ≥ `trading.profit_review_pct` (default 15%).
   - `MAX_HOLDING_PERIOD` — advisory: days held > `trading.max_holding_days` (default 15). Skipped for `reconciled` records whose true entry date is unknown.
4. For each signal, calls a lightweight, **monitor-only** system prompt (`_MONITOR_SYSTEM_PROMPT_TEXT`, ~300 tokens) that produces a HOLD/SELL verdict with an EXIT-CONVICTION edge score.
5. STOP_LOSS is never overridden: the decision is forced to SELL in code; the model is consulted for the journal explanation only.
6. Executable SELLs flow through the same `ExecutionEngine` (under the SHARED per-ticker lock so a cycle can never race a sell); realized P&L is recorded before the record is dropped.

### Monitor scheduling (exact)

Two cron jobs drive the monitor, both Mon–Fri ET: hours `9-15` every minute, plus `16:00`, each at a configurable second cadence (`*/15` default; `monitor.interval_seconds`, floor 5s). Both use `max_instances=1, coalesce=True`. Inside each tick the real NYSE calendar gates execution — weekends, holidays, and post-half-day-close ticks all no-op. Per-ticker state (`_last_monitor_review`) throttles re-evaluating SOFT signals (PROFIT_TARGET / PROFIT_REVIEW: 5 min / 15 min, both configurable) after a HOLD so the LLM isn't hit every tick; STOP_LOSS ignores the throttle entirely.

### Agent startup behavior (`start()`)

1. `configure_logging()` and log the effective `dry_run`.
2. Register the 3 cycle cron jobs + the monitor cron jobs, then `scheduler.start()`.
3. **Reconcile on boot** — fetch the live portfolio and run `position_manager.reconcile_with_mcp` (skipped if the portfolio pull is empty/missing `positions`, per the safety rule).
4. **Smart startup catch-up** — if the agent is started mid-day it immediately runs the appropriate cycle for the current time: before OPEN's slot → wait for OPEN; after OPEN's slot → run `OPEN` now; after MID's slot → run `MID` now; otherwise run `CLOSE` now. Weekends / after-hours simply wait for the next trading day.
5. Enter an idle `asyncio.sleep(60)` loop until shutdown (Ctrl-C).

---

## How a Decision Is Made

Every ticker decision is produced by `DecisionEngine`, which builds a rich prompt and parses a strict JSON response. Two entry points exist:

### `make_decision` — new-position assessment (watchlist tickers)

Used for tickers the account does **not** currently hold. It assembles:

- Any `SPY` market context line (when present) so the model can gauge regime.
- The `CYCLE` type (OPEN / MID / CLOSE), which changes the model's mandate (see schedule table).
- An **earnings window block** (`EarningsCalendar.earnings_window_for_prompt`) — informational catalyst context, never a ban.
- A **journal block** (`JournalManager.summarise_for_prompt`) — the last ~15 price/metric snapshots for the ticker. Crucially, past **decisions** (BUY/IGNORE/etc.) are deliberately **omitted** so the model cannot anchor on its own prior verdicts; only the price/metric trajectory is shown.
- `TICKER`, `CURRENT PRICE`, `NEWS HEADLINES` (JSON), `MARKET METRICS` (JSON), `FUNDAMENTALS`.
- `ACCOUNT` line: total value, effective buying power, positions count, `max_positions=12`.
- `OTHER OPEN POSITIONS` roster (`PositionManager.other_positions_line`) showing each held ticker + its live P&L %.
- The `EXISTING POSITION` detail block (only when a position record is supplied) — entry price, quantity, unrealized %, days held, stop/take-profit.
- `WEAKEST CURRENT POSITION` — which held name has the worst P&L (for ROTATE decisions).

The model must answer the **core forward-looking test**: *what specific condition — news-driven OR technical — would make this stock HIGHER in 1–2 weeks, and has that condition happened (priced in) or is it still developing?* A thesis that only explains past strength is explicitly rejected as a valid `bull_thesis`. Valid forward catalysts include both dated news events **and** technical/price-action setups (e.g., consolidation + a 3–8% low-volume dip with no bad news = selling exhaustion / mean-reversion).

The model returns a `TradeDecision`: `decision` (`BUY`/`SELL`/`HOLD`/`IGNORE`/`ROTATE`), `bull_thesis`, `bear_thesis`, `failure_conditions`, a structured `edge` score, `position_size_pct`, optional `rotate_from_ticker`, plus `risk_notes` / `reasoning_summary`. **ROTATE is a first-class two-leg operation** — ExecutionEngine sells `rotate_from_ticker` first (realized P&L recorded), then buys the replacement only if that sell filled, keeping the book count-neutral; both tickers get mirrored journal entries.

### `review_exit` — HOLD/SELL for open positions (monitor + cycles)

Used when a held position trips a threshold (stop-loss / profit-target / profit-review / max-holding) or on a cycle exit review. It uses the compact monitor prompt (`_MONITOR_SYSTEM_PROMPT_TEXT`) and a short `_EXIT_REVIEW_RULES` rubric:

- The model MUST state entry vs current price and unrealized %.
- `SELL` needs explicit evidence (thesis broken, technical failure vs plan, or capital better deployed).
- For EXIT decisions, edge is scored as **exit conviction**, not buy quality: `catalyst_strength` = strength of the exit reason (0–2), `technical_confirmation` = price supports exit (0–2), `portfolio_fit` = value of freeing capital / cutting risk (0–1).
- `HOLD` is valid when unrealized is small and the thesis is intact (including holding through earnings).
- `STOP_LOSS` remains a forced SELL via the existing code path.
- Earnings proximity is informational only — never an automatic SELL.

Output is an `ExitDecision` (ticker, decision, reasoning, edge_score). The code enforces that a STOP_LOSS signal always resolves to SELL regardless of the model's answer.

---

## Edge Scoring

`StructuredEdgeScore` replaces the old arbitrary 0–5 float. Claude fills **three bounded components** and the **code computes the total** — the model cannot fake the number.

| Component | Range | Meaning |
|-----------|-------|---------|
| `catalyst_strength` | 0 – 2 | Real news catalyst specific to the company: 0 none, 1 minor/unclear, 2 strong. |
| `technical_confirmation` | 0 – 2 | Price/volume support: 0 against trend, 1 neutral, 2 strong confirmation. |
| `portfolio_fit` | 0 – 1 | Improves portfolio: 0 redundant/risky, 0.5 neutral, 1 clearly improves. |
| **`total`** | 0 – 5 | Computed by `compute_total()` = sum of the three, clamped. |

The decision engine's system prompt teaches calibrated mapping: e.g., a strong technical mean-reversion setup alone can score technical_confirmation 1.5–1.7 plus portfolio_fit 0.7–0.9 to clear the threshold, while a pure "structural demand is strong" claim with no dated event and no qualifying technical pattern is mathematically unable to reach the threshold — by design.

### Position sizing from edge (`ExecutionEngine._size_from_edge`)

| Edge total `total` | Position size (% of portfolio value) |
|--------------------|--------------------------------------|
| `>= 4.5` | 8% |
| `>= 4.0` | 6% |
| `>= 3.5` | 4% |
| `>= 3.0` | 3% |
| `< 3.0` | 0% (no position) |

The trade must clear `trading.edge_score_threshold` (3.0) to open a position.

---

## Risk Controls

Risk is enforced deterministically in code, independent of the model's judgment:

### Position management (`core/position/manager.py`, `logs/positions.json`)

- **Position records**: each opened position stores `entry_price`, `entry_date`, `entry_edge_score`, `stop_loss_pct` (default `-0.07`), `take_profit_pct` (default `0.20`), `quantity`, `order_id`.
- **Reconciliation** (`reconcile_with_mcp`): synchronises local `positions.json` with live MCP holdings. **Safety rule:** if the MCP portfolio pull is empty/None, local records are left untouched (a failed/empty pull must never wipe records). Records are only removed when the live response is non-empty and the ticker is genuinely absent (or qty ≤ 0). Quantity mismatches are adjusted to live values.
- **Exit signals** (`check_exits`): as described in the monitor section — STOP_LOSS (hard, forced), PROFIT_TARGET (soft), PROFIT_REVIEW (advisory), MAX_HOLDING_PERIOD (advisory). Signals may combine (e.g., `PROFIT_REVIEW+MAX_HOLDING_PERIOD`).
- **Realized P&L**: on a SELL/ROTATE fill, `record_realized_pnl` logs the closed trade (`realized_pct`, `closed_at`) before the open record is removed (`record_exit`), and open positions are marked with live unrealized P&L.

### Execution safeguards (`core/execution.py`)

- **DRY_RUN enforced in code**: `effective_dry_run = settings.env.DRY_RUN(True default) OR param dry_run`. The only live/dry source is `execution.dry_run` in settings.json.
- **No time-window blocks**: orders are never refused because of the clock — exits (incl. stop-loss) especially. Entries are gated by evidence, not timing.
- **Edge bar per cycle**: BUY/ROTATE must clear `edge_score_threshold + cycle_edge_bonus[cycle]`; SELLs clear the base threshold. MONITOR cycles carry no bonus.
- **Vol-targeted sizing** (`trading.vol_sizing`): 30d annualized vol scales position size (30→×0.8, 60→×0.6, 90→×0.45) in lockstep with PositionManager's wider stops (`trading.stop_loss_bands`), keeping max dollar-risk per trade roughly constant across vol regimes. Vol past `extreme_vol_reject_30d` (default 120) is refused outright.
- **Non-trades skipped**: IGNORE / HOLD → `SKIPPED`.
- **Tradability check, FAIL-CLOSED**: before any entry the client checks `get_equity_tradability` — an error or an unparseable response rejects the trade (`REJECTED_TRADABILITY_UNKNOWN`); an explicit untradeable verdict → `REJECTED_UNTRADEABLE`.
- **Position limits**: `max_positions` and per-position cap (`max_position_pct`) enforced with fresh reads inside the lock. ROTATE is count-neutral and survives a full book.
- **Duplicate guard**: a held ticker can never be entered twice as a new position (if one ever slips through, `PositionManager.record_entry` merges weighted-average instead of overwriting).
- **No portfolio value → no order**: sizing fails safe when account value is unknown.
- **Budget**: cost tracking is **observability only** — nothing halts for spend (see Cost Tracking).

### Portfolio/config guardrails (`config/settings.json`)

| Setting | Default | Purpose |
|---------|---------|---------|
| `execution.dry_run` | true | **THE live/dry switch** (fail-safe: anything but explicit `false` is dry). |
| `execution.mcp_url` | https://agent.robinhood.com/mcp/trading | Robinhood MCP endpoint. |
| `execution.tradability_fail_mode` | closed | Reject trades when the tradability check errors. |
| `execution.min_dollar_volume` | 75M | 30d avg $-volume liquidity floor. |
| `trading.max_positions` | 12 | Hard cap on concurrent open names. |
| `trading.max_position_pct` | 0.08 | Per-position cap on portfolio value. |
| `trading.edge_score_threshold` | 3.0 | Base min edge to open a position. |
| `trading.cycle_edge_bonus` | OPEN 0 / MID 0.25 / CLOSE 0.5 | Extra bar for entries per cycle (code-enforced). |
| `trading.stop_loss_bands` | 0→−7%, 30→−10%, 60→−13%, 90→−16% | Vol-adjusted stop-loss bands. |
| `trading.vol_sizing` | 30→×0.8, 60→×0.6, 90→×0.45 | Vol-targeted size multipliers. |
| `trading.extreme_vol_reject_30d` | 120 | Absolute refuse-anything vol cap. |
| `trading.take_profit_pct` | 0.20 | Soft take-profit trigger. |
| `trading.profit_review_pct` | 0.15 | Advisory profit-review trigger. |
| `trading.max_holding_days` | 15 | Advisory max-holding trigger. |
| `monitor.interval_seconds` | 15 | Price-monitor tick cadence (floor 5s). |
| `monitor.check_max_holding` | true | Monitor also enforces max-hold days. |
| `reporting.attribution_*` | 16:05 ET | Daily attribution report schedule. |

**Note on intervals:** the stop-loss bands and take-profit default live on each `PositionRecord`; exit checks use the per-record values, set at entry time from the config tables.

---

## The MCP Client & OAuth

`robinhood_mcp/robinhood_client.py` is the **direct** Python bridge to Robinhood's trading MCP server. It is an architectural replacement for an earlier version that shelled out to `claude --print "…"` for every call (which cost ~45k input tokens of pure CLI overhead per call).

**Transport.** Uses the official `mcp` SDK over **Streamable HTTP** against `https://agent.robinhood.com/mcp/trading`.

**Authentication — OAuth 2.1 + PKCE + Dynamic Client Registration.**
- **Dynamic Client Registration** (RFC 7591) against the registration endpoint obtains a `client_id` (cached). Grant types: `authorization_code` + `refresh_token`.
- **Browser auth flow** (`_run_browser_auth_flow`): builds an authorization URL with a PKCE code challenge (`S256`), opens the browser, and catches the redirect on a local callback server (`http://localhost:53271/callback` in a background thread).
- **Token cache** (`.rh_oauth_cache.json`): access + refresh tokens persisted in the project root. Subsequent runs reuse the cached refresh token and **silently refresh** (`_refresh_access_token`) via the token endpoint; only if there's no valid refresh token does it re-open the browser.
- `_ensure_valid_token` is called before every MCP session opens; callers never manage token lifecycle.

**Session lifecycle.** A **fresh authenticated session is opened per tool call** (`_call_tool_with_retry`) and closed after — deliberate, to avoid stale-connection failures across the multi-hour gaps between OPEN/MID/CLOSE cycles. Retries are handled for transient failures (`max_retries = 3`).

**Account selection.** Account-scoped tools require an `account_number`. `_get_account_number()` calls `get_accounts` and — critically — selects the account where `agentic_allowed=true` (the Agentic account), *not* the default individual account. It caches the chosen number; if none is found it refuses to guess (safer than trading the wrong account).

**Why `Portfolio.positions` is populated.** Robinhood's `get_portfolio` tool returns only account-level aggregates (total value, cash, buying power) and never a per-symbol positions array. The client therefore also calls `get_equity_positions` and **merges** the two so `Portfolio.positions` reflects real live holdings (a failure there leaves `.positions` empty, preserving the safety net that prevents wiping local records on an empty pull).

**Response cleanup helpers** coerce Robinhood's string-typed numerics into floats (`_coerce_portfolio_fields`), unwrap nested `buying_power`, and remap quote/historicals fields into the Pydantic models — identical business logic to the validated legacy version.

**Legacy file.** `robinhood_mcp/robinhood_client(claude version).py` is the old CLI-subprocess implementation and is **no longer used**.

---

## Broker (MCP) Tool Coverage

The client wraps each broker MCP tool as an async Python method that returns a Pydantic model / parsed dict:

- `get_portfolio()` → `Portfolio` (merged with positions) — account-scoped.
- `get_equity_quotes(tickers)` → `list[EquityQuote]` — not account-scoped.
- `get_equity_historicals(ticker, span, interval)` → `list[OHLCVBar]`, plus `get_equity_historicals_batch(...)`.
- `get_equity_tradability(ticker)` → tradability metadata — account-scoped.
- `get_equity_positions()` → `list[Position]` — account-scoped.
- `get_watchlist_tickers(watchlist_name)` → symbols from a named watchlist.
- `get_earnings_calendar()` / `get_earnings_calendar_window(start_date, days)` → upcoming earnings.
- `get_equity_fundamentals(tickers)` → market cap / P/E / dividend / sector/industry.
- `get_technical_indicator(symbol, indicator, …)` → RSI / MACD / Bollinger / ATR / SMA series used by `MetricsEngine`.
- `get_realized_pnl(span)` / `get_pnl_trade_history(span)` / `get_equity_tax_lots(symbol)` → P&L and history — account-scoped.
- `review_equity_order(...)`, `place_equity_order(...)`, and cancel/option equivalents → order lifecycle.

Every call result flows through `robinhood_mcp/response_recorder.py` (`capture_direct_mcp_call`), which preserves observability (schema drift detection, numeric anomaly detection, credential redaction) without the old subprocess machinery.

> The full Robinhood MCP tool surface is catalogued in `config/robinhood_mcp_tools.json` (reference only — nothing reads it at runtime). The legacy `.claude/settings.local.json` allow-list file has been removed.

---

## Persistence & Logging

All state lives under `logs/` and `journals/` (auto-created by the app).

- **`logs/positions.json`** — The authoritative open/closed position ledger (`PositionManager`). Contains open records, per-position live marks (unrealized P&L), and a `closed_trades` list recording `realized_pct` / `closed_at` on each sale. This is the single source of truth for "what else is held" and is used to build the `OTHER OPEN POSITIONS` roster for prompts.
- **`logs/trades.json`** — A JSON array of every trade attempt (decision, edge score, position size %, action type, status, fill price, quantity, dry-run flag, reasoning summary). Written by `ExecutionEngine._log_trade`.
- **`journals/TICKER.json`** — Per-ticker rolling history of `JournalEntry` objects (`JournalManager`, max 90 entries, atomic `.tmp`→replace writes). Each entry records timestamp, cycle type, bull/bear thesis, decision, rationale, key metrics snapshot, news used, fill price, unrealized P&L, and earnings flag. Used to build the model's price/metric trajectory (decisions deliberately omitted).
- **`logs/cost_tracker.json`** — Persisted per-call cost records plus rollup daily/monthly totals (`CostTracker`).
- **`logs/agent.log`** — Main structured DEBUG log (UTC timestamps via `logger.py`).
- **`logs/performance.json`, `logs/attribution_report_*.json`** — Performance snapshots and optional on-demand attribution reports.
- **`logs/direct_mcp_trace.jsonl`, `logs/mcp_response_trace.jsonl`, `logs/schema_history.json`** — MCP observability / schema-drift history from `response_recorder.py`.
- **`data/earnings_calendar.json`** — Cached upcoming earnings (from RH MCP) read by the informational earnings-window logic.
- **`data/seen_headlines.json`** — Finnhub article-id dedup state so news isn't re-shown to the model every cycle of the same day.

**Logging** (`utils/logger.py`): a `robinhood-agent` logger routes to console (INFO) + `agent.log` (DEBUG), and a separate `robinhood-agent.trades` logger streams JSON lines to `logs/trades.json` (note: `ExecutionEngine` also writes `trades.json` directly). All times are UTC.

---

## Where Your Data Lives (per-user state)

Everything the agent records is **local to your machine** and private to you. It is generated at runtime, unique per user, and **git-ignored** so it can never be pushed to a remote:

| Path | Created by | Git status |
|---|---|---|
| `logs/positions.json`, `logs/trades.json`, `logs/performance.json`, `logs/agent.log`, `logs/cost_tracker.json`, `logs/*.jsonl` (MCP traces) | `PositionManager`, `ExecutionEngine`, `utils/logger.py`, `CostTracker`, `response_recorder` | ignored |
| `journals/<TICKER>.json` | `JournalManager` | ignored |
| `data/seen_headlines.json`, `data/earnings_calendar.json`, `data/selected_universe.json`, `data/weekly_news_cache.json` | `NewsFetcher`, `EarningsCalendar`, `WeeklySelector`, `WeeklyNewsFetcher` | ignored |
| `.rh_oauth_cache.json` | `RobinhoodMCPClient` (Robinhood OAuth tokens) | ignored |
| `.env` | you (API keys) | ignored |

A fresh clone recreates all of these automatically on first run — nothing in the repository ships with pre-existing trading history, journals, or traces. See [SECURITY.md](SECURITY.md) for why the MCP trace files (which contain real account numbers) must never be committed.

## Security & Contributing

- Security policy and the full local-data inventory: [SECURITY.md](SECURITY.md)
- Contribution rules, dev setup, test commands, and the never-commit list: [CONTRIBUTING.md](CONTRIBUTING.md)
- CI (`.github/workflows/ci.yml`) runs the test suite, the offline verification harness, and a **gitleaks secret scan** on every pull request; changes to `main` require the code owner's approval (`.github/CODEOWNERS`).

---

## Cost Tracking

`utils/cost_tracker.py` meters every Anthropic call — a singleton, so totals are shared process-wide. It is **observability only**: it will never stop, gate, or change trading behaviour for any level of spend.

**Accuracy contract.** Every number comes straight from the API's own `usage` object — no estimation. The formula mirrors the Anthropic dashboard exactly:

```
estimated_usd = (input − cache_create − cache_read) × input_rate
              + cache_create × cache_write_rate
              + cache_read   × cache_read_rate
              + output       × output_rate
```

Rates (USD per 1M tokens) are configurable in `settings.json → cost_tracking.pricing` — update them there the day Anthropic changes pricing and every derived number stays dashboard-accurate. Unknown model ids fall back to the `*` rate.

**Storage.** Per-call records are written append-only (one JSON object per line) to `logs/cost_records.jsonl` — constant-time writes that never rewrite history. Daily/monthly aggregates (plus the active pricing table) live in the small `logs/cost_tracker.json`. Each cycle end logs `spend_today` / `spend_month`.

**Retry** (`utils/anthropic_client.py`): completion attempts retry up to 3× on rate-limit/529-style errors (with a 5s sleep), strip markdown code fences (`strip_fences`, the single canonical implementation), and only send `temperature` when `models.send_temperature` is true (newer model generations reject the parameter).

---

## Configuration

### `config/settings.json` (primary — the ONLY tunable surface)

Everything toggleable lives here (see the repo's `config/settings.json` for the
current full file). Loaded at startup by `utils/config.py:load_config()` with
fail-safe defaults: a missing or malformed file ⇒ fully safe defaults with
dry-run ON. Key sections:

```jsonc
{
  "models":     { "decision": "claude-sonnet-5-5", "max_tokens": 4096, "send_temperature": false },
  "execution":  { "dry_run": true, "mcp_url": "…", "tradability_fail_mode": "closed", "min_dollar_volume": 75000000 },
  "trading":    { "max_positions": 12, "max_position_pct": 0.08, "edge_score_threshold": 3.0,
                  "cycle_edge_bonus": {"OPEN":0.0,"MID":0.25,"CLOSE":0.5},
                  "stop_loss_bands": [[0,-0.07],[30,-0.10],[60,-0.13],[90,-0.16]],
                  "size_ladder": [[3.0,0.03],[3.5,0.04],[4.0,0.06],[4.5,0.08]],
                  "vol_sizing": [[30,0.8],[60,0.6],[90,0.45]], "extreme_vol_reject_30d": 120,
                  "take_profit_pct": 0.2, "profit_review_pct": 0.15, "max_holding_days": 15,
                  "market_hours": {"calendar":"NYSE","stale_data_guard":true} },
  "schedule":   { "open_hour":9, "open_minute":35, "mid_hour":12, "mid_minute":30,
                  "close_hour":15, "close_minute":30, "timezone":"America/New_York" },
  "monitor":    { "enabled": true, "interval_seconds": 15, "historicals_span": "3month",
                  "profit_review_cooldown_min": 15, "profit_target_cooldown_min": 5,
                  "check_max_holding": true },
  "watchlist":  { "default_tickers": […], "robinhood_watchlist_name": "Default" },
  "news":       { "provider": "finnhub", "lookback_days": 3, "max_per_ticker": 5,
                  "fresh_window_hours": 24, "general_market": {…} },
  "cost_tracking": { "enabled": true, "pricing": { "claude-sonnet-5-5": {…}, "*": {…} } },
  "reporting":  { "attribution_enabled": true, "attribution_hour": 16, "attribution_minute": 5 }
}
```

### `.env` (secrets ONLY)

- `ANTHROPIC_API_KEY` — for `anthropic.AsyncAnthropic` (the model provider).
- `FINNHUB_API_KEY` — for `NewsFetcher` / the weekly selector (Finnhub news).
- `LOG_LEVEL` — optional.
- **Nothing else.** All configuration toggles (including dry-run) belong in settings.json. `.env.example` documents exactly this.

### Initialization order (`TradingAgent.__init__`)

`AsyncIOScheduler(ET)` → `load_config()` (settings.json + fail-safe defaults; `.env` read for secrets only) → `CostTracker()` (+`set_pricing` from config) → `RobinhoodMCPClient(url=execution.mcp_url)` → `DataIngestLayer` → `MetricsEngine` → `NewsFetcher(finnhub_key, settings)` (no LLM client) → `JournalManager` → `EarningsCalendar()` (+ `set_client(mcp_client)`) → `DecisionEngine(AnthropicClient(decision model), journal_manager, earnings_calendar, settings)` → `PositionManager(config)` → `PnLTracker(mcp_client)` → `ExecutionEngine(mcp_client, position_manager, settings)`. Monitor caches/cooldowns follow from `monitor.*`.

**DRY_RUN resolution (exact):** `settings["env"]["DRY_RUN"] = execution.dry_run`, coerced fail-safe — only an explicit boolean/string `false` in settings.json enables live. `ExecutionEngine` additionally forces dry-run via an OR with its own flag, so there is no path to an accidental live order.

---

## Setup

1. **Python 3.11+** (the code uses modern typing / async).
2. Create a virtual environment and activate it.
3. Install dependencies: `pip install -r requirements.txt` (includes `anthropic`, `apscheduler>=3.11`, `httpx`, `mcp`, `pydantic`, `pytz`, `python-dotenv`, `uvicorn`, `pandas-market-calendars`). Add `pip install -r requirements-ml.txt` **only** if you enable weekly sentiment selection (it pulls the ~2 GB `torch`/`transformers` stack), and `pip install -r requirements-dev.txt` for the test suite (`pytest`, `pytest-asyncio`, `ruff`).
4. Create `.env` with `ANTHROPIC_API_KEY` and `FINNHUB_API_KEY` (secrets only — see `.env.example`).
5. Review `config/settings.json` — it ships with `execution.dry_run: true` (simulate only). Every toggle lives there.
6. **(First run only)** Authenticate with Robinhood — the app opens the browser for the one-time OAuth approval; a `.rh_oauth_cache.json` is then reused/silently refreshed on later runs. Requires an active Robinhood **Agentic** account configured to allow (`agentic_allowed=true`).
7. Run: `python main.py`

> The legacy setup step `claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading` is no longer required (the client connects directly, and the legacy `.claude` permission file has been removed).

**Execution modes:**
- **Dry-run (default):** `execution.dry_run: true` in settings.json (the shipped value) → every trade is validated and logged as `SIMULATED` / `WOULD_EXECUTE` but no real orders are placed. A startup banner prints the mode loudly.
- **Live:** flip `execution.dry_run` to `false` in the same file; live orders flow through `review_equity_order` / `place_equity_order` only after passing every code-level risk gate.

---

## Weekly sentiment ticker selection (feature-flagged)

Set `sentiment_selection.enabled: true` in `config/settings.json` to let a
weekly scheduled job (Sunday evening ET) re-rank the trading universe using
Finnhub headlines scored by a ModernBERT financial sentiment model
(`core/weekly/selector.py`, `core/weekly/news.py`, `core/data/sentiment.py`,
`core/weekly/ticker_selector.py`). When the flag is off, the agent keeps using
`watchlist.default_tickers` and no weekly job is registered.

Guarantees: currently held tickers are NEVER dropped (even when holdings
outnumber `universe_size`), ticker scores are the mean of their headline
scores, and any pipeline failure keeps the previous week's universe.

Runtime artifacts `data/selected_universe.json` and
`data/weekly_news_cache.json` are git-ignored state files.

Verification harness (offline-deterministic; does a live Finnhub fetch only
when `FINNHUB_API_KEY` is set):

```
pytest tests/ -q
python scripts/verify_weekly_layer.py
```


---

## Important Warning

**This project is for research, experimentation, and education. Trading involves real financial risk, and this is genuinely autonomous money-moving software.** It places/simulates orders against a real brokerage account based on LLM judgment, and while risk rules are enforced in code, LLM output is non-deterministic and markets are unpredictable.

- Do **not** use live capital without independent code review, compliance checks, and an explicit risk policy.
- Start strictly in **dry-run** (`DRY_RUN=true`) and study `logs/trades.json`, `logs/positions.json`, and the journals before ever enabling live trading.
- The hard-coded pricing/limits, thresholds, and OAuth/account-selection logic should be re-validated against your account and Robinhood's current API/terms before live use.
- Nothing here is investment advice.
