# Robinhood authentication and account verification

Updated after the authorized clean-state reset on November 12, 2026.

## Current result

The existing session succeeded for the minimum before-and-after reset check. Each snapshot used one call to accounts, portfolio, holdings and orders. Both snapshots selected the same unique eligible account, produced a consistent portfolio fingerprint, returned complete empty holdings and zero available asset balances, and showed 27 terminal orders with no active orders. Renewal, browser authorization, credential writes and response recording were disabled.

The owner explicitly authorized using the previously confirmed unique Agentic account and initializing its local binding. The reset stored that verified fingerprint in `logs/positions.json`. It was compared in memory and never printed. [Reset evidence](verification/reset_local_2026-11-12.json)

Cash, total value, equity value, both buying-power fields and all returned asset-category values were $0.00. Withdrawable funds, unsettled funds and some liability fields were unavailable. Missing fields remain unavailable.

## Authentication storage and loading

| Item | Actual implementation |
| --- | --- |
| Local cache | Project-root `.rh_oauth_cache.json`, resolved relative to the client module. |
| Override | `ROBINHOOD_TOKEN_CACHE` is evaluated when the broker module imports. Set a process environment override before Python starts. |
| Normal config | JSON settings and project-root dotenv loading in `utils.config.load_config`. Dotenv is for API secrets. A token-path value loaded only after module import cannot change the already resolved cache constant. |
| Secret-free checks | `load_config(include_secrets=False)` skips dotenv and API-secret environment values. The offline preflight uses this path. |
| Local state | Schema-2 `logs/positions.json`, now empty and bound to the verified account. |
| Private-file handling | The token cache is atomically replaced with private permissions. Persistence errors now raise `AuthenticationError`. Pending credentials remain in memory but must be persisted successfully before a later session can use them. |

The current task did not inspect credential or dotenv contents. The existing client handled its cached authentication internally through its normal interface. Public model loading explicitly uses `token=False` and does not require an inference service or a model-provider login.

## Runtime flow

The CLI acquires its instance lock and constructs the application. Startup reconciles through `RobinhoodMCPClient`.

1. Reuse a fresh in-memory access token.
2. Reuse a fresh token loaded from the configured cache.
3. During normal owner-started operation, attempt a refresh when needed.
4. If renewal fails, the implemented browser PKCE flow may request approval using a localhost callback.
5. Save successful credentials atomically. An unsaved cache is an authentication error.
6. Resolve exactly one account with boolean `agentic_allowed=true`. Preserve its identity throughout the client session.
7. Compare portfolio identity with the persistent local binding before accepting holdings.

HTTP 401/403 and authentication failures retain their classification through account and holdings reads so health can report `AUTHENTICATION_FAILED`. Other reconciliation and transport failures remain distinguishable. Browser authorization URLs are no longer written to the application log. The callback listener closes after its request or timeout.

The callback URI remains `http://localhost:53271/callback`. Owner-authorized interactive approval belongs on the same machine running the client. Do not delete the cache to force a login. No standalone login command was added. Normal startup invokes the existing authentication flow, while the diagnostic command below refuses renewal.

## Read-only diagnostic

From the repository root with the existing environment, run the following when an empty-account diagnostic is appropriate.

```powershell
& .\.venv-upgrade\Scripts\python.exe -B scripts/probe_broker.py
```

It returns sanitized numeric summaries and booleans, without account identifiers. It does not reconcile or bind local state, submit or preview orders, cancel anything, renew tokens or write authentication files. A nonempty account produces a discrepancy result. A session requiring renewal produces a blocked result.

The destructive reset command is separate and requires its explicit `--apply` flag. It is a one-time maintenance operation, not a startup command. Do not rerun it after new journals or positions exist unless another reset is intended.

## Historical diagnosis

The November 11 probe stopped at a freshness guard before the normal renewal path, and a separate request using the then-saved access token returned HTTP 401. That did not establish refresh-token validity. Subsequent authenticated checks succeeded. The current probe now performs guarded reads through the actual client and reports their result. Existing dated evidence is retained.
