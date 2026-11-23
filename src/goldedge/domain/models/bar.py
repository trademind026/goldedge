from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass(frozen=True)
class Tick:
    symbol: str
    time: datetime
    bid: float
    ask: float
    last: Optional[float] = None
    volume: Optional[int] = None  # Genuine provider real_volume only (NULL on Spot CFD feeds)
    flags: Optional[int] = None

@dataclass(frozen=True)
class Bar:
    symbol: str
    time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    real_volume: Optional[int] = None
    spread: Optional[int] = None
    tick_count: Optional[int] = None
    tick_rate: Optional[float] = None
    is_feed_gap: bool = False

    @property
    def resolved_tick_count(self) -> int:
        return int(self.tick_count if self.tick_count is not None else self.tick_volume)
