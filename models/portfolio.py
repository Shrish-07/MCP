from __future__ import annotations

from pydantic import BaseModel, Field, ConfigDict, field_validator,model_validator
import re
from uuid import uuid4


class PositionRecord(BaseModel):
    """Persistent record of an opened position."""

    ticker: str = Field(..., min_length=1)
    entry_price: float = 0.0
    entry_date: str = ""
    entry_edge_score: float = 0.0
    stop_loss_pct: float = Field(-0.07,gt=-1,lt=0)
    take_profit_pct: float = 0.20
    quantity: float = 0.0
    order_id: str = ""
    reconciled: bool = False  # True = added by reconcile (not by our own fill);
                              # skips max-holding advisories until the next full
                              # cycle computes its vol-adjusted stop.
    model_config = ConfigDict(allow_inf_nan=False)
    schema_version: int = 2
    position_id: str = Field(default_factory=lambda: str(uuid4()))
    entry_date_known: bool = False
    broker_confirmed: bool = False
    verified_at: str | None = None
    peak_price: float = 0.0
    initial_entry_price: float | None = None
    peak_timestamp: str | None = None
    low_price: float | None = None
    history_complete: bool = False
    peak_reconstructed: bool = True
    initial_stop_px: float = 0.0
    trail_stop_px: float = 0.0
    effective_stop_px: float = 0.0
    stop_reason: str = "INITIAL_STOP"
    entry_gap_score: float | None = None
    entry_gap_z: float | None = None
    gap_reference: str | None = None
    gap_progress: float | None = None
    gap_inverted: bool = False
    trail_mode: str = "INITIAL_STOP"
    take_profit_review_state: str = "NOT_DUE"
    exit_state: str = "OPEN_INITIAL_STOP"
    last_trigger_ids: list[str] = Field(default_factory=list)
    review_events: dict = Field(default_factory=dict)
    trigger_memory: dict = Field(default_factory=dict)
    adds_count: int = 0
    last_add_timestamp: str | None = None
    entry_snapshot_id: str | None = None
    atr14: float | None = None
    atr_date: str | None = None
    atr_source: str | None = None
    quote_timestamp: str | None = None
    pending_exit_order_id: str | None = None
    thesis: str = ""
    invalidation: list[str] = Field(default_factory=list)
    thesis_invalidated: bool = False
    lots: list[dict] = Field(default_factory=list)

    @field_validator("ticker")
    @classmethod
    def valid_ticker(cls, value):
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", value):
            raise ValueError("invalid equity symbol")
        return value

    @field_validator("quantity", "entry_price")
    @classmethod
    def positive(cls, value):
        if value < 0:
            raise ValueError("position cost and quantity must be nonnegative")
        return value

    @model_validator(mode="after")
    def known_date_valid(self):
        if self.entry_date_known:
            from datetime import datetime
            datetime.fromisoformat(self.entry_date.replace("Z","+00:00"))
        return self


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
    reconciliation_status: str = "UNKNOWN"
    positions_complete: bool = False
    timestamp: str = ""
    errors: list[str] = Field(default_factory=list)


__all__ = ["Position", "Portfolio", "PositionRecord"]
