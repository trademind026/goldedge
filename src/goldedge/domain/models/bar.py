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
    volume: Optional[int] = None
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
