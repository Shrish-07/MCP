# November 16 operating audit

The latest standalone runtime investigation, fixes, verification and owner launch
instructions are in [MONDAY_READINESS.md](MONDAY_READINESS.md). Earlier dated
findings below remain historical evidence; they do not establish current broker
authentication, balances or live readiness.

# Complete local reset and pre-live readiness

Updated November 12, 2026 after the explicitly authorized reset. This current review supersedes the historical audits retained below. The scope is local operation.

## A. Verdict

**READY WITH EXPLICIT LIMITATIONS.** The software is ready for an owner-started local dry run from a clean state. The journal store is empty, the schema-2 portfolio is empty and bound to the verified account, required sentiment dependencies are installed, both configured models work locally, all 214 tests pass and the full lint check is green.

The normal scheduler was not started. Real paid-provider requests and live order execution were not tested. These limitations prevent a claim of proven live execution or profitability. Provider billing comparison remains pending until real usage exists and does not block local readiness.

## B. Reset results

| Item | Verified result |
| --- | --- |
| Remaining legacy journal files removed | 9. |
| Remaining legacy journal records removed | 169. The earlier cleanup had already removed 250 research-only records. |
| Journal files and old context remaining | 0. No archive or backup of old journals was created. |
| Legacy active position rows removed | 4. |
| Current active local positions | 0. |
| Position state | Valid schema 2, live mode, verified account binding present. |
| Local unresolved intents | 0 before reset and 0 afterward. No intent was silently discarded. |
| Position-level peaks, stops, entry state and reviews | Removed with the four legacy positions. |
| Other derived strategy files removed | 4, covering old earnings/headline caches, performance and application attribution output. |
| Other active cached selection, ranking, feature and sentiment memory | None remains. All configured derived-store paths were checked. |
| Canonical recovery/audit records | Preserved. Existing orders/fills were retained by the reset implementation and regression-tested. The actual local canonical order/fill maps were empty. Historical transaction and diagnostic logs remain intact. |
| Historical broker orders | All 27 remain terminal at Robinhood. No broker records changed. |

The cleared locations were `journals/`, active positions in `logs/positions.json`, `data/earnings_calendar.json`, `data/seen_headlines.json`, `logs/performance.json` and the old application attribution report under `logs/`. Derived shortlist, candidate-memory, feature, sentiment and review-cache patterns were absent after cleanup. No old ticker names or records were copied into this report.

Static constituent/candidate inputs and configured default watchlists are intentional inputs and were preserved. They contain no saved thesis/ranking history. Canonical execution logs and dated replay/test evidence are retained as audit material and are not read as research prompt context.

`PositionManager.reset_strategy_context` requires a complete empty broker portfolio, a verified account fingerprint, no unresolved local or broker order and no unresolved rotation. Backup checks reject unmatched fills or missing order identities while allowing an older snapshot of an already resolved order. The reset keeps canonical orders, fills, events and reconciliation records. It records a fill-ID baseline that prevents retained pre-reset fills from rebuilding journals.

Restart, stale-record and journal-reconstruction tests prove that old holdings/context cannot reappear from the retained canonical ledger. A fresh journal can still be created and summarized using the normal manager. [Reset evidence](verification/reset_local_2026-11-12.json), [clean-state verification](verification/clean_state_reset_2026-11-12.json)

## C. Robinhood verification

Two snapshots were necessary, one before the reset and one afterward. Each used exactly one request to accounts, portfolio, holdings and orders. Eight read-only tool requests were made in total.

| Field | Before and after |
| --- | --- |
| Unique eligible account | Yes, the previously owner-confirmed Agentic selection. |
| Selected-account fingerprint and portfolio identity | Consistent in both snapshots. |
| Local account binding | Initialized with that verified fingerprint and matched afterward. No identifier was printed. |
| Cash | $0.00. |
| Total account value and equity value | $0.00 each. |
| Holdings count, quantity and market value | 0, 0 and $0.00. Complete response. |
| Buying power and unleveraged buying power | $0.00 each, checked independently. |
| Other returned asset-category values and pending deposits | $0.00 each. |
| Open, pending and unresolved orders | 0 each. |
| Historical terminal orders | 27. |
| History completeness and fill consistency | Passed. No continuation page was advertised. |
| Withdrawable, unsettled and some liability fields | Unavailable. Not assumed zero. |

Read-only guards prohibited order previews, submissions, cancellation, registration, token renewal, browser authorization and credential writes. Raw response recording was disabled. No account mutation occurred, and the after-reset account snapshot matched the expected zero state. The client handled existing authentication internally. The task did not directly inspect credential or dotenv contents.

## D. Dependencies and local models

The existing project virtual environment remains CPython 3.13.6. The pinned packages now import and pass dependency consistency checks.

| Package | Installed version |
| --- | --- |
| Torch | 2.13.0 |
| Transformers | 5.16.1 |
| Accelerate | 1.14.0 |
| Hugging Face Hub | 1.29.0 |
| Safetensors | 0.8.0 |
| Tokenizers | 0.23.3 |

Supplemental transitive versions were pinned in `requirements-ml.txt` for reproducibility. Existing core packages were not broadly upgraded. The first Torch installation encountered Windows' path-length limit. Retrying the same pinned wheel with an extended-length prefix targeted the same existing environment and succeeded.

The primary model reused its existing 598,442,860-byte weight file. The required fallback's 267,835,644-byte weight file and minimal tokenizer/configuration files were downloaded anonymously after checking its public source, Apache-2.0 license, expected size and configuration. No duplicate primary weights were downloaded. Both retain their configured model IDs and are now pinned to verified revisions. [Primary model](https://huggingface.co/AnkitAI/FinSense-ModernBERT-Financial-News-Sentiment-Analysis), [fallback model](https://huggingface.co/AnkitAI/distilbert-base-uncased-financial-news-sentiment-analysis)

Primary and forced-fallback inference each processed three synthetic headlines locally. Both produced three finite scores in [-1,1], with the expected positive/negative/neutral label mapping. The primary returned approximately +1, −1 and 0. The fallback returned approximately +0.991, −0.989 and 0.001. No inference endpoint was called.

Initialization is serialized across threads. Loader requests use `token=False`, fixed revisions and the current supported dtype argument. Invalid batch sizes and nonfinite/wrong-width logits fail explicitly. A synthetic primary-load failure selected the actual cached fallback. [Local model evidence](verification/local_models_reset_2026-11-12.json)

## E. Code corrections

### Architectural choices

| Area | Alternatives considered | Selected approach |
| --- | --- | --- |
| Reset | Delete the position file, or reset a verified schema while retaining recovery records. | Schema-preserving reset with account/empty-state guards and explicit journal baselining. This preserves idempotency and prevents old fill narratives returning. |
| Health | Trust file existence/mtime, or verify process identity, heartbeat and reconciliation status. | PID plus creation time, fresh heartbeat and typed safety status. This distinguishes dead/reused processes from live-but-stale work. |
| Failure handling | Narrow every service boundary and risk losing fail-safe fallback, or retain intentional boundary catches with visible sanitized diagnostics. | Explicit data validation plus sanitized exception-type logging at operational boundaries. Silent failures and accidental exception-content logging were removed. |
| Compatibility modules | Remove old imports, or preserve their public objects without wildcard ambiguity. | Export the original public symbol surface and identity. Ten parameterized compatibility tests verify it. |
| Sentiment | Disable the configured workflow, or repair its local runtime. | Install compatible pinned dependencies and verify the existing models. Feature settings and thresholds remain unchanged. |

