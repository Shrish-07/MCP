"""Provider accounting behavior exercised entirely with synthetic responses."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from utils.anthropic_client import AnthropicClient


def completion_fixture():
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=2,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=4,
        model_dump=lambda **_: {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 4,
        },
    )
    response = SimpleNamespace(
        id="fixture-response",
        model="claude-sonnet-5-5",
        usage=usage,
        content=[SimpleNamespace(type="text", text="fixture completion")],
    )
    client = AnthropicClient("claude-sonnet-5-5")
    client.cost_tracker = Mock()
    client.client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=response))
    )
    return client, response


@pytest.mark.asyncio
async def test_success_records_actual_model_usage_and_request_id():
    client, _ = completion_fixture()
    assert (
        await client.complete("fixture system", "fixture input") == "fixture completion"
    )
    client.client.messages.create.assert_awaited_once()
    assert client.cost_tracker.record.call_args.args[:3] == ("claude-sonnet-5-5", 10, 2)
    assert (
        client.cost_tracker.record.call_args.kwargs["request_id"] == "fixture-response"
    )
    client.cost_tracker.record_unpriced.assert_not_called()


@pytest.mark.asyncio
async def test_retry_accounts_for_failed_attempt_and_success_separately(monkeypatch):
    import utils.anthropic_client as module

    client, response = completion_fixture()
    client.client.messages.create.side_effect = [
        RuntimeError("rate limit fixture"),
        response,
    ]
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    assert (
        await client.complete("fixture system", "fixture input") == "fixture completion"
    )
    assert client.client.messages.create.await_count == 2
    client.cost_tracker.record_unpriced.assert_called_once()
    client.cost_tracker.record.assert_called_once()


@pytest.mark.asyncio
async def test_ledger_failure_does_not_repeat_completed_provider_request():
    client, _ = completion_fixture()
    client.cost_tracker.record.side_effect = OSError("fixture ledger unavailable")
    assert (
        await client.complete("fixture system", "fixture input") == "fixture completion"
    )
    client.client.messages.create.assert_awaited_once()
    client.cost_tracker.record_unpriced.assert_not_called()


@pytest.mark.asyncio
async def test_missing_usage_remains_unpriced():
    client, response = completion_fixture()
    response.usage = None
    await client.complete("fixture system", "fixture input")
    client.cost_tracker.record.assert_not_called()
    client.cost_tracker.record_unpriced.assert_called_once_with(
        "claude-sonnet-5-5", "usage unavailable", "fixture-response"
    )
