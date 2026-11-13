import logging
import math
from typing import List, Optional
from ..models.signal import Signal
from ..models.scored_trade import ScoredTrade

logger = logging.getLogger(__name__)


def _score_band(val: float, good_min: float, good_max: float,
                bad_min: Optional[float], bad_max: Optional[float]) -> float:
    if good_min <= val <= good_max:
        return 1.0
    if val < good_min:
        if bad_min is None:
            return 1.0
        if val <= bad_min:
            return 0.0
        return (val - bad_min) / (good_min - bad_min)
    if val > good_max:
        if bad_max is None:
            return 1.0
        if val >= bad_max:
            return 0.0
        return (bad_max - val) / (bad_max - good_max)
    return 0.0


class TradeScorer:
    """
    Composite signal scorer — calibrated for XAUUSD 1m spot gold.

    XAUUSD vs Nifty Options calibration changes:
      - Shannon entropy good band: 2.5–3.2 (gold returns more structured than options)
      - EMA slope BUY band: 0.00005→0.00030 (gold can sustain long directional slopes)
      - Session score (5% weight): peaks during London+NY overlap, 0 in Asia dead zone
      - Removed RSI divergence penalty (too noisy on 1m gold, caused false negatives)
      - Acceleration multiplier unchanged at ×2.0 (valid for XAUUSD tick dynamics)
    """

    def __init__(self, w_ml: float = 0.5, w_quant: float = 0.5):
        self.w_ml = w_ml
        self.w_quant = w_quant

    def compute_quant_score(self, signal: Signal, regime: str, features: dict) -> float:
        """
        Model-Free Quantitative Scoring Engine — XAUUSD 1m bars.

        Feature value ranges (empirical for XAUUSD @ ~$4000–4700):
          kalman_vel   : per-tick price change, typical ±0.001–0.05 for strong moves
          kaufman_er   : 0.0–1.0; >0.20 = trending, >0.40 = strong trend on 1m
          shannon_ent  : 2.5–3.5 nats for 30-bar returns; <3.2 = structured/directional
          ema20_slope  : normalized, typical ±0.00005–0.0003 on 1m XAUUSD bars
          vwap_dist    : fractional distance from VWAP, typical ±0.001–0.005
          volume_ratio : 1.0 = average; >2.0 = spike; <0.7 = thin market
          utc_hour     : bar UTC hour (0–23) for session scoring
        """
        direction   = signal.direction.value
        kalman_vel  = features.get("kalman_vel", 0.0)
        entropy_val = features.get("shannon_entropy", 3.0)
        kaufman_er  = features.get("kaufman_er", 0.0)
        ema20_slope = features.get("ema20_slope", 0.0)
        vwap_dist   = features.get("vwap_dist", 0.0)
        utc_hour    = features.get("utc_hour", 12)

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
            # ── Kalman Velocity (35%) ──────────────────────────────────────────
            # Sideways: gentler momentum; trending: need stronger directional signal
            if is_sideways:
                mom_score = _score_band(kalman_vel, 0.008, 0.040, 0.002, 0.080)
            else:
                mom_score = _score_band(kalman_vel, 0.015, 0.060, 0.005, 0.120)
            score += 0.35 * mom_score

            # ── Kaufman ER (20%) ───────────────────────────────────────────────
            # >0.20 trending, >0.40 strong; bad below 0.08 (random) or above 0.90 (exhausted)
            score += 0.20 * _score_band(kaufman_er, 0.20, 0.60, 0.08, 0.90)

            # ── Shannon Entropy (15%) ──────────────────────────────────────────
            # XAUUSD: good 2.5–3.2 nats (structured returns). Nifty reference used 2.4–3.1
            # which was too tight for gold's longer-period directional moves.
            score += 0.15 * _score_band(entropy_val, 2.50, 3.20, None, 3.60)

            # ── EMA-20 Slope (15%) ────────────────────────────────────────────
            # XAUUSD 1m: slopes of 0.00005 to 0.0003 are meaningful uptrends
            # Nifty reference used 0.00002→0.00020 which was too narrow for gold
            score += 0.15 * _interp(ema20_slope, 0.000_05, 0.000_30)

            # ── VWAP Distance (10%) ───────────────────────────────────────────
            # BUY slightly above VWAP is good; far above = overextended
            score += 0.10 * _score_band(vwap_dist, -0.0005, 0.003, -0.003, 0.006)

            # ── Session Score (5%) ────────────────────────────────────────────
            # Peak during London+NY (07:00–20:00 UTC); penalise Asia dead zone
            score += 0.05 * _session_score(utc_hour)

        elif direction in ("PUT", "SELL"):
            # ── Kalman Velocity — negative for SELL (35%) ─────────────────────
            if is_sideways:
                mom_score = _score_band(kalman_vel, -0.040, -0.008, -0.080, -0.002)
            else:
                mom_score = _score_band(kalman_vel, -0.060, -0.015, -0.120, -0.005)
            score += 0.35 * mom_score

            # ── Kaufman ER (20%) ───────────────────────────────────────────────
            score += 0.20 * _score_band(kaufman_er, 0.20, 0.60, 0.08, 0.90)

            # ── Shannon Entropy (15%) ──────────────────────────────────────────
            score += 0.15 * _score_band(entropy_val, 2.50, 3.20, None, 3.60)

            # ── EMA-20 Slope — negative for SELL (15%) ────────────────────────
            score += 0.15 * _interp(ema20_slope, -0.000_05, -0.000_30)

            # ── VWAP Distance (10%) ───────────────────────────────────────────
            # SELL below VWAP is good (dist < 0) but not too overextended
            score += 0.10 * _score_band(vwap_dist, -0.003, 0.0005, -0.006, 0.003)

            # ── Session Score (5%) ────────────────────────────────────────────
            score += 0.05 * _session_score(utc_hour)

        # Clamp to [0, 1]
        quant_score = min(1.0, max(0.0, score))

        # ── Acceleration Bonus/Penalty (±0.08) ────────────────────────────────
        kalman_accel = features.get("kalman_accel", 0.0)
        if direction in ("CALL", "BUY"):
            accel_adj = max(-0.08, min(0.08, kalman_accel * 2.0))
        else:
            accel_adj = max(-0.08, min(0.08, -kalman_accel * 2.0))
        quant_score = min(1.0, max(0.0, quant_score + accel_adj))

        # ── Volume Filter ─────────────────────────────────────────────────────
        vol_ratio = features.get("volume_ratio", 1.0)
        if vol_ratio < 0.70:
            quant_score *= 0.75    # thin market — penalise
        elif is_sideways and vol_ratio < 1.10:
            quant_score *= 0.85    # sideways + below-average volume — mild penalty

        # NOTE: RSI divergence penalty removed.
        # On XAUUSD 1m bars, RSI divergence from a 30-period window is too noisy
        # and was causing valid trend signals to be penalised incorrectly.

        return round(quant_score, 4)

    def score_signal(self, signal: Signal, ml_probability: float = 1.0,
                     regime: str = "", features: dict = None) -> ScoredTrade:
        """Calculates the final composite score."""
        features = features or {}
        quant_score = self.compute_quant_score(signal, regime, features)

        final_score = (self.w_ml * ml_probability) + (self.w_quant * quant_score)

        return ScoredTrade(
            signal=signal,
            ml_probability=round(ml_probability, 4),
            technical_strength=round(quant_score, 4),
            final_score=round(final_score, 4),
            regime=regime,
            details={
                "w_ml":        self.w_ml,
                "w_quant":     self.w_quant,
                "quant_score": quant_score,
            }
        )

    def filter_and_rank(self, signals: List[Signal], min_score_threshold: float,
                        regime: str = "", features: dict = None) -> List[ScoredTrade]:
        scored    = [self.score_signal(sig, regime=regime, features=features) for sig in signals]
        qualified = [t for t in scored if t.final_score >= min_score_threshold]
        qualified.sort(key=lambda x: x.final_score, reverse=True)
        return qualified


def _session_score(utc_hour: int) -> float:
    """
    Session quality score for XAUUSD based on UTC hour.
    Peak during London + NY overlap (12:00–17:00 UTC = 1.0).
    Zero in Asia dead zone (00:00–07:00 UTC).
    """
    if 0 <= utc_hour < 7:
        return 0.0    # Asia dead zone: no credit
    elif 7 <= utc_hour < 9:
        return 0.4    # London pre-open: light
    elif 9 <= utc_hour < 12:
        return 0.7    # London open: active
    elif 12 <= utc_hour < 17:
        return 1.0    # London + NY overlap: peak liquidity
    elif 17 <= utc_hour < 20:
        return 0.7    # NY afternoon: slowing
    else:
        return 0.2    # NY close + early Asia: very thin
