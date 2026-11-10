# Robinhood swing-trading backend

One coordinated Python application for multi-day to multi-week equity positions.
Code owns holdings/order truth, sizing and stops. Claude Sonnet supplies advisory
theses. Local sentiment and discovery are optional. A 15-second poll enforces
exits; it is not an entry-generation schedule.

**Shipping settings: dry run, live release unapproved, discovery disabled.**
The upgrade has not been activated against a live account. See the evidence and
remaining release gates in [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md).
The previous README is retained in `docs/history/README_pre_swing_upgrade.md`.

## Setup and offline verification

Supported and tested: CPython **3.13.6**. Core and development dependencies are
UTF-8 and pinned; optional ML dependencies are separate.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/verify_weekly_layer.py
.\.venv\Scripts\python.exe scripts/preflight.py
```

On the Codex Windows sandbox, asyncio's local socket pair requires an approved
test run outside the sandbox. This is an environment restriction before tests
reach application code. Unit/integration fixtures do not call external APIs or
download sentiment weights. `pytest -m heavy` explicitly opts into real-model
tests. Install `requirements-ml.txt` for real sentiment inference; an unavailable
model retains the previous selection and never produces fabricated neutral
scores. Model failures do not disable deterministic exits.

Put only `ANTHROPIC_API_KEY` and `FINNHUB_API_KEY` in `.env`. Do not commit `.env`,
OAuth tokens or runtime account state. Settings and flags belong in
`config/settings.json`, loaded through `utils/config.py`.

## Execution and persistence

`logs/positions.json` is the schema-2 **live** state envelope: positions, durable
order intents, orders, confirmed fills, reconciliation events and recovery
records commit in one atomic replacement. Legacy list files are read safely and
backed up on migration. Corruption blocks orders; it never silently becomes a
verified empty portfolio. Archived files describe historical runs, not today's
account.

A complete successful empty broker response clears stale live holdings. Failed,
malformed or incomplete responses preserve last-known state as unverified. Both
positions and account open orders are reconciled before entry. Persisted
`CREATED`/unknown orders reserve their ticker across restart; a timed-out placement
is never automatically repeated. The placement transport makes one attempt.

Only confirmed execution IDs/cumulative fills change quantity, average cost or
realized P&L. Partial exits remain open. SELL has no entry conviction threshold.
ROTATE waits for the old position's confirmed sell, refreshes account cash and
the target quote, and then rechecks the buy. A deferred/ambiguous leg requires a
fresh decision after reconciliation. It is never reported as a complete swap.

Dry-run previews produce `WOULD_EXECUTE` decision records and no fills. Explicit
replay uses a separate paper envelope and report paths. It never seeds paper
holdings from stale live files or merges a paper book into a live book.

## Exit behavior

- Existing initial stop bands remain −7%, −10%, −13%, −16% at 30-day annualized
  volatility floors 0%, 30%, 60%, 90%.
- A +5% observed peak activates a nominal entry×1.005 profit lock. At +10%, a
  validated ATR14 permits `peak − 3×ATR14`. Stops never loosen on routine
  updates, adds, reconciliation or restart.
- ATR uses Wilder smoothing of daily true ranges, seeded from 14 ranges and
  requiring at least 15 contiguous completed exchange sessions. ATR7 is never
  substituted. Stale/missing ATR preserves existing protection.
- Valid source-timestamped bid prices update peaks each tick. Last trade is the
  fallback when a bid is unavailable. Missing source timestamps are unavailable
  data; receipt time is not a substitute. Polling cannot protect positions during
  feed/service/broker outages, halts or gaps, or guarantee a stop fill price.
- Hard stops are persisted before submission and do not call Claude. Soft
  triggers are grouped into one single-ticker review with persistent debounce
  and retry state. A model failure/hang cannot hold the execution lock.
- +20% graduation requests one defended harvest-or-hold review. A defended HOLD
  removes the fixed ceiling and continues the trail. A failed review preserves
  protection and retries with cooldown. Partial takes are disabled.
- Trend break, climax, sentiment reversal, signed gap closure, dead money,
  opportunity cost, PEAD and the 15-session time advisory request judgment.
  Unknown entry dates/baselines/event history disable dependent rules rather
  than create artificial evidence. No elapsed-time rule forces a sale.

Corporate actions require explicit verified source evidence. A suspected split
places the holding in reconciliation-required state; price transforms adjust
entry, lots, peaks, stops and ATR consistently. Do not infer a split from a quote.

## Adds and discovery

`ADD_TO_POSITION` is a typed action with required original/incremental thesis,
invalidation, new evidence, scenarios, horizon, alternatives and matching evidence
IDs/timestamps. Code enforces broker truth, exit priority, valid stop, pending
orders, cooldown/count, cash, size, exposure, liquidity and incremental risk.
The default permits one add, at most 25% of current position value and 0.25% of
account equity as incremental loss to stop; stricter existing limits win.
Confirmed adds retain the original thesis/gap anchor and have separate fill lots.

Discovery is disabled by `discovery.enabled=false`. Its configurable N defaults
to **10 new names**; broker-confirmed holdings are reviewed separately and do not
consume N. Fewer qualifying candidates means fewer names. The integrated existing
weekly selector also separates new-name slots from holdings.

The sourced S&P universe has a retrieval timestamp and SHA-256 version, and must
be refreshed within its age limit. Eligibility needs sufficient contiguous daily
history, price, dollar volume and volatility. F1 is a measured uptrend pullback;
F2 requires range/volatility contraction and shrinking down volume; F3 requires a
5–15% pullback from a 30-session high and shrinking down volume. Exact configurable
thresholds are in `utils/strategy_config.py`.

News uses normalized story/headline/URL dedup, a seven-day current window, prior
seven-day sentiment and a ticker's preceding 30-day headline-count baseline.
The pipeline fetches/persists 45 days so it does not invent a baseline from seven
days. Recent three-day scores receive double weight. Sparse/failed inference is
unavailable. Only FinSense ModernBERT and the existing financial distilbert
fallback are used. No paid-model tokens are used by the screen.

Components are clipped at mean±3 population SD and recentered/rescaled on a
timestamped surviving cohort. Weighted raw news-minus-price gaps are then
standardized **again** as `gap_z`. Small/degenerate cohorts fail publication.
Entry-cohort transforms are frozen for comparable held-gap progress; an unrelated
new cohort cannot silently redefine progress. Archetypes orient the model, never
recommend trades. C requires a confirmed, sourced catalyst; absent event data does
not produce a fictitious catalyst. D is a holding warning, not a short command.

Failed ranking retains the last good shortlist with separate failed status.
Stale/invalid output falls back to the existing selector. Turning discovery off
leaves stops and accounting active.

## Scheduler and operations

All jobs share one manager, locks, configuration revision and health ledger:

| Job | Schedule (America/New_York) |
| --- | --- |
| Decision cycles | Trading days 09:35, 12:30, 15:30 |
| Exit monitor | Every 15 seconds in regular NYSE sessions |
| Nightly held ATR/features/reviews | 20:00 |
| Discovery ranking | Sunday 20:00, only when enabled |
| Fill attribution | Trading days 16:05 |
| Heartbeat watchdog | Every 30 seconds |

Exchange holidays and early closes use the NYSE calendar. Extended hours are
disabled until the actual order contract is validated. `logs/health.json` records
attempt/success/error/failure counts. Operator alerts are emitted through the
application logger and persistent health state. A deployment must arrange an
independent alert destination/watchdog; an in-process monitor cannot notify anyone
after its entire host is down.

Create `data/STOP_TRADING` to refuse new live order submissions. Removing it
restores eligibility only after normal gates pass. The monitor continues
observing state and reporting errors. The stop file blocks all new orders,
including stops, so use it only when intentionally handing execution to an
operator. It does not cancel already submitted broker orders.

`python main.py` acquires an OS-held exclusive state-directory lock. One active
worker only. Multiple replicas require transactional shared storage and scheduler
leadership; shared JSON alone is insufficient.

`Dockerfile` and `compose.yaml` package the same one-process backend with durable
config/data/log/journal mounts and a health check. Supply a valid preauthenticated
OAuth cache as a secret-mounted file; the image excludes credentials and historical
account logs. Docker build/start was not available in the implementation host.

## Reports and evaluation

`logs/exit_ledger.jsonl` exports immutable canonical events. Fill-based daily
reports link decisions, orders, fills, lots and journals, separate live/paper
state and distinguish partial exits from closed lifecycles. Fees, legacy MFE/MAE
and benchmark histories remain unknown when absent. Lot excursions use valid
marks after each lot's entry; no pre-add excursion is attributed to an add.
Observed account drawdown is labeled as unadjusted for deposits/withdrawals.

API costs retain raw disjoint input/cache/output usage, actual returned model,
request ID and pricing version/rates. Unknown rates and billable categories are
unpriced; cost never changes trading behavior. Current official references are
recorded in `docs/UPGRADE_LOG.md`. Historical incorrect totals are retained
separately from version-2 records.

```powershell
python scripts/reconcile_costs.py dashboard.json --start 2026-11-01T00:00:00+00:00 --end 2026-11-02T00:00:00+00:00
python scripts/replay_archived.py --ticker MU --output-dir data/replays/new-run
python scripts/evaluate_discovery.py paired_dataset.json
```

The cost export must identify the same requests/timezone/window. Real dashboard
parity has not been claimed. Paired walk-forward evaluation rejects future
features and unmatched capital/risk/exit/cost assumptions; it reports sample size
and uncertainty without tuning weights. Archived sparse journal replay validates
mechanics, not profitability. There is **no fixed paper-trading period, minimum
trade count, automatic exposure ramp or calendar waiting condition**. The owner
reviews evidence and controls live activation after the technical gates pass.
