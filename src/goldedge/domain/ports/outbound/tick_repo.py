from typing import Protocol, List
from datetime import datetime
from ...models.bar import Tick

class ITickRepository(Protocol):
    def get_ticks(self, symbol: str, start: datetime, end: datetime) -> List[Tick]:
        ...
