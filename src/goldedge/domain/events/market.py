from dataclasses import dataclass
from .base import DomainEvent
from ..models.bar import Bar, Tick
from ..models.regime import MarketRegime
from ..models.session import TradingSession

@dataclass(frozen=True)
class BarClosed(DomainEvent):
    bar: Bar

@dataclass(frozen=True)
class TickReceived(DomainEvent):
    tick: Tick

@dataclass(frozen=True)
class RegimeChanged(DomainEvent):
    previous_regime: MarketRegime
    new_regime: MarketRegime

@dataclass(frozen=True)
class SessionChanged(DomainEvent):
    previous_session: TradingSession
    new_session: TradingSession
