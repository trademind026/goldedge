from dataclasses import dataclass
from typing import Optional
from .base import DomainEvent

@dataclass(frozen=True)
class SystemStarted(DomainEvent):
    mode: str
    version: str

@dataclass(frozen=True)
class SystemStopped(DomainEvent):
    reason: str

@dataclass(frozen=True)
class AlertFired(DomainEvent):
    level: str
    message: str
    details: Optional[str] = None

@dataclass(frozen=True)
class BacktestStarted(DomainEvent):
    strategy_name: str
    start_time: str
    end_time: str

@dataclass(frozen=True)
class BacktestCompleted(DomainEvent):
    strategy_name: str
    total_trades: int
    net_profit: float