### Important changes

| File or component | Correction and proof |
| --- | --- |
| `utils/state_reset.py`, `scripts/reset_local_context.py` | Explicit reset after guarded broker verification. Refuse unresolved intents, unmatched backup execution history and account mismatch. Report only sanitized summaries. Reset/refusal tests and actual before/after evidence pass. |
| `core/position/manager.py` | Retain the earlier stale-stop/balance protections. Add canonical-history-preserving context reset, binding checks and reset baselines. Zero/restart/stale-snapshot tests pass. |
| `core/backend.py` | Distinguish authentication, broker availability and state reconciliation failures. Refresh heartbeat even outside market hours. Exclude old fill IDs from journal reconstruction. |
| `utils/health.py`, `scripts/healthcheck.py` | Verify PID creation time and heartbeat. Report PROCESS_ABSENT, PROCESS_STALE, HEALTHY, BROKER_UNAVAILABLE, AUTHENTICATION_FAILED or STATE_RECONCILIATION_FAILED, with a separate market-data failure where applicable. Controlled health fixtures cover each branch. |
| `main.py` | Reject invalid startup configuration, track process lifecycle, arrange SIGTERM through the event loop, and clean up after startup failure or shutdown. The scheduler itself was not run. |
| `utils/config.py`, `scripts/preflight.py` | Add secret-free inspection, stable project-root dotenv resolution for normal startup, an offline validation report and optional explicit report output. Billing is pending and no longer a local-readiness gate. |
| `robinhood_mcp/robinhood_client.py` | Preserve authentication classification, retry pending credential persistence before session reuse, fail on unsaved credentials, close callback resources, stop logging authorization URLs, validate raw balance fields and retain authoritative zero buying power. Invalid balances cannot become fake zeros. |
| `models/portfolio.py`, decision/main prompts | Retain actual cash and compute cash percentage from valid balances. Missing cash displays as unavailable. |
| `core/data/sentiment.py`, `requirements-ml.txt` | Pin models/dependencies, serialize initialization, avoid implicit credentials and reject invalid logits. Real cached inference and failure tests pass. |
| `utils/journal_manager.py` | Validate symbols before constructing paths. Normal creation/retrieval works after the reset. |
| `core/weekly/selector.py` | Move blocking file reads/writes off the event loop and atomically persist selection. |
| `robinhood_mcp/response_recorder.py` | Offload JSONL append I/O while preserving serialization under its lock. Concurrent-write regression passes. |
| `utils/instance_lock.py` | Acquire ownership before initializing the lock byte. Re-entry does not grow or rewrite the file. Windows-aware lock tests pass. |
| `scripts/probe_broker.py` | Use the same guarded read-only client path. No direct freshness-only conclusion, renewal, local reconciliation or automatic report overwrite. |
| `scripts/replay_archived.py` | Validate symbol paths and report missing/empty journal observations clearly after a reset. |
| Compatibility wrappers and other active source/tests | Reviewed import/type/UTC/resource/error-handling cleanup, readable formatting and lint corrections. No risk threshold or sizing change. |
| Tests | Reset, health, restart, compatibility, model, concurrency, parsing and provider-accounting regressions. Existing assertions were retained. |

All 390 initial active-source Ruff findings were remediated. No new rule suppression was introduced. The dated offline runner received whitespace-only import separation, preserving its guard behavior and its historical evidence role.

The strategy remains thesis-driven over multi-day to multi-week horizons. BUY/ADD/SELL/ROTATE guards, pending reservations, hard-stop independence, monotonic protection, graduation, E1–E12 and review cooldowns retain their configured thresholds. Missing/unsupported features and invalid model outputs fail safely. The 15-second monitor remains a protection/event cadence, not a constant model-analysis cadence.

## F. Testing

The executable below is `./.venv-upgrade/Scripts/python.exe`. Actual commands and final results are recorded.

| Command or check | Exit | Result and scope |
| --- | --- | --- |
| `python -B docs/verification/run_offline_audit_2026-11-12.py -m ''` | 0 | 214 passed, 0 failed, 0 skipped/deselected, one AnyIO deprecation warning, 46.80 seconds. Includes three cached-model tests, offline/mocked broker tests and all added accounting tests. |
| `python -B docs/verification/run_offline_audit_2026-11-12.py tests/test_provider_accounting.py` | 0 | Four mocked provider-accounting tests passed. No SDK request or API-key use. Included again in the complete 213-test run. |
| `python -m ruff check . --no-cache` | 0 | No findings across the repository's Python checks. |
| `python -m ruff check main.py core robinhood_mcp utils models scripts tests --no-cache --select F` | 0 | Active-source Pyflakes passed. |
| `python -X pycache_prefix=.test-tmp/reset-task/pycache -m compileall -q main.py core robinhood_mcp utils models scripts tests` | 0 | Compilation passed with isolated bytecode output. |
| `python -m pip check` | 0 | No broken installed requirements. |
| `python -B scripts/preflight.py --output docs/verification/preflight_clean_reset_2026-11-12.json` | 0 | Valid clean state, binding present, zero positions/intents, required dependencies present and local validation passed. |
| `python -B scripts/healthcheck.py` | 1 | PROCESS_ABSENT, correctly reporting that the scheduler/backend was not started. Controlled fixtures passed all live/stale/failure health classifications. |
| `python -B scripts/reset_local_context.py --apply --output docs/verification/reset_local_2026-11-12.json` | 0 | Actual authorized reset, guarded broker reads before/after, RESET_VERIFIED. |
| Actual local primary and fallback inference | 0 | Six synthetic-headline scores, normalized and correctly mapped. Public downloads completed separately before offline inference. |
| Synthetic replay in the full suite | 0 | Partial fills, gap, stop ratchets and restart passed using local fixtures. Deleted historical journals were not reconstructed for replay. |
| `git diff --check` | 0 | Whitespace passed, with Windows line-ending notices. |

The guarded runner blocks external socket connections, secret-file reads and protected runtime writes, redirects cost records to scratch storage, and forces offline model access. Local model tests are normally heavy/excluded by default. They were explicitly included here because dependencies and both caches were available. No full-universe processing occurred.

Intermediate failures were corrected rather than hidden. The first package install hit the Windows long-path issue. Early mock-loader fixtures needed the new anonymous/revision kwargs, and backup validation was corrected to accept a resolved order's earlier snapshot. A new Windows lock test initially attempted a forbidden second-handle read and was corrected to inspect through the owning handle. Final tests pass. The remaining warning is the installed AnyIO plugin's deprecated Pytest CallSpec2 alias.

