from dataclasses import dataclass
from .base import DomainEvent
from ..models.signal import ScoredSignal
from ..models.trade import Position, ExitReason

@dataclass(frozen=True)
class SignalGenerated(DomainEvent):
    scored_signal: ScoredSignal

@dataclass(frozen=True)
class TradeRequested(DomainEvent):
    position: Position
    
@dataclass(frozen=True)
class TradeOpened(DomainEvent):
    position: Position

@dataclass(frozen=True)
class TradeClosed(DomainEvent):
    position: Position
    exit_reason: ExitReason

@dataclass(frozen=True)
class PositionUpdated(DomainEvent):
    position: Position

@dataclass(frozen=True)
class SLMoved(DomainEvent):
    ticket: int
    old_sl: float
    new_sl: float

@dataclass(frozen=True)
class JourneySnapshot(DomainEvent):
    ticket: int
    time: str
    price: float
    sl: float
    bars_held: int
