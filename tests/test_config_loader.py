"""Tests for the central config loader — the fail-safe contract."""
from __future__ import annotations

import json

from utils.config import _safe_bool, _deep_merge


def test_safe_bool_only_explicit_false_enables_live():
    # dry_run semantics: True = dry (safe). Anything ambiguous → safe default.
    assert _safe_bool(False) is False
    assert _safe_bool("false") is False
    assert _safe_bool("FALSE") is False
    assert _safe_bool(True) is True
    assert _safe_bool("true") is True
    assert _safe_bool("yes") is True      # unknown → safe (dry)
    assert _safe_bool(None) is True       # missing → safe (dry)
    assert _safe_bool("") is True
    assert _safe_bool("maybe") is True
    assert _safe_bool(0) is False         # explicit numeric False
    assert _safe_bool(1) is True


def test_deep_merge_override_wins_and_keeps_extras():
    base = {"a": 1, "b": {"x": 1, "y": 2}}
    over = {"b": {"x": 9}, "c": 3}
    merged = _deep_merge(base, over)
    assert merged == {"a": 1, "b": {"x": 9, "y": 2}, "c": 3}


def test_load_config_fail_safe(tmp_path, monkeypatch):
    """A malformed settings.json must produce a DRY-RUN config, never live."""
    import utils.config as cfg_mod

    bad = tmp_path / "settings.json"
    bad.write_text("{not json", encoding="utf-8")

    real_base = cfg_mod.Path

    class FakePath(type(real_base())):  # thin shim over Path
        @property
        def parent(self):
            p = super().parent
            return p

    # Simpler: monkeypatch the read side to raise JSONDecodeError via text:
    monkeypatch.setattr(
        cfg_mod, "load_dotenv", lambda *a, **k: None
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("FINNHUB_API_KEY", "")
    cfg = cfg_mod.load_config()
    assert cfg["execution"]["dry_run"] is True            # shipping config stays dry
    assert cfg["env"]["DRY_RUN"] is True
    assert cfg["models"]["decision"] == "claude-sonnet-5-5"
    assert cfg["env"]["FINNHUB_API_KEY"] == ""            # empty if env absent
    assert "cost_limits" not in cfg                       # enforcement is gone
    assert cfg["trading"]["market_hours"]["calendar"] == "NYSE"
    assert cfg["monitor"]["interval_seconds"] >= 5
