# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's
*Report a vulnerability* feature (Security tab → Advisories), or contact the
repository owner directly. Please include reproduction steps and the
affected file(s); you should receive a response within a few days.

## What this software is (read before running)

`robinhood-agent` is an **autonomous, LLM-driven trading system** that can
place real orders against a real Robinhood brokerage account. Treat it as
experimental research software:

- Keep `execution.dry_run: true` in `config/settings.json` until you have
  independently reviewed the code and accept the risk.
- LLM output is non-deterministic; markets are unpredictable. Hard risk
  rules (stop-loss, max positions, sizing, safe windows) are enforced in
  code, but no code can eliminate trading risk.
- Nothing here is investment advice.

## Local data & privacy

The agent writes runtime files locally. Robinhood supplies brokerage and
market data, Finnhub supplies news, and Anthropic supplies decision reviews.
Discovery can retrieve the public constituent CSV from GitHub and download
its pinned sentiment weights from Hugging Face. The offline test runner
blocks connections beyond loopback.

| Path | Contents | Why it's sensitive |
|---|---|---|
| `.env` | Anthropic + Finnhub API keys | secrets |
| `.rh_oauth_cache.json` | Robinhood OAuth access/refresh tokens | full brokerage access |
| `logs/*.jsonl` | Raw MCP traces | contain **real account numbers** and positions |
| `logs/positions.json`, `logs/trades.json`, `logs/performance.json` | your trade history | personal financial data |
| `journals/*.json` | per-ticker LLM decision journals | personal trading history |
| `data/seen_headlines.json`, `data/selected_universe.json`, `data/earnings_calendar.json` | runtime caches | regenerated automatically |

All of the above are covered by `.gitignore`. If you ever find one of them
in a commit, stop and alert the maintainers immediately — rotate the
affected credential (Robinhood connected-apps, API keys) before anything
else.

## Secrets policy

- API keys enter through environment variables loaded from `.env`
  (`ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`). Robinhood OAuth credentials are
  loaded from the private cache described in [the authentication guide](docs/AUTHENTICATION.md).
- The response recorder redacts known bearer, token, and key fields and masks
  `account_number` in trace arguments. Treat the trace files as sensitive.
