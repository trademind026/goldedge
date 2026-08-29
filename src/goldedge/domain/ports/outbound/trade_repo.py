from typing import Protocol, List, Optional
from ...models.trade import Position, TradeStatus

class ITradeRepository(Protocol):
    def save_position(self, position: Position) -> None:
        ...
        
    def update_position(self, position: Position) -> None:
        ...
        
    def get_position(self, ticket: int) -> Optional[Position]:
        ...
        
    def get_positions_by_status(self, status: TradeStatus) -> List[Position]:
        ...
        
    def get_all_positions(self) -> List[Position]:
        ...
