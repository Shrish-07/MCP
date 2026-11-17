from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from models.market_data import OHLCVBar
from models.portfolio import Portfolio
from robinhood_mcp.robinhood_client import RobinhoodMCPClient


class DataIngestLayer:
    """Phase 1 ingestion layer using the Robinhood MCP client."""

    def __init__(self, client: RobinhoodMCPClient | None = None) -> None:
        self.client = client or RobinhoodMCPClient()
        self.logger = logging.getLogger("robinhood-agent.core.data.ingest")

    async def ingest(self, tickers: list[str], existing_tickers: list[str]) -> dict:
        """Fetch portfolio, quotes, and historical bars for the union of watchlist and open positions."""
        deduped = list(dict.fromkeys([*existing_tickers, *tickers]))
        try:
            portfolio = await self.client.get_portfolio()
        except Exception as exc:
            self.logger.exception(
                "portfolio_fetch_failed: %s", type(exc).__name__, exc_info=False
            )
            portfolio = Portfolio(reconciliation_status="FAILED", errors=[str(exc)])

        try:
            quotes = []
            for start in range(0, len(deduped), 20):
                quotes.extend(
                    await self.client.get_equity_quotes(deduped[start : start + 20])
                )
            quote_map = {item.ticker: item for item in quotes}
        except Exception as exc:
            self.logger.exception(
                "quote_fetch_failed: %s", type(exc).__name__, exc_info=False
            )
            quote_map = {}

        tasks = [
            self.client.get_equity_historicals(ticker, span="year")
            for ticker in deduped
        ]
        historical_results = await asyncio.gather(*tasks, return_exceptions=True)

        historical_map: dict[str, list[OHLCVBar]] = {}
        for ticker, result in zip(deduped, historical_results, strict=False):
            if isinstance(result, Exception):
                self.logger.warning(
                    "historical_fetch_failed ticker=%s error=%s", ticker, result
                )
                historical_map[ticker] = []
            else:
                historical_map[ticker] = list(result)

        return {
            "portfolio": portfolio,
            "quotes": quote_map,
            "historicals": historical_map,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    async def ingest_quotes_only(
        self, tickers: list[str], existing_tickers: list[str] | None = None
    ) -> dict:
        """Fetch only portfolio and quotes for the union of watchlist and open positions.

        This intentionally skips fetching historical bars to save API/time.
        """
        existing_tickers = existing_tickers or []
        deduped = list(dict.fromkeys([*existing_tickers, *tickers]))
        try:
            portfolio = await self.client.get_portfolio()
        except Exception as exc:
            self.logger.exception(
                "portfolio_fetch_failed: %s", type(exc).__name__, exc_info=False
            )
            portfolio = Portfolio(reconciliation_status="FAILED", errors=[str(exc)])

        try:
            quotes = await self.client.get_equity_quotes(deduped)
            quote_map = {item.ticker: item for item in quotes}
        except Exception as exc:
            self.logger.exception(
                "quote_fetch_failed: %s", type(exc).__name__, exc_info=False
            )
            quote_map = {}

        return {
            "portfolio": portfolio,
            "quotes": quote_map,
            "historicals": {},
            "timestamp": datetime.now(UTC).isoformat(),
        }
