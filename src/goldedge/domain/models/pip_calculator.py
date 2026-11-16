from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PipCalculator:
    """
    Forex Pip Arithmetic Engine.
    Standard pairs: pip = 10^-4 (0.0001), pipette = 10^-5
    JPY pairs: pip = 10^-2 (0.01), pipette = 10^-3
    Gold (XAU/USD): pip = 0.10 (or 0.01 tick point depending on broker)
    """

    @staticmethod
    def get_pip_unit(symbol: str) -> float:
        """
        Determines the standard pip unit for a given currency pair or asset.
        """
        sym = symbol.upper().replace("/", "").replace("_", "")
        if "JPY" in sym:
            return 0.01
        elif "XAU" in sym or "GOLD" in sym:
            return 0.10
        elif "XAG" in sym or "SILVER" in sym:
            return 0.01
        elif "BTC" in sym or "ETH" in sym:
            return 1.0
        else:
            # Standard FX pairs (EURUSD, GBPUSD, AUDUSD, NZDUSD, USDCAD, USDCHF, etc.)
            return 0.0001

    @classmethod
    def price_to_pips(cls, price_diff: float, symbol: str) -> float:
        """Converts an absolute price difference to pips."""
        pip_unit = cls.get_pip_unit(symbol)
        return price_diff / pip_unit

    @classmethod
    def pips_to_price(cls, pips: float, symbol: str) -> float:
        """Converts pips to absolute price difference."""
        pip_unit = cls.get_pip_unit(symbol)
        return pips * pip_unit

    @classmethod
    def calculate_spread_pips(cls, bid: float, ask: float, symbol: str) -> float:
        """Calculates instantaneous spread in pip units."""
        if ask <= 0 or bid <= 0:
            return 0.0
        spread_price = max(0.0, ask - bid)
        return cls.price_to_pips(spread_price, symbol)
