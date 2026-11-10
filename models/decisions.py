from __future__ import annotations

from enum import Enum
from uuid import uuid4
from pydantic import BaseModel, Field, ConfigDict, model_validator


class DecisionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    IGNORE = "IGNORE"
    ROTATE = "ROTATE"
    ADD_TO_POSITION = "ADD_TO_POSITION"


# Canonical success statuses — single definition, imported by main.py.
# Submissions and previews are not confirmed ownership or closure.
SUCCESS_STATUSES = ("FILLED", "EXECUTED")

# Canonical exit-review rubric — single definition, shared by
# core/decision/engine.py (cycles) and main.py (monitor path).
EXIT_REVIEW_RULES = (
    "EXIT REVIEW RULES:\n"
    "- You MUST state entry vs current and unrealized % in reasoning.\n"
    "- SELL needs explicit evidence: thesis broken, technical failure vs plan, or capital better used elsewhere.\n"
    "- For EXIT decisions, score edge as EXIT CONVICTION (not buy quality): "
    "catalyst_strength = strength of the exit reason (0-2), "
    "technical_confirmation = price action supports exit (0-2), "
    "portfolio_fit = value of freeing capital / cutting risk (0-1).\n"
    "- HOLD is valid when unrealized is small and thesis still intact, including holding through earnings when the opportunity is sound.\n"
    "- STOP_LOSS trigger remains forced SELL (existing code path).\n"
    "- Earnings proximity is informational only — never auto-SELL solely because earnings are near."
)


class CycleType(str, Enum):
    OPEN = "OPEN"
    MID = "MID"
    CLOSE = "CLOSE"


class StructuredEdgeScore(BaseModel):
    """Replaces the arbitrary 0-5 float. Each component is defined and bounded.

    Claude outputs all three. Code computes total. No faking.
    """

    model_config=ConfigDict(allow_inf_nan=False)
    catalyst_strength: float = Field(
        0.0,
        ge=0.0,
        le=2.0,
        description=(
            "Real news catalyst present and specific to this company? "
            "0=no catalyst, 1=minor/unclear, 2=clear strong catalyst"
        ),
    )
    technical_confirmation: float = Field(
        0.0,
        ge=0.0,
        le=2.0,
        description=(
            "Do price/volume metrics support the move? "
            "0=against trend, 1=neutral, 2=strong confirmation"
        ),
    )
    portfolio_fit: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Does this trade improve the portfolio? "
            "0=redundant/risky, 0.5=neutral, 1=clearly improves"
        ),
    )
    total: float = 0.0

    def compute_total(self) -> "StructuredEdgeScore":
        """Call after Claude fills the three components. Sets total in code."""
        self.total = round(
            min(self.catalyst_strength, 2.0)
            + min(self.technical_confirmation, 2.0)
            + min(self.portfolio_fit, 1.0),
            4,
        )
        return self


class JournalEntry(BaseModel):
    """One timestamped entry in a ticker's running journal."""

    timestamp: str
    cycle_type: str
    bull_thesis: str = ""
    bear_thesis: str = ""
    decision: str = ""
    decision_rationale: str = ""
    key_metrics: dict = Field(default_factory=dict)
    news_used: list[str] = Field(default_factory=list)
    fill_price: float | None = None
    unrealized_pnl_pct: float | None = None
    earnings_flag: bool = False
    rotated_from: str | None = None
    rotated_to: str | None = None
    event_id: str | None = None
    mode: str = "legacy_unknown"
    decision_id: str | None = None
    position_id: str | None = None
    order_id: str | None = None
    entry_price: float | None = None


class AddThesis(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True,allow_inf_nan=False)
    existing_position_confirmed: bool
    thesis_still_valid: bool
    validity_evidence: str = Field(min_length=20)
    new_evidence: str = Field(min_length=20)
    original_thesis: str = Field(min_length=20)
    incremental_thesis: str = Field(min_length=20)
    original_invalidation: list[str] = Field(min_length=1)
    incremental_invalidation: list[str] = Field(min_length=1)
    mispricing_catalyst: str = Field(min_length=20)
    why_market_may_be_wrong: str = Field(min_length=20)
    horizon_trading_sessions: int = Field(ge=2,le=60)
    base_return_pct: float
    upside_return_pct: float
    downside_return_pct: float
    risks_and_events: list[str] = Field(min_length=1)
    not_averaging_down_reason: str = Field(min_length=20)
    requested_increment_pct: float = Field(gt=0,le=1)
    incremental_risk_dollars: float = Field(ge=0)
    aggregate_risk_dollars: float = Field(ge=0)
    alternatives: dict[str,str]
    evidence_ids: list[str] = Field(min_length=1)
    data_timestamps: dict[str,str]

    @model_validator(mode="after")
    def coherent(self):
        if set(self.alternatives)!={"HOLD","SELL","ROTATE"} or any(len(v.strip())<10 for v in self.alternatives.values()):
            raise ValueError("HOLD/SELL/ROTATE comparisons required")
        if not self.downside_return_pct < self.base_return_pct < self.upside_return_pct or self.downside_return_pct>=0:
            raise ValueError("contradictory return scenarios")
        return self


class TradeDecision(BaseModel):
    decision_id: str = Field(default_factory=lambda: str(uuid4()))
    ticker: str = Field(..., min_length=1)
    bull_thesis: str = ""
    bear_thesis: str = ""
    failure_conditions: list[str] = Field(default_factory=list)
    decision: DecisionType
    edge: StructuredEdgeScore = Field(default_factory=StructuredEdgeScore)
    position_size_pct: float = 0.0
    action_type: str = "NONE"
    replaced_ticker: str | None = None
    rotate_from_ticker: str | None = None
    risk_notes: str = ""
    reasoning_summary: str = ""
    timestamp: str = ""
    cycle_type: str = ""
    evidence: dict = Field(default_factory=dict)
    add_thesis: AddThesis | None = None
    portfolio_review: dict[str,str] = Field(default_factory=dict)

    @property
    def edge_score(self) -> float:
        """Backwards-compatible accessor."""
        return self.edge.total


class ExecutionResult(BaseModel):
    ticker: str = Field(..., min_length=1)
    order_id: str = ""
    status: str = "SIMULATED"
    fill_price: float = 0.0
    quantity: float = 0.0
    timestamp: str = ""
    dry_run: bool = True
    side: str = ""
    filled_quantity: float | None = None
    fills: list[dict] = Field(default_factory=list)
    fees: float | None = None
    raw_order: dict = Field(default_factory=dict)
    decision_id: str = ""
    # Two-leg rotation metadata (populated when a ROTATE decision executed):
    rotate_from_ticker: str | None = None
    rotate_sell_price: float = 0.0
    rotate_sell_quantity: float = 0.0
    rotate_realized_pnl: float = 0.0


class ExitDecision(BaseModel):
    ticker: str = Field(..., min_length=1)
    decision: DecisionType
    reasoning_summary: str = ""
    edge_score: float = 0.0


__all__ = [
    "DecisionType",
    "CycleType",
    "StructuredEdgeScore",
    "JournalEntry",
    "TradeDecision",
    "ExecutionResult",
    "ExitDecision",
]
