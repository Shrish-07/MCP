"""Versioned constituent source; stale universes cannot silently rank."""

import csv
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path

from models.portfolio import PositionRecord
from utils.persistence import atomic_json

SOURCE = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"


def parse_universe(text, *, as_of=None):
    rows = list(csv.DictReader(io.StringIO(text)))
    constituents = []
    for row in rows:
        if not all(
            isinstance(row.get(key), str) and row[key].strip()
            for key in ("Symbol", "Security", "GICS Sector")
        ):
            raise ValueError("invalid constituent source row")
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
        "as_of": as_of or datetime.now(UTC).isoformat(),
        "retrieved_at": datetime.now(UTC).isoformat(),
        "version": hashlib.sha256(text.encode()).hexdigest(),
        "constituents": sorted(constituents, key=lambda r: r["ticker"]),
    }


def load_universe(path, max_age_days=14, now=None):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        age = (
            (now or datetime.now(UTC)) - datetime.fromisoformat(value["as_of"])
        ).total_seconds() / 86400
        rows = value["constituents"]
        if not isinstance(rows, list):
            raise TypeError("expected constituent list")
        symbols = [PositionRecord.valid_ticker(row["ticker"]) for row in rows]
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid universe snapshot") from exc
    if (
        not 0 <= age <= max_age_days
        or not isinstance(value.get("source"), str)
        or not value["source"].strip()
        or not isinstance(value.get("version"), str)
        or not value["version"].strip()
    ):
        raise ValueError("universe stale or missing provenance")
    if len(set(symbols)) != len(symbols) or not 480 <= len(symbols) <= 520:
        raise ValueError("invalid universe")
    for row, symbol in zip(rows, symbols):
        row["ticker"] = symbol
    return value


async def refresh(path):
    import httpx

    async with httpx.AsyncClient() as client:
        response = await client.get(SOURCE, timeout=30)
        response.raise_for_status()
    value = parse_universe(response.text)
    atomic_json(Path(path), value)
    return value
