from datetime import datetime,timezone
from unittest.mock import AsyncMock
import pytest
from test_p0_safety import portfolio,decision,result
from core.execution.engine import ExecutionEngine
from utils.strategy_config import validate,DEFAULTS
from utils.config import _deep_merge,_DEFAULTS


@pytest.mark.asyncio
async def test_manual_stop_and_closed_market_refuse_live_orders(pm,monkeypatch):
    config={"env":{"DRY_RUN":False},"execution":{"live_release_approved":True}}
    pm.reconcile_with_mcp(portfolio());pm.orders_reconciled=True
    c=AsyncMock();e=ExecutionEngine(c,pm,config)
    kwargs={"dry_run":False,"avg_volume_30d":1e6,"quote_timestamp":datetime.now(timezone.utc).isoformat()}
    stop=pm._path.parent.parent/"data/STOP_TRADING";stop.parent.mkdir(exist_ok=True);stop.touch()
    assert (await e.execute(decision(),portfolio(),100,**kwargs)).status=="REJECTED_MANUAL_STOP"
    stop.unlink();monkeypatch.setattr("core.execution.engine.is_market_open_at",lambda:False)
    assert (await e.execute(decision(),portfolio(),100,**kwargs)).status=="REJECTED_MARKET_CLOSED"
    c.place_equity_order.assert_not_called()


def test_missing_calendar_fails_closed(monkeypatch):
    import utils.market_calendar as calendar
    monkeypatch.setattr(calendar,"_HAS_CALENDAR",False)
    assert not calendar.is_market_open_at(datetime(2026,11,6,16,tzinfo=timezone.utc))


def test_nyse_holiday_and_half_day():
    from utils.market_calendar import is_market_open_at
    assert not is_market_open_at(datetime(2026,1,1,16,tzinfo=timezone.utc))
    assert not is_market_open_at(datetime(2026,11,26,16,tzinfo=timezone.utc))
    assert is_market_open_at(datetime(2026,11,27,17,59,tzinfo=timezone.utc))
    assert not is_market_open_at(datetime(2026,11,27,18,1,tzinfo=timezone.utc))


def test_invalid_strategy_config_rejected():
    settings=_deep_merge(_DEFAULTS,DEFAULTS)
    assert not validate(settings)
    settings["trading"]["add_to_position"]["max_incremental_risk_fraction_of_equity"]=-1
    assert validate(settings)


def test_nonfinite_model_scores_not_promoted_to_conviction():
    from test_parsing import _engine
    engine=_engine()
    assert engine._parse_edge({"edge_score":float("nan")},"ABC").total==0
    assert engine._parse_edge({"edge":{"catalyst_strength":float("inf")}},"ABC").total==0


def test_missing_broker_order_id_never_becomes_a_fill(pm):
    key=pm.create_order(decision(),"buy",2,100)
    order=pm.update_order(key,result(order_id=""))
    assert order["state"]=="UNKNOWN_REQUIRES_RECONCILIATION"
    assert pm.get_open_records()==[]
