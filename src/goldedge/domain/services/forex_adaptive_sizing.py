import math
from typing import Optional, Any


def forex_adaptive_position_size(
    equity: float,
    entry_price: float,
    win_rate: float = 0.55,
    avg_win_pct: float = 0.0060,   # ~60 pips
    avg_loss_pct: float = 0.0030,  # ~30 pips
    regime: Optional[str] = None,
    contract_size: float = 100000.0,
    base_lot_size: float = 1.0,
    min_lot: float = 0.01,
    max_lot: float = 10.0,
    volume_step: float = 0.01,
    max_capital_per_trade: float = 0.20,
    margin_leverage: float = 100.0  # 1:100 leverage
) -> float:
    """
    Capital-aware Forex position sizing combining Half-Kelly Criterion with regime scaling.
    Kelly fraction: f* = (p*b - q) / b
    Applies half-Kelly for safety and clamps to affordability.
    """
    if entry_price <= 0 or equity <= 0:
        return min_lot

    # 1. Half-Kelly calculation
    b = avg_win_pct / max(avg_loss_pct, 0.0001)
    q = 1.0 - win_rate
    kelly_f = max(0.0, (win_rate * b - q) / b)
    half_kelly = kelly_f * 0.5
    effective_f = min(half_kelly, max_capital_per_trade)
    capital_at_risk = equity * effective_f

    # 2. Risk per standard lot
    loss_per_lot = entry_price * avg_loss_pct * contract_size
    if loss_per_lot <= 0:
        return min_lot

    raw_lots = capital_at_risk / loss_per_lot

    # 3. Regime Scaling
    regime_str = str(regime).upper() if regime else ""
    regime_mult = 1.0 if ("TRENDING" in regime_str or "BULL" in regime_str or "BEAR" in regime_str) else 0.75
    scaled_lots = raw_lots * regime_mult

    # 4. Clamp to broker bounds
    clamped = max(min_lot, min(max_lot, scaled_lots))
    # Round to volume_step
    steps = math.floor(clamped / volume_step)
    clamped_lots = round(steps * volume_step, 2)
    if clamped_lots < min_lot:
        clamped_lots = min_lot

    # 5. Affordability guard: required margin must never exceed available cash equity
    # Required Margin = (Notional Position Value) / Leverage
    while clamped_lots > min_lot:
        required_margin = (entry_price * contract_size * clamped_lots) / margin_leverage
        if required_margin <= equity * 0.80:  # 80% free margin buffer
            break
        clamped_lots = round(clamped_lots - volume_step, 2)

    return max(min_lot, clamped_lots)


def calculate_forex_position_size(
    state: Any,
    kelly_fraction: float,
    stop_distance_pips: float,
    pip_value_usd: float,
    max_allowed_dd_pct: float = 0.10
) -> float:
    """Sizes lots with Half-Kelly and scales down linearly with drawdown."""
    equity = getattr(state, "equity", 100000.0)
    peak_equity = getattr(state, "peak_equity", equity)

    # 1. Base Dollar Risk
    base_dollar_risk = equity * min(0.02, kelly_fraction)

    # 2. Drawdown Sizing Scaler: (1 - Current_DD / Max_Allowed_DD)
    current_dd_pct = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
    dd_scaler = max(0.0, 1.0 - (current_dd_pct / max_allowed_dd_pct))

    adjusted_dollar_risk = base_dollar_risk * dd_scaler

    # 3. Lot Calculation
    risk_per_lot = stop_distance_pips * pip_value_usd
    if risk_per_lot <= 0:
        return 0.0

    lots = round(adjusted_dollar_risk / risk_per_lot, 2)
    return max(0.01, lots)  # Minimum 0.01 micro-lot

