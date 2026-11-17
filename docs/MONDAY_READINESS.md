# Monday, November 16, 2026: standalone operating audit

Investigation and implementation performed November 15. Repository root:
`C:\Users\Shris\OneDrive\Desktop\Project\S-BOT\robinhood-agent`.

## 1. Final verdict

**READY FOR DRY RUN ONLY.** The inspected standalone code path and offline
regressions support an owner-started dry run. They do not establish full live
readiness. The current configuration still selects the legacy strategy, not the
upgraded discovery strategy. Enabling discovery is a separate owner choice from
authorizing live submissions. Neither choice was made during this audit.

Baseline: branch `Shrish-07-changes`, HEAD
`e81a48ffc859d4f2df671d2d205626229237f991`. The working tree was already extensively
modified, including five pre-existing deletions and untracked prior audit/reset
work. Its complete initial inventory is retained in
[check evidence](verification/checks_monday_2026-11-15.json). No reset, clean,
checkout, staging, commit or push occurred. `Instructions.md` was absent from the
current workspace; the current user directive governed the work.

Before edits: **214 tests passed**, one dependency warning. After fixes:
**243 tests passed**, one dependency warning, and **107 focused tests passed**.
The offline runner denies external connections, actual credential-file reads
and production log/journal/config writes. Cached sentiment-model tests are
included; no model downloads or paid inference calls were made.

Preflight passes: valid configuration, zero local positions, zero unresolved
local intents, account binding present, sentiment dependencies installed.
These are local facts. Current broker authentication, holdings, orders, balances,
buying power, provider access and model entitlement remain unverified. The last
retained successful broker check is November 12: zero observed cash/buying power
and holdings, with 27 terminal historical orders. That is not a current balance.
Live entries require sufficient available buying power.

## 2. Runtime architecture

The actual CLI path is:

```text
main.py __main__
  InstanceLock(logs/agent.lock)
  TradingAgent.__init__
    utils.config.load_config -> defaults + settings.json + API secrets
    RobinhoodMCPClient -> lazy OAuth/MCP session
    DataIngestLayer + MetricsEngine + NewsFetcher
    JournalManager + EarningsCalendar + DecisionEngine(AnthropicClient)
    PositionManager + ExecutionEngine + Backend(self)
  asyncio.run(TradingAgent.start)
    process identity / heartbeat; SIGTERM handler
    _start_jobs -> broker/order reconciliation -> scheduler
      position_monitor -> run_price_monitor -> Backend.monitor
      decision_open/mid/close -> run_cycle -> _run_cycle
      nightly -> Backend.nightly -> _nightly
      discovery_weekly -> Backend.weekly (when enabled)
      attribution -> _run_attribution_report -> Backend.attribution
      heartbeat -> Backend.heartbeat
    background ensure_nightly + ensure_discovery (when enabled)
    startup catch-up for the current decision window
    wait for stop; cancel work; mark STOPPED; release OS lock
```

`core/backend.py::Backend` is the shared in-process implementation. It is not a
separate backend daemon, service or deployment that must run first. `compose.yaml`
and `Dockerfile` launch the same `main.py`. Scripts are maintenance, diagnostics,
offline replay/evaluation or intentional one-cycle entry points; they do not
supply missing standalone callbacks. Flat compatibility modules re-export the
domain modules; production uses `core/data`, `core/decision`, `core/execution`,
`core/position`, `core/discovery` and `core/exits`.

Data flow: broker universe/bars/quotes and news -> discovery shortlist and held
research -> timestamped metrics/news/portfolio/position evidence -> Claude
structured proposal -> deterministic execution gates -> durable intent -> broker
response -> confirmed fills -> positions/lots/journals/attribution. Hard stops
take the direct monitor-to-execution path without Claude.

Authoritative live execution state is the schema-2 envelope in
`logs/positions.json`: account binding, positions, orders, fills, events and
rotations. `logs/paper_positions.json` is separate. Journals and trade logs are
context/reporting, not replacement execution truth. Feature, shortlist,
sentiment and attribution outputs are derived evidence; frozen entry evidence
is retained with lots. Atomic JSON replacement and the OS-held process lock
protect the supported single-process deployment. Reconciliation is required
again after restart; the presence of files alone does not establish readiness.

