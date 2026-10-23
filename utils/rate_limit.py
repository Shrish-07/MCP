"""Shared asynchronous rate limiter (sliding-window).

Originally lived in core/weekly_news.py; promoted here so every outbound
HTTP component (daily NewsFetcher, WeeklyNewsFetcher) uses the same
discipline against Finnhub's free-tier limit.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

logger = logging.getLogger("robinhood-agent.utils.rate_limit")


class RateLimiter:
    """Simple sliding-window rate limiter (default: Finnhub free tier 60 calls/min)."""

    def __init__(self, max_calls_per_minute: int = 60):
        self.max_calls = max_calls_per_minute
        self.calls: list[datetime] = []
        self.lock = asyncio.Lock()
        self.logger = logger

    async def acquire(self) -> None:
        """Acquire a rate limit token, waiting if necessary."""
        async with self.lock:
            now = datetime.now()
            self.calls = [c for c in self.calls if (now - c).total_seconds() < 60]
            if len(self.calls) >= self.max_calls:
                oldest = min(self.calls)
                wait_time = 60 - (now - oldest).total_seconds() + 0.1
                if wait_time > 0:
                    self.logger.info("rate_limit_wait %.1fs", wait_time)
                    await asyncio.sleep(wait_time)
                    now = datetime.now()
                    self.calls = [c for c in self.calls if (now - c).total_seconds() < 60]
            self.calls.append(datetime.now())
