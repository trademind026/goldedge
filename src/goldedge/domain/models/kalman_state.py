from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

@dataclass
class KalmanState:
    """
    Represents the real-time state of an open trade being managed by the Kalman Exit Agent.
    Tracks peak profitability, stagnation time, and dynamic stop loss adjustments.
    """
    trade_id: str
    symbol: str
    direction: str  # "CALL" or "PUT" (or "BUY" / "SELL" for Forex)
    entry_price: float
    initial_sl: float
    target_price: float
    
    # Live tracking
    current_sl: float = 0.0
    max_favorable_price: float = 0.0
    first_profit_time: Optional[datetime] = None
    breakeven_locked: bool = False
    
    # Last tick time for stagnation tracking
    last_update_time: Optional[datetime] = None
    
    def __post_init__(self):
        self.current_sl = self.initial_sl
        self.max_favorable_price = self.entry_price

    @property
    def max_profit_ticks(self) -> float:
        """Calculate maximum profit achieved in absolute price terms."""
        if self.direction in ("CALL", "BUY"):
            return self.max_favorable_price - self.entry_price
        else:
            return self.entry_price - self.max_favorable_price

    @property
    def current_profit_ticks(self, current_price: float) -> float:
        """Calculate current profit in absolute price terms."""
        if self.direction in ("CALL", "BUY"):
            return current_price - self.entry_price
        else:
            return self.entry_price - current_price