OAuth loads from project-root `.rh_oauth_cache.json`, unless the process sets
`ROBINHOOD_TOKEN_CACHE` before module import. The client reuses cache/in-memory
tokens, can refresh during normal owner-started operation, then uses its existing
interactive browser flow if necessary. This audit did not access or renew any
credential. Account binding and fingerprint were not reset or changed.
See [authentication details](AUTHENTICATION.md).

## 3. main.py versus automatic backend

| Capability | main.py | Automatic backend | Actual finding |
| --- | --- | --- | --- |
| Startup initialization | Constructs services | Shared Backend receives agent | One coordinated application |
| Broker reconciliation | Calls at startup/cycles | `Backend.reconcile` | Complete matching-account holdings and orders required |
| Discovery | Selects branch, registers jobs/bootstrap | `Discovery.rank/features` via Backend | Upgraded flag currently false |
| Research and sentiment | Ingest/metrics/news; held path | Discovery and held-news features | Local sentiment before paid final reasoning |
| Claude decision-making | `make_decision`, `review_exit` | Specialized `drain_reviews` | Same configured decision provider |
| BUY execution | `executor.execute` | Same ExecutionEngine | Gates and durable intent |
| ADD execution | Held review -> execute | Specialized review -> execute | Same `_add` validation |
| SELL execution | Held/stop review -> execute | Monitor/review -> execute | Same `_sell_leg`; no entry edge gate |
| ROTATE execution | Candidate decision -> execute | Same `_rotate` | Sell first; confirm/reconcile before buy |
| Position management | Shared PositionManager | Same manager | No separate holdings store |
| 15-second monitoring | Registers interval callback | `Backend.monitor` | Independent of decision/model lock |
| Nightly exits | Registers 20:00 ET | `nightly/_nightly` | ATR/features/advisories; no after-hours sell permission |
| Weekly discovery | Registers Sunday 20:00 ET | `weekly` | Also recovers missed/missing shortlist on startup |
| Persistence | Constructs state/engines | Uses same atomic stores | Durable intents and fills |
| Restart recovery | Lock, load, reconcile, catch-up | Order/position reconciliation | Unknown submissions stay unresolved |
| Health and heartbeat | Starts/stops process tracking | Async heartbeat/watchdog/Health | Real PID plus creation time required |
| Error handling | Isolates failed decision cycles | Per-operation failure state/retries | Failure is observable; unrelated jobs continue |
| Shutdown | Stop race and task cleanup | Cancels reviews; retains state | No broker cancellation during shutdown |

**main.py is independently sufficient as the operating entry point.** It depends
on the shared Backend class as an implementation module, not an external service.
That is the actual architecture, not a claim that every external integration has
been exercised successfully.

## 4. Discovery and strategy activation

Current `config/settings.json` values remain:

| Key | Value | Production consumer |
| --- | --- | --- |
| `execution.dry_run` | `true` | Loader -> `env.DRY_RUN` -> all execution callers/engine |
| `execution.live_release_approved` | `false` | Live submission gate |
| `discovery.enabled` | `false` | Backend construction, scheduling, candidate selection |
| `sentiment_selection.enabled` | `true` | Legacy selector when discovery is disabled |
| `monitor.enabled`, `monitor.interval_seconds` | `true`, `15` | Standalone interval job |
| `exits.enabled` | `true` | Stop ratchets/graduation/advisory rules |

With discovery disabled, the existing weekly sentiment selector is intentionally
retained: S&P 500 sentiment selection, then its selected universe; unavailable
legacy selection can use configured watchlist/broker-watchlist fallback. It is
not the upgraded divergence/archetype path. Open holdings are included separately
in either mode. With discovery enabled, the legacy selector is bypassed entirely;
an invalid upgraded shortlist yields no new candidates, rather than silently
switching strategy. Held research and protection continue.

The upgraded path is `Discovery.rank -> universe.refresh/load_universe ->
features -> technical_features/eligibility -> NewsHistory -> score_cohort ->
select_new/publish -> main._resolve_watchlist -> DecisionEngine`. It checks the
versioned S&P 500 universe, completed contiguous historical sessions (minimum
250), price/liquidity/volatility gates, technical fingerprints, deduplicated
45-day news coverage, current/prior sentiment windows and ticker velocity.
FinSense ModernBERT has a real local finance DistilBERT fallback. Small,
degenerate or missing measurement cohorts remain unavailable. Gap components
and raw divergence are separately standardized, classified into A/B/C/D
archetypes, ranked and shortlisted. Default `discovery.shortlist.new_candidate_count`
is **10 new names**, excluding all held names; it is a target, not a guarantee
that sparse data produces ten valid candidates. All held names remain separately
reviewable, even outside the index or shortlist.

