import json
import pytest
from test_cost_tracker import _fresh_tracker
from utils.cost_tracker import reconcile_window


@pytest.mark.parametrize(
    "input_,write,read,output",
    [(100, 0, 0, 10), (0, 100, 0, 0), (0, 0, 100, 0), (0, 0, 0, 0), (50, 100, 200, 20)],
)
def test_disjoint_usage(input_, write, read, output):
    t = _fresh_tracker()
    actual = t.record("claude-sonnet-5-5", input_, output, write, read)
    assert actual == pytest.approx(
        (input_ * 2 + write * 2.5 + read * 0.1 + output * 10) / 1e6
    )


def test_one_hour_cache_actual_model_and_request_id():
    t = _fresh_tracker()
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_creation_input_tokens": 300,
        "cache_creation": {
            "ephemeral_1h_input_tokens": 200,
            "ephemeral_5m_input_tokens": 100,
        },
        "inference_geo": "us",
    }
    usd = t.record(
        "claude-sonnet-5-5",
        100,
        10,
        300,
        0,
        raw_usage=usage,
        request_id="msg-1",
        batch=True,
    )
    assert usd == pytest.approx((200 + 100 + 800 + 250) / 1e6 * 0.55)
    assert t.record("claude-sonnet-5-5", 100, 10, request_id="msg-1") is None
    records = [json.loads(line) for line in t._records_path().read_text().splitlines()]
    assert len(records) == 1 and records[0]["raw_usage"] == usage
    records[0]["timestamp"] = "2026-11-07T16:00:00+00:00"
    report = reconcile_window(
        records,
        {"request_ids": ["msg-1"], "usd": usd},
        "2026-11-07T00:00:00+00:00",
        "2026-11-08T00:00:00+00:00",
    )
    assert report["matches"]
    assert not reconcile_window(
        records,
        {"request_ids": [], "usd": usd},
        "2026-11-07T00:00:00+00:00",
        "2026-11-08T00:00:00+00:00",
    )["matches"]
