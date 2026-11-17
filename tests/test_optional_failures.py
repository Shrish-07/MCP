import sys
from datetime import UTC
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.data import sentiment
from utils.anthropic_client import AnthropicClient


def test_missing_model_key_does_not_block_backend_construction(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = AnthropicClient("claude-sonnet-5-5")
    assert client.client is None


@pytest.mark.parametrize("fallback", [False, True])
def test_primary_and_fallback_loaders_are_mocked(monkeypatch, fallback):
    calls = []

    class Tokenizer:
        @classmethod
        def from_pretrained(cls, name, **kwargs):
            assert kwargs["token"] is False
            assert kwargs["revision"] == sentiment.MODEL_REVISIONS[name]
            calls.append(name)
            if fallback and name == sentiment.PRIMARY_MODEL:
                raise RuntimeError("primary unavailable")
            return MagicMock()

    model = MagicMock()
    model.config.id2label = {0: "negative", 1: "neutral", 2: "positive"}
    model.config.label2id = {"negative": 0, "neutral": 1, "positive": 2}
    model.config._commit_hash = "fixture-revision"
    transformers = SimpleNamespace(
        AutoTokenizer=Tokenizer,
        AutoModelForSequenceClassification=SimpleNamespace(
            from_pretrained=lambda *a, **k: model
        ),
    )
    torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False),
        backends=SimpleNamespace(),
        device=lambda name: SimpleNamespace(type=name),
        float16="fp16",
        float32="fp32",
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.setattr(sentiment, "_HAS_ML_DEPS", True)
    monkeypatch.setattr(sentiment, "torch", None)
    monkeypatch.setattr(sentiment, "AutoTokenizer", None)
    monkeypatch.setattr(sentiment, "AutoModelForSequenceClassification", None)
    monkeypatch.setattr(sentiment.SentimentModel, "_instance", None)
    m = sentiment.SentimentModel()
    assert m.model_name == (
        sentiment.FALLBACK_MODEL if fallback else sentiment.PRIMARY_MODEL
    )
    assert m.get_model_info()["revision"] == "fixture-revision"
    assert calls == (
        [sentiment.PRIMARY_MODEL, sentiment.FALLBACK_MODEL]
        if fallback
        else [sentiment.PRIMARY_MODEL]
    )


def test_corrupt_health_file_does_not_disable_safety_startup(tmp_path):
    from utils.health import Health

    path = tmp_path / "health.json"
    path.write_text("{broken")
    health = Health(path)
    health.record("monitor", success=True)
    assert health.state["monitor"]["last_success"]
    assert path.with_suffix(".json.bak").read_text() == "{broken"


@pytest.mark.asyncio
async def test_nightly_model_failure_preserves_atr_refresh(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    from test_backend_safety import build

    from models.market_data import OHLCVBar
    from utils.market_calendar import _NYSE
    from utils.strategy_config import DEFAULTS

    b, c, pm, _q = build(tmp_path, monkeypatch)
    b.agent.config["exits"] = DEFAULTS["exits"]
    b.agent.config["sentiment_selection"] = {"enabled": True}
    now = datetime.now(UTC)
    sessions = _NYSE.schedule(
        start_date=(now - timedelta(days=70)).date(), end_date=now.date()
    )
    dates = [
        i
        for i, row in sessions.iterrows()
        if row["market_close"].to_pydatetime() <= now
    ]
    c.get_equity_historicals.return_value = [
        OHLCVBar(
            timestamp=d.date().isoformat(),
            open=100,
            high=102,
            low=98,
            close=100,
            volume=1e6,
        )
        for d in dates
    ]

    def unavailable():
        raise RuntimeError("optional model offline")

    monkeypatch.setattr(sentiment, "get_sentiment_model", unavailable)
    await b.nightly()
    assert pm.get_open_records()[0].atr14 == 4
    assert b.health.state["held_news:ABC"]["error"]
