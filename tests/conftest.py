import pytest
from core.position.manager import PositionManager


@pytest.fixture
def pm(tmp_path):
    return PositionManager(str(tmp_path/"logs/positions.json"))


@pytest.fixture(autouse=True)
def broker_session_for_offline_tests(monkeypatch):
    # Execution tests explicitly model an open broker session. Calendar tests
    # exercise the actual NYSE function independently.
    monkeypatch.setattr("core.execution.engine.is_market_open_at",lambda:True)
