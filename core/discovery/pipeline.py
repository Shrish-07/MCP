"""Produce the L0–L5 shortlist consumed by the L6 decision hand-off."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from core.data.daily import completed_bars
from utils.persistence import atomic_json

from .catalysts import refresh_catalysts
from .divergence import score_cohort
from .feature_snapshot import feature_revision, latest_completed_session, load_features
from .features import technical_features
from .gates import eligibility
from .news_features import NewsHistory, SparseNewsError, collect_news_features
from .shortlist import mark_failed, prior_history, publish, select_new
from .universe import load_universe, refresh

logger = logging.getLogger(__name__)


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
        self.inference_lock = asyncio.Lock()
        self.feature_lock = asyncio.Lock()

    async def news_for(self, ticker):
        async with self.inference_lock:
            model = await asyncio.to_thread(self.model_loader)
            return await collect_news_features(
                NewsHistory(self.root / "data/sentiment_history.json"),
                self.news_fetcher,
                model,
                ticker,
            )

    async def _universe(self, refresh_first):
        path = self.root / self.cfg["universe_file"]
        if not refresh_first:
            try:
                return load_universe(path, self.cfg["universe_max_age_days"])
            except (OSError, ValueError):
                pass
        try:
            await refresh(path)
        except (OSError, ValueError, KeyError, httpx.HTTPError) as exc:
            logger.warning("universe_refresh_failed type=%s", type(exc).__name__)
        return load_universe(path, self.cfg["universe_max_age_days"])

    async def features(self, *, refresh_universe=False):
        """L0 provides the constituent snapshot, then L1/L2 prepare eligible rows."""
        if not self.cfg["enabled"]:
            return None
        async with self.feature_lock:
            universe = await self._universe(refresh_universe)
            now = datetime.now(UTC)
            try:
                return load_features(self.feature_path, universe, self.cfg, now)
            except (OSError, ValueError, OverflowError) as exc:
                logger.info("feature_snapshot_rebuild type=%s", type(exc).__name__)
            return await self._build_features(universe, now)

    async def _build_features(self, universe, now):
        session = latest_completed_session(now)
        symbols = [r["ticker"] for r in universe["constituents"]]
        rows, excluded = {}, {}
        # A 365-day window can contain fewer than 252 completed sessions.
        required = max(252, self.cfg["gates"]["min_bars"])
        lookback_days = (required * 366 + 249) // 250 + 21
        for start in range(0, len(symbols), 10):
            chunk = symbols[start : start + 10]
            bars = await self.client.get_equity_historicals_batch(
                chunk, lookback_days=lookback_days
            )
            quotes = {q.ticker: q for q in await self.client.get_equity_quotes(chunk)}
            for ticker in chunk:
                try:
                    row = technical_features(
                        completed_bars(bars.get(ticker, []), now, trailing_only=True),
                        self.cfg,
                    )
                    reasons = eligibility(row, quotes.get(ticker), self.cfg["gates"])
                    if reasons:
                        excluded[ticker] = reasons
                    else:
                        rows[ticker] = dict(
                            row, quote_timestamp=quotes[ticker].timestamp
                        )
                except ValueError as exc:
                    excluded[ticker] = [str(exc)]
        if len(rows) < self.cfg["min_cohort"]:
            raise ValueError("insufficient valid feature cohort")
        payload = {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "session": session,
            "universe_version": universe["version"],
            "feature_revision": feature_revision(self.cfg),
            "rows": rows,
            "excluded": excluded,
        }
        atomic_json(self.feature_path, payload)
        atomic_json(
            self.root / self.cfg["features_dir"] / f"features_{session}.json", payload
        )
        return payload

    async def rank(self):
        """L3 enriches the L2 cohort, L4 scores it and L5 publishes new names."""
        if not self.cfg["enabled"]:
            return None
        try:
            features = await self.features(refresh_universe=True)
            async with self.inference_lock:
                model = await asyncio.to_thread(self.model_loader)
            history = NewsHistory(self.root / "data/sentiment_history.json")
            rows = {}
            now = datetime.now(UTC)
            catalyst_path = self.root / "data/catalyst_events.json"
            try:
                catalysts = await refresh_catalysts(
                    self.client, catalyst_path, features["rows"], now=now
                )
            except (ValueError, TypeError, RuntimeError, OSError) as exc:
                logger.warning(
                    "discovery_catalysts_unavailable type=%s", type(exc).__name__
                )
                catalysts = {}
            excluded = dict(features["excluded"])
            for ticker, technical in features["rows"].items():
                try:
                    async with self.inference_lock:
                        news = await collect_news_features(
                            history,
                            self.news_fetcher,
                            model,
                            ticker,
                            now=now,
                            persist=False,
                        )
                except SparseNewsError:
                    excluded[ticker] = ["sparse_news"]
                    continue
                except (RuntimeError, ValueError, OverflowError) as exc:
                    excluded[ticker] = [f"news_unavailable:{type(exc).__name__}"]
                    continue
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
            history.flush()
            if len(rows) < self.cfg["min_cohort"]:
                raise ValueError("insufficient valid news cohort")
            reference = "cross-sectional-v1:" + now.isoformat()
            ranked = score_cohort(rows, self.cfg, reference)
            if not self.pm.reconciliation_ok:
                raise ValueError(
                    "holdings reconciliation required for new-name exclusion"
                )
            held = [r.ticker for r in self.pm.get_open_records()]
            candidates = select_new(
                ranked, held, self.cfg["shortlist"], prior_history(self.path)
            )
            metadata = {
                "data_cutoff": now.isoformat(),
                "feature_session": features["session"],
                "config_revision": self.settings.get("config_revision"),
                "scoring_version": "divergence-v1",
                "universe_version": features["universe_version"],
                "gap_reference": reference,
                "cohort_size": len(ranked),
                "excluded": excluded,
                "all_ranked": ranked,
            }
            result = publish(
                self.path,
                candidates,
                metadata,
                candidate_memory_runs=self.cfg["shortlist"]["candidate_memory_runs"],
            )
            try:
                atomic_json(self.root / "data/discovery_ranked_latest.json", result)
            except OSError:
                logger.warning("discovery_ranked_report_unavailable")
            return result
        except Exception as exc:
            logger.warning("discovery_ranking_failed type=%s", type(exc).__name__)
            try:
                mark_failed(self.path, type(exc).__name__)
            except OSError:
                logger.warning("discovery_status_write_failed")
            raise
