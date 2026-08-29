import logging
from datetime import datetime, timezone
from ...domain.models.signal import ScoredSignal
from ...domain.models.trade import Position, TradeStatus, OrderStatus
from ...domain.ports.outbound.broker import IBrokerAdapter
from ...domain.ports.outbound.trade_repo import ITradeRepository
from ...domain.ports.outbound.event_bus import IEventBus
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
        active_profile: RiskProfile
    ):
        self.broker = broker
        self.trade_repo = trade_repo
        self.event_bus = event_bus
        self.risk_validator = risk_validator
        self.sizing_service = sizing_service
        self.active_profile = active_profile

    def execute(
        self,
        scored_signal: ScoredSignal,
        current_spread: float,
        recent_median_spread: float,
        account_balance: float,
        current_daily_loss_pct: float,
        current_streak: int,
        entry_price: float,
        atr_value: float
    ) -> None:
        """
        Validates risk, sizes position, places order, and persists.

        IDEMPOTENCY: If Redis redelivers a SignalGenerated event after a consumer
        crash (before ACK), we must NOT create a second MT5 order.
        We check the idempotency key (signal_id) against xau_orders before proceeding.
        """
        signal_id = scored_signal.signal.signal_id  # Unique ID set at signal creation

        # --- Idempotency Guard ---
        # Check if an order for this exact signal already exists in the DB.
        # Prevents double execution when Redis redelivers an unACKed message.
        if self.trade_repo.order_exists_for_signal(signal_id):
            logger.warning(
                f"Idempotency: Order already exists for signal_id={signal_id}. "
                f"Skipping duplicate execution."
            )
            return

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
            # Persist the rejection in xau_orders for debugging
            self.trade_repo.save_rejected_order(
                signal_id=signal_id,
                symbol=scored_signal.signal.symbol,
                reason=decision.rejection_reason
            )
            return

        # 2. Position Sizing (broker-native)
        lot_size, sl_price, tp_price, size_rejection = self.sizing_service.calculate_size(
            account_balance=account_balance,
            risk_profile=self.active_profile,
            entry_price=entry_price,
            atr_value=atr_value,
            direction=scored_signal.signal.direction,
            symbol=scored_signal.signal.symbol
        )

        if lot_size is None:
            logger.info(f"Sizing rejected for signal {signal_id}: {size_rejection}")
            self.trade_repo.save_rejected_order(
                signal_id=signal_id,
                symbol=scored_signal.signal.symbol,
                reason=size_rejection
            )
            return

        # 3. Place Order at MT5 (broker-side SL/TP included)
        ticket = self.broker.place_order(
            symbol=scored_signal.signal.symbol,
            order_type=scored_signal.signal.direction.value,
            volume=lot_size,
            sl=sl_price,
            tp=tp_price
        )

        if ticket:
            position = Position(
                ticket=ticket,
                signal_id=signal_id,
                symbol=scored_signal.signal.symbol,
                type=scored_signal.signal.direction.value,
                volume=lot_size,
                price_open=entry_price,
                price_sl=sl_price,
                price_tp=tp_price,
                time_setup=datetime.now(timezone.utc),
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
        else:
            logger.error(
                f"MT5 order_send() failed for signal {signal_id}. "
                f"Broker rejected or connection issue."
            )
            self.trade_repo.save_rejected_order(
                signal_id=signal_id,
                symbol=scored_signal.signal.symbol,
                reason="MT5 order_send() returned no ticket"
            )
