"""Fetch news from Finnhub REST API with structured models and id-based
deduplication across cycles.

Pure data pipeline — NO LLM anywhere in this module (the old optional
Haiku summarizer/fallback was removed; neither feature was enabled).

Features:
- Finnhub company-news + general market news
- ID-based deduplication (cycles only show articles once per calendar day)
- Freshness window filter + per-ticker cap
- Shared sliding-window rate limiter (55 calls/min, under Finnhub's 60)
- Retries with exponential backoff + jitter on HTTP/transient failures
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from models.news import NewsItem
from utils.rate_limit import RateLimiter

_FINNHUB_COMPANY_NEWS = "https://finnhub.io/api/v1/company-news"
_FINNHUB_GENERAL_NEWS = "https://finnhub.io/api/v1/news"


class NewsFetcher:
    """Fetch per-ticker and general-market news from Finnhub REST.

    Each fetched article is parsed into a ``NewsItem``.  Deduplication
    uses Finnhub's ``id`` field, persisted to ``data/seen_headlines.json``
    so MID and CLOSE cycles don't re-surface the same articles within
    the same calendar day.
    """

    def __init__(
        self,
        finnhub_api_key: str = "",
        settings: dict | None = None,
        max_retries: int = 3,
    ) -> None:
        self.logger = logging.getLogger("robinhood-agent.core.news_fetch")
        self._seen_path = Path(__file__).resolve().parent.parent / "data" / "seen_headlines.json"
        self._seen_path.parent.mkdir(parents=True, exist_ok=True)

        cfg = settings or {}
        self._api_key: str = finnhub_api_key or os.getenv("FINNHUB_API_KEY", "")
        self._lookback_days: int = int(cfg.get("lookback_days", 3))
        self._max_per_ticker: int = int(cfg.get("max_per_ticker", 5))
        self._fresh_window_hours: int = int(cfg.get("fresh_window_hours", 24))
        gm = cfg.get("general_market", {})
        self._gm_enabled: bool = bool(gm.get("enabled", True))
        self._gm_category: str = str(gm.get("category", "general"))
        self._gm_count: int = int(gm.get("count", 8))
        self._max_retries = max_retries
        self._rate_limiter = RateLimiter(max_calls_per_minute=55)  # under Finnhub's 60/min

    # ------------------------------------------------------------------
    # Public API — same signature shape as before  (dict[str, …])
    # ------------------------------------------------------------------

    async def fetch_news(
        self, tickers: list[str], cycle_type: str = "OPEN"
    ) -> dict[str, list[NewsItem]]:
        """Return ``{ticker: [NewsItem, …]}`` for every supplied ticker.

        * Builds a per-day seen-set of Finnhub ``id`` values.
        * Fetches company-news for each ticker concurrently.
        * Optionally fetches general-market news and attaches it to
          ``"MARKET"`` and merges into ``"SPY"``.
        * Applies the freshness filter and ``max_per_ticker`` cap.
        """
        day_key = date.today().isoformat()

        seen_raw = self._read_seen_store()
        if seen_raw.get("_date") != day_key:
            existing_ids: list[int] = []
        else:
            existing_ids = list(seen_raw.get("ids", []))

        # Full seen-set — previously this silently sliced to [118:], which
        # re-admitted old headlines whenever fewer than 119 ids were stored.
        seen_ids: set[int] = set(existing_ids)

        result: dict[str, list[NewsItem]] = {}
        if not self._api_key:
            self.logger.warning("finnhub no API key — news fetch skipped")
            return {t: [] for t in tickers}

        async with httpx.AsyncClient(timeout=15.0) as http:
            sem = asyncio.Semaphore(10)

            async def _per_ticker(sym: str) -> tuple[str, list[NewsItem]]:
                async with sem:
                    raw = await self._fetch_company_news(http, sym)
                    items = self._parse_items(raw, sym)
                    return sym, items

            tasks = [_per_ticker(t) for t in tickers]
            for task in asyncio.as_completed(tasks):
                try:
                    sym, items = await task
                except Exception as exc:
                    self.logger.warning("news_fetch_failed task error=%s", exc)
                    continue
                result[sym] = items

            gm_items: list[NewsItem] = []
            if self._gm_enabled:
                gm_raw = await self._fetch_general_news(http)
                gm_items = self._parse_items(gm_raw, "MARKET")

        new_ids: set[int] = set()
        for sym, items in result.items():
            fresh: list[NewsItem] = []
            for it in items:
                if it.id in seen_ids:
                    continue
                age_h = (datetime.now(UTC) - datetime.fromtimestamp(it.datetime, tz=UTC)).total_seconds() / 3600.0
                if age_h > self._fresh_window_hours:
                    continue
                fresh.append(it)
                new_ids.add(it.id)
            result[sym] = fresh[: self._max_per_ticker]

        if gm_items:
            fresh_gm: list[NewsItem] = []
            for it in gm_items:
                if it.id in seen_ids:
                    continue
                age_h = (datetime.now(UTC) - datetime.fromtimestamp(it.datetime, tz=UTC)).total_seconds() / 3600.0
                if age_h > self._fresh_window_hours:
                    continue
                fresh_gm.append(it)
                new_ids.add(it.id)
            fresh_gm = fresh_gm[: self._gm_count]
            if fresh_gm:
                result["MARKET"] = fresh_gm
                if "SPY" in result:
                    result["SPY"] = (result["SPY"] + fresh_gm)[: self._max_per_ticker]

        # No-news tickers simply map to [] (the removed Haiku fallback used
        # to fabricate a sentence here — real data or silence now).
        for t in tickers:
            if result.get(t) is None:
                result[t] = []

        self._persist_seen(day_key, existing_ids, new_ids)
        return result

    # ------------------------------------------------------------------
    # Seen-store persistence
    # ------------------------------------------------------------------

    def _read_seen_store(self) -> dict:
        if not self._seen_path.exists():
            return {}
        try:
            return json.loads(self._seen_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _persist_seen(self, day_key: str, existing_ids: list[int], new_ids: set[int]) -> None:
        """Write today's merged id list (rolling prune to last ~400)."""
        try:
            merged = list(dict.fromkeys(existing_ids + sorted(new_ids)))[-400:]
            self._seen_path.write_text(
                json.dumps({"_date": day_key, "ids": merged}, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            self.logger.warning("seen_headlines_persist_failed: %s", exc)

    # ------------------------------------------------------------------
    # Finnhub fetchers (rate-limited + retried)
    # ------------------------------------------------------------------

    async def _fetch_with_retry(self, http: httpx.AsyncClient, endpoint: str,
                                params: dict, label: str) -> list:
        """GET one Finnhub endpoint with the shared rate limiter and
        exponential-backoff retries. Failures degrade to []."""
        params = {**params, "token": self._api_key}
        for attempt in range(self.max_retries):
            await self._rate_limiter.acquire()
            try:
                r = await http.get(endpoint, params=params)
                r.raise_for_status()
                data = r.json()
                return data if isinstance(data, list) else []
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if 400 <= status < 500 and status != 429:
                    # Client error (bad key, bad params) — retrying won't help.
                    self.logger.warning("finnhub_client_error %s status=%s label=%s", endpoint, status, label)
                    return []
                self.logger.warning("finnhub_http_retryable label=%s status=%s attempt=%d",
                                    label, status, attempt + 1)
                await asyncio.sleep((2 ** attempt) + random.uniform(0, 1))
            except Exception as exc:
                self.logger.warning("finnhub_error label=%s attempt=%d error=%s", label, attempt + 1, exc)
                await asyncio.sleep((2 ** attempt) + random.uniform(0, 1))
        self.logger.error("finnhub_failed label=%s after %d attempts", label, self.max_retries)
        return []

    async def _fetch_company_news(self, http: httpx.AsyncClient, symbol: str) -> list:
        params = {
            "symbol": symbol,
            "from": (date.today() - timedelta(days=self._lookback_days)).isoformat(),
            "to": date.today().isoformat(),
        }
        return await self._fetch_with_retry(http, _FINNHUB_COMPANY_NEWS, params, symbol)

    async def _fetch_general_news(self, http: httpx.AsyncClient) -> list:
        params = {"category": self._gm_category}
        return await self._fetch_with_retry(http, _FINNHUB_GENERAL_NEWS, params, "general_market")

    @staticmethod
    def _parse_items(raw: list, related: str) -> list[NewsItem]:
        items: list[NewsItem] = []
        for art in raw:
            try:
                items.append(
                    NewsItem(
                        id=int(art.get("id", 0)),
                        headline=str(art.get("headline", "")),
                        summary=art.get("summary"),
                        source=str(art.get("source", "")),
                        url=art.get("url"),
                        image=art.get("image"),
                        related=str(art.get("related", related or "")),
                        category=str(art.get("category", "")),
                        datetime=int(art.get("datetime", 0)),
                    )
                )
            except Exception:
                continue
        return items
