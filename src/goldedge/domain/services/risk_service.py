from ..models.signal import ScoredSignal
from ..models.risk import RiskDecision
from ...config.risk_profiles import RiskProfile

class RiskValidatorService:
    """
    Sequential gate pipeline for trade approval.
    All gates must pass before sizing and execution.

    Spread calibration note:
    The absolute spread gate (default: 300 points) MUST be calibrated against
    your specific broker's XAUUSD symbol specification before going live.
    Collect timestamp/session/spread_points data for several days on demo
    and calculate per-session spread distributions to set a meaningful limit.
    """

    def __init__(self, risk_profile: RiskProfile):
        self.risk_profile = risk_profile

        # Absolute spread limit (in MT5 symbol points).
        # Default 300 is a placeholder — calibrate against your broker's actual
        # XAUUSD spread distribution per session before trusting this value.
        self.absolute_spread_limit = 300

    def validate_trade(
        self,
        signal: ScoredSignal,
        current_spread: float,
        recent_median_spread: float,
        daily_loss_pct: float,
        current_streak: int
    ) -> RiskDecision:

        # Gate 1: Signal score
        # Threshold is configuration-driven; current V1 default = 0.85.
        # Backtest at 0.70 / 0.75 / 0.80 / 0.85 to find the right balance.
        if signal.score < self.risk_profile.min_score_threshold:
            return RiskDecision.reject(
                f"Score {signal.score:.3f} below configured threshold "
                f"{self.risk_profile.min_score_threshold} (V1 default: 0.85)"
            )

        # Gate 2: Absolute spread limit (broker-calibrated)
        if current_spread > self.absolute_spread_limit:
            return RiskDecision.reject(
                f"Spread {current_spread} exceeds absolute limit {self.absolute_spread_limit} points. "
                f"Calibrate this limit against your broker's XAUUSD distribution."
            )

        # Gate 3: Dynamic spread gate (relative to session median)
        if current_spread > recent_median_spread * 1.5:
            return RiskDecision.reject(
                f"Spread {current_spread} exceeds 1.5× session median {recent_median_spread}"
            )

        # Gate 4: Daily loss limit
        if daily_loss_pct >= self.risk_profile.max_daily_loss_pct:
            return RiskDecision.reject(
                f"Daily loss {daily_loss_pct:.2f}% reached max {self.risk_profile.max_daily_loss_pct}%"
            )

        # Gate 5: Consecutive loss streak
        if current_streak <= -3:
            return RiskDecision.reject(
                "Max consecutive losses (3) reached — pause trading for the session"
            )

        return RiskDecision(approved=True, lot_size=0.0, sl_price=0.0, tp_price=0.0)
