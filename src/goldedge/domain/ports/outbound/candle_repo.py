from typing import Protocol, List
from datetime import datetime
from ...models.bar import Bar

class ICandleRepository(Protocol):
    def save_bar(self, bar: Bar) -> None:
        ...
        
    def save_bars(self, bars: List[Bar]) -> None:
        ...
        
    def get_bars(self, symbol: str, start: datetime, end: datetime) -> List[Bar]:
        ...
        
    def get_latest_bars(self, symbol: str, count: int) -> List[Bar]:
        ...
