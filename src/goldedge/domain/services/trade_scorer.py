import logging
import math
from typing import List, Optional
from ..models.signal import Signal
from ..models.scored_trade import ScoredTrade

logger = logging.getLogger(__name__)

def _score_band(val: float, good_min: float, good_max: float, bad_min: Optional[float], bad_max: Optional[float]) -> float:
    if good_min <= val <= good_max:
        return 1.0
    if val < good_min:
        if bad_min is None: return 1.0
        if val <= bad_min: return 0.0
        return (val - bad_min) / (good_min - bad_min)
    if val > good_max:
        if bad_max is None: return 1.0
        if val >= bad_max: return 0.0
        return (bad_max - val) / (bad_max - good_max)
    return 0.0

class TradeScorer:
    """
    Computes a composite score for a raw signal.
    Phase 4: Integrates Model-Free Quant Scoring (Kalman Velocity, Entropy, Kaufman ER)
    before taking the ML Meta-Labeler probability.
    """

    def __init__(self, w_ml: float = 0.5, w_quant: float = 0.5):
        self.w_ml = w_ml
        self.w_quant = w_quant

    def compute_quant_score(self, signal: Signal, regime: str, features: dict) -> float:
        """
        Model-Free Quantitative Scoring Engine.
        Uses features: kalman_vel, shannon_entropy, kaufman_er, kalman_accel, ema20_slope, vwap_dist
        """
        direction = signal.direction.value
        kalman_vel = features.get("kalman_vel", 0.0)
        entropy_30m = features.get("shannon_entropy", 3.5)
        kaufman_er = features.get("kaufman_er", 0.0)
        ema20_slope = features.get("ema20_slope", 0.0)
        vwap_dist = features.get("vwap_dist", 0.0)
        
        is_sideways = "SIDEWAYS" in regime.upper()
        
        score = 0.0
        
        def _interp(val: float, min_val: float, max_val: float) -> float:
            if min_val < max_val:
                if val <= min_val: return 0.0
                if val >= max_val: return 1.0
                return (val - min_val) / (max_val - min_val)
            else:
                if val >= min_val: return 0.0
                if val <= max_val: return 1.0
                return (val - min_val) / (max_val - min_val)

        if direction in ("CALL", "BUY"):
            # Momentum
            if is_sideways:
                mom_score = _score_band(kalman_vel, 1.2, 3.5, 0.5, 5.0)
            else:
                mom_score = _score_band(kalman_vel, 2.2, 4.5, 1.0, 7.0)
            score += 0.40 * mom_score
            
            # Kaufman ER
            score += 0.20 * _score_band(kaufman_er, 0.25, 0.65, 0.10, 0.90)
            
            # Entropy
            score += 0.15 * _score_band(entropy_30m, 1.80, 2.30, None, 2.70)
            
            # Trend Alignment
            score += 0.15 * _interp(ema20_slope, 0.0001, 0.0004)
            
            # VWAP Alignment
            score += 0.10 * _score_band(vwap_dist, 0.000, 0.002, -0.002, 0.004)
            
        elif direction in ("PUT", "SELL"):
            # Momentum (Negative)
            if is_sideways:
                mom_score = _score_band(kalman_vel, -4.0, -1.2, -6.0, -0.5)
            else:
                mom_score = _score_band(kalman_vel, -5.0, -2.5, -8.0, -1.0)
            score += 0.40 * mom_score
            
            # Kaufman ER
            score += 0.20 * _score_band(kaufman_er, 0.25, 0.65, 0.10, 0.90)
            
            # Entropy
            score += 0.20 * _score_band(entropy_30m, 2.00, 2.60, 1.70, None)
            
            # Trend Alignment
            score += 0.10 * _interp(ema20_slope, -0.0001, -0.0004)
            
            # VWAP Alignment
            score += 0.10 * _score_band(vwap_dist, -0.002, 0.000, -0.004, 0.002)

        # Base quant score
        quant_score = min(1.0, max(0.0, score))

        # Acceleration Penalty/Bonus
        kalman_accel = features.get("kalman_accel", 0.0)
        if direction in ("CALL", "BUY"):
            accel_adj = max(-0.08, min(0.08, kalman_accel * 0.05))
        else:
            accel_adj = max(-0.08, min(0.08, -kalman_accel * 0.05))
            
        quant_score = min(1.0, max(0.0, quant_score + accel_adj))
        
        # Volume Filter
        vol_ratio = features.get("volume_ratio", 1.0)
        if vol_ratio < 0.8:
            quant_score *= 0.7
        elif is_sideways and vol_ratio < 1.2:
            quant_score *= 0.8
            
        # RSI Divergence Filter
        rsi_div = features.get("rsi_price_divergence", 0.0)
        rsi = features.get("rsi", 50.0)
        
        if direction in ("CALL", "BUY"):
            if rsi_div < -0.1 and rsi > 65:
                quant_score *= 0.6
        elif direction in ("PUT", "SELL"):
            if rsi_div < -0.1 and rsi < 35:
                quant_score *= 0.6
            
        return round(quant_score, 4)

    def score_signal(self, signal: Signal, ml_probability: float = 1.0, regime: str = "", features: dict = None) -> ScoredTrade:
        """Calculates the final composite score."""
        features = features or {}
        quant_score = self.compute_quant_score(signal, regime, features)
        
        final_score = (self.w_ml * ml_probability) + (self.w_quant * quant_score)
        
        return ScoredTrade(
            signal=signal,
            ml_probability=round(ml_probability, 4),
            technical_strength=round(quant_score, 4),  # Override tech strength with quant score
            final_score=round(final_score, 4),
            regime=regime,
            details={
                "w_ml": self.w_ml,
                "w_quant": self.w_quant,
                "quant_score": quant_score
            }
        )

    def filter_and_rank(self, signals: List[Signal], min_score_threshold: float, regime: str = "", features: dict = None) -> List[ScoredTrade]:
        scored = [self.score_signal(sig, regime=regime, features=features) for sig in signals]
        qualified = [t for t in scored if t.final_score >= min_score_threshold]
        qualified.sort(key=lambda x: x.final_score, reverse=True)
        return qualified
