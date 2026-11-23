from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import Engine, text

from ....domain.models.hwm_risk import HwmRiskState
from ....domain.ports.outbound.hwm_risk_repo import IHwmRiskRepository
from .connection_pool import get_connection

logger = logging.getLogger(__name__)


class HwmRiskRepository(IHwmRiskRepository):
    """PostgreSQL SQLAlchemy Core adapter for persistent 8% HWM kill-switch state & immutable audit logs."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def load_state(self, account_scope: str) -> Optional[HwmRiskState]:
        sql = text("""
            SELECT
                account_scope, account_id, account_currency, initial_equity,
                hwm_equity, current_equity, drawdown_pct, max_drawdown_pct,
                halted, halt_timestamp, halt_reason, trigger_hwm, trigger_equity,
                trigger_drawdown_pct, open_positions_at_trigger,
                emergency_exit_status, emergency_exit_details,
                reset_count, last_reset_at, last_reset_by, last_reset_reason, updated_at
            FROM risk_hwm_state
            WHERE account_scope = :scope
            LIMIT 1
        """)
        with get_connection(self.engine) as conn:
            row = conn.execute(sql, {"scope": account_scope}).fetchone()

        if row is None:
            return None

        details = row.emergency_exit_details
        if isinstance(details, str):
            details = json.loads(details)

        return HwmRiskState(
            account_scope=row.account_scope,
            account_id=row.account_id or "",
            account_currency=row.account_currency or "USD",
            initial_equity=float(row.initial_equity),
            hwm_equity=float(row.hwm_equity),
            current_equity=float(row.current_equity),
            drawdown_pct=float(row.drawdown_pct),
            max_drawdown_pct=float(row.max_drawdown_pct),
            halted=bool(row.halted),
            halt_timestamp=row.halt_timestamp,
            halt_reason=row.halt_reason,
            trigger_hwm=float(row.trigger_hwm) if row.trigger_hwm is not None else None,
            trigger_equity=float(row.trigger_equity) if row.trigger_equity is not None else None,
            trigger_drawdown_pct=float(row.trigger_drawdown_pct) if row.trigger_drawdown_pct is not None else None,
            open_positions_at_trigger=int(row.open_positions_at_trigger or 0),
            emergency_exit_status=row.emergency_exit_status or "NONE",
            emergency_exit_details=list(details) if details else [],
            reset_count=int(row.reset_count or 0),
            last_reset_at=row.last_reset_at,
            last_reset_by=row.last_reset_by,
            last_reset_reason=row.last_reset_reason,
            updated_at=row.updated_at,
        )

    def save_state_atomic(
        self,
        state: HwmRiskState,
        audit_event: Optional[Dict[str, Any]] = None,
    ) -> None:
        upsert_sql = text("""
            INSERT INTO risk_hwm_state (
                account_scope, account_id, account_currency, initial_equity,
                hwm_equity, current_equity, drawdown_pct, max_drawdown_pct,
                halted, halt_timestamp, halt_reason, trigger_hwm, trigger_equity,
                trigger_drawdown_pct, open_positions_at_trigger,
                emergency_exit_status, emergency_exit_details,
                reset_count, last_reset_at, last_reset_by, last_reset_reason, updated_at
            ) VALUES (
                :account_scope, :account_id, :account_currency, :initial_equity,
                :hwm_equity, :current_equity, :drawdown_pct, :max_drawdown_pct,
                :halted, :halt_timestamp, :halt_reason, :trigger_hwm, :trigger_equity,
                :trigger_drawdown_pct, :open_positions_at_trigger,
                :emergency_exit_status, CAST(:emergency_exit_details AS jsonb),
                :reset_count, :last_reset_at, :last_reset_by, :last_reset_reason, NOW()
            )
            ON CONFLICT (account_scope) DO UPDATE SET
                account_id = EXCLUDED.account_id,
                account_currency = EXCLUDED.account_currency,
                initial_equity = EXCLUDED.initial_equity,
                hwm_equity = EXCLUDED.hwm_equity,
                current_equity = EXCLUDED.current_equity,
                drawdown_pct = EXCLUDED.drawdown_pct,
                max_drawdown_pct = EXCLUDED.max_drawdown_pct,
                halted = EXCLUDED.halted,
                halt_timestamp = EXCLUDED.halt_timestamp,
                halt_reason = EXCLUDED.halt_reason,
                trigger_hwm = EXCLUDED.trigger_hwm,
                trigger_equity = EXCLUDED.trigger_equity,
                trigger_drawdown_pct = EXCLUDED.trigger_drawdown_pct,
                open_positions_at_trigger = EXCLUDED.open_positions_at_trigger,
                emergency_exit_status = EXCLUDED.emergency_exit_status,
                emergency_exit_details = EXCLUDED.emergency_exit_details,
                reset_count = EXCLUDED.reset_count,
                last_reset_at = EXCLUDED.last_reset_at,
                last_reset_by = EXCLUDED.last_reset_by,
                last_reset_reason = EXCLUDED.last_reset_reason,
                updated_at = NOW()
        """)
        lock_sql = text("SELECT halted FROM risk_hwm_state WHERE account_scope = :scope FOR UPDATE")

        with self.engine.begin() as conn:
            conn.execute(lock_sql, {"scope": state.account_scope}).fetchone()
            conn.execute(
                upsert_sql,
                {
                    "account_scope": state.account_scope,
                    "account_id": state.account_id,
                    "account_currency": state.account_currency,
                    "initial_equity": float(state.initial_equity),
                    "hwm_equity": float(state.hwm_equity),
                    "current_equity": float(state.current_equity),
                    "drawdown_pct": float(state.drawdown_pct),
                    "max_drawdown_pct": float(state.max_drawdown_pct),
                    "halted": bool(state.halted),
                    "halt_timestamp": state.halt_timestamp,
                    "halt_reason": state.halt_reason,
                    "trigger_hwm": state.trigger_hwm,
                    "trigger_equity": state.trigger_equity,
                    "trigger_drawdown_pct": state.trigger_drawdown_pct,
                    "open_positions_at_trigger": int(state.open_positions_at_trigger),
                    "emergency_exit_status": state.emergency_exit_status,
                    "emergency_exit_details": json.dumps(state.emergency_exit_details or []),
                    "reset_count": int(state.reset_count),
                    "last_reset_at": state.last_reset_at,
                    "last_reset_by": state.last_reset_by,
                    "last_reset_reason": state.last_reset_reason,
                },
            )
            if audit_event is not None:
                self._insert_audit_row(
                    conn=conn,
                    account_scope=state.account_scope,
                    event_type=str(audit_event.get("event_type", "STATE_UPDATED")),
                    state=state,
                    authorized_by=audit_event.get("authorized_by"),
                    reason=audit_event.get("reason"),
                    details=audit_event.get("details"),
                )

    def append_audit(
        self,
        account_scope: str,
        event_type: str,
        state: Optional[HwmRiskState] = None,
        authorized_by: Optional[str] = None,
        reason: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self.engine.begin() as conn:
            self._insert_audit_row(
                conn=conn,
                account_scope=account_scope,
                event_type=event_type,
                state=state,
                authorized_by=authorized_by,
                reason=reason,
                details=details,
            )

    def _insert_audit_row(
        self,
        conn,
        account_scope: str,
        event_type: str,
        state: Optional[HwmRiskState],
        authorized_by: Optional[str],
        reason: Optional[str],
        details: Optional[Dict[str, Any]],
    ) -> None:
        sql = text("""
            INSERT INTO risk_hwm_audit_log (
                event_time, account_scope, event_type,
                hwm_equity, current_equity, drawdown_pct,
                authorized_by, reason, details
            ) VALUES (
                :event_time, :account_scope, :event_type,
                :hwm_equity, :current_equity, :drawdown_pct,
                :authorized_by, :reason, CAST(:details AS jsonb)
            )
        """)
        conn.execute(
            sql,
            {
                "event_time": datetime.now(timezone.utc),
                "account_scope": account_scope,
                "event_type": event_type,
                "hwm_equity": float(state.hwm_equity) if state is not None else None,
                "current_equity": float(state.current_equity) if state is not None else None,
                "drawdown_pct": float(state.drawdown_pct) if state is not None else None,
                "authorized_by": authorized_by,
                "reason": reason,
                "details": json.dumps(details or {}),
            },
        )

    def get_audit_history(self, account_scope: str, limit: int = 100) -> List[Dict[str, Any]]:
        sql = text("""
            SELECT id, event_time, account_scope, event_type,
                   hwm_equity, current_equity, drawdown_pct,
                   authorized_by, reason, details
            FROM risk_hwm_audit_log
            WHERE account_scope = :scope
            ORDER BY id DESC
            LIMIT :lim
        """)
        with get_connection(self.engine) as conn:
            rows = conn.execute(sql, {"scope": account_scope, "lim": int(limit)}).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            det = json.loads(r.details) if isinstance(r.details, str) else (r.details or {})
            out.append({
                "id": int(r.id),
                "event_time": r.event_time.isoformat() if r.event_time else None,
                "account_scope": r.account_scope,
                "event_type": r.event_type,
                "hwm_equity": float(r.hwm_equity) if r.hwm_equity is not None else None,
                "current_equity": float(r.current_equity) if r.current_equity is not None else None,
                "drawdown_pct": float(r.drawdown_pct) if r.drawdown_pct is not None else None,
                "authorized_by": r.authorized_by,
                "reason": r.reason,
                "details": det,
            })
        return out
