"""
Virtual Broker Adapter for Backtesting.

Implements IBrokerAdapter + IMarketDataProvider in-memory.
The SAME ManagePositionUseCase that runs live with CTraderBrokerAdapter
runs here with zero code duplication.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Optional, List
from dataclasses import replace

from ...domain.ports.outbound.broker import IBrokerAdapter
from ...domain.models.trade import Position, TradeStatus
from ...domain.models.bar import Tick

logger = logging.getLogger("virtual_broker")


class VirtualBrokerAdapter:
    """
    In-memory broker that simulates cTrader for backtest context.
    Implements both IBrokerAdapter and IMarketDataProvider ports.
    """

    def __init__(self, initial_equity: float = 10000.0):
        self.equity = initial_equity
        self.initial_equity = initial_equity
        self._positions: dict[int, Position] = {}
        self._closed_positions: list[Position] = []
        self._next_ticket = 1000
        self._current_tick: Optional[Tick] = None

        # XAUUSD symbol spec (cTrader standard)
        self._symbol_info = {
            "point": 0.01,
            "digits": 2,
            "tick_size": 0.01,
            "tick_value": 1.0,
            "contract_size": 100.0,
            "min_lot": 0.01,
            "max_lot": 50.0,
            "volume_step": 0.01,
            "margin_required_per_lot": 1000.0,
        }

    # ── Price Feed (called by backtest loop on every tick) ────────────────

    def update_price(self, bid: float, ask: float, dt: datetime) -> None:
        """Feed a new tick price into the virtual broker."""
        self._current_tick = Tick(
            symbol="XAUUSD", time=dt, bid=bid, ask=ask, last=bid, volume=1
        )

    # ── IMarketDataProvider ──────────────────────────────────────────────

    def get_current_tick(self, symbol: str) -> Optional[Tick]:
        return self._current_tick

    def fetch_historical_bars(self, symbol, timeframe, count):
        return []

    def fetch_bars_range(self, symbol, timeframe, start, end):
        return []

    # ── IBrokerAdapter ───────────────────────────────────────────────────

    def place_order(
        self, symbol: str, order_type: str, volume: float, sl: float, tp: float
    ) -> Optional[int]:
        ticket = self._next_ticket
        self._next_ticket += 1

        price = self._current_tick.ask if order_type.upper() == "BUY" else self._current_tick.bid
        dt = self._current_tick.time if self._current_tick else datetime.now(timezone.utc)

        pos = Position(
            ticket=ticket,
            symbol=symbol,
            type=order_type.upper(),
            volume=volume,
            price_open=price,
            price_sl=sl,
            price_tp=tp,
            time_setup=dt,
            status=TradeStatus.OPEN,
            time_execution=dt,
        )
        self._positions[ticket] = pos
        return ticket

    def close_position(self, ticket: int, volume: Optional[float] = None) -> bool:
        pos = self._positions.pop(ticket, None)
        if pos is None:
            return False

        exit_price = (
            self._current_tick.bid if pos.type == "BUY" else self._current_tick.ask
        ) if self._current_tick else pos.price_open

        # Calculate P&L
        if pos.type == "BUY":
            pnl = (exit_price - pos.price_open) * pos.volume * self._symbol_info["contract_size"]
        else:
            pnl = (pos.price_open - exit_price) * pos.volume * self._symbol_info["contract_size"]

        dt = self._current_tick.time if self._current_tick else datetime.now(timezone.utc)
        closed = replace(
            pos,
            status=TradeStatus.CLOSED,
            price_close=exit_price,
            time_closing=dt,
            profit=pnl,
        )
        self._closed_positions.append(closed)
        self.equity += pnl
        return True

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Position]:
        positions = list(self._positions.values())
        if symbol:
            positions = [p for p in positions if p.symbol == symbol]
        return positions

    def modify_position(
        self, ticket: int, sl: Optional[float] = None, tp: Optional[float] = None
    ) -> bool:
        pos = self._positions.get(ticket)
        if pos is None:
            return False

        new_sl = sl if sl is not None else pos.price_sl
        new_tp = tp if tp is not None else pos.price_tp
        self._positions[ticket] = replace(pos, price_sl=new_sl, price_tp=new_tp)
        return True

    def get_symbol_info(self, symbol: str = "XAUUSD") -> dict:
        return self._symbol_info

    # ── Backtest Helpers ─────────────────────────────────────────────────

    @property
    def closed_trades(self) -> list[Position]:
        return self._closed_positions

    @property
    def open_count(self) -> int:
        return len(self._positions)

    def reset(self, equity: float = None) -> None:
        self.equity = equity or self.initial_equity
        self._positions.clear()
        self._closed_positions.clear()
        self._next_ticket = 1000
        self._current_tick = None
