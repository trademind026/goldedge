from typing import Protocol, List, Generator, Optional
from datetime import datetime
from ...models.bar import Tick

class ITickRepository(Protocol):
    def save_ticks(self, ticks: List[Tick]) -> None:
        ...

    def get_ticks(self, symbol: str, start: datetime, end: datetime) -> List[Tick]:
        ...

    def stream_ticks(self, symbol: str, start: datetime, end: datetime, chunk_size: int = 50000) -> Generator[Tick, None, None]:
        ...

    def get_latest_tick_time(self, symbol: str) -> Optional[datetime]:
        ...
