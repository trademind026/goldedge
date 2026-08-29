import math
import sys
from typing import Tuple, Optional
from ...config.risk_profiles import RiskProfile
from ..models.signal import SignalDirection

if sys.platform.startswith('win'):
    try:
        import MetaTrader5 as mt5
        _MT5_AVAILABLE = True
    except ImportError:
        _MT5_AVAILABLE = False
else:
    _MT5_AVAILABLE = False


class PositionSizingService:
    def __init__(
        self,
        contract_size: float = 100.0,
        tick_size: float = 0.01,
        tick_value: float = 1.0,
        min_lot: float = 0.01,
        max_lot: float = 50.0,
        volume_step: float = 0.01,
        margin_required_per_lot: float = 1000.0
    ):
        self.contract_size = contract_size
        self.tick_size = tick_size
        self.tick_value = tick_value
        self.min_lot = min_lot
        self.max_lot = max_lot
        self.volume_step = volume_step
        self.margin_required_per_lot = margin_required_per_lot

    def calculate_size(
        self,
        account_balance: float,
        risk_profile: RiskProfile,
        entry_price: float,
        atr_value: float,
        direction: SignalDirection,
        symbol: str = "XAUUSD"
    ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
        """
        Broker-aware position sizing pipeline.
        Returns (lot_size, sl_price, tp_price, rejection_reason).
        lot_size is None if the trade should be SKIPPED.

        Actual risk is always <= allowed risk after floor normalization.
        Never rounds up to avoid exceeding the configured risk %.
        """
        # 1. Base Risk = Account Equity × Allowed Risk %
        max_monetary_risk = account_balance * (risk_profile.risk_per_trade_pct / 100.0)

        # 2. SL / TP Distances (configurable per RiskProfile)
        sl_distance = atr_value * risk_profile.sl_atr_multiplier
        tp_distance = atr_value * risk_profile.tp_atr_multiplier

        if direction == SignalDirection.BUY:
            sl_price = entry_price - sl_distance
            tp_price = entry_price + tp_distance
        else:
            sl_price = entry_price + sl_distance
            tp_price = entry_price - tp_distance

        if sl_distance <= 0:
            return None, None, None, "Invalid SL distance (ATR or multiplier is zero)"

        # 3. Loss per lot using MT5 tick_size and tick_value (broker-native)
        ticks_at_risk = sl_distance / self.tick_size
        loss_per_lot = ticks_at_risk * self.tick_value

        if loss_per_lot <= 0:
            return None, None, None, "Loss per lot is zero — check tick_size / tick_value"

        raw_lot_size = max_monetary_risk / loss_per_lot

        # 4. Normalize: ALWAYS floor to volume_step to prevent exceeding risk
        steps = math.floor(raw_lot_size / self.volume_step)
        normalized_size = round(steps * self.volume_step, 2)

        # 5. Gate: Below minimum lot — SKIP, never round up
        if normalized_size < self.min_lot:
            return None, None, None, (
                f"Calculated lot {normalized_size} < broker min {self.min_lot}. "
                f"Skip trade rather than violating risk limit."
            )

        # 6. Gate: Clamp to max lot
        normalized_size = min(normalized_size, self.max_lot)

        # 7. Gate: Margin affordability
        margin_required = normalized_size * self.margin_required_per_lot
        if margin_required > account_balance:
            max_affordable = math.floor(
                (account_balance / self.margin_required_per_lot) / self.volume_step
            ) * self.volume_step
            max_affordable = round(max_affordable, 2)
            if max_affordable < self.min_lot:
                return None, None, None, (
                    f"Insufficient free margin. Need {margin_required:.2f} "
                    f"but have {account_balance:.2f}"
                )
            normalized_size = min(normalized_size, max_affordable)

        # 8. Recalculate actual monetary risk and verify it ≤ max allowed
        actual_risk = (sl_distance / self.tick_size) * self.tick_value * normalized_size
        if actual_risk > max_monetary_risk + 0.01:  # 1 cent tolerance for float precision
            return None, None, None, (
                f"Post-normalization actual risk {actual_risk:.2f} "
                f"exceeds allowed {max_monetary_risk:.2f}"
            )

        # 9. Optional: broker-native profit/loss validation via order_calc_profit
        # This is a stronger sanity check than pure tick-math.
        # It requires MT5 to be connected (live/demo only, skipped in backtest).
        if _MT5_AVAILABLE and mt5.terminal_info() is not None:
            try:
                mt5_order_type = mt5.ORDER_TYPE_BUY if direction == SignalDirection.BUY else mt5.ORDER_TYPE_SELL
                broker_sl_loss = mt5.order_calc_profit(
                    mt5_order_type,
                    symbol,
                    normalized_size,
                    entry_price,
                    sl_price
                )
                if broker_sl_loss is not None and abs(broker_sl_loss) > max_monetary_risk + 0.10:
                    return None, None, None, (
                        f"Broker order_calc_profit reports SL loss {abs(broker_sl_loss):.2f} "
                        f"> max allowed {max_monetary_risk:.2f}"
                    )
            except Exception:
                pass  # If unavailable, fall through to tick-math result

        return normalized_size, sl_price, tp_price, None
