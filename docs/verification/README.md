# November 16 operating verification

The current audit is [MONDAY_READINESS.md](../MONDAY_READINESS.md).
Its [full suite](pytest_monday_2026-11-15.txt) passed 243 tests, and its
[focused suite](pytest_monday_targeted_2026-11-15.txt) passed 107 tests.
[Check results](checks_monday_2026-11-15.json) include the initial working-tree
inventory and final configuration, lint, format, dependency and health findings.
These are offline results, not current authentication or live-order evidence.
Earlier dated evidence is preserved below.

# Clean reset verification

The current authorized reset and local readiness checks supersede the earlier partial journal cleanup. Original dated evidence remains below.

| Evidence | Result |
| --- | --- |
| [Reset and broker snapshots](reset_local_2026-11-12.json) | Nine journal files / 169 remaining entries and four legacy positions removed. Four derived context files removed. Broker snapshots before and after show zero observed assets/holdings/active orders and 27 terminal orders. Binding consistency verified without identifiers. |
| [Clean-state check](clean_state_reset_2026-11-12.json) | Empty valid schema-2 state, zero old journal/context stores and no unresolved local intents. Configuration unchanged. |
| [Final verified test suite](pytest_clean_reset_verified_2026-11-12.txt) | 214 passed, zero failures/skips/deselections, one AnyIO warning. Includes cached-model and credential-persistence recovery tests. |
| [Earlier complete suite](pytest_clean_reset_complete_2026-11-12.txt) | 213 passed before the last credential-persistence recovery regression. |
| [Earlier final suite](pytest_clean_reset_final_2026-11-12.txt) | 209 passed before the four accounting tests were added. |
| [Earlier full reset suite](pytest_clean_reset_2026-11-12.txt) | 199 passed before compatibility and accounting tests were added. |
| [Provider accounting tests](provider_accounting_reset_2026-11-12.txt) | Four mocked tests passed, subsequently included in the complete suite. |
| [Local models](local_models_reset_2026-11-12.json) | Primary and actual forced-fallback inference succeeded locally with anonymous, pinned model loading. |
| [Current preflight](preflight_clean_reset_2026-11-12.json) | Local validation passed, zero positions/intents, binding present, dependencies present, live execution disabled. |
| [Final checks](checks_clean_reset_2026-11-12.json) | Lint, compile, dependency and retention results. |

The installed model tests were run with offline model access and the guarded runner. No inference endpoint, news provider, live broker write or scheduler was called. The original runner's import spacing was formatted without changing its guard behavior. The current readiness report records corrected intermediate failures and the remaining third-party warning.

The standalone health check now correctly returns PROCESS_ABSENT before startup. Health is demonstrated with controlled current/stale/PID-mismatch/broker/auth/state fixtures, not a fabricated runtime heartbeat.

## Earlier evidence retained

# Current verification evidence

The November 12, 2026 review performed actual authenticated read-only account requests and guarded offline checks. It made no broker write, paid-provider, model-download, scheduler or discovery call. Credential renewal and credential writes were blocked.

| Artifact | Scope |
| --- | --- |
| [Account state](account_state_2026-11-12.json) | Sanitized balances, holdings and terminal-order summary from exactly four broker reads. No account identifiers or raw records. Missing financial fields and historical-identity comparison remain unavailable. |
| [Journal cleanup](journal_cleanup_2026-11-12.json) | 250 research-only rows removed, 169 financial/outcome rows preserved, zero research-only rows remaining. A complete fresh-context reset remains incomplete. |
| [Pytest output](pytest_2026-11-12.txt) | 169 passed, two heavy tests deselected, one AnyIO compatibility warning, exit 0. |
| [Offline runner](run_offline_audit_2026-11-12.py) | Reproducible socket/secret/protected-write guard, temporary cost storage and fixture storage. Run with the repository virtual environment and Python -B. |
| [Preflight](preflight_2026-11-12.json) | Current configuration and legacy-state summary. Dotenv and API-secret reads were disabled. The report was written under a temporary root. Actual state stayed unchanged. |
| [Check summary](checks_2026-11-12.json) | Command exits, dependency availability, model-cache observations and protected-state verification. |

Run the complete safe suite from the repository root with the following command.

```powershell
& .\.venv-upgrade\Scripts\python.exe -B docs/verification/run_offline_audit_2026-11-12.py
```

The runner accepts additional pytest arguments. It defaults to the repository's non-heavy suite. Heavy tests can load/download real models, so they were excluded. Actual trade transport, cloud hosting, API billing reconciliation and live model inference were unverified. The latest full Ruff check failed with 390 findings. Pyflakes rules, compilation and pip check passed. Health exited 1 because the health file is absent.

For the preflight check, the review called `scripts.preflight.preflight` with a temporary root, replaced its manager factory with a read-only manager of the existing local file, disabled `load_dotenv`, and returned empty API-secret environment values. This executes the original preflight logic without writing `logs/preflight.json` or creating an account binding. It is distinct from running the unmodified CLI, which writes a runtime report.

The [current audit](../PRODUCTION_READINESS_AUDIT.md), [authentication guide](../AUTHENTICATION.md) and [deployment preparation](../DEPLOYMENT.md) explain remaining limitations.

## Retained November 10 evidence

The following text and artifacts retain the earlier observations. Their counts and environment findings are historical.

# Dated verification evidence

These original November 10, 2026 artifacts retain useful offline test and replay evidence. They are historical results, not a fresh run of the November 11 environment. The current interpretation is maintained in [the readiness audit](../PRODUCTION_READINESS_AUDIT.md) and [authentication guide](../AUTHENTICATION.md).

| Artifact | Recorded command or origin | Recorded result |
| --- | --- | --- |
| `pytest_2026-11-10.txt` | `python -m pytest -q` | 164 passed, two deselected. |
| `verify_weekly_layer_2026-11-10.txt` | `python scripts/verify_weekly_layer.py` | 35 passed. |
| `preflight_2026-11-10.json` | `python scripts/preflight.py` | Dry run enabled, release unapproved, configuration errors empty, live readiness false. Sentiment dependencies were reported available in that runtime. |
| `replay_archived_MU_2026-11-10.txt` | `python scripts/replay_archived.py --ticker MU --output-dir .test-tmp/replay-run` | 90 observations, one simulated closed position. |
| `replay_MU_report_2026-11-10.json` | Output of that archived replay | Paper-mode attribution with one losing close. This does not establish predictive performance. |
| `healthcheck_2026-11-10.txt` | `python scripts/healthcheck.py` | Unhealthy, exit 1. The result does not identify the authentication cause. |

The earlier claim that no credentials existed was incorrect. The November 11 inspection found a readable OAuth cache with refresh credentials. Its access token returned HTTP 401, while refresh acceptance remains untested. The probe's freshness guard skips the renewal flow. The latest health check found no health file, and the current inspected virtual environment could not discover the optional sentiment modules. Preserve the original dated JSON as evidence of its own runtime.

The duplicate preflight text export and prior summary image were removed during cleanup. Retain the original machine-readable report and command outputs above. Follow the repository README for current verification commands and use its installed virtual environment. Running the preflight or replay scripts writes local reports. They were not rerun during the read-only authentication investigation.
