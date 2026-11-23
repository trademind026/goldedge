from __future__ import annotations

import hmac
import math
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..models.hwm_risk import EmergencyExitRecord, HwmEvaluationResult, HwmRiskState
from ..ports.outbound.hwm_risk_repo import IHwmRiskRepository


def compute_unrealized_pnl(
    direction: str,
    entry_price: float,
    bid: float,
    ask: float,
    lots: float,
    contract_size: float = 100.0,
    fx_rate: float = 1.0,
    pending_exit_comm: float = 0.0,
    accrued_swap_pnl: float = 0.0,
) -> float:
    """
    Compute executable mark-to-market unrealized P&L for one open position.
    BUY positions mark against executable BID; SELL positions mark against executable ASK.
    Rejects non-positive / inverted quotes to prevent fabricated equity spikes.
    """
    b = float(bid)
    a = float(ask)
    ep = float(entry_price)
    l = float(lots)
    fx = float(fx_rate)
    if not (math.isfinite(b) and math.isfinite(a) and math.isfinite(ep) and math.isfinite(l) and math.isfinite(fx)):
        raise ValueError("Non-finite price, volume, or FX conversion rate")
    if b <= 0.0 or a <= 0.0 or b >= a:
        raise ValueError(f"Invalid quote for MTM valuation: bid={b}, ask={a}")
    if l < 0.0 or fx <= 0.0:
        raise ValueError(f"Invalid lots={l} or fx_rate={fx}")

    dir_norm = str(direction).upper()
    if dir_norm in ("BUY", "CALL", "LONG"):
        diff = b - ep
    elif dir_norm in ("SELL", "PUT", "SHORT"):
        diff = ep - a
    else:
        raise ValueError(f"Unsupported position direction: {direction}")

    return (diff * l * float(contract_size) * fx) - float(pending_exit_comm) + float(accrued_swap_pnl)


def compute_account_equity(
    closed_balance: float,
    open_unrealized_pnl: float,
    profit_currency: str = "USD",
    account_currency: str = "USD",
    fx_rate: Optional[float] = None,
) -> float:
    """
    Explicitly convert unrealized P&L into account currency and return true MTM equity.
    Refuses silent 1:1 conversion when profit_currency != account_currency without fx_rate.
    """
    bal = float(closed_balance)
    unr = float(open_unrealized_pnl)
    if not (math.isfinite(bal) and math.isfinite(unr)):
        raise ValueError("Non-finite closed_balance or open_unrealized_pnl")

    if profit_currency.upper() == account_currency.upper():
        return bal + unr

    if fx_rate is None or float(fx_rate) <= 0.0 or not math.isfinite(float(fx_rate)):
        raise ValueError(
            f"Explicit positive fx_rate required to convert {profit_currency}->{account_currency}"
        )
    return bal + (unr * float(fx_rate))


