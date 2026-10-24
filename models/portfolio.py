from __future__ import annotations

from pydantic import BaseModel, Field


class PositionRecord(BaseModel):
    """Persistent record of an opened position."""

    ticker: str = Field(..., min_length=1)
    entry_price: float = 0.0
    entry_date: str = ""
    entry_edge_score: float = 0.0
    stop_loss_pct: float = -0.07
    take_profit_pct: float = 0.20
    quantity: float = 0.0
    order_id: str = ""
    reconciled: bool = False  # True = added by reconcile (not by our own fill);
                              # skips max-holding advisories until the next full
                              # cycle computes its vol-adjusted stop.


class Position(BaseModel):
    """Current portfolio position."""

    ticker: str = Field(..., min_length=1)
    quantity: float = 0.0
    avg_cost: float = 0.0
    current_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl_pct: float = 0.0
    sector: str = ""


class Portfolio(BaseModel):
    """Portfolio snapshot returned by the broker interface."""

    account_id: str = ""
    total_value: float = 0.0
    buying_power: float = 0.0
    positions: list[Position] = Field(default_factory=list)
    cash_pct: float = 0.0


__all__ = ["Position", "Portfolio", "PositionRecord"]