Real order placement, paid inference/news requests, token renewal and the normal scheduler were intentionally not exercised. [Final complete test output](verification/pytest_clean_reset_verified_2026-11-12.txt), [evidence index](verification/README.md)

## G. Remaining blockers and explicit limitations

There is no remaining identified blocker to the controlled local dry run with the current settings.

- The scheduler has not been run in this task. Operational health appears only after the owner starts the process. PROCESS_ABSENT is expected beforehand.
- Paid news and Claude availability were not tested and secret values were not inspected. Normal jobs require the owner's existing credentials.
- Live fills, rejection payloads, market gaps and actual ambiguous-order recovery have offline/mock coverage only. No live-execution certification is made.
- Unavailable withdrawal/unsettled/liability fields remain unavailable.
- The third-party test warning remains visible.
- Billing comparison is pending until real usage exists. It is not a readiness blocker or a spending gate.

No deployment task or hosting recommendation is part of this phase. No profitability claim follows from these tests.

## H. Exact local startup procedure

From the repository root, when ready, run the following in PowerShell.

```powershell
Set-Location 'C:\Users\Shris\OneDrive\Desktop\Project\S-BOT\robinhood-agent'
& .\.venv-upgrade\Scripts\python.exe -B scripts/preflight.py
& .\.venv-upgrade\Scripts\python.exe main.py
```

The last command was not executed during this task. It takes the instance lock, initializes process health, reconciles the bound account and empty book, starts the scheduler and can run a catch-up cycle. Regular jobs can use configured paid news/Claude services during actual owner-started operation.

The current settings remain exactly unchanged. Dry run is true, live-release approval is false and discovery is disabled. Legacy sentiment selection remains enabled on Sunday at 20:00 New York time. Decisions remain 09:35, 12:30 and 15:30 on eligible sessions. The monitor is 15 seconds during regular sessions, heartbeat 30 seconds and nightly maintenance 20:00. Held-news maintenance has no holdings to process in the clean book. No optional workflow was silently enabled.

In another terminal, use `python -B scripts/healthcheck.py` from the same environment to inspect process, heartbeat and applicable safety status. Ctrl+C or SIGTERM initiates shutdown cleanup. No trial period or minimum trade count was added.

## I. Git state

Branch remains `Shrish-07-changes`. HEAD remains `e81a48ffc859d4f2df671d2d205626229237f991`. Initial status and file digests were recorded before changes. Existing user work and prior deletions were preserved. No staging, commit, push, branch switch, PR, release or deployment occurred.

The exact final modified/new/deleted paths are listed below. Runtime journals, position state and derived caches are git-ignored, so their authorized reset is separately recorded above.

```text
## Shrish-07-changes...origin/Shrish-07-changes
 M CONTRIBUTING.md
 M README.md
 M SECURITY.md
 M core/attribution.py
 M core/backend.py
 M core/data/__init__.py
 M core/data/ingest.py
 M core/data/metrics.py
 M core/data/news.py
 M core/data/sentiment.py
 M core/data_ingest.py
 M core/decision/__init__.py
 M core/decision/engine.py
 M core/decision_engine.py
 M core/discovery/catalysts.py
 M core/discovery/divergence.py
 M core/discovery/evidence.py
 M core/discovery/features.py
 M core/discovery/gates.py
 M core/discovery/news_features.py
 M core/discovery/pipeline.py
 M core/discovery/shortlist.py
 M core/discovery/universe.py
 M core/evaluation.py
 M core/execution/engine.py
 M core/exits/exit_evidence.py
 M core/exits/exit_state.py
 M core/exits/pead_ingestion.py
 M core/exits/trail_manager.py
 M core/exits/triggers.py
 M core/metrics.py
 M core/news_fetch.py
 M core/pnl_tracker.py
 M core/position/__init__.py
 M core/position/manager.py
 M core/position/pnl_tracker.py
 M core/position_manager.py
 M core/sentiment_model.py
 M core/simulation.py
 M core/ticker_selector.py
 M core/weekly/__init__.py
 M core/weekly/news.py
 M core/weekly/selector.py
 M core/weekly/ticker_selector.py
 M core/weekly_news.py
 M core/weekly_selector.py
 D docs/BASELINE.json
 M docs/END_TO_END.md
 M docs/PRODUCTION_READINESS_AUDIT.md
 D docs/history/README_pre_swing_upgrade.md
 M docs/verification/README.md
 D docs/verification/pipeline_verified_2026-11-10.png
 D docs/verification/preflight_2026-11-10.txt
 M main.py
 M models/__init__.py
 M models/decisions.py
 M models/news.py
 M models/orders.py
 M models/portfolio.py
 M requirements-ml.txt
 M robinhood_mcp/response_recorder.py
 M robinhood_mcp/robinhood_client.py
 M scripts/evaluate_discovery.py
 M scripts/healthcheck.py
 M scripts/ingest_pead.py
 M scripts/preflight.py
 M scripts/probe_broker.py
 M scripts/reconcile_costs.py
 D scripts/record_baseline.py
 M scripts/refresh_universe.py
 M scripts/replay_archived.py
 M scripts/smoke_cycle.py
 M scripts/verify_weekly_layer.py
 M tests/conftest.py
 M tests/test_add.py
 M tests/test_attribution.py
 M tests/test_backend_safety.py
 M tests/test_config_loader.py
 M tests/test_cost_tracker.py
 M tests/test_cost_v2.py
 M tests/test_discovery.py
 M tests/test_evaluation_deployment.py
 M tests/test_event_ingestion.py
 M tests/test_execution_guards.py
 M tests/test_exits.py
 M tests/test_mcp_state_contract.py
 M tests/test_optional_failures.py
 M tests/test_p0_safety.py
 M tests/test_parsing.py
 M tests/test_position_logic.py
 M tests/test_production_integrity.py
 M tests/test_release_guards.py
 M tests/test_rotation.py
 M tests/test_sentiment.py
 M tests/test_sentiment_unit.py
 M tests/test_strategic_exits.py
 M tests/test_ticker_selector.py
 M tests/test_weekly_e2e.py
 M utils/anthropic_client.py
 M utils/config.py
 M utils/cost_tracker.py
 M utils/earnings_calendar.py
 M utils/freshness.py
 M utils/health.py
 M utils/instance_lock.py
 M utils/journal_manager.py
 M utils/locks.py
 M utils/logger.py
 M utils/market_calendar.py
 M utils/persistence.py
 M utils/rate_limit.py
 M utils/strategy_config.py
?? docs/AUTHENTICATION.md
?? docs/DEPLOYMENT.md
?? docs/verification/account_state_2026-11-12.json
?? docs/verification/checks_2026-11-12.json
?? docs/verification/checks_clean_reset_2026-11-12.json
?? docs/verification/clean_state_reset_2026-11-12.json
?? docs/verification/journal_cleanup_2026-11-12.json
?? docs/verification/local_models_reset_2026-11-12.json
?? docs/verification/preflight_2026-11-12.json
?? docs/verification/preflight_clean_reset_2026-11-12.json
?? docs/verification/provider_accounting_reset_2026-11-12.txt
?? docs/verification/pytest_2026-11-12.txt
?? docs/verification/pytest_clean_reset_2026-11-12.txt
?? docs/verification/pytest_clean_reset_complete_2026-11-12.txt
?? docs/verification/pytest_clean_reset_final_2026-11-12.txt
?? docs/verification/pytest_clean_reset_verified_2026-11-12.txt
?? docs/verification/reset_local_2026-11-12.json
?? docs/verification/run_offline_audit_2026-11-12.py
?? scripts/reset_local_context.py
?? tests/test_clean_reset.py
?? tests/test_final_readiness.py
?? tests/test_provider_accounting.py
?? utils/state_reset.py
```

