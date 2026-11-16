import math
from typing import Tuple


def gold_adaptive_position_size(
    account_equity: float,
    win_rate: float,
    win_loss_ratio: float,
    current_price: float,
    sl_price: float,
    regime: str = "TRENDING",
    max_risk_pct: float = 2.0,
    leverage: float = 100.0,
    contract_size_oz: float = 100.0,
    min_lot: float = 0.01,
    max_lot: float = 50.0,
    lot_step: float = 0.01
) -> Tuple[float, float, str]:
    """
    Half-Kelly Position Sizing specifically calibrated for Spot Gold (XAUUSD).
    - Fractional Kelly: f* = ((p * b - q) / b) * 0.5
    - Regime scaling: Trend = 1.0, Sideways/Chop = 0.65, Volatile = 0.50
    - Margin affordability guard: Margin = (Lots * 100 oz * Price) / Leverage <= 90% Equity
    - Returns: (lots, risk_dollars, rationale)
    """
    if account_equity <= 0 or current_price <= 0:
        return 0.0, 0.0, "Zero or negative equity/price"

    price_risk = abs(current_price - sl_price)
    if price_risk < 0.10:  # Minimum 1 pip risk
        return 0.0, 0.0, "Stop loss too tight (< $0.10)"

    # 1. Half-Kelly calculation
    p = max(0.01, min(0.99, win_rate))
    q = 1.0 - p
    b = max(0.1, win_loss_ratio)

    kelly_f = (p * b - q) / b
    half_kelly = max(0.0, kelly_f * 0.5)

    # 2. Regime scaling
    regime_upper = regime.upper()
    if "TREND" in regime_upper:
        regime_scale = 1.0
    elif "SIDEWAYS" in regime_upper or "CHOP" in regime_upper or "RANGE" in regime_upper:
        regime_scale = 0.65
    else:
        regime_scale = 0.50

    target_risk_pct = min(max_risk_pct, half_kelly * 100.0) * regime_scale
    if target_risk_pct <= 0:
        target_risk_pct = 0.5  # Base defensive allocation

    risk_dollars = account_equity * (target_risk_pct / 100.0)

    # 3. Lot calculation
    dollar_risk_per_lot = price_risk * contract_size_oz
    raw_lots = risk_dollars / dollar_risk_per_lot

    # 4. Margin affordability guard
    max_affordable_margin = account_equity * 0.90
    max_lots_by_margin = (max_affordable_margin * leverage) / (contract_size_oz * current_price)
    bounded_lots = min(raw_lots, max_lots_by_margin)

    if bounded_lots < min_lot:
        return 0.0, 0.0, f"Affordable lots ({bounded_lots:.3f}) below broker min ({min_lot})"

    # 5. Quantize to broker lot step
    steps = math.floor(bounded_lots / lot_step)
    final_lots = round(steps * lot_step, 2)
    final_lots = min(max_lot, final_lots)

    actual_risk_dollars = final_lots * dollar_risk_per_lot
    rationale = (
        f"Kelly f*={half_kelly:.3f}, Regime={regime_scale:.2f}, "
        f"TargetRisk={target_risk_pct:.2f}%, Lots={final_lots}"
    )

    return final_lots, actual_risk_dollars, rationale
