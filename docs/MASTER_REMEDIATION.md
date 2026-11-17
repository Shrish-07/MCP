# Engineering remediation — November 15, 2026

Completed with the existing Python environment and offline socket/credential guards.
No normal trading process, provider request, broker request, credential renewal,
commit, push, deployment or activation was performed. Existing working-tree changes
were preserved. Configuration and HEAD match the pre-task hashes/identity.

## Changes by file

| File | Change |
| --- | --- |
| `core/data/news.py` | Retry attribute alias and consistent private retry counter; isolated general-news failure; newest-first selection; persist only selected article IDs; monitor does not consume delivery state. Fresh catalysts remain available across decision cycles within the existing freshness window. |
| `core/data/ingest.py` | Fetch year-span daily bars rather than three months, allowing real 90-session measurements when sufficient history exists. |
| `core/data/metrics.py` | Chronological maximum drawdown; exclude unfinished daily bars using the exchange calendar; retain return reference prices and Bollinger center/scale for live marks. Volume and realized volatility use completed sessions. |
| `models/market_data.py` | Persist calculation reference fields in the metrics object for consistent quote updates. |
| `main.py` | Defensive news-provider isolation; one held review per cycle; skip specialized reviews already active; exclude explicit model-error fallbacks from journals and mark cycle failure accurately; use result notional for BUY cash accounting; update returns/Bollinger/SMA distance with live quotes; persist measured sector metadata; cache SPY context for specialized reviews. |
| `core/decision/engine.py` | Repair prompt punctuation and actual metric names; explicitly specify portfolio-review fields and display current count/sector evidence; tag API-error fallback decisions. |
| `models/decisions.py` | Internal `is_fallback` marker, default false. Model response text cannot independently authorize a fallback trade. |
| `models/portfolio.py` | Optional sector metadata on persisted position records, compatible with existing schema-2 records. |
| `core/execution/engine.py` | Carry known sectors into refreshed rotation portfolios; preview both rotation legs without changing live holdings or treating hypothetical proceeds as confirmed; guard rotation persistence in dry run; defer rotation leg logging until metadata is attached; retain sell price/quantity/P&L in records; sector review uses proposed ladder/vol size rather than maximum allocation; optional REDUCE fraction applies only when explicitly enabled. |
| `core/backend.py` | Match/freshness-check execution quotes instead of indexing an empty list; include portfolio P&L roster and latest available cached SPY context in specialized reviews; journal valid completed reviews; expose sector/count evidence; permit the requested controlled-pullback ADD evidence; use shared spread-aware marks; request trailing contiguous history for nightly ATR. |
| `core/exits/exit_evidence.py` | Failed reviews retry after 15 minutes by default; normal approved/in-progress review cooldown behavior remains 24 hours. Retry window is an optional function parameter. |
| `core/exits/trail_manager.py` | Opt-in trailing-contiguous history recovery for nightly maintenance; discovery remains strict by default. ATR still needs at least 15 contiguous completed sessions and a fresh last session. Shared mark selection falls back to last trade when the bid/ask spread exceeds 2%. |
| `core/position/manager.py` | Preserve sector through reconciliation and fills; recompute remaining FIFO lot basis after partial sells; compute realized FIFO cost when the sold quantity has complete lot coverage. Existing stop maxima and atomic state guarantees remain intact. |
| `utils/strategy_config.py` | Validate partial-take fraction rather than rejecting the entire optional feature. Checked-in `partial_take.enabled` remains false. |
| `core/discovery/news_features.py` | Batch in-memory ticker updates and merge dirty tickers at flush; immediate persistence remains the default. On model changes only freshly rescored stories survive, preventing mixed-model history without deleting unrelated tickers. |
| `core/discovery/pipeline.py` | Score the cohort with deferred news-history persistence, then flush once before publication. |
| `scripts/verify_weekly_layer.py` | Execute its existing weekly test selection through the guarded offline runner, protecting future verification from accidental provider access. |
| `tests/test_master_remediation.py` | Seven new deterministic cases covering retries/general news/order/cycle retention, chronological drawdown and partial bars, historical gaps, retry/spread behavior, history batching/model changes, FIFO/REDUCE, actual sector allocation and pullback evidence. |
| `tests/test_monday_wiring.py` | Exercise API fallbacks across actual main-cycle paths and verify a triggered held review is not repeated as ROUTINE; adapt the completed-bars fixture to the new explicit trailing-history option. |
| `tests/test_backend_safety.py` | Live-mode mocked rotation retains another holding whose refreshed sector is blank; verify successful leg 2 and unique complete rotation metadata. |
| `tests/test_rotation.py` | Update dry-run acceptance to two successful previews while retaining assertions that live holdings and confirmed P&L do not change. |

`PositionManager.check_exits()` already emitted only STOP_LOSS; it did not contain
the reported legacy PROFIT_TARGET/PROFIT_REVIEW behavior. No obsolete trigger was
reintroduced. Graduation continues through the active typed exit-review system.

## Verification

Before edits: **243 passed**, one AnyIO dependency deprecation warning.

After remediation:

* Full guarded suite, including cached local-model tests: **254 passed**, one
  warning, **14.77 seconds**. No external model downloads or inference services.
* Focused runtime/rotation/remediation checks: **53 passed** before the last
  additional allocation case; that additional case is included in the final full suite.
* New remediation module independently: **7 passed**.
* `python -B scripts/verify_weekly_layer.py`: **35 passed**, one warning.
* `ruff check . --no-cache`: passed.
* `ruff format . --check --no-cache`: **126 files already formatted**.
* In-memory compilation: **114 active Python source/test files**, passed.
* `pip check`: no broken requirements.
* Preflight: passed; zero local positions, zero unresolved intents, binding present.
* `git diff --check`: passed; `git diff -- config/settings.json`: empty;
  `git diff --cached --name-only`: empty. No staging occurred.

Full output: [pytest](verification/pytest_master_2026-11-15.txt),
[weekly verification](verification/weekly_master_2026-11-15.txt).
Documentation changes are this report and the README link; the two linked
verification outputs are new evidence files. No historical evidence was deleted.
The safe full-suite command was:

```powershell
& .\.venv-upgrade\Scripts\python.exe -B docs/verification/run_offline_audit_2026-11-12.py -m ''
```

Intermediate syntax/fixture failures were corrected before these results. Existing
rotation tests were updated because the requested dry-run contract changed from
sell-preview-only to two-leg preview. Their no-fill/no-live-book assertions remain.

## Limits and safety

`execution.dry_run=true`, `execution.live_release_approved=false`, upgraded
discovery disabled and partial takes disabled remain unchanged. No real order or
preview was used to verify execution. Missing sector metadata still fails closed;
it is never guessed. Stale/missing quotes and incomplete reconciliation still
block dependent live actions. Existing OS/process locks and schema-2 atomic
persistence remain in place.

These fixes remove demonstrated implementation defects and implement the requested
strategy options. They do **not** establish improved expectancy, alpha or
profitability. Spread fallback and pullback additions require separate historical
and forward evaluation. Real account authentication, funding, provider access,
deployment behavior and actual live fills remain outside this offline task.

The previous [Monday operating report](MONDAY_READINESS.md) remains historical
evidence for the earlier snapshot. Its partial-take restriction and test counts
are superseded by this report; its owner launch/monitoring instructions still
apply. Do not interpret either report as live activation approval.
