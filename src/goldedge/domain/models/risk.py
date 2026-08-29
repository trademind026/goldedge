from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    lot_size: float
    sl_price: float
    tp_price: float
    rejection_reason: Optional[str] = None
    
    @classmethod
    def reject(cls, reason: str) -> "RiskDecision":
        return cls(
            approved=False,
            lot_size=0.0,
            sl_price=0.0,
            tp_price=0.0,
            rejection_reason=reason
        )
