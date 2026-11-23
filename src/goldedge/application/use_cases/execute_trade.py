import logging
from datetime import datetime, timezone
from typing import Optional
from ...domain.models.signal import ScoredSignal
from ...domain.models.trade import Position, TradeStatus
from ...domain.ports.outbound.broker import IBrokerAdapter
from ...domain.ports.outbound.trade_repo import ITradeRepository
from ...domain.ports.outbound.event_bus import IEventBus
from ...domain.services.hwm_kill_switch_service import HwmKillSwitchService
from ...domain.services.risk_service import RiskValidatorService
from ...domain.services.sizing_service import PositionSizingService
from ...domain.events.trading import TradeOpened
from ...config.risk_profiles import RiskProfile

logger = logging.getLogger(__name__)


class ExecuteTradeUseCase:
    def __init__(
        self,
        broker: IBrokerAdapter,
        trade_repo: ITradeRepository,
        event_bus: IEventBus,
        risk_validator: RiskValidatorService,
        sizing_service: PositionSizingService,
        active_profile: RiskProfile,
        hwm_kill_switch: Optional[HwmKillSwitchService] = None,
    ):
        self.broker = broker
        self.trade_repo = trade_repo
        self.event_bus = event_bus
        self.risk_validator = risk_validator
        self.sizing_service = sizing_service
        self.active_profile = active_profile
        self.hwm_kill_switch = hwm_kill_switch

    def _record_rejection(self, signal_id: str, symbol: str, reason: str) -> None:
        if hasattr(self.trade_repo, "save_rejected_order"):
            self.trade_repo.save_rejected_order(
                signal_id=signal_id,
                symbol=symbol,
                reason=reason,
            )

    def execute(
        self,
        scored_signal: ScoredSignal,
        current_spread: float,
        recent_median_spread: float,
        account_balance: float,
        current_daily_loss_pct: float,
        current_streak: int,
        entry_price: float,
        atr_value: float,
        account_equity: Optional[float] = None,
        account_scope: Optional[str] = None,
    ) -> Optional[int]:
        """
        Validates HWM kill-switch + risk gates, sizes position, places order, and persists.

        IDEMPOTENCY: If Redis redelivers a SignalGenerated event after a consumer
        crash (before ACK), we must NOT create a second order.
        """
        signal_id = scored_signal.signal.signal_id  # Unique ID set at signal creation
        symbol = scored_signal.signal.symbol

        # 0. Persistent 8% HWM Hard Risk Kill-Switch Check (Fail-Closed)
        if self.hwm_kill_switch is not None:
            if account_equity is not None:
                self.hwm_kill_switch.evaluate_equity(
                    current_equity=float(account_equity),
                    account_scope=account_scope,
                    symbol=symbol,
                )
            allowed, halt_reason = self.hwm_kill_switch.can_open_new_position(
                symbol=symbol,
                account_scope=account_scope,
            )
            if not allowed:
                logger.warning(f"Trade blocked by 8% HWM Kill-Switch for signal {signal_id}: {halt_reason}")
                self._record_rejection(signal_id=signal_id, symbol=symbol, reason=halt_reason)
                return None

        # --- Idempotency Guard ---
        if hasattr(self.trade_repo, "order_exists_for_signal") and self.trade_repo.order_exists_for_signal(signal_id):
            logger.warning(
                f"Idempotency: Order already exists for signal_id={signal_id}. "
                f"Skipping duplicate execution."
            )
            return None

        # 1. Risk Validation
        decision = self.risk_validator.validate_trade(
            scored_signal,
            current_spread,
            recent_median_spread,
            current_daily_loss_pct,
            current_streak
        )

        if not decision.approved:
            logger.info(f"Trade rejected for signal {signal_id}: {decision.rejection_reason}")
            self._record_rejection(signal_id=signal_id, symbol=symbol, reason=decision.rejection_reason)
            return None

        # 2. Position Sizing (broker-native)
        lot_size, sl_price, tp_price, size_rejection = self.sizing_service.calculate_size(
            account_balance=account_balance,
            risk_profile=self.active_profile,
            entry_price=entry_price,
            atr_value=atr_value,
            direction=scored_signal.signal.direction,
            symbol=symbol
        )

        if lot_size is None:
            logger.info(f"Sizing rejected for signal {signal_id}: {size_rejection}")
            self._record_rejection(signal_id=signal_id, symbol=symbol, reason=size_rejection)
            return None

        # 3. Re-verify HWM Kill-Switch atomically right before broker order placement
        if self.hwm_kill_switch is not None:
            allowed, halt_reason = self.hwm_kill_switch.can_open_new_position(
                symbol=symbol,
                account_scope=account_scope,
            )
            if not allowed:
                logger.warning(f"Pre-order HWM Kill-Switch check blocked signal {signal_id}: {halt_reason}")
                self._record_rejection(signal_id=signal_id, symbol=symbol, reason=halt_reason)
                return None

        # 4. Place Order at Broker (broker-side SL/TP included)
        ticket = self.broker.place_order(
            symbol=symbol,
            order_type=scored_signal.signal.direction.value,
            volume=lot_size,
            sl=sl_price,
            tp=tp_price
        )

        if ticket:
            position = Position(
                ticket=ticket,
                symbol=symbol,
                type=scored_signal.signal.direction.value,
                volume=lot_size,
                price_open=entry_price,
                price_sl=sl_price,
                price_tp=tp_price,
                time_setup=datetime.now(timezone.utc),
                time_execution=datetime.now(timezone.utc),
                status=TradeStatus.OPEN,
                ml_score=scored_signal.score,
                risk_profile=self.active_profile.name
            )
            self.trade_repo.save_position(position)
            self.event_bus.publish(TradeOpened(position=position))
            logger.info(
                f"Order placed: ticket={ticket}, signal={signal_id}, "
                f"lots={lot_size}, sl={sl_price}, tp={tp_price}"
            )
            return ticket

        logger.error(
            f"Broker order_send() failed for signal {signal_id}. "
            f"Broker rejected or connection issue."
        )
        self._record_rejection(signal_id=signal_id, symbol=symbol, reason="Broker order_send() returned no ticket")
        return None
