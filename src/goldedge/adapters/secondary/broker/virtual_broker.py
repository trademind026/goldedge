from typing import List, Optional, Dict
from dataclasses import dataclass
from datetime import datetime, timezone
import math

from ....domain.ports.outbound.broker import IBrokerAdapter
from ....domain.models.trade import Position, TradeStatus
from ....domain.models.bar import Tick
from ....domain.models.symbol_profile import SymbolProfile
from ....config.settings import settings

@dataclass
class BrokerProfile:
    """Account-level configuration."""
    initial_capital: float = 10000.0
    leverage: float = 100.0
    commission_per_lot: float = 7.0 # $7 per round-turn standard lot


class VirtualBrokerAdapter(IBrokerAdapter):
    """
    Simulates a broker for backtesting.
    Holds positions in memory, manages virtual equity, and processes tick data
    to trigger SL/TP. Now multi-asset capable.
    """

    def __init__(self, profile: BrokerProfile = None):
        self.profile = profile or BrokerProfile()
        self.balance = self.profile.initial_capital
        self.margin_used = 0.0
        
        self.positions: Dict[int, Position] = {}
        self.closed_positions: List[Position] = []
        self._next_ticket = 1
        
        # Virtual clocks
        self.current_time = datetime.min.replace(tzinfo=timezone.utc)
        
        # Track the last tick for each symbol independently
        self.last_ticks: Dict[str, Tick] = {}

    def set_time(self, current_time: datetime):
        self.current_time = current_time

    def get_symbol_profile(self, symbol: str) -> SymbolProfile:
        """
        Retrieves the symbol specification. In a live adapter, this calls MT5.
        In the VirtualBroker, we use the fallback config.
        """
        if symbol not in settings.SYMBOL_PROFILES_FALLBACK:
            raise ValueError(f"Symbol {symbol} not configured in settings.SYMBOL_PROFILES_FALLBACK")
        return settings.SYMBOL_PROFILES_FALLBACK[symbol]

    @property
    def equity(self) -> float:
        """Calculates balance + floating PnL of all open positions."""
        floating_pnl = 0.0
        for pos in self.positions.values():
            sym_prof = self.get_symbol_profile(pos.symbol)
            last_tick = self.last_ticks.get(pos.symbol)
            
            if last_tick is not None:
                current_price = last_tick.bid if pos.type == "BUY" else last_tick.ask
                if pos.type == "BUY":
                    gross = (current_price - pos.price_open) * sym_prof.contract_size * pos.volume
                else:
                    gross = (pos.price_open - current_price) * sym_prof.contract_size * pos.volume
                commission = self.profile.commission_per_lot * pos.volume
                floating_pnl += (gross - commission)
        
        return self.balance + floating_pnl

    def place_order(self, symbol: str, order_type: str, volume: float, sl: float, tp: float) -> Optional[int]:
        sym_prof = self.get_symbol_profile(symbol)
        
        # Reject if volume too small
        if volume < sym_prof.min_volume or volume > sym_prof.max_volume:
            return None
            
        # Volume must respect step
        volume = math.floor(volume / sym_prof.volume_step) * sym_prof.volume_step

        last_tick = self.last_ticks.get(symbol)
        if last_tick is None:
            return None
            
        price = last_tick.ask if order_type == "BUY" else last_tick.bid
        
        # Calculate margin required (Simplified Forex/CFD Margin = (Volume * ContractSize * Price) / Leverage)
        margin_required = (volume * sym_prof.contract_size * price) / self.profile.leverage
        if self.equity - self.margin_used < margin_required:
            return None # Margin call rejection
            
        ticket = self._next_ticket
        self._next_ticket += 1
        
        pos = Position(
            ticket=ticket,
            symbol=symbol,
            type=order_type,
            volume=volume,
            price_open=price,
            price_sl=sl,
            price_tp=tp,
            time_execution=self.current_time,
            status=TradeStatus.OPEN,
            profit=0.0
        )
        
        self.positions[ticket] = pos
        self.margin_used += margin_required
        return ticket

    def close_position(self, ticket: int, volume: Optional[float] = None) -> bool:
        if ticket not in self.positions:
            return False
            
        pos = self.positions[ticket]
        sym_prof = self.get_symbol_profile(pos.symbol)
        last_tick = self.last_ticks.get(pos.symbol)
        
        if last_tick is None:
            return False
            
        exit_price = last_tick.bid if pos.type == "BUY" else last_tick.ask
        
        if pos.type == "BUY":
            gross_profit = (exit_price - pos.price_open) * sym_prof.contract_size * pos.volume
        else:
            gross_profit = (pos.price_open - exit_price) * sym_prof.contract_size * pos.volume
            
        commission = self.profile.commission_per_lot * pos.volume
        net_profit = gross_profit - commission
        
        closed_pos = Position(
            ticket=pos.ticket,
            symbol=pos.symbol,
            type=pos.type,
            volume=pos.volume,
            price_open=pos.price_open,
            price_sl=pos.price_sl,
            price_tp=pos.price_tp,
            price_close=exit_price,
            time_execution=pos.time_execution,
            time_closing=self.current_time,
            status=TradeStatus.CLOSED,
            profit=net_profit,
            commission=commission,
            swap=0.0 # Simplified for now
        )
        
        self.balance += net_profit
        
        # Release margin
        margin_released = (pos.volume * sym_prof.contract_size * pos.price_open) / self.profile.leverage
        self.margin_used = max(0.0, self.margin_used - margin_released)
        
        self.closed_positions.append(closed_pos)
        del self.positions[ticket]
        return True

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Position]:
        if symbol:
            return [p for p in self.positions.values() if p.symbol == symbol]
        return list(self.positions.values())

    def modify_position(self, ticket: int, sl: Optional[float] = None, tp: Optional[float] = None) -> bool:
        if ticket not in self.positions:
            return False
        
        pos = self.positions[ticket]
        new_sl = sl if sl is not None else pos.price_sl
        new_tp = tp if tp is not None else pos.price_tp
        
        self.positions[ticket] = Position(
            ticket=pos.ticket,
            symbol=pos.symbol,
            type=pos.type,
            volume=pos.volume,
            price_open=pos.price_open,
            price_sl=new_sl,
            price_tp=new_tp,
            time_execution=pos.time_execution,
            status=pos.status,
            profit=pos.profit
        )
        return True

    def evaluate_tick(self, tick: Tick) -> List[int]:
        """
        Feeds a tick into the broker to check if any open position's SL/TP was hit.
        Returns a list of tickets that were closed during this tick.
        """
        self.last_ticks[tick.symbol] = tick
        self.current_time = tick.time
        closed_tickets = []

        for ticket, pos in list(self.positions.items()):
            if pos.symbol != tick.symbol:
                continue
                
            if pos.type == "BUY":
                if tick.bid <= pos.price_sl:
                    self.close_position(ticket)
                    closed_tickets.append(ticket)
                elif pos.price_tp > 0 and tick.bid >= pos.price_tp:
                    self.close_position(ticket)
                    closed_tickets.append(ticket)
            else: # SELL
                if tick.ask >= pos.price_sl:
                    self.close_position(ticket)
                    closed_tickets.append(ticket)
                elif pos.price_tp > 0 and tick.ask <= pos.price_tp:
                    self.close_position(ticket)
                    closed_tickets.append(ticket)
                    
        return closed_tickets
