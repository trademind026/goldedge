import math
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class GoldPipEngine:
    """
    Spot Gold (XAU/USD) Pip & Margin Engine.
    Strictly calibrated for Gold market specifications:
    - 1.00 standard lot = 100 oz, 0.10 mini lot = 10 oz, 0.01 micro lot = 1 oz.
    - 1 point = $0.01 (cent)
    - 1 standard pip = $0.10 (10 cents)
    - 1 pip cash value = $10.00 per 1.00 lot
    - $1.00 price move = 10 pips = $100.00 cash value per 1.00 lot
    - Typical spread: $0.15 - $0.35 (1.5 - 3.5 pips)
    - Hard rejection gate: spread > $0.60 (6.0 pips)
    - Leverage: Default 1:100 (up to 1:500)
    """

    PIP_SIZE: float = 0.10
    POINT_SIZE: float = 0.01
    CONTRACT_SIZE_OZ: float = 100.0
    DEFAULT_LEVERAGE: float = 100.0
    MAX_SPREAD_DOLLARS: float = 0.60
    MIN_LOT_SIZE: float = 0.01
    MAX_LOT_SIZE: float = 100.0
    LOT_STEP: float = 0.01

    @classmethod
    def price_to_pips(cls, price_diff: float) -> float:
        """Converts an absolute gold price difference ($) to pips ($0.10/pip)."""
        return round(price_diff / cls.PIP_SIZE, 6)

    @classmethod
    def pips_to_price(cls, pips: float) -> float:
        """Converts pips to absolute gold price difference ($)."""
        return round(pips * cls.PIP_SIZE, 6)

    @classmethod
    def calculate_cash_pnl(
        cls,
        direction: str,
        entry_price: float,
        exit_price: float,
        lots: float
    ) -> float:
        """
        Calculates exact cash PnL in USD for XAU/USD.
        PnL = (exit - entry) * 100 oz * lots for BUY.
        PnL = (entry - exit) * 100 oz * lots for SELL.
        """
        dir_upper = direction.upper()
        if dir_upper in ("BUY", "CALL", "LONG"):
            price_delta = exit_price - entry_price
        elif dir_upper in ("SELL", "PUT", "SHORT"):
            price_delta = entry_price - exit_price
        else:
            raise ValueError(f"Unknown direction '{direction}'")

        return price_delta * cls.CONTRACT_SIZE_OZ * lots

    @classmethod
    def calculate_required_margin(
        cls,
        price: float,
        lots: float,
        leverage: float = DEFAULT_LEVERAGE
    ) -> float:
        """
        Calculates required margin in USD:
        Margin = (lots * 100 oz * price) / leverage
        """
        if leverage <= 0 or price <= 0 or lots <= 0:
            return 0.0
        return (lots * cls.CONTRACT_SIZE_OZ * price) / leverage

    @classmethod
    def calculate_lot_size(
        cls,
        account_equity: float,
        risk_pct: float,
        entry_price: float,
        sl_price: float,
        max_leverage: float = DEFAULT_LEVERAGE
    ) -> float:
        """
        Calculates position size in lots based on risk budget and margin constraints.
        - Risk Dollars = account_equity * (risk_pct / 100)
        - Risk per lot = |entry - sl| * 100 oz
        - Respects margin affordability and rounds down to 0.01 lot steps.
        """
        if account_equity <= 0 or risk_pct <= 0 or entry_price <= 0:
            return 0.0

        risk_dollars = account_equity * (risk_pct / 100.0)
        price_risk = abs(entry_price - sl_price)
        if price_risk < 1e-4:
            return 0.0

        risk_per_lot = price_risk * cls.CONTRACT_SIZE_OZ
        raw_lots = risk_dollars / risk_per_lot

        # Margin Affordability Guard (cannot exceed max margin capacity)
        max_lots_by_margin = (account_equity * max_leverage * 0.95) / (cls.CONTRACT_SIZE_OZ * entry_price)
        safe_lots = min(raw_lots, max_lots_by_margin)

        if safe_lots < cls.MIN_LOT_SIZE:
            return 0.0

        # Step quantization (floor to nearest 0.01 step)
        steps = math.floor(safe_lots / cls.LOT_STEP)
        quantized = round(steps * cls.LOT_STEP, 2)
        return min(cls.MAX_LOT_SIZE, quantized)

    @classmethod
    def validate_spread(cls, bid: float, ask: float) -> Tuple[bool, float, float]:
        """
        Validates whether current bid/ask spread is tradeable.
        Rejection gate: spread > $0.60 (6.0 pips) or negative/zero prices.
        Returns: (is_valid, spread_dollars, spread_pips)
        """
        if bid <= 0 or ask <= 0 or ask < bid:
            return False, 0.0, 0.0

        spread_dollars = ask - bid
        spread_pips = cls.price_to_pips(spread_dollars)
        is_valid = spread_dollars <= cls.MAX_SPREAD_DOLLARS

        return is_valid, spread_dollars, spread_pips