## J. Final recommendation

Start with the documented preflight and normal local command when ready. The software now begins from zero local positions and zero old prompt context, with the verified account binding and existing safety gates intact. Keep the current live-trading settings unchanged while inspecting actual process health. Compare provider billing after real usage is generated.

<details>
<summary>Historical audit before the broader reset authorization</summary>

# Production readiness audit

Updated November 12, 2026. The current review below supersedes the conclusions in the retained November 11 appendix. Original dated test evidence is preserved.

## A. Executive verdict

**NOT READY for live operation.** Four strictly read-only requests successfully authenticated and established zero returned cash, account value, holdings and active orders. The offline suite passed 169 tests. This closes the previous inability to connect.

The current legacy state still loads four historical rows as open before reconciliation, has no account binding, and requires owner-approved remediation before live startup. Matching the current eligible account against the owner's earlier manual confirmation remains unavailable because no expected identity was retained. Container startup, permissions, restart/outage behavior and continuous monitoring remain unverified. The configured sentiment path lacks installed dependencies. Full Ruff fails.

No scheduler, trading cycle, discovery, weekly research, paid provider, model download or broker mutation was invoked. Credential files were not inspected directly. The existing client loaded its authentication through its normal interface, with renewal, registration, browser authorization and credential writes disabled for the check.

## B. Account verification

| Requested field | November 12 result |
| --- | --- |
| Exactly one eligible Agentic account selected | Yes. |
| Same account as previously manually approved | Unavailable. No retained expected identifier or persisted account binding provides a comparison. The prior owner confirmation is acknowledged. |
| Correct intended account fully confirmed | No independent current confirmation. Selection was unique and stable within the request group. |
| Portfolio requested for selected account | Yes. |
| Fingerprint consistency | The SHA-256 computation was traced in `get_portfolio`. The prior owner-supplied check reported consistency. A separate live typed-Portfolio check was not repeated in this request group. |
| Holdings response complete | Yes. |
| Active holdings and open positions | 0. |
| Total holding quantity and market value | 0 shares and $0.00. |
| Cash | $0.00. |
| Total account value | $0.00. |
| Equity value | $0.00. |
| Buying power | $0.00. |
| Unleveraged buying power | $0.00, separately reported by the broker. |
| Pending deposits | $0.00. |
| Options, futures, event contracts, crypto, mutual funds and fixed income values | Each returned $0.00. |
| Withdrawable funds, unsettled cash/funds, margin balance and debit balance | Unavailable. These were not silently treated as zero. |
| Open, pending and unresolved broker orders | 0 each. |
| Historical terminal orders | 27. |
| Order IDs, execution IDs and quantity consistency | Passed the in-memory checks. Filled quantities were nonnegative, within ordered quantities, matched terminal filled quantities when required and matched available executions. |
| Pagination | Complete one-page response, with no continuation. Multi-page cursor behavior is covered by mocked tests and was not exercised against the broker in this request group. |

Exactly one call each was made to `get_accounts`, `get_portfolio`, `get_equity_positions` and `get_equity_orders`. Repeated account selection inside public helpers reused the same in-memory account response. Response recording was disabled so no raw financial records were persisted by the check. No quotes were requested because quotes were unnecessary for the specifically permitted empty-account verification.

[Sanitized account evidence](verification/account_state_2026-11-12.json) contains the required summary only. The available fields support zero observed assets and active orders. Unavailable balances and the missing prior-identity comparison prevent a broader claim that every possible liability or account attribute was independently verified.

### Legacy local state

`PositionManager._load` wraps a legacy list in an in-memory schema-2 envelope. `get_open_records` interprets its four valid positive-quantity rows as open records. They are initially unverified and cannot establish broker readiness. They can affect open-position counts and the set of held symbols before reconciliation. Live BUY, ADD, ROTATE and SELL require applicable broker/order readiness, while the monitor requests quotes only for broker-confirmed rows.

Normal startup calls complete broker/order reconciliation before registering the jobs. A successful complete empty holdings response removes absent local holdings and records discrepancy events. The isolated legacy-empty test demonstrates that behavior with a temporary fixture and no account binding. Failed reconciliation preserves old rows as unverified. The actual local file was never rewritten or bound.

Treat the unbound legacy book as a live-activation blocker. After separate explicit owner authorization, confirm the account identity and perform a controlled migration that preserves the historical rows and events while establishing a verified empty live book. Do not delete the financial history or run startup solely to achieve an implicit binding.

## C. Journal cleanup

The executable store is `journals/`, resolved from the project root by `JournalManager`. Its per-symbol files are JSON arrays of `JournalEntry`. `get_recent`, `summarise_for_prompt` and `get_all_tickers` are the readers. `append` is the writer used by decision cycles and `Backend.sync_fill_journals`. No alternate active journal implementation was found.

| Cleanup result | Count |
| --- | --- |
| Initial files and records | 10 files, 419 records. |
| Research-only records removed | 250. |
| Empty research-only files deleted | 1. |
| Mixed files rewritten to retain protected rows | 9. |
| Protected rows retained | 169. |
| Remaining research-only rows | 0. |
| New backup or journal archive created | None. |

The retained rows carry fill, P&L, order references or execution/outcome indicators. They remain semantically identical to their original records. Some include old narrative and metric context. Removing or rewriting those protected records exceeds this directive's financial-record boundaries, so **the complete fresh-context requirement is PARTIALLY VERIFIED**. It cannot be described as a completely empty journal store. A separately reviewed context-exclusion mechanism or explicit authorization for the protected mixed rows is still needed.

The isolated journal test proves that the normal manager can create, read and summarize a new entry in an empty temporary store. No synthetic entry was placed in the actual store.

Eighteen pre-existing files under `logs/` and `config/` matched their initial content digests after the tests and cleanup. This includes position/account state, available order/execution/financial records and configuration. Existing audit and test evidence was preserved. Credential-writing paths were blocked rather than inspected. [Cleanup evidence](verification/journal_cleanup_2026-11-12.json)

## D. Completed checklist

Statuses describe the stated scope. CLOSED offline checks do not certify live order execution.