Claude receives metrics, fresh per-ticker quote, news, holding/portfolio context,
entry evidence, research cutoffs and measured discovery facts. It can HOLD,
IGNORE, BUY, ADD_TO_POSITION, SELL or ROTATE through the matching decision paths.
Discovery ranking is evidence, not an instruction to buy. Malformed decisions
fall back to HOLD/IGNORE; ADD/ROTATE require their structured fields and gates.
Provider failure is not permission to trade. There is one configured Claude
model, not an unverified automatic paid-model fallback.

To select the upgraded strategy, the owner changes only
`discovery.enabled` to `true` and restarts. `sentiment_selection.enabled` can
remain true: upgraded mode now bypasses it. This does not permit live orders.
Allow bootstrap time and confirm `discovery_ranking` success; initial S&P 500
research can be lengthy. A failed refresh retains previous valid output within
its configured eight-day limit, with failed refresh health recorded. A missing
valid shortlist cannot manufacture candidates.

Configuration precedence is code defaults in `utils/config.py`, merged with
strategy defaults in `utils/strategy_config.py`, then project-root
`config/settings.json`. The loader requires an explicit JSON boolean false to
disable dry run; invalid configuration forces dry-run semantics and normal
startup rejects validation errors. `.env` supplies API secrets only; it does not
override trading flags. Existing process secret environment values take
precedence over dotenv. No YAML trading configuration, CLI activation option or
database trading override exists. Compose passes deployment environment and
mounts JSON read-only.

| Operational controls | Exact source/key and behavior |
| --- | --- |
| Entry size / caps | `trading.size_ladder`, `vol_sizing`, `max_position_pct`, `max_positions`, `sector_cap_pct`, `extreme_vol_reject_30d`, `cycle_edge_bonus`, `edge_score_threshold` |
| Cash / buying power | `ExecutionEngine._available_cash`, pending-buy/fill reservations and 0.99 availability buffer; no invented cash override |
| Liquidity | `execution.min_dollar_volume`; discovery eligibility separately uses `discovery.gates.*` |
| Adds | `trading.add_to_position.*`; explicit thesis, stop, count, cooldown, equity/incremental-risk checks |
| Orders | Market orders are selected in `_submit`; extended hours remains disabled/unreleased |
| Stop bands | `trading.stop_loss_bands`; unchanged −7/−10/−13/−16% |
| Profit lock/trail | `exits.breakeven_*`, `exits.chandelier.*`; ATR period 14 |
| Graduation | `exits.take_profit_pct=0.20`, `take_profit_mode=graduate`; no automatic fixed +20% ceiling |
| Advisories | `exits.trend_break`, `climax`, `sentiment_reversal`, `dead_money`, `gap_closure_z`, `rotation_margin_z`, `specialized_review`, `pead`; `trading.max_holding_days` |
| Decision times | `schedule.open_hour/open_minute`, `mid_hour/mid_minute`, `close_hour/close_minute`, `timezone` |
| Nightly/heartbeat | Actual callback registration uses 20:00 ET / 30 seconds; not separate YAML jobs |
| Weekly rank | `discovery.schedule.rank_day`, `rank_hour_et` (Sunday 20 ET) |
| Legacy rank | `sentiment_selection.schedule_day/hour/minute/timezone`, only in legacy mode |
| Report | `reporting.attribution_enabled/hour/minute` (16:05 ET) |
| Providers/models | `news.provider=finnhub`; `models.decision=claude-sonnet-5-5`; `sentiment_selection.model_name/fallback_model/batch_size` for local sentiment |
| Freshness | `execution.quote_max_age_seconds=60` now used consistently; `reconciliation_max_age_seconds=120` controls manager readiness; feature age live gate is five days |
| Emergency stop | `execution.manual_stop_file=data/STOP_TRADING`; blocks every live submission including protective sells |

Some retained compatibility fields are not independent active scheduler/exit
controls: `discovery.schedule.feature_hour_et` does not schedule a second feature
job (features run in the 20:00 nightly callback); old monitor soft-profit cooldown
fields and `_evaluate_threshold_signal` belong to the unused legacy helper.
`trading.take_profit_pct` is retained position/prompt metadata, while active
graduation uses `exits.take_profit_pct`. Do not toggle these expecting to replace
the active exit rules. They were not used to claim readiness, and no values were
changed. Their removal/independent rescheduling is outside Monday remediation.

