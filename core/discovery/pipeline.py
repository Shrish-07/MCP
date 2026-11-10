"""Local L0–L6 orchestration. No paid-model call can occur in this pipeline."""

import asyncio
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from .universe import load_universe, refresh
from .features import technical_features
from .gates import eligibility
from .divergence import score_cohort
from .shortlist import publish, mark_failed
from .shortlist import select_new
from .news_features import NewsHistory, deduplicate
from core.exits.trail_manager import completed_bars
from utils.persistence import atomic_json


class Discovery:
    def __init__(self, root, settings, client, pm, news_fetcher, model_loader):
        self.root = Path(root)
        self.settings = settings
        self.cfg = settings["discovery"]
        self.client, self.pm, self.news_fetcher, self.model_loader = (
            client,
            pm,
            news_fetcher,
            model_loader,
        )
        self.feature_path = self.root / "data/discovery_features_latest.json"
        self.path = self.root / self.cfg["shortlist_file"]
        self.inference_lock=asyncio.Lock()

    async def news_for(self,ticker):
        from .news_features import collect_news_features
        async with self.inference_lock:
            model=await asyncio.to_thread(self.model_loader)
            return await collect_news_features(NewsHistory(self.root/"data/sentiment_history.json"),self.news_fetcher,model,ticker)

    async def features(self):
        if not self.cfg["enabled"]:
            return None
        universe = load_universe(
            self.root / self.cfg["universe_file"], self.cfg["universe_max_age_days"]
        )
        symbols = [r["ticker"] for r in universe["constituents"]]
        rows, excluded = {}, {}
        for start in range(0, len(symbols), 10):
            chunk = symbols[start : start + 10]
            bars = await self.client.get_equity_historicals_batch(chunk, span="year")
            quotes = {q.ticker: q for q in await self.client.get_equity_quotes(chunk)}
            for ticker in chunk:
                try:
                    row = technical_features(
                        completed_bars(bars.get(ticker, [])), self.cfg
                    )
                    reasons = eligibility(row, quotes.get(ticker), self.cfg["gates"])
                    if reasons:
                        excluded[ticker] = reasons
                    else:
                        rows[ticker] = row
                except ValueError as exc:
                    excluded[ticker] = [str(exc)]
        if len(rows) < self.cfg["min_cohort"]:
            raise ValueError("insufficient valid feature cohort")
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "generated_at": now,
            "universe_version": universe["version"],
            "rows": rows,
            "excluded": excluded,
        }
        atomic_json(self.feature_path, payload)
        atomic_json(
            self.root / self.cfg["features_dir"] / f"features_{now[:10]}.json", payload
        )
        return payload

    async def rank(self):
        if not self.cfg["enabled"]:
            return None
        try:
            # Controlled weekly constituent refresh; failure retains previous shortlist.
            await refresh(self.root / self.cfg["universe_file"])
            features = await self.features()
            model = await asyncio.to_thread(self.model_loader)
            info = model.get_model_info()
            if not info.get("model_name"):
                raise ValueError("model identity unavailable")
            history = NewsHistory(self.root / "data/sentiment_history.json")
            rows = {}
            now = datetime.now(timezone.utc)
            start = now - timedelta(days=45)
            catalyst_path = self.root / "data/catalyst_events.json"
            from .catalysts import refresh_catalysts
            try:
                catalysts = await refresh_catalysts(self.client, catalyst_path, features["rows"], now=now)
            except (ValueError, RuntimeError, OSError):
                catalysts = {}
            for ticker, technical in features["rows"].items():
                items = await self.news_fetcher.fetch_company_news(
                    ticker, from_date=start.date(), to_date=now.date(), strict=True
                )
                unique, _ = deduplicate(items)
                scores = await asyncio.to_thread(
                    model.score_headlines, [n.headline for n in unique]
                )
                try:
                    news = history.update(
                        ticker,
                        items,
                        scores,
                        info,
                        now=now,
                        coverage_start=start.isoformat(),
                    )
                except ValueError as exc:
                    if "sparse ticker" in str(exc):
                        continue
                    raise
                catalyst = catalysts.get(ticker)
                confirmed = False
                if (
                    catalyst
                    and catalyst.get("source")
                    and catalyst.get("confirmed") is True
                ):
                    stamp = datetime.fromisoformat(catalyst["timestamp"])
                    confirmed = (
                        stamp.tzinfo is not None
                        and now - timedelta(days=7) <= stamp <= now
                    )
                rows[ticker] = dict(
                    technical,
                    **news,
                    confirmed_catalyst=catalyst if confirmed else None,
                )
            if len(rows) < self.cfg["min_cohort"]:
                raise ValueError("insufficient valid news cohort")
            reference = "cross-sectional-v1:" + now.isoformat()
            ranked = score_cohort(rows, self.cfg, reference)
            previous = json.loads(self.path.read_text()) if self.path.exists() else {}
            held = (
                [r.ticker for r in self.pm.get_open_records()]
                if self.pm.reconciliation_ok
                else []
            )
            if not self.pm.reconciliation_ok:
                raise ValueError(
                    "holdings reconciliation required for new-name exclusion"
                )
            candidates = select_new(
                ranked, held, self.cfg["shortlist"], previous.get("history", [])
            )
            metadata = {
                "data_cutoff": min(r["data_cutoff"] for r in ranked.values()),
                "config_revision": self.settings.get("config_revision"),
                "scoring_version": "divergence-v1",
                "universe_version": features["universe_version"],
                "gap_reference": reference,
                "cohort_size": len(ranked),
                "excluded": features["excluded"],
                "all_ranked": ranked,
            }
            result = publish(self.path, candidates, metadata)
            atomic_json(self.root / "data/discovery_ranked_latest.json", result)
            return result
        except Exception as exc:
            mark_failed(self.path, exc)
            raise
