from dataclasses import dataclass, field
from typing import Dict, Any
from .signal import Signal

@dataclass
class ScoredTrade:
    """A fully scored trade candidate ready for risk validation."""
    signal: Signal
    ml_probability: float
    technical_strength: float
    final_score: float
    regime: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def symbol(self) -> str:
        return self.signal.symbol

    @property
    def direction(self) -> str:
        return self.signal.direction

    @property
    def strategy(self) -> str:
        return self.signal.strategy

    @property
    def entry_price(self) -> float:
        return self.signal.entry_price
