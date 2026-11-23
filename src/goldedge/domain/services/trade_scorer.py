import logging
import math
from typing import List, Optional
from ..models.signal import Signal
from ..models.scored_trade import ScoredTrade

logger = logging.getLogger(__name__)

VALID_SCORER_MODES = ("legacy", "g2_calibrated")


def _normalize_scorer_mode(scorer_mode: Optional[str]) -> str:
    mode = str(scorer_mode or "legacy").strip().lower()
    if mode not in VALID_SCORER_MODES:
        raise ValueError(f"Unsupported scorer_mode={scorer_mode!r}; expected one of {VALID_SCORER_MODES}")
    return mode


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

    def _compute_g2_calibrated_quant_score(
        self,
        signal: Signal,
        regime: str,
        features: dict,
        use_tick_intensity_features: bool = True,
    ) -> tuple[float, dict]:
        """
        G2 calibrated & decorrelated quantitative scorer (pure Python, zero external libs).
        Fixes 5m Shannon entropy saturation, 5m EMA20 slope saturation/monotonicity,
        SIDEWAYS Kalman threshold halving, $/tick unit mismatch, inverted TWP distance,
        single-tick acceleration noise, and severe EMA20 overextension.
        """
        direction = signal.direction.value
        if direction not in ("CALL", "BUY", "PUT", "SELL"):
            return 0.0, {"scorer_mode": "g2_calibrated"}

        d_sign = 1.0 if direction in ("CALL", "BUY") else -1.0
        atr = float(features.get("atr", 1.0) or 1.0)
        entry_5m_tr = float(features.get("entry_5m_tick_rate", 0.0) or 0.0)
        if "kalman_vel_1m_atr" in features:
            kv_1m_atr = float(features["kalman_vel_1m_atr"])
        else:
            kv = float(features.get("kalman_vel", 0.0))
            kv_1m_atr = (kv * (max(entry_5m_tr, 1.0) / 5.0)) / max(atr, 0.50)

        ent_5m_norm = float(features.get("shannon_entropy_5m_norm", 1.0))
        er = float(features.get("kaufman_er", 0.0))
        slp = float(features.get("ema20_slope", 0.0))
        twd = float(features.get("tick_weighted_price_dist", features.get("vwap_dist", 0.0)))
        ext_atr = float(features.get("ema20_dist_atr", 0.0))
        trr = float(features.get("tick_rate_ratio", 1.0))
        utc_hour = int(features.get("utc_hour", 12))
        is_sideways = "SIDEWAYS" in regime.upper()

        c_mom = _score_band(d_sign * kv_1m_atr, 0.12, 0.48, -0.10, 1.00)
        c_er = _score_band(er, 0.20, 0.55, 0.08, 0.85)
        c_ent = _score_band(1.0 - ent_5m_norm, 0.18, 1.0, 0.06, None)
        c_slp = _score_band(d_sign * slp, 0.00025, 0.00080, 0.00010, 0.00150)
        c_twd = _score_band(d_sign * twd, 0.0050, 0.0150, 0.0015, None)
        c_ses = _session_score(utc_hour)

        if use_tick_intensity_features:
            base_s = 0.20 * c_mom + 0.15 * c_er + 0.20 * c_ent + 0.15 * c_slp + 0.25 * c_twd + 0.05 * c_ses
        else:
            base_s = ((0.20 * c_mom + 0.15 * c_er + 0.20 * c_ent + 0.15 * c_slp) / 0.70) * 0.95 + 0.05 * c_ses

        qs = min(1.0, max(0.0, base_s))
        trr_mult = 1.0
        if use_tick_intensity_features:
            if trr < 0.70:
                trr_mult = 0.75
            elif is_sideways and trr < 1.10:
                trr_mult = 0.85

        ext_mult = 0.55 + 0.45 * _score_band(d_sign * ext_atr, -1.0, 1.80, None, 3.00)
        quant_score = round(min(1.0, max(0.0, qs * trr_mult * ext_mult)), 4)

        comp = {
            "scorer_mode": "g2_calibrated",
            "sub_mom_kalman": round(c_mom, 4),
            "sub_er": round(c_er, 4),
            "sub_entropy": round(c_ent, 4),
            "sub_ema_slope": round(c_slp, 4),
            "sub_twp_breakout": round(c_twd, 4),
            "sub_session": round(c_ses, 4),
            "base_score_pre_accel": round(base_s, 4),
            "accel_adj": 0.0,
            "trr_mult": round(trr_mult, 4),
            "ext_mult": round(ext_mult, 4),
        }
        return quant_score, comp

    def compute_quant_score(
        self,
        signal: Signal,
        regime: str,
        features: dict,
        use_tick_intensity_features: bool = True,
        scorer_mode: str = "legacy",
    ) -> float:
        """
        Model-Free Quantitative Scoring Engine — XAUUSD 1m bars.

        Feature value ranges (empirical for XAUUSD @ ~$4000–4700):
          kalman_vel              : per-tick price change, typical ±0.001–0.05 for strong moves
          kaufman_er              : 0.0–1.0; >0.20 = trending, >0.40 = strong trend on 1m
          shannon_ent             : 2.5–3.5 nats for 30-bar returns; <3.2 = structured/directional
          ema20_slope             : normalized, typical ±0.00005–0.0003 on 1m XAUUSD bars
          tick_weighted_price_dist: fractional distance from quote-weighted average, ±0.001–0.005
          tick_rate_ratio         : quote arrival rate vs 20-bar SMA (1.0 = avg; NOT traded volume)
          utc_hour                : bar UTC hour (0–23) for session scoring
        """
        mode = _normalize_scorer_mode(scorer_mode)
        if mode == "g2_calibrated":
            quant_score, _ = self._compute_g2_calibrated_quant_score(
                signal,
                regime,
                features,
                use_tick_intensity_features=use_tick_intensity_features,
            )
            return quant_score

        direction   = signal.direction.value
        kalman_vel  = features.get("kalman_vel", 0.0)
        entropy_val = features.get("shannon_entropy", 3.0)
        kaufman_er  = features.get("kaufman_er", 0.0)
        ema20_slope = features.get("ema20_slope", 0.0)
        twp_dist    = features.get("tick_weighted_price_dist", features.get("vwap_dist", 0.0))
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

            # ── Tick-Weighted Price Distance (10%) ───────────────────────────
            if use_tick_intensity_features:
                score += 0.10 * _score_band(twp_dist, -0.0005, 0.003, -0.003, 0.006)
            else:
                score = score / 0.90  # renormalize pure price/Kalman components without quote-weighted price

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

            # ── Tick-Weighted Price Distance (10%) ───────────────────────────
            if use_tick_intensity_features:
                score += 0.10 * _score_band(twp_dist, -0.003, 0.0005, -0.006, 0.003)
            else:
                score = score / 0.90

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

        # ── Quote-Arrival Activity Filter (`tick_rate_ratio`, NOT traded volume) ─
        if use_tick_intensity_features:
            tick_ratio = features.get("tick_rate_ratio", 1.0)
            if tick_ratio < 0.70:
                quant_score *= 0.75    # low quote-arrival rate — penalise
            elif is_sideways and tick_ratio < 1.10:
                quant_score *= 0.85    # sideways + below-average tick rate — mild penalty

        # NOTE: RSI divergence penalty removed.
        # On XAUUSD 1m bars, RSI divergence from a 30-period window is too noisy
        # and was causing valid trend signals to be penalised incorrectly.

        return round(quant_score, 4)

    def score_signal(self, signal: Signal, ml_probability: float = 1.0,
                     regime: str = "", features: dict = None,
                     use_tick_intensity_features: bool = True,
                     scorer_mode: str = "legacy") -> ScoredTrade:
        """Calculates the final composite score."""
        mode = _normalize_scorer_mode(scorer_mode)
        features = features or {}
        extra_details: dict = {"scorer_mode": mode}
        if mode == "g2_calibrated":
            quant_score, g2_details = self._compute_g2_calibrated_quant_score(
                signal,
                regime,
                features,
                use_tick_intensity_features=use_tick_intensity_features,
            )
            extra_details.update(g2_details)
        else:
            quant_score = self.compute_quant_score(
                signal,
                regime,
                features,
                use_tick_intensity_features=use_tick_intensity_features,
                scorer_mode=mode,
            )

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
                **extra_details,
            }
        )

    @staticmethod
    def compute_institutional_quant_score(
        s_trend: float,
        vel_norm: float,
        norm_entropy: float
    ) -> float:
        """
        Stage 3.1: Model-Free Quantitative Math Engine.
        quant_score = (0.40 * S_trend) + (0.35 * S_kalman) + (0.25 * (1.0 - H_t))
        Hard Veto: If quant_score < 0.80, candidate is killed.
        """
        s_kalman = min(1.0, max(0.0, abs(vel_norm) / 2.5))
        cleanliness = max(0.0, min(1.0, 1.0 - norm_entropy))
        score = (0.40 * s_trend) + (0.35 * s_kalman) + (0.25 * cleanliness)
        return round(float(score), 4)

    @staticmethod
    def compute_composite_final_score(
        quant_score: float,
        directional_prob: float,
        s_flow: float = 1.0,
        s_tech: float = 1.0
    ) -> float:
        """
        Stage 3.2 & 3.4: Composite Formula Qualification.
        final_score = (0.35 * quant_score) + (0.35 * directional_prob) + (0.20 * S_flow) + (0.10 * S_tech)
        Requires final_score >= 0.75.
        """
        score = (0.35 * quant_score) + (0.35 * directional_prob) + (0.20 * s_flow) + (0.10 * s_tech)
        return round(float(score), 4)

    @staticmethod
    def compute_macro_composite_score(
        quant_score: float,
        macro_prob: float,
    ) -> float:
        """
        Macro Model 50% Advisory Weight:
        Composite = 0.50 * quant_score + 0.50 * macro_prob
        """
        score = (0.50 * quant_score) + (0.50 * macro_prob)
        return round(float(score), 4)

    def filter_and_rank(self, signals: List[Signal], min_score_threshold: float,
                        regime: str = "", features: dict = None,
                        use_tick_intensity_features: bool = True,
                        scorer_mode: str = "legacy") -> List[ScoredTrade]:
        mode = _normalize_scorer_mode(scorer_mode)
        scored = [
            self.score_signal(
                sig,
                regime=regime,
                features=features,
                use_tick_intensity_features=use_tick_intensity_features,
                scorer_mode=mode,
            )
            for sig in signals
        ]
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
