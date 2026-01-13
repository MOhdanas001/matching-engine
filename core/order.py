import uuid
from datetime import datetime
from decimal import Decimal, getcontext, ROUND_DOWN
from dataclasses import dataclass
from typing import Optional
from pydantic import BaseModel, Field

# Decimal precision
getcontext().prec = 18
getcontext().rounding = ROUND_DOWN

def D(x) -> Decimal:
    """Helper to convert to Decimal safely."""
    return Decimal(str(x))

def now_iso():
    """Current UTC timestamp in ISO format."""
    return datetime.utcnow().isoformat() + "Z"

@dataclass
class Order:
    """Internal order object for matching engine."""
    id: str
    symbol: str
    side: str  # "buy" or "sell"
    order_type: str  # "market", "limit", "ioc", "fok", "stoploss"
    quantity: Decimal
    price: Optional[Decimal]
    remaining: Decimal
    timestamp: float
    created_at: str

    @classmethod
    def create(cls, symbol: str, side: str, order_type: str, quantity: Decimal, price: Optional[Decimal]):
        now = datetime.utcnow()
        return cls(
            id=str(uuid.uuid4()),
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            remaining=quantity,
            timestamp=now.timestamp(),
            created_at=now.isoformat() + "Z",
        )

class OrderSubmission(BaseModel):
    symbol: str = Field(..., example="BTC-USDT")
    order_type: str = Field(..., pattern="^(market|limit|ioc|fok|stoploss)$")
    side: str = Field(..., pattern="^(buy|sell)$")
    quantity: str = Field(..., example="0.5")
    price: Optional[str] = Field(None, example="30000")
