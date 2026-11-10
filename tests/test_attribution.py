from core.attribution import build_report
from test_p0_safety import decision, result


def test_attribution_partial_fills_and_unknown_costs(pm):
    key = pm.create_order(decision(), "buy", 2, 100)
    pm.update_order(key, result(qty=2))
    key = pm.create_order(decision("SELL"), "sell", 2, 110)
    pm.update_order(
        key,
        result("PARTIALLY_FILLED", qty=1, price=109, side="sell", order_id="sell-1"),
    )
    assert build_report(pm)["metrics"]["closed_positions"] == 0
    pm.update_order(key, result(qty=2, price=110, side="sell", order_id="sell-1"))
    r = build_report(pm)
    assert r["metrics"]["closed_positions"] == 1
    assert r["closed_positions"][0]["gross_realized_pnl"] == 20
    assert r["closed_positions"][0]["net_realized_pnl"] is None
    build_report(pm)  # export is idempotent
    assert len((pm._path.parent / "exit_ledger.jsonl").read_text().splitlines()) == 3


