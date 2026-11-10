# Production readiness audit

## 1. Final verdict

**NOT READY for live trading.** Local offline behavior passed the default suite and archived replay. Actual broker identity, payloads, fills, runtime health, deployment and provider cost reconciliation remain unverified. The configuration reports dry run enabled, live release approval disabled and optional discovery disabled.

## 2. Remaining issues

| Severity | Finding |
| --- | --- |
| Critical | The read-only probe stopped at authentication. The selected account, balances, holdings, quotes, open/recent orders and execution payloads were not verified against Robinhood. |
| Critical | There is no running healthy backend in this audit environment. Docker and Compose are unavailable here. Runtime survival, mount ownership, credential refresh, restart and outage recovery remain unproven on a deployment host. Software-managed stops require a running process, usable quotes and broker connectivity. |
| High | Order reads bracketing holdings detect observable changes during reconciliation. The broker reads are separate and their consistency under eventual propagation has not been established. An intent without a broker ID needs operator investigation. A legacy ledger needs account confirmation before it binds for the first time. |
| High | Real Anthropic usage and dashboard totals were not compared. Two model-loading tests are excluded from the default suite, and preflight reports that sentiment dependencies are missing. |
| High | A source-complete point-in-time PEAD dataset and unbiased walk-forward evidence for strategy efficacy were not verified. The optional PEAD path remains evidence-dependent. |
| Medium | Full Ruff reports 423 findings. The active-code Pyflakes check passes. The decision-cycle lock is process-local and provides no durable schedule deduplication. |

The source review traced discovery, entry, monitoring, exit and attribution through the normal application entry points. It found deterministic checks for exposure and loss handling, monotonic stops, explicit gates for adding capital, and full source-exit confirmation before rotation. Those safeguards and the replay do not establish the profitability of the signal or a fill price during a gap.

## 3. Corrections made

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

Python commands below used the existing Python 3.13.6 virtual environment.

| Command | Observed result |
| --- | --- |
| `python -m pytest -xq -o faulthandler_timeout=12` | 164 passed, two heavy cases deselected, one AnyIO compatibility warning. The restricted run failed at temporary-directory fixture creation. The successful run used a normal Windows temporary directory. |
| `python -m ruff check . --output-format concise` | 423 findings. |
| `python -m ruff check . --select F` | Four findings in the archived MCP schema dump. |
| `python -m ruff check main.py core robinhood_mcp utils models scripts tests --select F` | Passed. |
| `python -m compileall -q main.py core robinhood_mcp utils scripts tests` | Passed. |
| `python -m pip check` | No broken requirements. Optional model dependencies are checked separately. |
| `python scripts/preflight.py` | Exit 0, configuration and state-load checks passed, live readiness false. |
| `python scripts/healthcheck.py` | Exit 1, unhealthy without a running backend heartbeat. |
| `python scripts/replay_archived.py` | Exit 0, 90 observations, one simulated closed position. |
| `python scripts/probe_broker.py` | Reported `AUTHENTICATION_REQUIRED`. |
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

The probe required authentication before returning usable account data. Account identity, buying power, quotes, positions, pending and terminal orders, and fill schema have only source or mocked evidence here. The archived run is simulation. No actual order placement or execution was performed.

## 6. Live activation requirements

1. Authenticate on the intended host and verify the approved account, legacy ledger binding, balances, complete holdings, live bid/timestamps, order history and fill identifiers against the broker. Resolve unknown intents before activation.
2. Build the container and verify Compose, persistent directory ownership, restart behavior, heartbeat and broker or host outage recovery in dry run. Verify credentials can recover on the target host and confirm the manual stop procedure and operational coverage of software-managed stops.
3. Validate required provider feeds and model weights for the strategy configuration to be enabled. Compare the local usage ledger with an exact matching Anthropic billing window. Verify point-in-time earnings and PEAD inputs before relying on those advisories.
4. Review the remaining reconciliation limits and obtain owner-controlled live release approval. This audit does not certify real order placement or fill handling.

## 7. End-to-end technical documentation

See [End-to-end walkthrough](END_TO_END.md) for startup, inputs, L0–L6, Claude, execution, E1–E12, ADD and ROTATE, storage, costs, schedule, restart and an example.

## 8. Architecture diagrams

