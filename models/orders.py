"""Validated persisted order envelope, separate from requested decisions."""
from typing import Literal
from pydantic import BaseModel,Field,ConfigDict,model_validator


class OrderRecord(BaseModel):
    model_config=ConfigDict(allow_inf_nan=False)
    local_id: str=Field(min_length=1)
    broker_id: str=""
    ticker: str=Field(min_length=1)
    side: Literal["buy","sell"]
    quantity: float=Field(gt=0)
    filled_quantity: float=Field(ge=0)
    filled_notional: float=Field(ge=0)
    state: Literal["CREATED","SUBMITTED","PARTIALLY_FILLED","FILLED","CANCEL_REQUESTED","CANCELLED","REJECTED","EXPIRED","UNKNOWN_REQUIRES_RECONCILIATION"]
    decision: dict
    timestamp: str
    mode: Literal["live","paper"]

    @model_validator(mode="after")
    def coherent(self):
        if self.filled_quantity>self.quantity+1e-6:raise ValueError("order overfill")
        if self.state=="FILLED" and abs(self.filled_quantity-self.quantity)>1e-6:raise ValueError("filled order lacks quantity")
        if not self.broker_id and self.state not in ("CREATED","UNKNOWN_REQUIRES_RECONCILIATION","REJECTED"):
            raise ValueError("broker order ID missing")
        return self
