from datetime import datetime, timezone
from unittest.mock import AsyncMock
import pytest
from models.decisions import AddThesis, ExecutionResult
from core.execution.engine import ExecutionEngine
from test_p0_safety import seed, portfolio, decision


def defended():
    stamp = datetime.now(timezone.utc).isoformat()
    return AddThesis(
        existing_position_confirmed=True,
        thesis_still_valid=True,
        validity_evidence="Verified contract revenue remains intact.",
        new_evidence="New contract milestone exceeded measured estimates.",
        original_thesis="Original measured contract growth thesis.",
        incremental_thesis="New milestone improves the incremental expected return.",
        original_invalidation=["Contract cancellation"],
        incremental_invalidation=["Milestone reversal"],
        mispricing_catalyst="A verified milestone with remaining delivery upside.",
        why_market_may_be_wrong="The measured milestone may not yet be priced in.",
        horizon_trading_sessions=10,
        base_return_pct=0.08,
        upside_return_pct=0.15,
        downside_return_pct=-0.06,
        risks_and_events=["Earnings gap"],
        not_averaging_down_reason="New evidence improves expected business performance, independent of price.",
        requested_increment_pct=0.25,
        incremental_risk_dollars=3.5,
        aggregate_risk_dollars=17.5,
        alternatives={
            "HOLD": "Hold retains the existing exposure.",
            "SELL": "No measured invalidation has occurred.",
            "ROTATE": "Alternatives have less confirmed evidence.",
        },
        evidence_ids=["fact-1"],
        data_timestamps={"quote": stamp, "features": stamp},
    )


def setup(pm):
    seed(pm)
    pm.orders_reconciled = True
    rec = pm.get_open_records()[0]
    rec.thesis = "Original measured contract growth thesis."
    rec.invalidation = ["Contract cancellation"]
    pm.update_record(rec)
    d = decision("ADD_TO_POSITION")
    d.add_thesis = defended()
    d.evidence = {
        "evidence_ids": ["fact-1"],
        "incremental_opportunity_confirmed": True,
        "sector": "Technology",
    }
    d.evidence.update(
        quote_timestamp=d.add_thesis.data_timestamps["quote"],
        feature_timestamp=d.add_thesis.data_timestamps["features"],
    )
    c = AsyncMock()
    c.get_equity_tradability.return_value = {"tradeable": True}
    return ExecutionEngine(c, pm, {"env": {"DRY_RUN": True}}), d


@pytest.mark.asyncio
async def test_defended_add_is_sized_within_caps(pm):
    e, d = setup(pm)
    r = await e.execute(d, portfolio(), 100, True, vol_30d=20, avg_volume_30d=1e6)
    assert r.status == "WOULD_EXECUTE" and r.quantity == 0.5
    assert pm.get_open_records()[0].quantity == 2


@pytest.mark.parametrize(
    "change,reason",
    [
        ("missing", "THESIS"),
        ("stale", "STALE_DATA"),
        ("unsupported", "UNSUPPORTED_EVIDENCE"),
        ("invalidated", "EXIT_PRIORITY"),
        ("crossed", "STOP_CONFLICT"),
        ("count", "COUNT"),
        ("cooldown", "COOLDOWN"),
    ],
)
@pytest.mark.asyncio
async def test_bad_add_rejected(pm, change, reason):
    e, d = setup(pm)
    r = pm.get_open_records()[0]
    if change == "missing":
        d.add_thesis = None
    if change == "stale":
        d.add_thesis.data_timestamps["quote"] = "2000-01-01T00:00:00+00:00"
    if change == "unsupported":
        d.evidence["evidence_ids"] = []
    if change == "invalidated":
        r.thesis_invalidated = True
    if change == "crossed":
        r.effective_stop_px = 101
    if change == "count":
        r.adds_count = 1
    if change == "cooldown":
        r.last_add_timestamp = datetime.now(timezone.utc).isoformat()
    pm.update_record(r)
    result = await e.execute(d, portfolio(), 100, True, avg_volume_30d=1e6)
    assert result.status == "REJECTED_ADD_" + reason


def test_add_partial_fills_count_once_and_preserve_anchor(pm):
    e, d = setup(pm)
    r = pm.get_open_records()[0]
    r.peak_price = 120
    r.trail_stop_px = 110
    r.effective_stop_px = 110
    r.entry_gap_z = 1.5
    pm.update_record(r)
    key = pm.create_order(d, "buy", 0.5, 115)
    for qty, status in [(0.2, "PARTIALLY_FILLED"), (0.5, "FILLED")]:
        pm.update_order(
            key,
            ExecutionResult(
                ticker="ABC",
                order_id="add",
                side="buy",
                status=status,
                quantity=0.5,
                filled_quantity=qty,
                fill_price=115,
                dry_run=False,
            ),
        )
    r = pm.get_open_records()[0]
    assert r.adds_count == 1 and r.quantity == 2.5 and r.entry_price == 103
    assert r.entry_gap_z == 1.5 and r.peak_price == 120 and r.effective_stop_px >= 110
