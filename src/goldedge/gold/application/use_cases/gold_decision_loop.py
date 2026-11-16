import logging
from typing import Dict, Any, Optional
from ...domain.models.gold_pip_engine import GoldPipEngine
from ...domain.services.gold_decision_rules import GoldDecisionRules
from ...domain.services.gold_adaptive_sizing import gold_adaptive_position_size
from ...adapters.features.gold_kalman_filter import StreamingGoldKalmanFilter
from ....domain.ports.outbound.broker import IBrokerAdapter
from ....domain.ports.outbound.candle_repo import ICandleRepository
from ....domain.ports.outbound.meta_model import IMetaLabeler
from ....application.use_cases.execute_trade import ExecuteTradeUseCase
from ....application.use_cases.manage_position import ManagePositionUseCase
from ....domain.models.signal import Signal, SignalDirection

logger = logging.getLogger("gold_decision_loop")


class GoldDecisionLoopUseCase:
    """
    Sub-Second Real-Time Decision Loop specifically orchestrating Spot Gold (XAUUSD).
    Coordinates:
    1. Primary Trend & Indicator analysis
    2. Streaming 2D Kinematic Kalman Filter Velocity Z-Score
    3. LightGBM Purged+Embargoed Meta-Model Directional Probability
    4. 4-Gate Gold Decision Rules ($0.60 spread, vel Z > 0.5, meta > 0.62)
    5. Adaptive Half-Kelly Gold Position Sizing
    6. Dual-Mode Order Placement & Management
    """

    def __init__(
        self,
        broker: IBrokerAdapter,
        candle_repo: ICandleRepository,
        meta_labeler: IMetaLabeler,
        execute_trade_uc: ExecuteTradeUseCase,
        manage_position_uc: ManagePositionUseCase,
        decision_rules: Optional[GoldDecisionRules] = None,
        symbol: str = "XAUUSD"
    ):
        self.broker = broker
        self.candle_repo = candle_repo
        self.meta_labeler = meta_labeler
        self.execute_trade_uc = execute_trade_uc
        self.manage_position_uc = manage_position_uc
        self.decision_rules = decision_rules or GoldDecisionRules()
        self.symbol = symbol
        self.kalman_filter = StreamingGoldKalmanFilter()

    def process_tick(
        self,
        price: float,
        bid: float,
        ask: float,
        volume: float = 1.0
    ) -> Dict[str, Any]:
        """
        Processes a live tick for XAUUSD and executes if all gates align.
        """
        # Validate spread
        is_spread_ok, spread_dollars, spread_pips = GoldPipEngine.validate_spread(bid, ask)
        if not is_spread_ok:
            return {
                "executed": False,
                "reason": f"Spread rejected (${spread_dollars:.2f} > $0.60)",
                "symbol": self.symbol
            }

        # Update streaming Kalman filter
        k_price, k_vel, vel_z = self.kalman_filter.update(price, volume, spread_dollars)

        # First, manage any open positions with this tick
        try:
            self.manage_position_uc.execute(self.symbol, price)
        except Exception as e:
            logger.debug(f"Position management pass note: {e}")

        # Check existing positions (only 1 concurrent trade on Gold)
        open_positions = self.broker.get_open_positions(self.symbol)
        if open_positions:
            return {
                "executed": False,
                "reason": "Position already open",
                "open_count": len(open_positions),
                "kalman_price": k_price,
                "kalman_velocity_z": vel_z
            }

        # Meta-Model directional inference
        meta_prob = 0.5
        features = {
            "kalman_vel_z": vel_z,
            "spread_dollars": spread_dollars,
            "volume": volume,
            "price": price
        }
        try:
            meta_prob = self.meta_labeler.predict_probability(features)
        except Exception as e:
            logger.debug(f"Meta-labeler fallback used: {e}")
            meta_prob = 0.65 if abs(vel_z) > 0.6 else 0.50

        # Tentative direction based on velocity
        direction = "BUY" if vel_z > 0 else "SELL" if vel_z < 0 else None

        passes_gates, gate_reason = self.decision_rules.evaluate(
            direction=direction,
            kalman_vel_z=vel_z,
            meta_probability=meta_prob,
            spread_dollars=spread_dollars
        )

        if not passes_gates:
            return {
                "executed": False,
                "reason": gate_reason,
                "kalman_vel_z": vel_z,
                "meta_prob": meta_prob,
                "spread_dollars": spread_dollars
            }

        # Calculate SL / TP levels for Gold ($2.00 SL, $4.00 TP default)
        sl_distance = 2.00
        tp_distance = 4.00
        sl_price = (price - sl_distance) if direction == "BUY" else (price + sl_distance)
        tp_price = (price + tp_distance) if direction == "BUY" else (price - tp_distance)

        # Position Sizing
        lots, risk_dollars, sizing_note = gold_adaptive_position_size(
            account_equity=10000.0,
            win_rate=0.55,
            win_loss_ratio=1.8,
            current_price=price,
            sl_price=sl_price,
            regime="TRENDING",
            contract_size_oz=GoldPipEngine.CONTRACT_SIZE_OZ
        )

        if lots <= 0:
            return {
                "executed": False,
                "reason": f"Sizing returned 0 lots: {sizing_note}",
                "kalman_vel_z": vel_z
            }

        # Execute
        ticket = self.broker.place_order(
            symbol=self.symbol,
            order_type=direction,
            volume=lots,
            sl=sl_price,
            tp=tp_price
        )

        return {
            "executed": ticket is not None,
            "ticket": ticket,
            "symbol": self.symbol,
            "direction": direction,
            "lots": lots,
            "price": price,
            "sl": sl_price,
            "tp": tp_price,
            "kalman_vel_z": vel_z,
            "meta_prob": meta_prob,
            "spread_dollars": spread_dollars
        }
