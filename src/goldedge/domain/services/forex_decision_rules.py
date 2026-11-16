from dataclasses import dataclass
from typing import Optional, List
from ..models.signal import Signal, SignalDirection


@dataclass(frozen=True)
class ForexDecisionResult:
    approved: bool
    rejection_reasons: List[str]
    kalman_vel_z: float
    meta_probability: float
    spread_pips: float
    kalman_accel: float = 0.0
    entropy: float = 0.0


class ForexDecisionRules:
    """
    Forex Institutional Decision Engine Rule Evaluator.
    Enforces the full quantitative gate hurdle:
    1. Primary Strategy generates a BUY/SELL candidate.
    2. 2D Kalman Velocity Z-score > min_kalman_vel_z in signal direction (default 0.5 to 1.5).
    3. Positive acceleration in trade direction (if require_acceleration is True).
    4. Shannon Entropy chop lockout (H < max_entropy, vetoing turbulent chop).
    5. Meta-Model Calibrated Win Probability > min_meta_prob (default >= 0.62 to 0.65).
    6. Live spread < max_spread_pips (slippage guard).
    """

    def __init__(
        self,
        min_kalman_vel_z: float = 0.5,
        min_meta_prob: float = 0.62,
        max_spread_pips: float = 1.8,
        require_acceleration: bool = False,
        max_entropy: float = 3.4
    ):
        self.min_kalman_vel_z = min_kalman_vel_z
        self.min_meta_prob = min_meta_prob
        self.max_spread_pips = max_spread_pips
        self.require_acceleration = require_acceleration
        self.max_entropy = max_entropy

    def evaluate(
        self,
        signal: Optional[Signal],
        kalman_vel_z: float,
        meta_probability: float,
        spread_pips: float,
        kalman_accel: Optional[float] = None,
        entropy: Optional[float] = None
    ) -> ForexDecisionResult:
        rejections: List[str] = []

        if signal is None:
            rejections.append("No primary strategy candidate signal generated")

        # 1. Directional Kalman Velocity check
        # For BUY: kalman_vel_z must be > min_kalman_vel_z
        # For SELL: -kalman_vel_z must be > min_kalman_vel_z
        if signal is not None:
            is_buy = signal.direction in (SignalDirection.BUY, "BUY", "CALL")
            directional_vel_z = kalman_vel_z if is_buy else -kalman_vel_z
            if directional_vel_z <= self.min_kalman_vel_z:
                rejections.append(
                    f"2D Kalman Velocity Z-score ({directional_vel_z:.3f}) <= {self.min_kalman_vel_z} in signal direction"
                )

            # 2. Acceleration Gate in trade direction (delta_v > 0 for BUY, delta_v < 0 for SELL)
            if self.require_acceleration and kalman_accel is not None:
                if is_buy and kalman_accel <= 0:
                    rejections.append(
                        f"Kalman Acceleration ({kalman_accel:+.4f}) <= 0: momentum not accelerating for BUY"
                    )
                elif not is_buy and kalman_accel >= 0:
                    rejections.append(
                        f"Kalman Acceleration ({kalman_accel:+.4f}) >= 0: momentum not accelerating for SELL"
                    )

        # 3. Chop Filter (Shannon Entropy)
        if entropy is not None and entropy >= self.max_entropy:
            rejections.append(
                f"CHOP LOCKOUT: Shannon Entropy ({entropy:.3f}) >= {self.max_entropy} threshold"
            )

        # 4. Meta-Model Calibrated Probability Gate
        if meta_probability <= self.min_meta_prob:
            rejections.append(
                f"Meta-Model Calibrated Win Probability ({meta_probability:.3f}) <= {self.min_meta_prob}"
            )

        # 5. Live Spread Gate
        if spread_pips >= self.max_spread_pips:
            rejections.append(
                f"Live spread ({spread_pips:.2f} pips) >= threshold {self.max_spread_pips} pips"
            )

        return ForexDecisionResult(
            approved=(len(rejections) == 0),
            rejection_reasons=rejections,
            kalman_vel_z=kalman_vel_z,
            meta_probability=meta_probability,
            spread_pips=spread_pips,
            kalman_accel=kalman_accel or 0.0,
            entropy=entropy or 0.0
        )