## 5. Execution and exit verification

| Action | Actual dispatch and checks | Fill/recovery behavior |
| --- | --- | --- |
| BUY | Main candidate decision -> `execute -> _buy_leg -> _check_buy_gates -> _submit`; edge/cycle bonus, liquidity, tradability, capacity, sector and cash/volatility sizing | Intent before one placement attempt; only confirmed fills create lots |
| ADD_TO_POSITION | Held routine or specialized review -> `execute -> _add -> _submit`; thesis/evidence, current lifecycle, viable existing stop, add count/cooldown, exposure and incremental risk | Weighted cost/lots update from fills; persisted stop maxima preserved |
| SELL | Held review or direct hard stop -> `execute -> _sell_leg -> _submit`; current lifecycle, fresh quote, matching reconciliation, owner gates | No entry edge gate; full position quantity; partial fill remains open |
| ROTATE | Main candidate -> `execute -> _rotate`; two-leg thesis, valid source/target and source quote | Sell first. Partial/unknown sell prevents buy. Full fill requires refreshed matching account/cash/target quote and revalidated buy gates. No blind restart replay |

Live requests additionally require dry run false, explicit release approval,
absent stop file, open NYSE session, good state and reconciled orders. Pending
intents reserve cash/tickers; unknown orders block new dependent entries.
Provider and placement errors retain failure states. A timeout is ambiguous,
not a rejected or completed trade. Cancellation during placement can leave a
durable CREATED intent; reconcile it before any replacement. No automatic retry
of placement is used. Complete order pagination is required; malformed or
incomplete broker data cannot become a verified empty book.

Dry run can call Robinhood's **review/preview** API through `_submit`; it is not
an entirely offline application mode. This investigation did not invoke it.
The main-cycle integration tests mock execution dispatch; the existing execution
tests separately exercise the real engine against synthetic broker responses.
Neither test category proves a real order can be placed or filled.

The 15-second callback runs `Backend.monitor`: calendar gate -> independent
monitor lock -> periodic reconciliation -> fresh bid/last quote -> reload same
position lifecycle under locks -> `update_trail` -> persist -> direct SELL when
crossed. It cannot wait on Claude. Pending adds cannot disable an existing hard
stop. Stops use the maximum of persisted, initial, profit-lock and eligible
trailing levels. PositionManager merges maxima and rejects stale balance/cost
updates, so routine merges/restart cannot loosen them. A verified split uses the
separate corporate-action price-basis adjustment.

At +5% peak the default lock is entry ×1.005; at +10%, fresh nightly Wilder ATR14
permits peak −3×ATR14. At +20%, graduation queues one defended harvest-or-HOLD
review; a valid HOLD continues the trail. The monitor also queues debounced
drawdown/new-high reviews. Nightly processing updates ATR and completed-session
technical/news evidence, then `nightly_signals` evaluates trend break, climax,
sentiment reversal, dead money, comparable gap closure/inversion, optional
catalyst-spent/PEAD, opportunity cost and advisory time stop. Climax can tighten
the trail. Optional missing measurements are explicitly unavailable; there is
no invented PEAD estimate. The repository has a point-in-time PEAD ingestion
procedure, not a proven complete automatic EPS/benchmark feed.

Nightly advisories are persisted review events. The next market-hours monitor
starts `drain_reviews`; it groups per ticker, calls `review_exit`, and routes
accepted SELL/ADD through the same engine. **20:00 ET does not authorize
after-hours liquidation.** Review retry/debounce state persists. An accepted
model SELL is not recorded as a completed exit until fills confirm it.

## 6. Issues found and fixed

All operational fixes below are covered by
`tests/test_monday_wiring.py`, the focused command in section 8, and the final
full suite. Existing safety tests additionally verify fills, rotations, restart,
bad state, stale quotes, account mismatches, timeouts, pagination and stop merges.

