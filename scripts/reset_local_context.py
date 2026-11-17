"""Verify the empty Agentic account and optionally apply the authorized local reset.

This command permits broker reads only and never renews credentials.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import math
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import robinhood_mcp.robinhood_client as broker
from utils.state_reset import reset_local_state


def _number(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("Invalid balance")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Invalid balance")
    return number


async def inspect_account(client, cache, requests):
    account = await client._get_account_number()
    portfolio = await client.get_portfolio()
    if portfolio.account_id != hashlib.sha256(account.encode()).hexdigest():
        raise ValueError("Account fingerprint mismatch")
    if portfolio.reconciliation_status != "SUCCESS" or not portfolio.positions_complete:
        raise ValueError("Incomplete holdings")
    raw = client._extract_json_payload(
        cache[
            ("get_portfolio", json.dumps({"account_number": account}, sort_keys=True))
        ]
    )
    data = raw.get("data", raw)
    names = (
        "cash",
        "total_value",
        "equity_value",
        "pending_deposits",
        "options_value",
        "futures_value",
        "event_contracts_value",
        "crypto_value",
        "mutual_funds_value",
        "fixed_income_value",
        "withdrawable_funds",
        "unsettled_cash",
        "unsettled_funds",
        "margin_balance",
        "debit_balance",
    )
    balances = {name: _number(data.get(name)) for name in names}
    balances["buying_power"] = portfolio.buying_power
    buying_power = data.get("buying_power")
    balances["unleveraged_buying_power"] = (
        _number(buying_power.get("unleveraged_buying_power"))
        if isinstance(buying_power, dict)
        else None
    )
    summary = {
        "unique_eligible_account": True,
        "account_fingerprint_consistent": True,
        "holdings_complete": True,
        "holding_count": len(portfolio.positions),
        "holding_quantity": sum(row.quantity for row in portfolio.positions),
        "holding_market_value": sum(row.market_value for row in portfolio.positions),
        "balances": balances,
    }
    if portfolio.positions or any(
        value != 0 for value in balances.values() if value is not None
    ):
        return (
            portfolio,
            [],
            dict(summary, empty=False, reason="Nonzero broker account"),
        )
    orders = await client.get_equity_orders()
    terminal = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
    consistent = True
    for order in orders:
        filled = order.filled_quantity
        consistent = (
            consistent and filled is not None and 0 <= filled <= order.quantity + 1e-8
        )
        if order.status == "FILLED":
            consistent = (
                consistent
                and filled is not None
                and abs(filled - order.quantity) <= 1e-8
            )
        if filled:
            consistent = (
                consistent
                and bool(order.fills)
                and abs(sum(float(fill["quantity"]) for fill in order.fills) - filled)
                <= 1e-8
            )
    active = [order for order in orders if order.status not in terminal]
    summary.update(
        open_orders=len(active),
        pending_orders=sum(
            order.status
            in {"CREATED", "SUBMITTED", "PARTIALLY_FILLED", "CANCEL_REQUESTED"}
            for order in active
        ),
        unresolved_orders=sum(
            order.status == "UNKNOWN_REQUIRES_RECONCILIATION" for order in active
        ),
        historical_terminal_orders=len(orders) - len(active),
        order_history_complete=True,
        order_ids_present=all(bool(order.order_id) for order in orders),
        execution_ids_present=all(
            bool(fill.get("id")) for order in orders for fill in order.fills
        ),
        fill_quantities_consistent=consistent,
        empty=not active
        and consistent
        and portfolio.cash == portfolio.total_value == portfolio.buying_power == 0,
    )
    summary["requests"] = dict(requests)
    return portfolio, orders, summary


async def run(root, *, apply=False):
    client = broker.RobinhoodMCPClient()
    client.max_retries = 1
    cache, requests = {}, {}
    original = client._call_tool_with_retry

    async def forbidden(*_args, **_kwargs):
        raise broker.AuthenticationError(
            "Existing authentication requires owner renewal"
        )

    async def no_trace(**_kwargs):
        return None

    async def read_only(name, arguments):
        if name not in {
            "get_accounts",
            "get_portfolio",
            "get_equity_positions",
            "get_equity_orders",
        }:
            raise RuntimeError("Broker mutation or unrelated request prohibited")
        key = (name, json.dumps(arguments, sort_keys=True))
        if key not in cache:
            requests[name] = requests.get(name, 0) + 1
            cache[key] = await original(name, arguments)
        return cache[key]

    with (
        patch.object(client, "_refresh_access_token", forbidden),
        patch.object(client, "_register_client", forbidden),
        patch.object(client, "_run_browser_auth_flow", forbidden),
        patch.object(
            client,
            "_save_token_cache",
            side_effect=RuntimeError("Credential writes prohibited"),
        ),
        patch.object(client, "_call_tool_with_retry", read_only),
        patch.object(broker, "capture_direct_mcp_call", no_trace),
    ):
        portfolio, orders, before = await inspect_account(client, cache, requests)
        report = {"before": before, "broker_mutations": 0, "credential_writes": 0}
        if not before["empty"]:
            return dict(report, status="ACCOUNT_DISCREPANCY")
        if not apply:
            return dict(report, status="VERIFIED_EMPTY")
        report["reset"] = reset_local_state(root, portfolio, orders)
        cache.clear()
        _, _, after = await inspect_account(client, cache, requests)
        report["after"] = after
        report["status"] = "RESET_VERIFIED" if after["empty"] else "ACCOUNT_DISCREPANCY"
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Explicitly erase legacy local strategy context after verifying the empty account",
    )
    parser.add_argument(
        "--output", type=Path, help="Write a sanitized verification report"
    )
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        report = asyncio.run(run(Path(__file__).resolve().parents[1], apply=args.apply))
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        report = {"status": "BLOCKED", "error_type": type(error).__name__}
    if args.output:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    raise SystemExit(
        0 if report["status"] in {"VERIFIED_EMPTY", "RESET_VERIFIED"} else 1
    )
