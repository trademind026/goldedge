from dataclasses import dataclass

@dataclass
class SymbolProfile:
    """
    Holds broker-specific metadata for a financial instrument.
    Crucial for accurate position sizing, spread calculation, and PnL.
    """
    symbol: str
    contract_size: float
    digits: int
    point: float
    tick_size: float
    tick_value: float
    min_volume: float
    max_volume: float
    volume_step: float
    currency_base: str
    currency_profit: str
    currency_margin: str
