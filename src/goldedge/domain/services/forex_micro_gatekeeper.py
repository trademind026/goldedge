from typing import Tuple, Dict, Any, Optional
from ..models.signal import SignalDirection


class ForexMicroGatekeeper:
    """
    STAGE 3.2: Microstructure Hard Gatekeeper Kill-Switch.
    Evaluates immediately prior to order dispatch:
    1. Checks P(Micro Clean Breakout) from High-Frequency Microstructure Model:
       If P <= 0.40 -> HARD VETO (Momentum Stalled / Fakeout Trap).
    2. Checks Raw Order Flow Imbalance (OFI_30s):
       - If BUY and OFI < -0.10 (Sellers hitting bid) -> HARD VETO
       - If SELL and OFI > +0.10 (Buyers hitting ask) -> HARD VETO
    """

    def __init__(
        self,
        min_probability_hurdle: float = 0.40,
        ofi_veto_threshold: float = 0.10,
    ):
        self.min_probability_hurdle = min_probability_hurdle
        self.ofi_veto_threshold = ofi_veto_threshold

    def evaluate(
        self,
        micro_prob: float,
        ofi_30s: float,
        direction: Any,
    ) -> Tuple[bool, str]:
        """
        Evaluates microstructure kill-switch.
        Returns: (passed: bool, reason: str)
        """
        # 1. Probability Gatekeeper Kill-Switch
        if micro_prob <= self.min_probability_hurdle:
            return False, f"MICRO_MOMENTUM_STALLED: P(Breakout)={micro_prob:.3f} <= {self.min_probability_hurdle}"

        # Normalize direction
        is_buy = False
        if isinstance(direction, SignalDirection):
            is_buy = (direction == SignalDirection.BUY)
        elif isinstance(direction, str):
            is_buy = direction.upper() in ("BUY", "CALL", "1", "+1")
        elif isinstance(direction, (int, float)):
            is_buy = (direction > 0)

        # 2. Raw Order Flow Imbalance (OFI) Directional Gate
        if is_buy and ofi_30s < -self.ofi_veto_threshold:
            return False, f"OFI_SELLER_ABSORPTION: BUY signal vetoed by negative OFI={ofi_30s:.3f} < -{self.ofi_veto_threshold}"

        if (not is_buy) and ofi_30s > self.ofi_veto_threshold:
            return False, f"OFI_BUYER_ABSORPTION: SELL signal vetoed by positive OFI={ofi_30s:.3f} > +{self.ofi_veto_threshold}"

        return True, "PASSED"