| File/function | Original behavior and root cause | Exact correction / independent regression |
| --- | --- | --- |
| `main._start_jobs`, `Backend.ensure_discovery` | Fresh upgraded start could wait until next Sunday; an eight-day-valid snapshot could also hide a missed weekly run | Background bootstrap validates shortlist and most recent configured weekly boundary, then serializes ranking; tests cover missing data, reconciliation failure, valid reuse and missed Sunday |
| `main._start_jobs`, `_resolve_watchlist` | Enabled upgraded discovery could still initialize legacy research or fall back to legacy names after an invalid shortlist | Exclusive upgraded/legacy branch; invalid upgraded shortlist yields no new names; real startup registration and fallback refusal tested |
| `main._run_cycle` | Day cache could hide a new upgraded shortlist | Upgraded mode resolves current shortlist each cycle; legacy day cache retained; actual call path inspected |
| `main._start_jobs` | Cron seconds encoding became every second when configured period reached 60; duplicate monitor registrations were possible | Single interval job, stable IDs, replacement, coalescing and one instance; real registration tested at 15 and 60 seconds and callbacks invoked |
| `main._start_jobs` | Attribution registration occurred after optional legacy initialization/bootstrap, so a hanging provider could prevent that unrelated report schedule | Register attribution alongside the other jobs before starting optional research; legacy startup regression asserts registration before selector initialization |
| `Backend.nightly`, `weekly`, `attribution` | Failed reconciliation was ignored before dependent research/reporting | Return without using unverified holdings; nightly/weekly record failure. Tests assert no historical/rank request after failed reconciliation |
| `Backend.monitor`, `_nightly` | Reload by ticker after asynchronous work could target a newly opened position | Reload by immutable position ID; inspected both production callbacks |
| `Backend._nightly` | Missing 250-bar technical fields could raise during frozen-gap calculation, suppressing otherwise available nightly exits | Compute comparable gap only when its measured feature inputs exist; 20-bar and 250-bar production callback tests |
| `Backend._nightly` | Opportunity comparison lacked a common gap reference; old cohort scores could be compared or never trigger | Recompute holding and candidate against the holding's frozen entry transform; test deliberately supplies a different cohort and bogus stored score, then checks recomputation |
| `Backend._nightly` | Failed fresh-news fetch could leave weekly sentiment z-components available to an ostensibly current exit check; old held-feature rows could persist | Replace temporal sentiment components with fresh-only components and clear held-feature output each valid run; failed-news production regression |
| `Backend._nightly`, `attribution` | Derived held-feature persistence could fail after nightly health was marked successful; attribution could return after failed reconciliation without marking its report failed | Record nightly disk-write and attribution reconciliation failures explicitly; injected disk-error and failed-reconciliation tests confirm no misleading current success/report |
| `Backend.ensure_nightly`, `main._start_jobs` | Restart had no recovery of missed held nightly maintenance | Background catch-up checks completed NYSE close, latest successful maintenance and each held ATR date; one recovery then no repeat, tested |
| `main.run_cycle` | Exception in immediate catch-up escaped startup and shut down all jobs | Record failure, release decision lock, leave monitor and next cycle available; failing-then-successful regression. Success is recorded only at actual cycle completion |
| `main._decision_quote`, held/candidate loops | Later reviews inherited quotes fetched before slow ingestion/research/earlier model calls | Refresh and validate each ticker before its review, update price-dependent metrics; real main-cycle tests cover BUY/ADD/SELL/ROTATE dispatch and quote calls. Engine still rejects a quote that ages during the model call |
| `Backend.heartbeat` | Synchronous APScheduler callback ran in a worker thread alongside mutable health writes | Native async callback keeps health access on the event loop; coroutine and heartbeat-state test |
| `main.start` | SIGTERM set a stop event but could wait behind startup research/catch-up | Race startup task against stop event; stop scheduler and cancel startup/bootstrap/review work in cleanup. Stop-during-active-startup and existing startup-failure tests verify cleanup |
| `utils.freshness.fresh_quote`, main/Backend/ExecutionEngine callers | Advertised `execution.quote_max_age_seconds` was bypassed by repeated hardcoded 60-second checks | Shared validated policy used in research, protection, review, ADD and rotation/regular execution; 30/60-second mocked execution tests and five invalid-policy cases. Checked-in value remains 60 |
| `ExecutionEngine._execute`, held evidence | Late SELL/ADD reviews could reach execution after the original position lifecycle ended | Attach position identity to held evidence/hard stops; reject mismatched current lifecycle before submission; SELL and ADD stale-lifecycle regressions |
| Dated offline runner | Repository-wide format check found pre-existing layout differences | Format only; AST hash before/after identical, guards unchanged; final repository format check passes |
| README/current audit/walkthrough/deployment docs | Old deployment steps still described already-completed legacy/journal reset work | Link latest audit, preserve old evidence, correct operating branch/bootstrap/shutdown and remove obsolete reset requirements from active procedure |

