# Verification evidence — 2026-11-10

All artifacts in this directory were produced by actually running the repo on
2026-11-10 (Windows, Python 3.13.6, repo root `robinhood-agent/`, dry-run mode).
Every number in the PNG is copied verbatim from these logs.

| File | Command that produced it | Result |
| --- | --- | --- |
| `pytest_2026-11-10.txt` | `python -m pytest -q` | `164 passed, 2 deselected in 5.78s` (exit 0) |
| `verify_weekly_layer_2026-11-10.txt` | `python scripts/verify_weekly_layer.py` | `35 passed in 1.04s` (exit 0) |
| `preflight_2026-11-10.txt` / `.json` | `python scripts/preflight.py` | `configuration_errors: []`, `dry_run: true`, `live_ready: false` — correctly gated |
| `replay_archived_MU_2026-11-10.txt` | `python scripts/replay_archived.py --ticker MU --output-dir .test-tmp/replay-run` | exit 0; `90 observations, 1 closed position` |
| `replay_MU_report_2026-11-10.json` | output of the replay above | `MU_report.json` (schema_version 2, mode paper): gross=net realized PnL -785.60, win rate 0.0 (Wilson 95% [0.0, 0.793]), MFE +4.71%, MAE -9.58% |
| `healthcheck_2026-11-10.txt` | `python scripts/healthcheck.py` | prints `unhealthy`, exit 1 — **expected** in this environment because no live broker credentials exist here; the check fails closed |
| `pipeline_verified_2026-11-10.png` | generated from the logs above | start-to-end pipeline diagram with the verified numbers rendered on it |

Additional run performed but not archived here: `python -m compileall -q core
utils models scripts main.py robinhood_mcp` exited 0 (no syntax errors).

## How to reproduce

```powershell
cd robinhood-agent
venv\Scripts\python.exe -m pytest -q
venv\Scripts\python.exe scripts/verify_weekly_layer.py
venv\Scripts\python.exe scripts/preflight.py
venv\Scripts\python.exe scripts/replay_archived.py --ticker MU --output-dir .test-tmp\replay-run
venv\Scripts\python.exe scripts/healthcheck.py
```

Note: `logs/` and `.test-tmp/` are git-ignored runtime directories; the copies
here are the committed snapshots of those runs.
