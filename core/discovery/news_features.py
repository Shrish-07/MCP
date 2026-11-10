"""Persistent scored-story history, ticker-specific baseline and explicit coverage."""

import hashlib
import json
import math
import re
import statistics
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from utils.persistence import atomic_json


def story_keys(item):
    headline = re.sub(
        r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", item.headline.lower())
    ).strip()
    day = datetime.fromtimestamp(item.datetime, timezone.utc).date().isoformat()
    keys = {
        "headline:"
        + hashlib.sha256((headline + day + item.related.upper()).encode()).hexdigest()
    }
    if item.id:
        keys.add("provider:" + str(item.id))
    if item.url:
        u = urlsplit(item.url)
        keys.add(
            "url:"
            + urlunsplit((u.scheme, u.netloc.lower(), u.path.rstrip("/"), "", ""))
        )
    return keys


def deduplicate(items):
    seen = set()
    unique = []
    for item in sorted(items, key=lambda x: (x.datetime, x.id, x.headline)):
        keys = story_keys(item)
        if keys & seen:
            continue
        if not item.headline.strip() or item.datetime <= 0:
            raise ValueError("invalid headline")
        seen.update(keys)
        unique.append(item)
    return unique, len(items) - len(unique)


class NewsHistory:
    def __init__(self, path):
        self.path = Path(path)

    def update(
        self, ticker, items, scores, model_info, *, now=None, coverage_start=None
    ):
        now = now or datetime.now(timezone.utc)
        unique, dedup_count = deduplicate(items)
        if len(scores) != len(unique) or not all(
            isinstance(s, (int, float)) and math.isfinite(s) and -1 <= s <= 1
            for s in scores
        ):
            raise ValueError("incomplete/invalid sentiment inference")
        state = (
            json.loads(self.path.read_text()) if self.path.exists() else {"tickers": {}}
        )
        old = state["tickers"].get(ticker, {"stories": [], "coverage_start": None})
        merged = {s["story_id"]: s for s in old["stories"]}
        for item, score in zip(unique, scores):
            story_id = min(story_keys(item))
            merged[story_id] = {
                "story_id": story_id,
                "timestamp": datetime.fromtimestamp(
                    item.datetime, timezone.utc
                ).isoformat(),
                "score": float(score),
                "source": item.source,
                "model": model_info,
                "headline": item.headline,
            }
        stories = [
            s
            for s in merged.values()
            if now - timedelta(days=45) <= datetime.fromisoformat(s["timestamp"]) <= now
        ]
        start = coverage_start or old.get("coverage_start")
        if not start or datetime.fromisoformat(start) > now - timedelta(days=44):
            raise ValueError("insufficient historical coverage; fetch at least 44 days")
        if any(
            s["model"].get("model_name") != model_info.get("model_name")
            for s in stories
        ):
            raise ValueError("mixed sentiment model history; rescore window")
        state["tickers"][ticker] = {
            "stories": stories,
            "coverage_start": start,
            "last_fetch": now.isoformat(),
            "model": model_info,
        }
        atomic_json(self.path, state)
        current = [
            s
            for s in stories
            if datetime.fromisoformat(s["timestamp"]) >= now - timedelta(days=7)
        ]
        prior = [
            s
            for s in stories
            if now - timedelta(days=14)
            <= datetime.fromisoformat(s["timestamp"])
            < now - timedelta(days=7)
        ]
        baseline = [
            s
            for s in stories
            if now - timedelta(days=37)
            <= datetime.fromisoformat(s["timestamp"])
            < now - timedelta(days=7)
        ]
        if not current or not prior or not baseline:
            raise ValueError("sparse ticker sentiment/baseline unavailable")
        weights = [
            2
            if datetime.fromisoformat(s["timestamp"]) >= now - timedelta(days=3)
            else 1
            for s in current
        ]
        mean = sum(s["score"] * w for s, w in zip(current, weights)) / sum(weights)
        prior_mean = sum(s["score"] for s in prior) / len(prior)
        historical_deltas=[]
        for offset in range(1,31):
            end=now-timedelta(days=offset)
            a=[s for s in stories if end-timedelta(days=7)<=datetime.fromisoformat(s["timestamp"])<end]
            b=[s for s in stories if end-timedelta(days=14)<=datetime.fromisoformat(s["timestamp"])<end-timedelta(days=7)]
            if a and b:
                aw=[2 if datetime.fromisoformat(s["timestamp"])>=end-timedelta(days=3) else 1 for s in a]
                historical_deltas.append(sum(s["score"]*w for s,w in zip(a,aw))/sum(aw)-statistics.mean(s["score"] for s in b))
        delta_z=None
        if len(historical_deltas)>=10 and statistics.pstdev(historical_deltas)>1e-9:
            delta_z=((mean-prior_mean)-statistics.mean(historical_deltas))/statistics.pstdev(historical_deltas)
        return {
            "sentiment_mean": mean,
            "sentiment_delta": mean - prior_mean,
            "z_components":{"sentiment_delta":delta_z},
            "delta_standardization":{"reference":"ticker preceding30 daily seven-day deltas; overlapping windows", "samples":len(historical_deltas)},
            "news_velocity": (len(current) / 7) / (len(baseline) / 30),
            "recent_negative_tone": mean < 0,
            "distinct_headlines": len(current),
            "source_count": len({s["source"] for s in current}),
            "dedup_count": dedup_count,
            "baseline_headlines": len(baseline),
            "baseline_days": 30,
            "model": model_info,
            "fallback": model_info.get("model_name", "")
            != "AnkitAI/FinSense-ModernBERT-Financial-News-Sentiment-Analysis",
            "news_cutoff": now.isoformat(),
            "data_quality": "valid",
            "velocity_definition": "distinct headline count/day; current7 vs prior30",
        }


async def collect_news_features(history,fetcher,model,ticker,*,now=None):
    now=now or datetime.now(timezone.utc);start=now-timedelta(days=45)
    info=model.get_model_info()
    if not info.get("model_name"):raise ValueError("model identity unavailable")
    items=await fetcher.fetch_company_news(ticker,from_date=start.date(),to_date=now.date(),strict=True)
    unique,_=deduplicate(items)
    scores=await asyncio.to_thread(model.score_headlines,[n.headline for n in unique])
    return history.update(ticker,items,scores,info,now=now,coverage_start=start.isoformat())