| Task | Status | Evidence | Remaining limitation |
| --- | --- | --- | --- |
| Repository mapping and active import paths | CLOSED | Main imports domain packages. Flat `core/*.py` wrappers re-export them. Call sites and persistence paths were searched. | External callers may still use compatibility imports, so they were preserved. |
| Existing authentication usable for reads | CLOSED | Four authenticated broker reads succeeded without renewal. | Renewal/revocation recovery was not attempted. |
| Approved identity and persisted binding | PARTIALLY VERIFIED | Unique eligible account and selected-account arguments. Actual local binding remains absent. | Historical identity comparison and approved migration. |
| Raw balance and holdings reads | PARTIALLY VERIFIED | All returned assets zero and complete empty holdings. | Missing withdrawable, unsettled and liability fields. |
| Order history and fills | PARTIALLY VERIFIED | 27 terminal records, zero active orders, identifier and fill consistency checks. | Multi-page transport exercised only with mocks. |
| Partial, cancelled and rejected orders | CLOSED | Offline broker-contract, P0, position and rotation tests pass. | No actual order was submitted. |
| Ambiguous BUY/SELL and restart | CLOSED | Integrity tests persist unresolved intents and submit once. | Broker acceptance after an actual timeout remains untested. |
| BUY sizing and pending reservations | CLOSED | Guard and cash-reservation code traced and mocked suite passes. Zero account value blocks entry sizing. | Actual execution not tested. |
| ADD exposure/thesis/invalidation | CLOSED | Typed thesis and rejected stale, missing, invalidated, cooldown and count cases. | Actual thesis validity requires real evidence. |
| SELL and ROTATE sequencing | CLOSED | Partial/unresolved source exits cannot release the replacement leg in mocked tests. | Real two-leg behavior remains unverified. |
| Hard-stop independence | CLOSED | Hung-model mock leaves stop execution independent. | Broker/host/feed availability remains necessary. |
| Stop ratchets and stale snapshots | CLOSED | New regression preserves stop maxima after stale review and rejects pre-fill balances. | Explicit verified split adjustment remains separate. |
| E1–E4 | CLOSED | Initial bands, profit lock, ATR chandelier and monotonic protection tests. | Fill/slippage during a real gap not tested. |
| E5–E12 | PARTIALLY VERIFIED | Trend, climax, sentiment, stagnation, signed gap, PEAD, opportunity and time paths traced with offline tests. | Real event data and strategy validity unverified. |
| Graduation, specialized drop and new-high review | CLOSED | Persistence, failure, cooldown and rearm tests pass. | Model decisions were mocked. |
| L0–L6 discovery | PARTIALLY VERIFIED | Provenance, gates, features, news/sentiment, gap scoring, archetypes and shortlist tests pass. | Disabled discovery was not run against the universe. |
| Holding inclusion and quotas | CLOSED | Integrated selector excludes holdings from new-name slots, then ingest includes held names independently. | Standalone legacy helper keeps its older total-universe contract. |
| Missing/stale data handling | CLOSED | Freshness, malformed holdings, unavailable-model and failure-retention tests pass. | Actual feed outage not simulated outside fixtures. |
| Sentiment model usability | BLOCKED | Primary cached weight/config files exist, fallback cache absent. Torch/Transformers/Accelerate/Safetensors unavailable in the inspected environment. | Installation and real local inference require separate authorization. |
| Market/news dependencies | CLOSED | HTTPX, MCP, NumPy, Pandas, calendar and scheduler modules installed. News uses HTTPX directly and needs no Finnhub SDK. | No provider data freshness or API key was checked. |
| Cost ledger behavior | CLOSED | Formula, cache categories, request deduplication, unpriced usage and scope comparison tests pass. | Provider reconciliation remains separate. |
| Provider usage reconciliation | BLOCKED | No designated dashboard export found. | Needs UTC [start,end), request IDs, provider USD total and rounding tolerance. |
| Market calendar and overlap | CLOSED | Holiday/half-day, decision overlap, symbol lock and instance-lock tests pass. | No durable cycle deduplication across independent state copies. |
| Continuous single-instance operation | PARTIALLY VERIFIED | Shared-path OS lock, durable intents and startup paths traced. | Multi-host or copied-volume clients remain unsafe. |
| Health | FAILED | Current health check exits 1, with no health file. | No backend was started under the task restrictions. |
| Full active-source Ruff | FAILED | 390 findings after the focused corrections. | Broad lint cleanup remains. |
| Active Pyflakes, compile and installed dependency consistency | CLOSED | Each command exited 0. | Optional dependencies are not part of pip-check completeness. |
| Container deployment | BLOCKED | YAML parsed and mount/user/startup contract inspected. | Docker absent, build/permissions/restart not exercised. |
| Journal fresh context | PARTIALLY VERIFIED | 250 removed, zero research-only rows remain. | 169 protected mixed rows retained. |

The configured legacy sentiment selection remains enabled while the newer discovery flag remains disabled. Disabling discovery does not disable nightly held news or the legacy weekly selection path. No research work ran during this audit.

## E. Corrections made

| Category | File | Correction |
| --- | --- | --- |
| Code | `core/position/manager.py` · `update_record` | Preserve persisted initial/trailing/effective stops and peak maxima. Reject stale quantity/cost or changed lifecycle before overwriting a position. Missing positions are not recreated by a stale record. |
| Code | `utils/logger.py` · `configure_logging` | Use bounded diagnostic file rotation. Send diagnostic trade JSONL to `trade_events.jsonl` so it cannot append non-array data to the execution-result `trades.json` file. |
| Tests | `tests/test_final_readiness.py` | Five regressions for stale stops, stale partial-fill balances, isolated legacy-empty reconciliation, new journal creation and diagnostic-ledger separation. |
| Local journal data | `journals/` | Remove only research-only rows and preserve protected financial/outcome rows without a duplicate archive. |
| Documentation | README, authentication guide, walkthrough, this audit and verification index | Update actual authentication, empty-account findings, partial journal cleanup, legacy-state blocker and current test evidence. |
| Documentation | `docs/DEPLOYMENT.md` | Add concrete single-host preparation, persistence/permissions, health limits and a sourced two-provider comparison. |
| Evidence | `docs/verification/` | Add sanitized account, cleanup, preflight and check summaries, the guarded offline runner and test output. |

No trading configuration changed. Pre-existing uncommitted documentation changes and deletions from the earlier cleanup were preserved.

## F. Test results

Commands use `./.venv-upgrade/Scripts/python.exe` as `python`. All checks occurred on November 12 unless labeled historical.

