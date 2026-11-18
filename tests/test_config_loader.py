"""Tests for the central config loader — the fail-safe contract."""

from __future__ import annotations

import json

import pytest

from utils.config import _deep_merge, load_config


@pytest.mark.parametrize(
    ("requested", "dry_run"),
    [(False, False), (True, True), ("false", True), (0, True), (None, True)],
)
def test_only_explicit_false_requests_live_mode(tmp_path, requested, dry_run):
    path = tmp_path / "config/settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"execution": {"dry_run": requested}}), encoding="utf-8")
    cfg = load_config(root=tmp_path, include_secrets=False)
    assert cfg["execution"]["dry_run"] is dry_run
    assert cfg["env"]["DRY_RUN"] is dry_run
    assert cfg["execution"]["live_release_approved"] is False


def test_deep_merge_override_wins_and_keeps_extras():
    base = {"a": 1, "b": {"x": 1, "y": 2}}
    over = {"b": {"x": 9}, "c": 3}
    merged = _deep_merge(base, over)
    assert merged == {"a": 1, "b": {"x": 9, "y": 2}, "c": 3}


@pytest.mark.parametrize(
    "raw", ["{not json", '{"execution":{"dry_run":true,"dry_run":false}}']
)
def test_load_config_fail_safe(tmp_path, raw):
    """A malformed or ambiguous file uses safe defaults."""
    path = tmp_path / "config/settings.json"
    path.parent.mkdir()
    path.write_text(raw, encoding="utf-8")
    cfg = load_config(root=tmp_path, include_secrets=False)
    assert cfg["execution"]["dry_run"] is True
    assert cfg["env"]["DRY_RUN"] is True
    assert cfg["models"]["decision"] == "claude-sonnet-5-5"
    assert cfg["env"]["FINNHUB_API_KEY"] == ""  # empty if env absent
    assert "cost_limits" not in cfg  # enforcement is gone
    assert cfg["trading"]["market_hours"]["calendar"] == "NYSE"
    assert cfg["monitor"]["interval_seconds"] >= 5


def test_comments_are_excluded_from_runtime_and_revision(tmp_path):
    path = tmp_path / "config/settings.json"
    path.parent.mkdir()
    path.write_text('{"execution": {"dry_run": true}}', encoding="utf-8")
    before = load_config(root=tmp_path, include_secrets=False)
    path.write_text(
        '{"execution": {"// dry_run": "A note", "dry_run": true}}',
        encoding="utf-8",
    )
    assert load_config(root=tmp_path, include_secrets=False) == before
