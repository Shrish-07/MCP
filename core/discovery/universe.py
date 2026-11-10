"""Versioned constituent source; stale universes cannot silently rank."""

import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from models.portfolio import PositionRecord
from utils.persistence import atomic_json

SOURCE = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"


def parse_universe(text, *, as_of=None):
    rows = list(csv.DictReader(io.StringIO(text)))
    constituents = []
    for row in rows:
        ticker = PositionRecord.valid_ticker(row["Symbol"])
        constituents.append(
            {"ticker": ticker, "name": row["Security"], "sector": row["GICS Sector"]}
        )
    symbols = [r["ticker"] for r in constituents]
    if not 480 <= len(set(symbols)) <= 520 or len(symbols) != len(set(symbols)):
        raise ValueError("unexpected S&P constituent count/duplicates")
    return {
        "source": SOURCE,
        "upstream_source": "Wikipedia S&P 500 constituents via datasets repository",
        "as_of": as_of or datetime.now(timezone.utc).isoformat(),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "version": hashlib.sha256(text.encode()).hexdigest(),
        "constituents": sorted(constituents, key=lambda r: r["ticker"]),
    }


def load_universe(path, max_age_days=14, now=None):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    age = (
        (now or datetime.now(timezone.utc)) - datetime.fromisoformat(value["as_of"])
    ).total_seconds() / 86400
    if (
        not 0 <= age <= max_age_days
        or not value.get("source")
        or not value.get("version")
    ):
        raise ValueError("universe stale or missing provenance")
    symbols = [PositionRecord.valid_ticker(r["ticker"]) for r in value["constituents"]]
    if len(set(symbols)) != len(symbols) or not 480 <= len(symbols) <= 520:
        raise ValueError("invalid universe")
    return value


async def refresh(path):
    import httpx

    async with httpx.AsyncClient() as client:
        response = await client.get(SOURCE, timeout=30)
        response.raise_for_status()
    value = parse_universe(response.text)
    atomic_json(Path(path), value)
    return value
