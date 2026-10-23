"""Parsing fixes: edge clamping (no more silent zeroing) and
case-insensitive decision parsing in the DecisionEngine."""
from __future__ import annotations

from unittest.mock import MagicMock

from core.decision_engine import DecisionEngine
from models.decisions import DecisionType


def _engine() -> DecisionEngine:
    client = MagicMock()
    journal = MagicMock()
    journal.summarise_for_prompt.return_value = ""
    earnings = MagicMock()
    return DecisionEngine(client, journal, earnings, settings={
        "trading": {"edge_score_threshold": 3.0, "max_positions": 12},
        "news": {}, "sentiment_selection": {},
    })


def test_edge_clamps_out_of_range_components():
    eng = _engine()
    # catalyst_strength 2.5 was previously SILENTLY zeroed (Pydantic reject)
    e = eng._parse_edge({"edge": {"catalyst_strength": 2.5,
                                  "technical_confirmation": 1.0,
                                  "portfolio_fit": 2.0}}, "NVDA")
    assert e.catalyst_strength == 2.0
    assert e.portfolio_fit == 1.0
    assert e.total == 4.0  # survives instead of dying as 0.0 (2.0+1.0+1.0)


def test_edge_legacy_total_clamped_to_five():
    eng = _engine()
    e = eng._parse_edge({"edge_score": 9.9}, "MSFT")
    assert e.total == 5.0


def test_edge_missing_defaults_to_zero():
    eng = _engine()
    e = eng._parse_edge({}, "AAPL")
    assert e.total == 0.0


def test_decision_parsing_case_insensitive():
    eng = _engine()
    assert eng._parse_decision("buy", DecisionType.IGNORE) == DecisionType.BUY
    assert eng._parse_decision("Sell", DecisionType.IGNORE) == DecisionType.SELL
    assert eng._parse_decision(" ROTATE ", DecisionType.IGNORE) == DecisionType.ROTATE
    assert eng._parse_decision("nonsense", DecisionType.IGNORE) == DecisionType.IGNORE
    assert eng._parse_decision(None, DecisionType.HOLD) == DecisionType.HOLD


def test_strip_fences_single_source():
    from utils.anthropic_client import strip_fences
    from core.decision_engine import DecisionEngine as DE
    assert strip_fences("```json\n{\"a\":1}\n```") == '{"a":1}'
    assert DE._strip_fences("```json\n{\"a\":1}\n```") == '{"a":1}'  # same behaviour
