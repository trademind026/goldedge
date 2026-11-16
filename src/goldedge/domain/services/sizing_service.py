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
        symbol: str = "XAUUSD",
        kalman_uncertainty: Optional[float] = None,
        win_probability: Optional[float] = None,
        current_drawdown_pct: float = 0.0,
        max_allowed_drawdown_pct: float = 0.08
    ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
        """
        Broker-aware position sizing pipeline.
        Returns (lot_size, sl_price, tp_price, rejection_reason).
        lot_size is None if the trade should be SKIPPED.

        Actual risk is always <= allowed risk after floor normalization.
        Never rounds up to avoid exceeding the configured risk %.
        """
        # 1. Dynamic Drawdown Contraction Scaling (Stage 4.2)
        if max_allowed_drawdown_pct > 0:
            dd_scaler = max(0.0, 1.0 - (current_drawdown_pct / max_allowed_drawdown_pct))
        else:
            dd_scaler = 1.0

        if dd_scaler <= 0:
            return None, None, None, f"Dynamic Drawdown Scaler is 0.0 (Account in maximum drawdown lock)."

        # 2. SL / TP Distances (ATR or 1.5x Kalman Uncertainty)
        base_sl_distance = atr_value * risk_profile.sl_atr_multiplier
        if kalman_uncertainty is not None and kalman_uncertainty > 0:
            kalman_sl_distance = 1.5 * kalman_uncertainty
            sl_distance = max(base_sl_distance, kalman_sl_distance)
        else:
            sl_distance = base_sl_distance

        tp_distance = atr_value * risk_profile.tp_atr_multiplier

        if sl_distance <= 0:
            return None, None, None, "Invalid SL distance (ATR or multiplier is zero)"

        # 3. Fractional Half-Kelly Risk Budget (Stage 4.1 & 4.3)
        if win_probability is not None and 0.0 < win_probability < 1.0:
            b = tp_distance / max(sl_distance, 1e-6)
            p = win_probability
            q = 1.0 - p
            kelly = (p * b - q) / b if b > 0 else 0.0
            if kelly <= 0:
                return None, None, None, f"Half-Kelly expectation non-positive ({kelly:.4f} <= 0). Aborting."
            f_star = min(0.015, kelly * 0.5)
            max_monetary_risk = account_balance * f_star * dd_scaler
        else:
            max_monetary_risk = account_balance * (risk_profile.risk_per_trade_pct / 100.0) * dd_scaler

        if direction == SignalDirection.BUY:
            sl_price = entry_price - sl_distance
            tp_price = entry_price + tp_distance
        else:
            sl_price = entry_price + sl_distance
            tp_price = entry_price - tp_distance

        # 4. Loss per lot using MT5 tick_size and tick_value (broker-native)
        ticks_at_risk = sl_distance / self.tick_size
        loss_per_lot = ticks_at_risk * self.tick_value

        if loss_per_lot <= 0:
            return None, None, None, "Loss per lot is zero — check tick_size / tick_value"

        raw_lot_size = max_monetary_risk / loss_per_lot

        # 5. Normalize: ALWAYS floor to volume_step to prevent exceeding risk
        steps = math.floor(raw_lot_size / self.volume_step)
        normalized_size = round(steps * self.volume_step, 2)

        # 6. Gate: Below minimum lot — SKIP, never round up
        if normalized_size < self.min_lot:
            return None, None, None, (
                f"Calculated lot {normalized_size} < broker min {self.min_lot}. "
                f"Skip trade rather than violating risk limit."
            )

        # 7. Gate: Clamp to max lot
        normalized_size = min(normalized_size, self.max_lot)

        # 8. Gate: Margin affordability & 300% Margin Level Buffer
        margin_required = normalized_size * self.margin_required_per_lot
        if margin_required > 0 and (account_balance / margin_required) * 100 < 300:
            max_affordable_margin = account_balance / 3.0
            max_affordable = math.floor(
                (max_affordable_margin / self.margin_required_per_lot) / self.volume_step
            ) * self.volume_step
            max_affordable = round(max_affordable, 2)
            if max_affordable < self.min_lot:
                return None, None, None, (
                    f"Insufficient free margin to maintain > 300% margin buffer. "
                    f"Need {margin_required:.2f} but have {account_balance:.2f}"
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
