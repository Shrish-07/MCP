import json,logging
from unittest.mock import AsyncMock
import pytest
from robinhood_mcp.robinhood_client import RobinhoodMCPClient,MCPError,AccountResolutionError


def client(payload):
    c=RobinhoodMCPClient.__new__(RobinhoodMCPClient)
    c.logger=logging.getLogger("test.mcp")
    c._get_account_number=AsyncMock(return_value="fixture")
    c._call_tool_with_retry=AsyncMock(return_value=[{"type":"text","text":json.dumps(payload)}])
    return c


@pytest.mark.asyncio
async def test_empty_positions_explicit_success():
    c=client({"data":{"results":[]}})
    assert await c.get_equity_positions()==[]


@pytest.mark.parametrize("payload",[{},None,{"data":{}},{"data":{"results":"invalid"}},{"data":{"results":[],"next":"page2"}}])
@pytest.mark.asyncio
async def test_unknown_positions_cannot_be_empty(payload):
    with pytest.raises(MCPError):await client(payload).get_equity_positions()


@pytest.mark.asyncio
async def test_bad_row_preserves_valid_but_marks_incomplete():
    c=client({"data":{"results":[{"symbol":"ABC","quantity":"1.5","average_buy_price":"100"},
        {"symbol":"BAD","quantity":"broken","average_buy_price":"100"}]}})
    with pytest.raises(MCPError):await c.get_equity_positions()
    assert c._last_valid_position_rows[0].ticker=="ABC"


@pytest.mark.asyncio
async def test_order_submission_does_not_use_limit_price_as_fill():
    c=client({"data":{"order":{"id":"id-1","state":"queued","price":"100"}}})
    r=await c.place_equity_order("ABC","buy",2,dry_run=False)
    assert r.status=="SUBMITTED" and r.fill_price==0 and r.filled_quantity is None


@pytest.mark.asyncio
async def test_orders_unknown_envelope_refused():
    with pytest.raises(MCPError):await client({"data":{}}).get_equity_orders()


@pytest.mark.asyncio
@pytest.mark.parametrize("accounts", [[], [{"is_default": True, "account_number": "private"}],
    [{"agentic_allowed": True, "account_number": ""}],
    [{"agentic_allowed": True, "account_number": n} for n in ("one", "two")]])
async def test_invalid_account_never_reaches_order_transport(accounts):
    c = client({"data": {"accounts": accounts}})
    del c._get_account_number
    with pytest.raises(AccountResolutionError):
        await c.place_equity_order("ABC", "sell", 1, dry_run=False)
    c._call_tool_with_retry.assert_awaited_once_with("get_accounts", {})


@pytest.mark.asyncio
async def test_only_explicit_agentic_account_is_selected():
    c = client({"data": {"accounts": [{"is_default": True, "account_number": "other"},
        {"agentic_allowed": True, "account_number": "agentic"}]}})
    del c._get_account_number
    assert await c._get_account_number() == "agentic"


@pytest.mark.asyncio
async def test_paginated_order_history_fails_closed():
    c = client({"data": {"results": [], "next_cursor": "fixture-continuation"}})
    c._verify_order_cursor_contract = AsyncMock(side_effect=MCPError("schema unavailable"))
    with pytest.raises(MCPError):
        await c.get_equity_orders()
    c._call_tool_with_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_order_pagination_only_relays_verified_cursor():
    c = client({})
    c._verify_order_cursor_contract = AsyncMock()
    pages = [
        {"data": {"orders": [], "next": "https://example.invalid/path?cursor=page-2"}},
        {"data": {"orders": [{"id": "fixture-id", "symbol": "ABC", "side": "buy",
                              "state": "queued", "quantity": "2"}]}},
    ]
    c._call_tool_with_retry.side_effect = [
        [{"type": "text", "text": json.dumps(page)}] for page in pages
    ]
    orders = await c.get_equity_orders()
    assert [order.order_id for order in orders] == ["fixture-id"]
    c._verify_order_cursor_contract.assert_awaited_once()
    assert c._call_tool_with_retry.await_args.args == (
        "get_equity_orders", {"account_number": "fixture", "cursor": "page-2"}
    )


def test_direct_trace_drops_bare_secrets():
    from robinhood_mcp.response_recorder import _build_direct_mcp_record
    record = _build_direct_mcp_record(tool_name="get_accounts", arguments={"nested": {"token": "fixture-secret"}},
        duration_ms=1, attempt=1, success=False,
        response_content=[{"text": '{"account_number": "fixture-account"}'}],
        exception_info={"exception_class": "TimeoutError", "exception_traceback": "fixture-secret"})
    encoded = json.dumps(record)
    assert "fixture-secret" not in encoded and "fixture-account" not in encoded
    assert record["exception"]["exception_class"] == "TimeoutError"
