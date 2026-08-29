from typing import List, Optional
from datetime import datetime
import bisect

from ....domain.ports.outbound.market_data import IMarketDataProvider
from ....domain.models.bar import Bar, Tick

class HistoricalMarketDataProvider(IMarketDataProvider):
    """
    Simulates a live market data feed using pre-loaded historical data.
    Maintains a 'virtual clock'. When strategies ask for historical bars,
    it only returns bars up to the virtual clock time.
    """
    def __init__(self, bars: List[Bar]):
        # Bars must be sorted by time ascending
        self.all_bars = sorted(bars, key=lambda b: b.time)
        self.bar_times = [b.time for b in self.all_bars]
        self.current_time = datetime.min
        self.current_tick: Optional[Tick] = None

    def set_time(self, current_time: datetime):
        self.current_time = current_time

    def set_current_tick(self, tick: Tick):
        self.current_tick = tick

    def get_current_tick(self, symbol: str) -> Optional[Tick]:
        if self.current_tick and self.current_tick.symbol == symbol:
            return self.current_tick
        return None

    def _get_index_up_to_current(self) -> int:
        # Find the index of the first bar that is strictly strictly AFTER current_time.
        # This means all bars before this index happened on or before current_time.
        idx = bisect.bisect_right(self.bar_times, self.current_time)
        return idx

    def fetch_historical_bars(self, symbol: str, timeframe: str, count: int) -> List[Bar]:
        if timeframe.lower() != "1m":
            # In a full implementation, we'd resample M1 to the requested timeframe here.
            # For V1 GoldEdge, we only use M1 and then resample to M5 inside the regime detector.
            raise NotImplementedError("Historical provider only supports 1m base bars currently.")
            
        end_idx = self._get_index_up_to_current()
        start_idx = max(0, end_idx - count)
        
        # Filter by symbol
        bars_slice = self.all_bars[start_idx:end_idx]
        return [b for b in bars_slice if b.symbol == symbol]

    def fetch_bars_range(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> List[Bar]:
        if timeframe.lower() != "1m":
            raise NotImplementedError("Historical provider only supports 1m base bars currently.")
            
        start_idx = bisect.bisect_left(self.bar_times, start)
        end_idx = bisect.bisect_right(self.bar_times, min(end, self.current_time))
        
        bars_slice = self.all_bars[start_idx:end_idx]
        return [b for b in bars_slice if b.symbol == symbol]
