from typing import List, Optional
from datetime import datetime, timedelta, timezone
import random
from ....domain.ports.outbound.market_data import IMarketDataProvider
from ....domain.models.bar import Bar, Tick

class MockMarketDataProvider(IMarketDataProvider):
    """
    Mock Data Provider to test the Application layer pipelines without MT5.
    Generates plausible random walk data for XAUUSD.
    """
    def __init__(self):
        self.current_price = 2000.0

    def get_current_tick(self, symbol: str) -> Optional[Tick]:
        spread = random.uniform(0.1, 0.5)
        self.current_price += random.uniform(-0.5, 0.5)
        
        return Tick(
            symbol=symbol,
            time=datetime.now(timezone.utc),
            bid=self.current_price,
            ask=self.current_price + spread,
            last=self.current_price,
            volume=random.randint(1, 10),
            flags=0
        )
        
    def fetch_historical_bars(self, symbol: str, timeframe: str, count: int) -> List[Bar]:
        bars = []
        now = datetime.now(timezone.utc)
        
        price = 2000.0
        for i in range(count):
            time = now - timedelta(minutes=count - i)
            open_p = price
            close_p = price + random.uniform(-2, 2)
            high_p = max(open_p, close_p) + random.uniform(0, 1)
            low_p = min(open_p, close_p) - random.uniform(0, 1)
            
            bars.append(Bar(
                symbol=symbol,
                time=time,
                open=round(open_p, 2),
                high=round(high_p, 2),
                low=round(low_p, 2),
                close=round(close_p, 2),
                tick_volume=random.randint(100, 500),
                real_volume=0,
                spread=15
            ))
            price = close_p
            
        return bars
        
    def fetch_bars_range(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> List[Bar]:
        return self.fetch_historical_bars(symbol, timeframe, 100)