| Exact command or procedure | Exit | Passed / failed / excluded | Mode and warnings |
| --- | --- | --- | --- |
| `python -B docs/verification/run_offline_audit_2026-11-12.py` | 0 | 169 / 0 / 2 deselected | Full offline suite. Harness denies external socket connections, secret-file reads and writes under real logs/journals/config. Costs and fixtures use scratch storage. One AnyIO CallSpec2 deprecation warning. |
| Five new regression tests | 0 within suite | 5 / 0 / 0 | Synthetic fixtures only. |
| Production-integrity, guard, position, exit, discovery, cost and broker-contract tests | 0 within suite | Included in the 169 total | Mocked and local data. No subset counts were invented. |
| Synthetic replay test in `tests/test_exits.py` | 0 within suite | 1 / 0 / 0 | Partial fills, gap, stop monotonicity and restart. Archived journal replay was not rerun after cleanup. |
| `python -m ruff check main.py core robinhood_mcp utils models scripts tests --no-cache --output-format concise` | 1 | 390 findings | Full active-source lint failed. |
| `python -m ruff check main.py core robinhood_mcp utils models scripts tests --no-cache --select F` | 0 | No findings | Pyflakes rules through Ruff. |
| `python -X pycache_prefix=.test-tmp/audit-pycache -m compileall -q main.py core robinhood_mcp utils models scripts tests` | 0 | Compilation passed | Redirected bytecode cache. |
| `python -m pip check` | 0 | No broken installed requirements | Does not certify absent optional modules or model weights. |
| Isolated `scripts.preflight.preflight` invocation | 0 | Valid config, no state-load error, live-ready false | Disabled dotenv and secret environment reads. Original position state read without modification. Report output redirected to a temporary root. Invocation is retained in the verification notes. |
| `python -B scripts/healthcheck.py` | 1 | Unhealthy | No health file. |
| PyYAML `safe_load(Path('compose.yaml').read_text())` | 0 | Static parse passed | Did not expand or read dotenv. |
| Docker Compose config and image build | Unavailable | Blocked | Docker executable absent. |
| Four broker reads through existing client | 0 | Successful summary | Actual authenticated read-only requests, no orders/previews/cancellations, no model or quote requests. |
| Heavy model tests, actual order execution, real provider inference, cloud APIs and scheduler runs | Not run | Two heavy tests deselected | Prohibited external usage or execution. |
| `git diff --check` | 0 | Whitespace passed | Windows line-ending notices. |

Primary evidence is linked in [the verification index](verification/README.md). The Sonnet 5.5 base/cache rates match the official public pricing table inspected in this review. This verifies the rate assumptions, while actual billed usage still needs the owner's export. [Official model pricing](https://platform.claude.com/docs/en/about-claude/pricing)

## G. Remaining blockers

| Severity | Issue and required closure |
| --- | --- |
| Critical | Approve the intended account identity and controlled legacy-state migration before live startup. The real legacy book remains unbound and restores four apparent holdings until reconciliation. |
| Critical | Validate the actual deployment, durable mount ownership, credential persistence, single-process exclusion, host restart, shutdown, external watchdog and outage response. No running healthy backend was demonstrated. |
| High | Live order transport, real timeout recovery and real fill/slippage behavior were not tested under these restrictions. Offline mocks cannot supply that evidence. |
| High | Some financial fields and comparison to the prior manually approved identity remain unavailable. Do not infer those values from zero holdings. |
| High | The configured sentiment path cannot run in the inspected environment without its optional dependencies. The fallback weights are not cached. No model was loaded. |
| High | The protected journal rows still contain old prompt context. A full reset is blocked by the instruction to preserve their execution/P&L information. |
| Medium | Full lint fails with 390 findings. |
| Medium | Provider usage reconciliation and source-complete PEAD/walk-forward efficacy evidence remain unavailable. No evidence of strategy profitability is claimed. |
| Medium | The health script accepts parseable state outside market hours without checking heartbeat age. Docker restart policy alone does not recover an unhealthy running process. |
| Medium | Typed Portfolio currently drops raw cash and defaults cash percentage. Buying power is parsed separately. Raw balance presence must remain explicit in account verification. |
| Medium | The existing broker probe's freshness guard cannot distinguish a stale access token from an unusable refresh session. Its report is insufficient to diagnose login status. |

## H. Deployment recommendation

