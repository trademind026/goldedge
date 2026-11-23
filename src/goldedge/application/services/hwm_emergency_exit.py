from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ...domain.events.trading import TradeClosed
from ...domain.models.hwm_risk import EmergencyExitRecord
from ...domain.models.trade import ExitReason, Position, TradeStatus
from ...domain.ports.outbound.broker import IBrokerAdapter
from ...domain.ports.outbound.event_bus import IEventBus
from ...domain.ports.outbound.market_data import IMarketDataProvider
from ...domain.ports.outbound.trade_repo import ITradeRepository
from ...domain.services.hwm_kill_switch_service import HwmKillSwitchService

logger = logging.getLogger(__name__)


def _executable_close_price(direction: str, bid: float, ask: float) -> float:
    return float(bid) if str(direction).upper() in ("BUY", "CALL", "LONG") else float(ask)


class HwmEmergencyLiquidator:
    """
    Phase 3 Open-Position Emergency Exit Orchestrator.

    When the 8% HWM kill-switch activates:
      1. Requires executable bid/ask quotes (never assumes theoretical SL or synthetic prices).
      2. Submits broker market close with bounded retry attempts.
      3. Inspects post-close broker position state to detect partial fills vs rejections vs disconnects.
      4. NEVER marks a position CLOSED until closure is confirmed; updates remaining volume on partial fills
         and persists escalation status on incomplete liquidation.
    """

    def __init__(
        self,
        kill_switch: HwmKillSwitchService,
        max_retries: int = 2,
    ):
        self.kill_switch = kill_switch
        self.max_retries = max(0, int(max_retries))

    def liquidate_open_positions(
        self,
        broker: IBrokerAdapter,
        trade_repo: Optional[ITradeRepository] = None,
        market_data: Optional[IMarketDataProvider] = None,
        event_bus: Optional[IEventBus] = None,
        quotes_by_symbol: Optional[Dict[str, Tuple[float, float]]] = None,
        account_scope: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> List[EmergencyExitRecord]:
        ts_now = now or datetime.now(timezone.utc)
        if ts_now.tzinfo is None:
            ts_now = ts_now.replace(tzinfo=timezone.utc)

        # Authoritative open positions from broker first, falling back to repo only if broker query fails cleanly
        broker_positions: List[Position] = []
        broker_query_failed = False
        try:
            broker_positions = list(broker.get_open_positions() or [])
        except Exception as exc:
            broker_query_failed = True
            logger.error(f"[EMERGENCY_EXIT] Failed querying broker open positions: {exc}")

        repo_positions: List[Position] = []
        if trade_repo is not None:
            try:
                repo_positions = list(trade_repo.get_positions_by_status(TradeStatus.OPEN) or [])
            except Exception as exc:
                logger.warning(f"[EMERGENCY_EXIT] Failed querying trade_repo open positions: {exc}")

        # Merge unique tickets preferring live broker position volume
        by_ticket: Dict[int, Position] = {p.ticket: p for p in repo_positions}
        for bp in broker_positions:
            by_ticket[bp.ticket] = bp

        if not by_ticket and broker_query_failed:
            records = [
                EmergencyExitRecord(
                    ticket=-1,
                    symbol="UNKNOWN",
                    direction="UNKNOWN",
                    requested_volume=0.0,
                    filled_volume=0.0,
                    remaining_volume=0.0,
                    requested_at=ts_now,
                    requested_price=0.0,
                    actual_execution_price=None,
                    exit_reason=ExitReason.HWM_KILL_SWITCH_8PCT.value,
                    status="DISCONNECTED_PENDING",
                    retry_attempts=0,
                    error="Broker get_open_positions() failed during kill-switch liquidation",
                )
            ]
            self.kill_switch.record_emergency_exit_outcomes(records, account_scope=account_scope, timestamp=ts_now)
            return records

        records: List[EmergencyExitRecord] = []
        for ticket, pos in by_ticket.items():
            req_price = self._resolve_exec_quote(pos, market_data=market_data, quotes_by_symbol=quotes_by_symbol)
            if req_price is None:
                rec = EmergencyExitRecord(
                    ticket=ticket,
                    symbol=pos.symbol,
                    direction=pos.type,
                    requested_volume=float(pos.volume),
                    filled_volume=0.0,
                    remaining_volume=float(pos.volume),
                    requested_at=ts_now,
                    requested_price=0.0,
                    actual_execution_price=None,
                    exit_reason=ExitReason.HWM_KILL_SWITCH_8PCT.value,
                    status="REJECTED",
                    retry_attempts=0,
                    error=f"No valid executable bid/ask quote available for {pos.symbol}; position left OPEN.",
                )
                records.append(rec)
                logger.critical(f"[EMERGENCY_EXIT] #{ticket} ({pos.symbol}): {rec.error}")
                continue

            closed_confirmed = False
            last_error: Optional[str] = None
            attempts = 0
            remaining_vol = float(pos.volume)
            actual_exec_px: Optional[float] = None

            for attempt in range(self.max_retries + 1):
                attempts = attempt + 1
                try:
                    ok = bool(broker.close_position(ticket, volume=remaining_vol))
                except Exception as exc:
                    ok = False
                    last_error = f"Exception closing #{ticket}: {exc}"
                    logger.error(f"[EMERGENCY_EXIT] {last_error} (attempt {attempts})")
                    continue

                # Check optional adapter fill metadata + broker post-close open positions truth
                meta_fn = getattr(broker, "get_last_close_result", None)
                meta = meta_fn(ticket) if callable(meta_fn) else None

                try:
                    still_open = [p for p in (broker.get_open_positions(pos.symbol) or []) if p.ticket == ticket]
                except Exception as exc:
                    still_open = []
                    if not ok:
                        last_error = f"Close check error: {exc}"

                if ok and not still_open:
                    closed_confirmed = True
                    actual_exec_px = (
                        float(meta["execution_price"])
                        if (isinstance(meta, dict) and meta.get("execution_price") is not None)
                        else self._resolve_exec_quote(pos, market_data=market_data, quotes_by_symbol=quotes_by_symbol) or req_price
                    )
                    remaining_vol = 0.0
                    break

                if still_open:
                    rem = float(still_open[0].volume)
                    if rem < remaining_vol - 1e-8:
                        # Partial fill occurred — update remaining volume & keep OPEN for next retry/tick
                        actual_exec_px = (
                            float(meta["execution_price"])
                            if (isinstance(meta, dict) and meta.get("execution_price") is not None)
                            else req_price
                        )
                        remaining_vol = max(0.0, rem)
                        last_error = f"Partial close on #{ticket}: remaining volume={remaining_vol:.2f}"
                        logger.warning(f"[EMERGENCY_EXIT] {last_error} (attempt {attempts})")
                    else:
                        last_error = (
                            str(meta.get("error")) if (isinstance(meta, dict) and meta.get("error"))
                            else f"Broker did not confirm close on #{ticket} (still open vol={rem:.2f})"
                        )
                elif not ok:
                    last_error = (
                        str(meta.get("error")) if (isinstance(meta, dict) and meta.get("error"))
                        else f"broker.close_position({ticket}) returned False"
                    )

            filled_vol = round(max(0.0, float(pos.volume) - remaining_vol), 8)
            if closed_confirmed:
                status = "CONFIRMED_CLOSED"
                if trade_repo is not None:
                    closed_pos = replace(
                        pos,
                        status=TradeStatus.CLOSED,
                        price_close=actual_exec_px,
                        time_closing=ts_now,
                        exit_reason=ExitReason.HWM_KILL_SWITCH_8PCT,
                    )
                    trade_repo.save_position(closed_pos)
                    if event_bus is not None:
                        event_bus.publish(TradeClosed(position=closed_pos, exit_reason=ExitReason.HWM_KILL_SWITCH_8PCT))
            elif filled_vol > 1e-8 and remaining_vol > 1e-8:
                status = "PARTIAL_CLOSE"
                if trade_repo is not None:
                    partial_pos = replace(pos, volume=remaining_vol, status=TradeStatus.OPEN)
                    trade_repo.save_position(partial_pos)
            else:
                status = "DISCONNECTED_PENDING" if (last_error and "disconnect" in last_error.lower()) else "REJECTED"

            records.append(
                EmergencyExitRecord(
                    ticket=ticket,
                    symbol=pos.symbol,
                    direction=pos.type,
                    requested_volume=float(pos.volume),
                    filled_volume=filled_vol,
                    remaining_volume=round(remaining_vol, 8),
                    requested_at=ts_now,
                    requested_price=req_price,
                    actual_execution_price=actual_exec_px if (closed_confirmed or filled_vol > 0) else None,
                    exit_reason=ExitReason.HWM_KILL_SWITCH_8PCT.value,
                    status=status,
                    retry_attempts=attempts,
                    error=last_error,
                )
            )

        self.kill_switch.record_emergency_exit_outcomes(records, account_scope=account_scope, timestamp=ts_now)
        return records

    @staticmethod
    def _resolve_exec_quote(
        pos: Position,
        market_data: Optional[IMarketDataProvider],
        quotes_by_symbol: Optional[Dict[str, Tuple[float, float]]],
    ) -> Optional[float]:
        if quotes_by_symbol and pos.symbol in quotes_by_symbol:
            bid, ask = quotes_by_symbol[pos.symbol]
            if bid > 0 and ask > 0 and bid < ask:
                return _executable_close_price(pos.type, bid, ask)
        if market_data is not None:
            try:
                t = market_data.get_current_tick(pos.symbol)
                if t and t.bid > 0 and t.ask > 0 and t.bid < t.ask:
                    return _executable_close_price(pos.type, float(t.bid), float(t.ask))
            except Exception:
                pass
        return None
