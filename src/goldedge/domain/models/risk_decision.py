from dataclasses import dataclass

@dataclass
class RiskDecision:
    """Result from the risk manager's validation and sizing math."""
    approved: bool
    quantity: float             # Volume / Lots
    stop_loss: float
    target: float
    risk_amount: float
    rejection_reason: str = ""
