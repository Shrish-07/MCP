# Contributing to robinhood-agent

Thanks for your interest in contributing! This is autonomous trading software, so a few extra rules apply to keep every user (and their brokerage account) safe.

## Ground rules (read first)

1. **Never commit secrets or personal runtime data.** The following are per-user local state and are git-ignored on purpose — never `git add -f` any of them:
   - `.env` (API keys), `.rh_oauth_cache.json` (Robinhood OAuth tokens)
   - anything under `logs/` (trade history, positions, MCP traces with **real account numbers**)
   - anything under `journals/` (your personal LLM trading journals)
   - `data/seen_headlines.json`, `data/earnings_calendar.json`, `data/selected_universe.json`, `data/weekly_news_cache.json`
2. **`.env` is for secrets only** — the two API keys (`ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`). If you need a new secret, read it via `os.getenv(...)` and add a placeholder line to `.env.example`.
3. **Every toggle lives in `config/settings.json`.** Do not introduce environment-variable switches for behaviour; the JSON config is the single source of truth.
4. **Dry-run stays the default.** `trading.dry_run` must remain `true` in the shipped `config/settings.json`. Never change it in a PR.
5. **Pull requests require approval from the repository owner** (see `.github/CODEOWNERS`) and must pass CI, which includes a secret scan (gitleaks) that will block a PR containing tokens, keys, or files from `logs/`/`journals/`.

## Development setup

```bash
git clone <your-fork>
cd robinhood-agent
python -m venv .venv
.venv\Scripts\activate        # Windows   (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
pip install -r requirements-dev.txt
# Optional — only if you enable sentiment_selection in config/settings.json:
# pip install -r requirements-ml.txt
copy .env.example .env         # fill in your two API keys
```

Run everything in **dry-run** (`trading.dry_run: true` is the shipped default) while developing.

## Running the tests

```bash
pytest                                    # light suite (default; heavy tests excluded)
pytest -m heavy                           # includes the ModernBERT model tests (~large download)
python scripts/verify_weekly_layer.py     # offline-deterministic weekly-selection harness
```

CI runs the light suite and the verification harness on every PR (Python 3.11 + 3.13, Linux).

## Pull request checklist

- [ ] No secrets, no `logs/` or `journals/` files, no OAuth caches in the diff
- [ ] `trading.dry_run` is still `true` in `config/settings.json`
- [ ] New configuration (if any) is added to `config/settings.json`, not to `.env`
- [ ] `pytest` passes locally
- [ ] README updated if behaviour changed

## Repository governance

- `main` is protected: PRs only, at least one approval **from the code owner**, CI must be green, force-pushes blocked.
- Enable GitHub's **secret scanning + push protection** (Settings → Code security) so leaked keys are blocked at push time.

## License

A LICENSE file must be added before this repository is made public. Until then, all rights are reserved by the repository owner.
