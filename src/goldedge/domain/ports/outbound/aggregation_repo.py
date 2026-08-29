from typing import Protocol, Optional
from datetime import datetime

class IAggregationRepository(Protocol):
    def aggregate_ticks_to_bars(self, symbol: str, start: Optional[datetime] = None, end: Optional[datetime] = None) -> int:
        """
        Aggregates raw market ticks into multi-timeframe candles (e.g., 1m).
        Returns the number of bars successfully aggregated and saved.
        """
        ...
