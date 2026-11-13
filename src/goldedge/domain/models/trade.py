from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

class ExitReason(str, Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    MANUAL_CLOSE = "MANUAL_CLOSE"
    SYSTEM_CLOSE = "SYSTEM_CLOSE"
    SESSION_CLOSE = "SESSION_CLOSE"
    # Aliases used by ManagePositionUseCase
    SL = "STOP_LOSS"
    TARGET = "TAKE_PROFIT"
    TIMEOUT = "TIMEOUT"

class TradeStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"

@dataclass(frozen=True)
class Position:
    ticket: int
    symbol: str
    type: str  # BUY or SELL
    volume: float
    price_open: float
    price_sl: float
    price_tp: float
    time_setup: datetime
    status: TradeStatus
    
    time_execution: Optional[datetime] = None
    time_closing: Optional[datetime] = None
    price_close: Optional[float] = None
    commission: float = 0.0
    swap: float = 0.0
    profit: Optional[float] = None
    exit_reason: Optional[ExitReason] = None
    ml_score: Optional[float] = None
    risk_profile: Optional[str] = None