One intermediate missed-weekly test failed because its backend clock was frozen
but its shortlist validator clock was not. Both clocks now use the same Monday
fixture; the test then passed. Initial lint findings on newly added code were
corrected rather than excluded or suppressed. No test was weakened or skipped.
`test_production_integrity`'s cycle-overlap fixture gained a health mock to model
the new real health call; its non-overlap assertion is unchanged.

## 7. Remaining errors, limitations and unverified paths

* **Live blockers:** no current matching-account read, authentication test,
  funded-account check, paid Claude/Finnhub call, real market-data/discovery run,
  broker preview or live order verification was permitted/performed here. Owner
  must establish these before claiming live readiness. No claim is made that a
  configured model name proves availability or entitlement.
* Current flags still select legacy discovery. Upgraded activation requires the
  owner setting in section 4. Data coverage/rate limits may prevent a valid
  cohort/shortlist. Failure is visible and cannot manufacture evidence.
* Stops are software managed, market-hours only. Process/host/broker/feed outages,
  gaps and halts interrupt protection. Stop prices do not guarantee fill prices.
  No host watchdog or external alert delivery was deployed/tested.
* Scheduler state is in memory. Startup catches up one current decision window,
  not every missed historical cycle; held-nightly and weekly discovery recovery
  are separate. Restart may repeat research for the current window. Durable
  intents/conflicting-order guards prevent blind pending-order repetition; this
  is not an exactly-once historical research ledger.
* Weekly and nightly discovery workloads can be expensive in time/provider
  requests, although local scoring is not a paid inference call. The protection
  lock is independent; overnight scans have not been measured on a deployment.
* A model call that outlasts quote freshness can still be rejected after review.
  Quotes are not silently relabeled fresh or replaced after the decision.
* Missing PEAD/catalyst/entry-reference data disables only the affected measured
  advisory. It does not establish an unsupported signal. Partial exits and
  extended-hours execution remain unreleased.
* No Docker executable is installed in this environment. No image, mounted-file
  permissions, cross-host authentication, reboot, container health or remote
  operation was tested. Compose uses the same main entry and persistent
  `/app/{config,data,logs,journals}` mounts, but its named OAuth volume at
  `/app/oauth/token.json` is **not automatically the Windows credential cache**.
  Private owner provisioning and model dependencies/cache remain deployment
  requirements; `INSTALL_SENTIMENT` defaults false despite enabled sentiment.
* One AnyIO pytest plugin warning concerns the CallSpec2 alias scheduled for
  pytest 10 removal. Current pinned pytest tests pass; this is not an operating
  runtime failure. No known unresolved critical/high code defect was found in
  the inspected Monday path after remediation; that statement is bounded by
  the unverified external integrations above.

