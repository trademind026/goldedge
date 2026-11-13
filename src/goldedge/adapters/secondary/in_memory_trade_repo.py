"""
In-memory Trade Repository for Backtesting.
Implements ITradeRepository with a simple Python dict.
"""
from typing import Optional, List
from ...domain.models.trade import Position, TradeStatus


class InMemoryTradeRepository:
    """ITradeRepository implementation backed by a dict. No database needed."""

    def __init__(self):
        self._positions: dict[int, Position] = {}

    def save_position(self, position: Position) -> None:
        self._positions[position.ticket] = position

    def update_position(self, position: Position) -> None:
        self._positions[position.ticket] = position

    def get_position(self, ticket: int) -> Optional[Position]:
        return self._positions.get(ticket)

    def get_positions_by_status(self, status: TradeStatus) -> List[Position]:
        return [p for p in self._positions.values() if p.status == status]

    def get_all_positions(self) -> List[Position]:
        return list(self._positions.values())

    def clear(self) -> None:
        self._positions.clear()
