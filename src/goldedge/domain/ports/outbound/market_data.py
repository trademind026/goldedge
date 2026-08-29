from typing import Protocol, List, Optional
from datetime import datetime
from ...models.bar import Bar, Tick

class IMarketDataProvider(Protocol):
    def get_current_tick(self, symbol: str) -> Optional[Tick]:
        ...
        
    def fetch_historical_bars(self, symbol: str, timeframe: str, count: int) -> List[Bar]:
        ...
        
    def fetch_bars_range(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> List[Bar]:
        ...