class HwmKillSwitchService:
    """
    Single authoritative, thread-safe, persistent 8% High-Water-Mark Hard Kill-Switch.

    Pure-Python domain service (zero SQLAlchemy/Pandas/network imports).
    Uses `IHwmRiskRepository` for durable state & immutable audit logging with fail-closed semantics:
      - `drawdown_pct = ((HWM - current_equity) / HWM) * 100.0`
      - Triggers hard halt when `drawdown_pct >= max_drawdown_pct` (default 8.0%).
      - Sticky once halted: survives equity recovery AND application restarts.
      - Reset requires explicit authorization (`authorized_by`, `reason`, optional secret) + audit record.
    """

    def __init__(
        self,
        max_drawdown_pct: float = 8.0,
        initial_equity: float = 10000.0,
        default_account_scope: str = "default",
        default_account_id: str = "",
        account_currency: str = "USD",
        repo: Optional[IHwmRiskRepository] = None,
        reset_auth_secret: Optional[str] = None,
    ):
        self.max_drawdown_pct: float = float(max_drawdown_pct)
        self.default_initial_equity: float = float(initial_equity)
        self.default_account_scope: str = str(default_account_scope or "default")
        self.default_account_id: str = str(default_account_id or "")
        self.default_currency: str = str(account_currency or "USD").upper()
        self.repo: Optional[IHwmRiskRepository] = repo
        self.reset_auth_secret: Optional[str] = reset_auth_secret

        self._lock = threading.RLock()
        self._states: Dict[str, HwmRiskState] = {}
        self._loaded_scopes: set[str] = set()
        self._load_failed_scopes: set[str] = set()

    @staticmethod
    def build_account_scope(mode: str, account_id: str, currency: str = "USD") -> str:
        clean_mode = (mode or "demo").strip().lower()
        clean_acct = str(account_id or "default").strip()
        clean_ccy = (currency or "USD").strip().upper()
        return f"{clean_mode}:{clean_acct}:{clean_ccy}"

    def _now(self, ts: Optional[datetime] = None) -> datetime:
        if ts is None:
            return datetime.now(timezone.utc)
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)

    def _get_or_load_state(
        self,
        account_scope: Optional[str] = None,
        account_id: Optional[str] = None,
        account_currency: Optional[str] = None,
        initial_equity: Optional[float] = None,
    ) -> HwmRiskState:
        scope = str(account_scope or self.default_account_scope)
        with self._lock:
            if scope in self._states and scope in self._loaded_scopes and scope not in self._load_failed_scopes:
                st = self._states[scope]
                if account_id is not None and str(account_id) and st.account_id != str(account_id):
                    # Account ID changed under same scope label — isolate safely into new state or fail-closed if mismatch
                    new_scope = self.build_account_scope(scope.split(":")[0], str(account_id), account_currency or st.account_currency)
                    return self._get_or_load_state(
                        account_scope=new_scope,
                        account_id=str(account_id),
                        account_currency=account_currency or st.account_currency,
                        initial_equity=initial_equity,
                    )
                return st

            loaded: Optional[HwmRiskState] = None
            if self.repo is not None:
                try:
                    loaded = self.repo.load_state(scope)
                    self._load_failed_scopes.discard(scope)
                except Exception:
                    # Fail CLOSED if persistent storage cannot be read on startup or check
                    self._load_failed_scopes.add(scope)
                    existing = self._states.get(scope)
                    if existing is None:
                        init_eq = float(initial_equity if initial_equity is not None else self.default_initial_equity)
                        existing = HwmRiskState(
                            account_scope=scope,
                            account_id=str(account_id or self.default_account_id),
                            account_currency=str(account_currency or self.default_currency).upper(),
                            initial_equity=init_eq,
                            hwm_equity=init_eq,
                            current_equity=init_eq,
                            persistence_degraded=True,
                        )
                        self._states[scope] = existing
                    else:
                        existing.persistence_degraded = True
                    return existing

            if loaded is not None:
                loaded.persistence_degraded = False
                self._states[scope] = loaded
                self._loaded_scopes.add(scope)
                return loaded

            init_eq = float(initial_equity if initial_equity is not None else self.default_initial_equity)
            if init_eq <= 0.0:
                init_eq = self.default_initial_equity
            st = HwmRiskState(
                account_scope=scope,
                account_id=str(account_id or self.default_account_id),
                account_currency=str(account_currency or self.default_currency).upper(),
                initial_equity=init_eq,
                hwm_equity=init_eq,
                current_equity=init_eq,
                drawdown_pct=0.0,
                max_drawdown_pct=0.0,
                updated_at=self._now(),
            )
            self._states[scope] = st
            self._loaded_scopes.add(scope)

            if self.repo is not None:
                try:
                    self.repo.save_state_atomic(
                        st,
                        audit_event={
                            "event_type": "INITIALIZED",
                            "reason": "Initialized HWM risk state for account scope",
                            "details": {"initial_equity": init_eq, "max_drawdown_pct": self.max_drawdown_pct},
                        },
                    )
                    st.persistence_degraded = False
                except Exception:
                    st.persistence_degraded = True
            return st

    def _persist_locked(self, state: HwmRiskState, audit_event: Optional[Dict[str, Any]] = None) -> bool:
        if self.repo is None:
            return True
        try:
            self.repo.save_state_atomic(state, audit_event=audit_event)
            state.persistence_degraded = False
            self._load_failed_scopes.discard(state.account_scope)
            return True
        except Exception:
            state.persistence_degraded = True
            return False

    def restore_all(self, account_scope: Optional[str] = None) -> HwmRiskState:
        """Explicitly load/reconcile persisted state on startup (never resets an active halt)."""
        return self._get_or_load_state(account_scope=account_scope)

    def get_state(self, account_scope: Optional[str] = None) -> HwmRiskState:
        with self._lock:
            st = self._get_or_load_state(account_scope=account_scope)
            return HwmRiskState(**{k: getattr(st, k) for k in st.__dataclass_fields__})

    def can_open_new_position(
        self,
        symbol: str = "XAUUSD",
        account_scope: Optional[str] = None,
        record_block: bool = True,
    ) -> Tuple[bool, str]:
        """
        Authoritative pre-entry gate. Fail-closed on active halt OR DB persistence failure.
        """
        with self._lock:
            scope = str(account_scope or self.default_account_scope)
            st = self._get_or_load_state(account_scope=scope)
            if scope in self._load_failed_scopes:
                if record_block:
                    st.trades_blocked += 1
                return False, "RISK_PERSISTENCE_UNAVAILABLE: HWM state could not be loaded (fail-closed)."

            if st.halted:
                if record_block:
                    st.trades_blocked += 1
                    self._append_audit_safe(
                        st,
                        "ENTRY_BLOCKED",
                        reason=st.halt_reason or "8% HWM Kill-Switch active",
                        details={"symbol": symbol, "trades_blocked": st.trades_blocked},
                    )
                return (
                    False,
                    f"HWM_KILL_SWITCH_8PCT: Trading halted at {st.trigger_drawdown_pct or st.drawdown_pct:.2f}% drawdown "
                    f"(HWM=${st.trigger_hwm or st.hwm_equity:,.2f}, Eq=${st.trigger_equity or st.current_equity:,.2f}). "
                    f"Authorized reset required.",
                )

            if st.persistence_degraded:
                # Re-sync degraded state before allowing entries
                if not self._persist_locked(st):
                    if record_block:
                        st.trades_blocked += 1
                    return False, "RISK_PERSISTENCE_DEGRADED: Unable to persist HWM state (fail-closed)."

            return True, "OK"

    def evaluate_equity(
        self,
        current_equity: float,
        timestamp: Optional[datetime] = None,
        open_positions_count: int = 0,
        account_scope: Optional[str] = None,
        account_id: Optional[str] = None,
        account_currency: Optional[str] = None,
        symbol: Optional[str] = None,
        persist_non_breach: bool = False,
    ) -> HwmEvaluationResult:
        """
        Update HWM and check 8% drawdown breach atomically under lock.
        Once halted, stays halted even if equity subsequently recovers.
        """
        now_dt = self._now(timestamp)
        eq = float(current_equity)
        if not math.isfinite(eq):
            raise ValueError(f"Invalid non-finite equity: {current_equity}")

        with self._lock:
            st = self._get_or_load_state(
                account_scope=account_scope,
                account_id=account_id,
                account_currency=account_currency,
                initial_equity=None,
            )
            st.current_equity = eq
            st.updated_at = now_dt

            # If already halted: remain permanently halted regardless of equity recovery!
            if st.halted:
                dd_from_hwm = max(0.0, ((st.hwm_equity - eq) / st.hwm_equity) * 100.0) if st.hwm_equity > 0 else 0.0
                st.drawdown_pct = dd_from_hwm
                if st.persistence_degraded:
                    self._persist_locked(st)
                return HwmEvaluationResult(
                    halted=True,
                    newly_triggered=False,
                    hwm_equity=st.hwm_equity,
                    current_equity=eq,
                    drawdown_pct=dd_from_hwm,
                    reason=st.halt_reason or "HWM_KILL_SWITCH_8PCT already active",
                    persistence_degraded=st.persistence_degraded,
                )

            # Advance HWM only when not halted
            hwm_advanced = False
            if eq > st.hwm_equity:
                st.hwm_equity = eq
                hwm_advanced = True

            if st.hwm_equity > 0.0:
                raw_dd = max(0.0, ((st.hwm_equity - eq) / st.hwm_equity) * 100.0)
            else:
                raw_dd = 100.0 if eq <= 0.0 else 0.0

            st.drawdown_pct = raw_dd
            if raw_dd > st.max_drawdown_pct:
                st.max_drawdown_pct = raw_dd

            # Exact breach check: round(raw_dd, 10) avoids binary float 7.999999999999998% missing 8.00%
            if round(raw_dd, 10) >= self.max_drawdown_pct:
                reason = (
                    f"8% HWM Kill-Switch triggered: drawdown {raw_dd:.4f}% >= {self.max_drawdown_pct:.2f}% "
                    f"(HWM=${st.hwm_equity:,.2f}, equity=${eq:,.2f})"
                )
                st.halted = True
                st.halt_timestamp = now_dt
                st.halt_reason = reason
                st.trigger_hwm = st.hwm_equity
                st.trigger_equity = eq
                st.trigger_drawdown_pct = raw_dd
                st.open_positions_at_trigger = int(open_positions_count)
                st.emergency_exit_status = "PENDING" if open_positions_count > 0 else "NONE"
                st.halt_count += 1

                self._persist_locked(
                    st,
                    audit_event={
                        "event_type": "HALT_TRIGGERED",
                        "reason": reason,
                        "details": {
                            "symbol": symbol,
                            "trigger_hwm": st.trigger_hwm,
                            "trigger_equity": st.trigger_equity,
                            "trigger_drawdown_pct": st.trigger_drawdown_pct,
                            "open_positions_at_trigger": int(open_positions_count),
                            "halt_count": st.halt_count,
                        },
                    },
                )
                return HwmEvaluationResult(
                    halted=True,
                    newly_triggered=True,
                    hwm_equity=st.hwm_equity,
                    current_equity=eq,
                    drawdown_pct=raw_dd,
                    reason=reason,
                    persistence_degraded=st.persistence_degraded,
                )

            if persist_non_breach or hwm_advanced or st.persistence_degraded:
                self._persist_locked(st)

            return HwmEvaluationResult(
                halted=False,
                newly_triggered=False,
                hwm_equity=st.hwm_equity,
                current_equity=eq,
                drawdown_pct=raw_dd,
                reason="OK",
                persistence_degraded=st.persistence_degraded,
            )

    def trigger_manual_halt(
        self,
        reason: str = "Manual kill-switch activated",
        current_equity: Optional[float] = None,
        timestamp: Optional[datetime] = None,
        open_positions_count: int = 0,
        account_scope: Optional[str] = None,
        authorized_by: str = "operator",
    ) -> HwmEvaluationResult:
        """Trigger immediate persistent halt manually without duplicating existing halt."""
        now_dt = self._now(timestamp)
        with self._lock:
            st = self._get_or_load_state(account_scope=account_scope)
            if st.halted:
                return HwmEvaluationResult(
                    halted=True,
                    newly_triggered=False,
                    hwm_equity=st.hwm_equity,
                    current_equity=st.current_equity,
                    drawdown_pct=st.drawdown_pct,
                    reason=st.halt_reason or reason,
                    persistence_degraded=st.persistence_degraded,
                )

            eq = float(current_equity) if current_equity is not None else st.current_equity
            dd = max(0.0, ((st.hwm_equity - eq) / st.hwm_equity) * 100.0) if st.hwm_equity > 0 else 0.0
            st.current_equity = eq
            st.drawdown_pct = dd
            st.halted = True
            st.halt_timestamp = now_dt
            st.halt_reason = reason
            st.trigger_hwm = st.hwm_equity
            st.trigger_equity = eq
            st.trigger_drawdown_pct = dd
            st.open_positions_at_trigger = int(open_positions_count)
            st.emergency_exit_status = "PENDING" if open_positions_count > 0 else "NONE"
            st.halt_count += 1
            st.updated_at = now_dt

            self._persist_locked(
                st,
                audit_event={
                    "event_type": "HALT_TRIGGERED",
                    "authorized_by": authorized_by,
                    "reason": reason,
                    "details": {
                        "manual": True,
                        "open_positions_at_trigger": int(open_positions_count),
                    },
                },
            )
            return HwmEvaluationResult(
                halted=True,
                newly_triggered=True,
                hwm_equity=st.hwm_equity,
                current_equity=eq,
                drawdown_pct=dd,
                reason=reason,
                persistence_degraded=st.persistence_degraded,
            )

    def record_emergency_exit_outcomes(
        self,
        records: List[EmergencyExitRecord],
        account_scope: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> str:
        """Persist explicit outcome of emergency liquidation attempts."""
        now_dt = self._now(timestamp)
        with self._lock:
            st = self._get_or_load_state(account_scope=account_scope)
            if not records:
                status = "NO_OPEN_POSITIONS" if st.halted else "NONE"
            elif all(r.status == "CONFIRMED_CLOSED" for r in records):
                status = "ALL_CLOSED_CONFIRMED"
            elif any(r.status == "PARTIAL_CLOSE" for r in records):
                status = "PARTIAL_LIQUIDATION_ESCALATED"
            else:
                status = "ESCALATION_REQUIRED_OPEN_EXPOSURE"

            st.emergency_exit_status = status
            st.emergency_exit_details = [r.to_dict() for r in records]
            st.updated_at = now_dt

            self._persist_locked(
                st,
                audit_event={
                    "event_type": f"EMERGENCY_EXIT_{status}",
                    "reason": f"Emergency exit processing finished with status={status}",
                    "details": {"records": st.emergency_exit_details},
                },
            )
            return status

    def reset(
        self,
        authorized_by: str,
        reason: str,
        current_equity: float,
        auth_token: Optional[str] = None,
        account_scope: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> HwmRiskState:
        """
        Explicitly authorize resetting an active halt.
        Resets HWM baseline cleanly to verified `current_equity` and logs an immutable audit record.
        """
        actor = str(authorized_by or "").strip()
        why = str(reason or "").strip()
        if not actor:
            raise ValueError("Authorized reset requires non-empty `authorized_by`.")
        if not why:
            raise ValueError("Authorized reset requires non-empty `reason`.")
        if self.reset_auth_secret is not None:
            if not auth_token or not hmac.compare_digest(str(auth_token), str(self.reset_auth_secret)):
                raise PermissionError("Invalid authorization token for HWM kill-switch reset.")

        eq = float(current_equity)
        if not math.isfinite(eq) or eq <= 0.0:
            raise ValueError(f"Reset requires positive finite `current_equity`, got {current_equity}.")

        now_dt = self._now(timestamp)
        with self._lock:
            st = self._get_or_load_state(account_scope=account_scope)
            prev_snapshot = st.to_dict()

            st.halted = False
            st.halt_timestamp = None
            st.halt_reason = None
            st.hwm_equity = eq
            st.current_equity = eq
            st.drawdown_pct = 0.0
            st.max_drawdown_pct = 0.0
            st.trigger_hwm = None
            st.trigger_equity = None
            st.trigger_drawdown_pct = None
            st.open_positions_at_trigger = 0
            st.emergency_exit_status = "NONE"
            st.emergency_exit_details = []
            st.reset_count += 1
            st.last_reset_at = now_dt
            st.last_reset_by = actor
            st.last_reset_reason = why
            st.updated_at = now_dt

            if self.repo is not None:
                # Reset MUST persist cleanly before re-enabling trading (fail-closed on DB outage!)
                self.repo.save_state_atomic(
                    st,
                    audit_event={
                        "event_type": "AUTHORIZED_RESET",
                        "authorized_by": actor,
                        "reason": why,
                        "details": {"previous_state": prev_snapshot, "new_hwm_equity": eq},
                    },
                )
                st.persistence_degraded = False
            return self.get_state(account_scope=account_scope)

    def _append_audit_safe(
        self,
        state: HwmRiskState,
        event_type: str,
        authorized_by: Optional[str] = None,
        reason: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self.repo is None:
            return
        try:
            self.repo.append_audit(
                state.account_scope,
                event_type,
                state=state,
                authorized_by=authorized_by,
                reason=reason,
                details=details,
            )
        except Exception:
            state.persistence_degraded = True