Section M of the walkthrough contains the system graph and separate BUY, ADD, SELL, ROTATE and hard-stop sequences.

## 9. Final repository state

HEAD is `9743074` on `upgrade/swing-safety`. The index is empty of staged changes. No commit or push was performed for this audit. The baseline folder is absent from the workspace root and the nested repository. The following is the exact short status at report generation.

```text
## upgrade/swing-safety
 M .gitignore
 M README.md
 M config/settings.json
 M core/data/ingest.py
 M core/data/metrics.py
 M core/data/news.py
 M core/data/sentiment.py
 M core/decision/engine.py
 M core/discovery/__init__.py
 M core/execution/__init__.py
 M core/execution/engine.py
 M core/exits/__init__.py
 M core/position/manager.py
 M core/weekly/news.py
 M core/weekly/selector.py
 M main.py
 M models/decisions.py
 M models/market_data.py
 M models/portfolio.py
 M pytest.ini
 M requirements-dev.txt
 M robinhood_mcp/response_recorder.py
 M robinhood_mcp/robinhood_client.py
 M scripts/smoke_cycle.py
 M scripts/verify_weekly_layer.py
 M tests/test_config_loader.py
 M tests/test_cost_tracker.py
 M tests/test_execution_guards.py
 M tests/test_position_logic.py
 M tests/test_rotation.py
 M tests/test_sentiment.py
 M tests/test_ticker_selector.py
 M tests/test_weekly_e2e.py
 M utils/anthropic_client.py
 M utils/config.py
 M utils/cost_tracker.py
 M utils/journal_manager.py
 M utils/market_calendar.py
 M utils/rate_limit.py
?? .dockerignore
?? Dockerfile
?? archive/verify_weekly_layer_legacy.py
?? compose.yaml
?? core/attribution.py
?? core/backend.py
?? core/data_ingest.py
?? core/decision_engine.py
?? core/discovery/catalysts.py
?? core/discovery/divergence.py
?? core/discovery/evidence.py
?? core/discovery/features.py
?? core/discovery/gates.py
?? core/discovery/news_features.py
?? core/discovery/pipeline.py
?? core/discovery/shortlist.py
?? core/discovery/universe.py
?? core/evaluation.py
?? core/exits/exit_evidence.py
?? core/exits/exit_state.py
?? core/exits/pead_ingestion.py
?? core/exits/trail_manager.py
?? core/exits/triggers.py
?? core/metrics.py
?? core/news_fetch.py
?? core/pnl_tracker.py
?? core/position_manager.py
?? core/sentiment_model.py
?? core/simulation.py
?? core/ticker_selector.py
?? core/weekly_news.py
?? core/weekly_selector.py
?? data/sp500_universe.json
?? docs/BASELINE.json
?? docs/END_TO_END.md
?? docs/FOLLOW_UP_AUDIT.md
?? docs/PRODUCTION_READINESS_AUDIT.md
?? docs/RELEASE_CHECKLIST.md
?? docs/UPGRADE_LOG.md
?? docs/history/README_pre_swing_upgrade.md
?? models/orders.py
?? scripts/evaluate_discovery.py
?? scripts/healthcheck.py
?? scripts/ingest_pead.py
?? scripts/preflight.py
?? scripts/probe_broker.py
?? scripts/reconcile_costs.py
?? scripts/record_baseline.py
?? scripts/refresh_universe.py
?? scripts/replay_archived.py
?? tests/conftest.py
?? tests/test_add.py
?? tests/test_attribution.py
?? tests/test_backend_safety.py
?? tests/test_cost_v2.py
?? tests/test_discovery.py
?? tests/test_evaluation_deployment.py
?? tests/test_event_ingestion.py
?? tests/test_exits.py
?? tests/test_mcp_state_contract.py
?? tests/test_optional_failures.py
?? tests/test_p0_safety.py
?? tests/test_production_integrity.py
?? tests/test_release_guards.py
?? tests/test_sentiment_unit.py
?? tests/test_strategic_exits.py
?? utils/freshness.py
?? utils/health.py
?? utils/instance_lock.py
?? utils/locks.py
?? utils/persistence.py
?? utils/strategy_config.py
```

## 10. Final recommendation

Keep live execution disabled until broker and deployment evidence closes the critical issues. Keep the code and audit in the working tree for review.
