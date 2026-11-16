from typing import Tuple, Optional


class GoldDecisionRules:
    """
    4-Gate Institutional Decision Rules specifically calibrated for Spot Gold (XAUUSD):
    1. Primary Trend Signal: Base indicator / regime alignment.
    2. Kinematic Kalman Velocity: Velocity Z-score > min_kalman_vel_z (default 0.5).
    3. LightGBM Meta-Model Directional Probability: Prob > min_meta_prob (default 0.62).
    4. Spread Gate: Instantaneous spread <= max_spread_dollars (default $0.60 / 6 pips).
    """

    def __init__(
        self,
        min_kalman_vel_z: float = 0.5,
        min_meta_prob: float = 0.62,
        max_spread_dollars: float = 0.60
    ):
        self.min_kalman_vel_z = min_kalman_vel_z
        self.min_meta_prob = min_meta_prob
        self.max_spread_dollars = max_spread_dollars

    def evaluate(
        self,
        direction: Optional[str],
        kalman_vel_z: float,
        meta_probability: float,
        spread_dollars: float
    ) -> Tuple[bool, str]:
        """
        Evaluates trade entry feasibility for Gold.
        Returns: (passes_all_gates, rejection_reason_or_ok)
        """
        # Gate 1: Directional Signal
        if not direction or direction.upper() not in ("BUY", "SELL", "CALL", "PUT", "LONG", "SHORT"):
            return False, "GATE_1_FAILED: No valid primary directional signal"

        dir_norm = "BUY" if direction.upper() in ("BUY", "CALL", "LONG") else "SELL"

        # Gate 2: Kinematic Kalman Velocity in trade direction
        if dir_norm == "BUY" and kalman_vel_z < self.min_kalman_vel_z:
            return False, f"GATE_2_FAILED: Kalman velocity Z ({kalman_vel_z:.2f}) < threshold ({self.min_kalman_vel_z})"
        if dir_norm == "SELL" and kalman_vel_z > -self.min_kalman_vel_z:
            return False, f"GATE_2_FAILED: Kalman velocity Z ({kalman_vel_z:.2f}) > threshold ({-self.min_kalman_vel_z})"

        # Gate 3: Meta-Model Directional Probability
        if meta_probability < self.min_meta_prob:
            return False, f"GATE_3_FAILED: Meta probability ({meta_probability:.2f}) < required ({self.min_meta_prob})"

        # Gate 4: Spread Gate
        if spread_dollars > self.max_spread_dollars:
            return False, f"GATE_4_FAILED: Spread (${spread_dollars:.2f}) > max limit (${self.max_spread_dollars:.2f})"

        return True, "PASSED_ALL_GATES"