Use one DigitalOcean Basic Droplet in NYC3, with 4 vCPUs, 8 GiB RAM and 160 GiB SSD for the configured sentiment workload. Budget roughly $62–75/month including daily backups and modest monitoring, before tax and provider API usage. These are capacity estimates rather than measured model requirements. The published base is $48 and daily backups add 30%. [Official pricing](https://www.digitalocean.com/pricing/droplets)

Second choice is AWS Lightsail in Northern Virginia, using its 8-GB, 2-vCPU, 160-GB Linux bundle at $44/month before backup and monitoring costs. Allow approximately $50–65 depending on snapshot retention. Provider choice does not materially change the trading signal. US East is a reasonable operating region, with persistence and operator response taking priority over fine latency differences. [Official bundles](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html)

The [deployment guide](DEPLOYMENT.md) covers exact preparation commands, existing mount layout, OAuth transfer under separate authorization, UID ownership, one-instance operation, restart/health distinctions, backups and alerting. No deployment, cloud login or resource creation occurred.

## I. Exact next steps and end-to-end review

### Owner actions in order

1. Confirm the current eligible account against the intended account using a private identity reference. Preserve the absence of a binding until explicit approval.
2. Separately authorize a controlled migration of the legacy financial state after identity and empty holdings are confirmed. Preserve its historical account and execution information.
3. Decide how protected journal rows should be excluded from future context while preserving their financial records. The current deletion authorization does not safely permit deleting them wholesale.
4. Supply the exact-scope provider usage export if cost reconciliation is required. The tracker accepts request IDs, UTC start/end, USD total and tolerance.
5. Authorize the required dependency/weight installation and deployment preparation on the chosen host. Do not start the scheduler merely to test authentication.
6. Review release evidence and explicitly control any later live activation.

### Repository and operating work remaining

Complete the lint remediation, model inference checks, raw-balance/identity validation and deployment tests described above. Establish an external watchdog and tested credential-renewal procedure under separate authorization. No fixed trial period, trade-count requirement or automatic exposure ramp was added.

### Startup through closure

| Step | Executable path and ordering |
| --- | --- |
| 1. Process startup | The CLI takes `InstanceLock(logs/agent.lock)`. |
| 2. Config | `TradingAgent.__init__` loads JSON settings and normal dotenv configuration. Broker token-path resolution occurs at module import, so process environment overrides must precede import. |
| 3. Components | Create clients, ingestion, metrics, news, journal, calendar and decision engines. |
| 4. Position restoration | The position manager loads before the backend and before live reconciliation. Legacy rows appear open but unverified. |
| 5. Backend initialization | Execution and backend share that manager and its locks. No alternate flat compatibility implementation runs. |
| 6. Authentication/account | Startup reconciliation invokes the broker's token flow and selects the unique eligible account. |
| 7. Reconciliation | Recover owned orders and require complete matching-account holdings. Bracket holdings with order snapshots where pending intents exist. Successful empty holdings clear old rows. |
| 8. Scheduler | Register heartbeat, monitor, nightly work, optional discovery, reports, legacy weekly selection and catch-up jobs. |
| 9. Candidate universe | Disabled discovery remains disabled. Legacy selection is separately enabled by current configuration. |
| 10. Features/news/sentiment | Public data and local scoring feed facts. Missing inputs fail or preserve prior valid selection. Optional model work runs in threads. |
| 11. Shortlist | New-name quota excludes holdings in the integrated path. Ingestion adds current holdings separately. |
| 12. Model decision | Normal cycles call structured Claude evaluation. This audit called no model. |
| 13. BUY | Exposure, liquidity, quote, capital and release gates precede durable intent and a single submission attempt. Zero account value blocks sizing. |
| 14. ADD | Explicit incremental thesis, invalidation, evidence, cooldown/count and exposure checks precede a new lot. |
| 15. Monitor | Every configured 15 seconds, observe eligible holdings, persist peaks/stops and submit valid hard stops independently of Claude. |
| 16. Nightly | Refresh completed-bar ATR and held evidence, then evaluate measured advisories. |
| 17. Exit review | Group due trend, climax, sentiment, stagnation, gap, PEAD, opportunity, time and graduation evidence. Cooldown/rearm prevent constant model calls. |
| 18. SELL | One durable intent tracks status and partial fills. Unresolved outcomes remain reserved. |
| 19. ROTATE | Full source exit and fresh matching account/cash/target quote are required before the replacement BUY. |
| 20. Attribution | Confirmed fills derive realized P&L and observed excursions, with unavailable history/costs marked explicitly. |
| 21. Journal | Cycle decisions and confirmed fills append through the journal manager. Financial rows are distinct from erasable research-only context. |
| 22. Persistence | Atomic state replacement and backups support restart. Diagnostic logs now use separate rotating files. |
| 23. Shutdown/restart | Cancel scheduler/review work and reacquire the instance lock on restart. Recover unresolved intents and reconcile before new dependent actions. |

The active monitor is `Backend.monitor`. The older `TradingAgent._evaluate_threshold_signal` helper is tested but is not registered as the scheduler's monitor. The calendar module's old docstring mentions a naive fallback, but executable `is_market_open_at` returns false if the calendar is unavailable. The full [walkthrough](END_TO_END.md) contains the strategy rules and diagrams.

A verified empty broker response allows the program to reconcile to zero holdings. The unmodified legacy file means it does not begin with a clean verified local book before that reconciliation. The offline fixture demonstrates the distinction.

## J. Git state

Current branch remains `Shrish-07-changes`, with HEAD `e81a48ffc859d4f2df671d2d205626229237f991`. All work remains local and unstaged. No commit, push, PR, release, branch switch or deployment occurred during this audit.

The final short status is recorded below. Deletions of the baseline files, historical README, old summary image and duplicate preflight export were already present when this task began. They were preserved as existing user work. No existing audit or test evidence was deleted during this task.

```text
## Shrish-07-changes...origin/Shrish-07-changes
 M CONTRIBUTING.md
 M README.md
 M SECURITY.md
 M core/position/manager.py
 D docs/BASELINE.json
 M docs/END_TO_END.md
 M docs/PRODUCTION_READINESS_AUDIT.md
 D docs/history/README_pre_swing_upgrade.md
 M docs/verification/README.md
 D docs/verification/pipeline_verified_2026-11-10.png
 D docs/verification/preflight_2026-11-10.txt
 D scripts/record_baseline.py
 M utils/logger.py
?? docs/AUTHENTICATION.md
?? docs/DEPLOYMENT.md
?? docs/verification/account_state_2026-11-12.json
?? docs/verification/checks_2026-11-12.json
?? docs/verification/journal_cleanup_2026-11-12.json
?? docs/verification/preflight_2026-11-12.json
?? docs/verification/pytest_2026-11-12.txt
?? docs/verification/run_offline_audit_2026-11-12.py
?? tests/test_final_readiness.py
```

## K. Final conclusion

The current authenticated account reads and offline execution/state tests pass within their stated scope. The full journal reset is incomplete because protected financial rows remain. The intended historical identity, controlled legacy migration, required model runtime and actual single-host deployment checks still need closure. Those conditions prevent safe live activation.

<details>
<summary>Retained historical November 11 audit</summary>

# Production readiness audit

Updated November 11, 2026. Current authentication findings and read-only local checks are distinguished below from the retained November 10 test evidence. The full test suite was not rerun for this documentation cleanup.

## 1. Final verdict

**NOT READY for live trading.** Local offline behavior passed the default suite and archived replay. Actual broker identity, payloads, fills, runtime health, deployment and provider cost reconciliation remain unverified. The configuration reports dry run enabled, live release approval disabled and optional discovery disabled.

## 2. Remaining issues

| Severity | Finding |
| --- | --- |
| Critical | The saved access token received HTTP 401 during read-only MCP initialization. The probe skips refresh when the access token is stale. Refresh credentials exist, but their current acceptance was not tested because refresh can rotate and overwrite them. The intended account, balances, holdings, quotes, order history and fills remain unverified. |
| Critical | There is no running healthy backend in this audit environment. Docker and Compose are unavailable here. Runtime survival, mount ownership, credential refresh, restart and outage recovery remain unproven on a deployment host. Software-managed stops require a running process, usable quotes and broker connectivity. |
| High | Order reads bracketing holdings detect observable changes during reconciliation. Consistency under eventual propagation remains unverified. The current local state is a legacy list with four historical records and no account binding. Confirm the intended account before its first binding. An intent without a broker ID needs operator investigation. |
| High | Real Anthropic usage and dashboard totals were not compared. Two model-loading tests are excluded from the default suite. November 11 module discovery reports sentiment dependencies missing in the inspected virtual environment. |
| High | A source-complete point-in-time PEAD dataset and unbiased walk-forward evidence for strategy efficacy were not verified. The optional PEAD path remains evidence-dependent. |
| Medium | The November 11 full Ruff check reports 392 findings. Repository-wide Pyflakes passes. The decision-cycle lock is process-local and provides no durable schedule deduplication. |

The source review traced discovery, entry, monitoring, exit and attribution through the normal application entry points. It found deterministic checks for exposure and loss handling, monotonic stops, explicit gates for adding capital, and full source-exit confirmation before rotation. Those safeguards and the replay do not establish the profitability of the signal or a fill price during a gap.

## 3. Existing safeguards reviewed

| File and function | Change |
| --- | --- |
| `core/backend.py` · `Backend.reconcile` | Clear order readiness before each refresh, compare order state before and after holdings when unresolved orders exist, and apply holdings only after successful same-account order reconciliation. |
| `core/position/manager.py` · `account_matches`, `reconcile_with_mcp` | Persist and enforce the account binding. Preserve existing holdings on a mismatched snapshot. |
| `robinhood_mcp/robinhood_client.py` · `get_portfolio`, `_token_is_fresh` | Attach an account fingerprint and reject invalid cache expiration values. |
| `core/execution/engine.py` · `reconcile_orders`, `_available_cash`, `_submit`, `_rotate` | Consume the checked order snapshot, account for pending and recently filled buys, preserve explicit unresolved outcomes, and validate source exit and fresh quotes before releasing the replacement leg. |
| `main.py` · `TradingAgent.run_cycle`, `_run_cycle` | Prevent decision-cycle overlap and remove holdings replacement from the independent ingest path. |
| `scripts/probe_broker.py` · `main` | Use temporary state, restrict the probe to read operations and stop before interactive authentication. |
| `tests/test_production_integrity.py` | Exercise moving snapshots, persisted account binding, ambiguous BUY and SELL and overlapping cycles. |

## 4. Verification results

November 11 read-only checks used the existing Python 3.13.6 virtual environment. Retained November 10 suite and replay evidence is linked in [the verification index](verification/README.md). Historical results describe those runs and do not establish current live readiness.

| Command | Observed result |
| --- | --- |
| `python -m pytest -q` | Retained November 10 log reports 164 passed and two deselected. No suite rerun during this cleanup. |
| `python -m ruff check . --no-cache --output-format concise` | November 11, 392 findings. |
| `python -m ruff check . --no-cache --select F` | November 11, passed. The obsolete archived schema dump has already been removed. |
| `python scripts/verify_weekly_layer.py` | Retained November 10 log reports 35 passed. |
| `python -m compileall -q main.py core robinhood_mcp utils scripts tests` | Previously passed. Not rerun during this cleanup. |
| `python -m pip check` | November 11, no broken requirements. Current module discovery reports optional sentiment dependencies unavailable in this virtual environment. |
| `python scripts/preflight.py` | Retained November 10 JSON reports valid configuration and live readiness false. It reported sentiment dependencies available in that runtime. The November 11 environment differs. This script was not rerun because it writes its report to runtime logs. |
| `python -B scripts/healthcheck.py` | November 11, exit 1 and unhealthy. The health file is absent. This does not establish whether credentials exist. |
| `python scripts/replay_archived.py --ticker MU --output-dir .test-tmp/replay-run` | Retained November 10 evidence reports 90 observations and one simulated closed position. |
| `python scripts/probe_broker.py` | Previously reported `AUTHENTICATION_REQUIRED`. Source inspection now establishes its freshness guard bypasses silent refresh. It was not rerun because it writes a report. |
| Read-only MCP initialization with the saved access token | November 11, HTTP 401. No refresh, registration or browser flow attempted. |
| `docker build .` and `docker compose config --quiet` | Docker executable unavailable. |
| `git diff --check` | Passed with Windows line-ending notices. |

| Failure scenario | Local evidence and limit |
| --- | --- |
| 1. Broker outage | Mocked broker outage and stale quote block submission. Real outage recovery unverified. |
| 2. Mid-cycle OAuth expiration | Cache validity and refresh logic reviewed. Authenticated mid-cycle recovery unverified. |
| 3. Account selection failure | Mocked zero/multiple eligible-account failures and persisted account mismatch. |
| 4. Incomplete market data | Mocked malformed/incomplete broker data and stale quotes exercise rejection paths. |
| 5. Claude timeout | Mocked hung review leaves hard-stop path operational. Real provider timing unverified. |
| 6. Malformed Claude JSON | Structured parse and fail-closed path inspected. No live provider response used. |
| 7. Ambiguous BUY | Mocked timeout leaves one durable unresolved intent and one transport attempt. |
| 8. Ambiguous SELL | Same test for SELL preserves holdings while unresolved. |
| 9. Partial fill | Mocked and state tests exercise cumulative quantities and duplicate updates. |
| 10. Rejection | Mocked terminal and execution-guard tests. Real rejection payload unverified. |
| 11. Cancellation | State tests preserve the remaining position after a terminal partial sell. Real cancellation payload unverified. |
| 12. Overnight pending | Durable reservation and unresolved status inspected. An actual overnight session was unverified. |
| 13. Restart during pending | Test rebuilds the manager and sees unresolved intent. |
| 14. Restart after partial fill | Test reloads the partial state before final fill. |
| 15. Restart after stop ratchet | Exit tests and replay cover stop persistence and recovery. |
| 16. Broker and local mismatch | Mocked reconciliation and account-change and moving-order tests. |
| 17. Gap through stop | Archived replay exercises a gap with simulated execution. |
| 18. Overlapping jobs | Tests exercise concurrent duplicate submissions and overlapping decision cycles. |
| 19. Repeated specialized review | Exit tests and source inspection cover cooldown, hysteresis and persistence. |
| 20. Sentiment model failure | Mocked loaders and nightly maintenance test cover failure handling. Real model load unverified. |

## 5. Broker verification

The OAuth cache exists and is readable. Its access token expired on August 24, 2026, and returned HTTP 401. Refresh credentials and the OAuth client identifier are present. The logs record successful renewal through August 13, with no later refresh rejection in the inspected application log. Whether silent renewal is still usable remains unverified.

Local startup can attempt silent renewal. The production probe instead skips broker calls if the access token is stale. Its result does not establish that browser login is required. The local holdings file has no persisted account binding. Compose uses a separate OAuth volume and does not automatically inherit the local login. See [the authentication guide](AUTHENTICATION.md) for the full trace, paths, supported renewal procedure and read-only account checks.

Account identity, balances, quotes, positions, pending and terminal orders, and fill schema remain unverified against current authenticated responses. The archived run is simulation. No actual order placement or execution was performed.

## 6. Live activation requirements

1. Permit silent renewal on the intended host and use browser approval only if needed. Confirm the intended account before the legacy ledger acquires its first binding. Verify balances, complete holdings, live bid/timestamps, order history and fill identifiers against the broker. Resolve unknown intents before activation.
2. Build the container and verify Compose, persistent directory ownership, restart behavior, heartbeat and broker or host outage recovery in dry run. Verify credentials can recover on the target host and confirm the manual stop procedure and operational coverage of software-managed stops.
3. Validate required provider feeds and model weights for the strategy configuration to be enabled. Compare the local usage ledger with an exact matching Anthropic billing window. Verify point-in-time earnings and PEAD inputs before relying on those advisories.
4. Review the remaining reconciliation limits and obtain owner-controlled live release approval. This audit does not certify real order placement or fill handling.

## 7. End-to-end technical documentation

See [End-to-end walkthrough](END_TO_END.md) for startup, inputs, L0–L6, Claude, execution, E1–E12, ADD and ROTATE, storage, costs, schedule, restart and an example.

## 8. Architecture diagrams

Section M of the walkthrough contains the system graph and separate BUY, ADD, SELL, ROTATE and hard-stop sequences.

## 9. Repository state at the latest review

The November 11 review started at HEAD `e81a48f` on `Shrish-07-changes`. The earlier `9743074` / `upgrade/swing-safety` snapshot has been superseded. This documentation cleanup changes the audit, guides and evidence index and removes unused baseline material and duplicate evidence. It does not change authentication data, account state or execution settings. No staging, commit, push or deployment was performed during this review. Use `git status --short --branch` for the current working tree.

## 10. Final recommendation

Keep live execution disabled until broker and deployment evidence closes the critical issues. Retain this audit as the current readiness record, with original dated test evidence in the verification directory.


</details>


</details>
