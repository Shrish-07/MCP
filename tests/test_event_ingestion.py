from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from core.discovery.catalysts import refresh_catalysts
from core.exits.pead_ingestion import build_pead_snapshot


@pytest.mark.asyncio
async def test_catalog_ingests_only_verified_reported_earnings(tmp_path):
    broker = AsyncMock()
    broker.get_earnings_calendar_window.return_value = {
        "data": {
            "results": [
                {
                    "symbol": "ABC",
                    "report": {"date": "2026-11-09", "verified": True},
                    "eps": {"actual": "1.12", "estimate": "1.05"},
                },
                {
                    "symbol": "DEF",
                    "report": {"date": "2026-11-09", "verified": True},
                    "eps": {"actual": "nan", "estimate": "1.05"},
                },
            ]
        }
    }
    rows = await refresh_catalysts(
        broker,
        tmp_path / "catalyst_events.json",
        {"ABC", "DEF"},
        now=datetime(2026, 11, 10, 22, tzinfo=timezone.utc),
    )
    assert set(rows) == {"ABC"}
    assert rows["ABC"]["timestamp_basis"] == "observation_time"


def test_pead_import_aligns_closes_and_rejects_post_release_consensus():
    release = "2026-11-06T21:05:00+00:00"
    event = {
        "ticker": "ABC",
        "actual_eps": 2,
        "consensus_eps": 1.5,
        "release_timestamp": release,
        "consensus_timestamp": "2026-11-06T17:00:00+00:00",
        "stock_adjusted_close": {
            "2026-11-06": 100,
            "2026-11-09": 110,
            "2026-11-10": 112,
        },
        "benchmark_adjusted_close": {
            "2026-11-06": 100,
            "2026-11-09": 105,
            "2026-11-10": 106,
        },
    }
    cutoff = datetime(2026, 11, 10, 22, tzinfo=timezone.utc)
    data = build_pead_snapshot(
        {
            "source": "fixture",
            "data_cutoff": cutoff.isoformat(),
            "events": [event, dict(event, ticker="DEF", consensus_timestamp=release)],
        },
        now=cutoff,
    )
    assert data["rejected_rows"] == 1
    assert data["history"] == []
    assert data["current"]["ABC"]["car_by_session"]["2"] == pytest.approx(0.06)
