from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class HwmRiskState:
    """Persistent state of the account-level High-Water-Mark hard kill-switch."""

    account_scope: str
    account_id: str = ""
    account_currency: str = "USD"
    initial_equity: float = 10000.0
    hwm_equity: float = 10000.0
    current_equity: float = 10000.0
    drawdown_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    halted: bool = False
    halt_timestamp: Optional[datetime] = None
    halt_reason: Optional[str] = None
    trigger_hwm: Optional[float] = None
    trigger_equity: Optional[float] = None
    trigger_drawdown_pct: Optional[float] = None
    open_positions_at_trigger: int = 0
    emergency_exit_status: str = "NONE"
    emergency_exit_details: List[Dict[str, Any]] = field(default_factory=list)
    reset_count: int = 0
    last_reset_at: Optional[datetime] = None
    last_reset_by: Optional[str] = None
    last_reset_reason: Optional[str] = None
    updated_at: Optional[datetime] = None
    persistence_degraded: bool = False
    halt_count: int = 0
    trades_blocked: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_scope": self.account_scope,
            "account_id": self.account_id,
            "account_currency": self.account_currency,
            "initial_equity": round(self.initial_equity, 4),
            "hwm_equity": round(self.hwm_equity, 4),
            "current_equity": round(self.current_equity, 4),
            "drawdown_pct": round(self.drawdown_pct, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "halted": self.halted,
            "halt_timestamp": self.halt_timestamp.isoformat() if self.halt_timestamp else None,
            "halt_reason": self.halt_reason,
            "trigger_hwm": round(self.trigger_hwm, 4) if self.trigger_hwm is not None else None,
            "trigger_equity": round(self.trigger_equity, 4) if self.trigger_equity is not None else None,
            "trigger_drawdown_pct": round(self.trigger_drawdown_pct, 4) if self.trigger_drawdown_pct is not None else None,
            "open_positions_at_trigger": self.open_positions_at_trigger,
            "emergency_exit_status": self.emergency_exit_status,
            "emergency_exit_details": list(self.emergency_exit_details),
            "reset_count": self.reset_count,
            "last_reset_at": self.last_reset_at.isoformat() if self.last_reset_at else None,
            "last_reset_by": self.last_reset_by,
            "last_reset_reason": self.last_reset_reason,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "persistence_degraded": self.persistence_degraded,
            "halt_count": self.halt_count,
            "trades_blocked": self.trades_blocked,
        }


@dataclass(frozen=True)
class HwmEvaluationResult:
    """Result of an atomic HWM drawdown evaluation step."""

    halted: bool
    newly_triggered: bool
    hwm_equity: float
    current_equity: float
    drawdown_pct: float
    reason: str
    persistence_degraded: bool = False


@dataclass(frozen=True)
class EmergencyExitRecord:
    """Structured audit outcome for a single open position during emergency halt liquidation."""

    ticket: int
    symbol: str
    direction: str
    requested_volume: float
    filled_volume: float
    remaining_volume: float
    requested_at: datetime
    requested_price: float
    actual_execution_price: Optional[float]
    exit_reason: str
    status: str  # CONFIRMED_CLOSED | PARTIAL_CLOSE | REJECTED | DISCONNECTED_PENDING
    retry_attempts: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticket": self.ticket,
            "symbol": self.symbol,
            "direction": self.direction,
            "requested_volume": self.requested_volume,
            "filled_volume": self.filled_volume,
            "remaining_volume": self.remaining_volume,
            "requested_at": self.requested_at.isoformat(),
            "requested_price": self.requested_price,
            "actual_execution_price": self.actual_execution_price,
            "exit_reason": self.exit_reason,
            "status": self.status,
            "retry_attempts": self.retry_attempts,
            "error": self.error,
        }
