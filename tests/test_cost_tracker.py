"""Cost tracker: accuracy contract (Anthropic-formula parity) + JSONL append."""
from __future__ import annotations

import json

from utils.cost_tracker import CostTracker


def _fresh_tracker() -> CostTracker:
    CostTracker._instance = None
    t = CostTracker()
    # Point storage at a scratch dir via the _logs_dir hook
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    t._summary_path = lambda: tmp / "cost_tracker.json"  # type: ignore
    t._records_path = lambda: tmp / "cost_records.jsonl"  # type: ignore
    t._daily_totals.clear()
    t._monthly_totals.clear()
    return t


def test_exact_dashboard_formula():
    t = _fresh_tracker()
    t.set_pricing({"claude-sonnet-5-5": {"input": 3.0, "cache_write": 3.75, "cache_read": 0.3, "output": 15.0}})
    # input=1000 with 600 cache_create + 200 cache_read → regular input 200
    usd = t.record("claude-sonnet-5-5", input_tokens=1000, output_tokens=500,
                   cache_creation_tokens=600, cache_read_tokens=200)
    expected = (200 * 3.0 + 600 * 3.75 + 200 * 0.3 + 500 * 15.0) / 1_000_000
    assert abs(usd - expected) < 1e-12
    # record file is append-only JSONL
    lines = t._records_path().read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["regular_input_tokens"] == 200
    assert abs(rec["estimated_usd"] - expected) < 1e-12
    # summary holds aggregates only
    summary = json.loads(t._summary_path().read_text(encoding="utf-8"))
    assert summary["daily_totals"]
    assert "records" not in summary or not summary.get("records")


def test_unknown_model_falls_back_to_star_rate():
    t = _fresh_tracker()
    t.set_pricing({"claude-sonnet-5-5": {"input": 3.0, "cache_write": 3.75, "cache_read": 0.3, "output": 15.0}})
    usd = t.record("claude-mystery-future-9", input_tokens=1_000_000, output_tokens=0)
    assert abs(usd - 3.0) < 1e-9  # falls to "*" rate (sonnet table)


def test_no_enforcement_methods_exist():
    t = _fresh_tracker()
    assert not hasattr(t, "check_limits")
    # daily/monthly totals remain for display
    assert t.daily_total() >= 0.0
    assert t.monthly_total() >= 0.0
