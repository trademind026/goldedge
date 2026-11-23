from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol

from ...models.hwm_risk import HwmRiskState


class IHwmRiskRepository(Protocol):
    """Outbound port for atomic persistence of 8% HWM kill-switch state and immutable audit logs."""

    def load_state(self, account_scope: str) -> Optional[HwmRiskState]:
        """Load persisted HWM risk state for `account_scope`. Raises on storage error."""
        ...

    def save_state_atomic(
        self,
        state: HwmRiskState,
        audit_event: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist `state` atomically under row lock and optionally append `audit_event`."""
        ...

    def append_audit(
        self,
        account_scope: str,
        event_type: str,
        state: Optional[HwmRiskState] = None,
        authorized_by: Optional[str] = None,
        reason: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append an immutable audit log entry."""
        ...

    def get_audit_history(self, account_scope: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Retrieve recent immutable audit entries ordered newest-first."""
        ...