The scheduler's coalescing/concurrency settings do not provide persisted
cross-restart catch-up. This agrees with the
[APScheduler 3.x guide](https://apscheduler.readthedocs.io/en/3.x/userguide.html).
The installed AsyncIO executor source was inspected as well: shutdown cancels
pending coroutine futures; setting wait=True alone is not an order-completion
guarantee. Durable unresolved-intent handling remains necessary.

## 8. Verification results

Run from the repository root using `.venv-upgrade\Scripts\python.exe`.

| Command/check actually run | Result |
| --- | --- |
| `python -B docs/verification/run_offline_audit_2026-11-12.py -m ''` before edits | 214 passed, 1 warning, 51.75 seconds |
| Same command after final implementation | 243 passed, 1 warning, 14.17 seconds |
| `python -B docs/verification/run_offline_audit_2026-11-12.py tests/test_monday_wiring.py tests/test_backend_safety.py tests/test_clean_reset.py tests/test_production_integrity.py tests/test_execution_guards.py tests/test_add.py tests/test_rotation.py -x` | 107 passed, 1 warning, 5.04 seconds |
| `python -B -m ruff check . --no-cache` | Exit 0 |
| `python -B -m ruff format . --check --no-cache` | Exit 0 |
| `python -B -m ruff check . --select F --no-cache` | Exit 0; static unresolved-name/import checks |
| In-memory `compile(source, path, 'exec')` over main/core/models/utils/MCP/scripts/tests | 113 Python files, exit 0; no bytecode writes |
| `python -B -m pip check` | Exit 0; no broken requirements |
| `python -B scripts/preflight.py` | Exit 0; local validation true, live_ready false |
| `python -B scripts/healthcheck.py` | Exit 1, PROCESS_ABSENT, expected because the actual app was not started |
| `git diff --check` | Final whitespace check performed; pre-existing line-ending notices are not trading failures |
| Final configuration hash / HEAD | Unchanged from investigation baseline |
| Normal startup / actual broker / actual live execution | Not run; prohibited external/runtime effects |

Evidence: [full tests](verification/pytest_monday_2026-11-15.txt),
[focused tests](verification/pytest_monday_targeted_2026-11-15.txt),
[other checks and initial inventory](verification/checks_monday_2026-11-15.json).
The [before-edit output](verification/pytest_monday_baseline_2026-11-15.txt) is
also retained alongside the final evidence.
Full test execution includes heavy cached-model tests by overriding the default
marker expression. Socket guards block provider traffic. Mocked order assertions
are not broker execution verification. Import paths are exercised by these tests
and statically checked; importing every maintenance script for its side effects
was not used as a substitute for safe verification.

## 9. Files changed in this investigation

Existing user changes were preserved. The much larger Git diff also contains
work that predates this investigation; it must not be attributed entirely to it.

| Modified or added | Purpose |
| --- | --- |
| `main.py` | Exclusive strategy startup, bootstrap/catch-up waiting, stable schedules/interval, fresh per-ticker quotes, observable cycle failures, responsive cleanup, held lifecycle evidence |
| `core/backend.py` | Discovery/nightly recovery, reconciliation gating, lifecycle reloads, comparable opportunity evidence, fresh-only sentiment, async heartbeat |
| `core/execution/engine.py` | Shared configured freshness and stale-lifecycle refusal |
| `utils/freshness.py` | Fail-closed shared quote-age policy |
| `tests/test_monday_wiring.py` (added) | 29 parametrized cases covering actual standalone registration/cycles, recovery, shutdown, evidence, policy, lifecycle and persistence failures |
| `tests/test_production_integrity.py` | Health-aware overlap fixture; existing assertion preserved |
| `README.md` | Latest report link |
| `docs/END_TO_END.md` | Current runtime/branch/catch-up/shutdown description |
| `docs/PRODUCTION_READINESS_AUDIT.md` | Latest-report pointer above preserved historical findings |
| `docs/DEPLOYMENT.md` | Correct obsolete pre-reset steps |
| `docs/verification/README.md` | Current evidence index |
| `docs/verification/run_offline_audit_2026-11-12.py` | Formatting only, AST unchanged |
| `docs/MONDAY_READINESS.md` (added) | This report and owner operating procedure |
| `docs/verification/pytest_monday_2026-11-15.txt` (added) | Final full-suite output |
| `docs/verification/pytest_monday_targeted_2026-11-15.txt` (added) | Focused output |
| `docs/verification/pytest_monday_baseline_2026-11-15.txt` (added) | Before-edit full-suite output |
| `docs/verification/checks_monday_2026-11-15.json` (added) | Actual check results, baseline inventory and safety/change evidence |

No file was deleted by this investigation. Prior deleted baseline/history files
were already deleted at the start and were not reset or recreated. No historical
audit evidence or canonical execution record was deleted. Configuration,
credentials, account binding, sizing and risk thresholds were not modified.

## 10. Exact Monday instructions

1. Select the strategy yourself in `config/settings.json`: set
   `discovery.enabled=true` for upgraded L0–L6. Keep `execution.dry_run=true` and
   `execution.live_release_approved=false` for initial operation. Keep existing
   sizing, stops, add limits, `monitor.enabled=true`, 15-second interval and
   `exits.enabled=true` unchanged. Do not remove a stop file or reset state just
   to obtain a green result.
2. Establish provider access and the intended account using the existing owner
   procedures. The latest historical zero-balance result is not sufficient to
   place a new buy. Normal main startup can refresh credentials or request
   interactive approval; do not force it by deleting caches. The existing
   `scripts/probe_broker.py` is a non-renewing **empty-account** diagnostic;
   after funding/opening positions its discrepancy result is not an auth
   verdict. Do not use the reset script as a launch command.
3. Run preflight and then deliberately start the application. Starting it can
   use providers and broker previews even with dry run on. These commands were
   not used to start the real scheduler during this audit.

```powershell
Set-Location 'C:\Users\Shris\OneDrive\Desktop\Project\S-BOT\robinhood-agent'
& .\.venv-upgrade\Scripts\python.exe -B scripts/preflight.py
& .\.venv-upgrade\Scripts\python.exe -B main.py
```

Launch sequence: obtain lock; validate JSON and construct services; establish
process identity; reconcile eligible-account fingerprint, complete holdings and
orders; register/start safety and attribution jobs; schedule held-nightly recovery and upgraded
bootstrap (or legacy selector/bootstrap); run at most the
current eligible decision-window catch-up; wait. Broker reconciliation failure
does not authorize any trading. Failed optional discovery/research does not
disable monitor scheduling. On Monday before 09:35 there is no immediate OPEN
decision; the monitor starts regular-market protection at 09:30.

While running: 15-second protection polling, 30-second heartbeat, 09:35/12:30/15:30
decision cycles, 16:05 attribution, 20:00 held maintenance/features/advisories and
Sunday 20:00 discovery ranking. Exchange calendar gates trading on holidays and
half-days; 15:30 CLOSE is skipped if the exchange has already closed early.
There is no special guaranteed execution exactly at an exchange auction.

Use another PowerShell terminal for read-only local verification:

```powershell
Set-Location 'C:\Users\Shris\OneDrive\Desktop\Project\S-BOT\robinhood-agent'
& .\.venv-upgrade\Scripts\python.exe -B scripts/healthcheck.py
Get-Content .\logs\agent.log -Tail 60 -Wait
```

For scheduler registration and actual cycle log markers, stop the log tail with
Ctrl+C and run:

```powershell
Select-String -Path .\logs\agent.log -Pattern 'monitor_registered|weekly_scheduler_registered|attribution_report_registered|cycle_start|cycle_complete|cycle_failed'
$runtimeHealth = Get-Content .\logs\health.json -Raw | ConvertFrom-Json
$runtimeHealth | Select-Object -Property process,heartbeat,broker,monitor,quotes,nightly,discovery_ranking,attribution,'decision_cycle:OPEN','decision_cycle:MID','decision_cycle:CLOSE'
```

Inspect `last_attempt`, `last_success` and `error`, not file existence. Expect a
matching current process/creation time, fresh heartbeat, broker success without
an error, fresh market-hours monitor/quotes and successful discovery ranking
when enabled. Missing optional job state before its first run is not proof it
ran. Re-read health to refresh the PowerShell snapshot. Full account/holdings,
order-history and quote parsing are read-only operations inside reconciliation
and monitoring; do not infer their success from a registered callback. This
audit cannot certify the account until that real read succeeds and matches the
preserved binding. Local health does not independently prove provider billing
access or the absence of broker-side pending orders after its last snapshot.

Stop safely with **Ctrl+C in the terminal running main.py**. SIGTERM is supported
for service supervisors; a Windows forced process termination is not a graceful
stop. Confirm `PROCESS_ABSENT` afterward and the shutdown/process state. Do not
delete locks, positions or credentials. Open broker orders may survive shutdown
and must reconcile on restart.

Only after resolving the external blockers and deliberately authorizing live
orders does the owner set both `execution.dry_run=false` and
`execution.live_release_approved=true`, then restart. This report does not grant
that approval. No additional trial period, closed-trade count, automatic exposure
ramp, spend cap or altered position sizing was introduced.

## 11. Final safety assessment

| Requirement | Finding |
| --- | --- |
| Accidental live-order path during investigation | No normal application startup; all execution tests used mocks/isolated stores; external socket connections blocked |
| Credentials/secrets | No credential contents read, printed, renewed or changed |
| Paid calls | Zero |
| Broker requests/mutations/previews | Zero during this investigation |
| Risk thresholds/sizing/live flags | Configuration hash unchanged; no activation |
| Commits/pushes/deployment | None; HEAD unchanged |
| Local intents/state | Preflight: zero unresolved local intents, zero local positions, binding present; no financial-state cleanup performed |
| Duplicate processes/schedules | OS-lock regression and callback/interval/decision-overlap regressions pass; actual running/deployed instance was not started |
| Restart/failure behavior | Controlled pending/partial/unknown orders, timeout/auth failure, incomplete data, malformed state, stale stops and shutdown fixtures pass |
| Live readiness/account correctness | Not established by mocks; requires current successful matching-account reads and owner-controlled external verification |

All conclusions are about inspected code and executed verification at this
snapshot. No profitability or uninterrupted protection guarantee is implied.
