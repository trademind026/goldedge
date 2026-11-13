import numpy as np
import pandas as pd
import logging
from collections import deque
from ..models.regime import MarketRegime

logger = logging.getLogger(__name__)

class RegimeDetectorService:
    """
    Detects market regime from a DataFrame of OHLCV candles enriched with indicators.

    XAUUSD-calibrated:
      - trend_threshold raised to 0.003 (EMA crossover must be more decisive for gold)
      - sideways range_pct tightened to 0.004 (XAUUSD 20-bar range of $16 on $4500 = 0.36%)
      - Hysteresis: regime only changes when new classification holds for 3 consecutive bars.
        This eliminates the SIDEWAYS <-> LOW_VOLATILITY oscillation seen every few minutes.
    """

    def __init__(
        self,
        vol_high_pct: float = 75,
        vol_low_pct: float = 25,
        trend_threshold: float = 0.003,   # raised from 0.002 — XAUUSD needs decisive EMA cross
        atr_period: int = 14,
        hysteresis_bars: int = 3,          # regime must persist N bars before switching
    ):
        self.vol_high_pct = vol_high_pct
        self.vol_low_pct = vol_low_pct
        self.trend_threshold = trend_threshold
        self.atr_period = atr_period
        self.hysteresis_bars = hysteresis_bars

        # Ring buffer tracking recent raw regime detections (before hysteresis filter)
        self._raw_history: deque = deque(maxlen=hysteresis_bars)
        self._committed_regime: MarketRegime = MarketRegime.UNKNOWN

    def detect(self, df: pd.DataFrame) -> MarketRegime:
        """
        Takes a DataFrame containing at least 'high', 'low', 'close', 'ema20', 'ema50'.
        Returns the hysteresis-filtered MarketRegime.
        """
        raw = self._raw_detect(df)
        self._raw_history.append(raw)

        # Only switch committed regime when ALL recent raw detections agree
        if len(self._raw_history) == self.hysteresis_bars:
            all_same = len(set(self._raw_history)) == 1
            if all_same:
                self._committed_regime = self._raw_history[-1]

        return self._committed_regime

    def _raw_detect(self, df: pd.DataFrame) -> MarketRegime:
        """Raw regime classification without hysteresis."""
        if df.empty or len(df) < 20:
            return MarketRegime.UNKNOWN

        # ── EMA Trend ────────────────────────────────────────────────────────
        ema_s = df["ema20"].iloc[-1]
        ema_l = df["ema50"].iloc[-1]
        ema_s_prev = df["ema20"].iloc[-5] if len(df) >= 5 else ema_s

        ema_diff = (ema_s - ema_l) / ema_l
        ema_slope = (ema_s - ema_s_prev) / ema_s_prev

        # ── ATR / Volatility ─────────────────────────────────────────────────
        tr1 = df["high"] - df["low"]
        tr2 = (df["high"] - df["close"].shift(1)).abs()
        tr3 = (df["low"] - df["close"].shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        atr = tr.rolling(self.atr_period).mean()
        atr_pct = atr / df["close"]   # ATR as % of price

        current_atr_pct = atr_pct.iloc[-1]
        atr_history = atr_pct.dropna()

        if len(atr_history) > 10:
            vol_high_threshold = np.percentile(atr_history, self.vol_high_pct)
            vol_low_threshold  = np.percentile(atr_history, self.vol_low_pct)
        else:
            vol_high_threshold = float("inf")
            vol_low_threshold  = 0.0

        # ── Price Range Compression (sideways detection) ─────────────────────
        # XAUUSD: 20-bar range < 0.4% of price = tight sideways
        recent_high = df["high"].tail(20).max()
        recent_low  = df["low"].tail(20).min()
        range_pct   = (recent_high - recent_low) / df["close"].iloc[-1]

        # ── Classification Logic (priority order) ────────────────────────────

        # Priority 1: Extreme volatility (news events, spikes)
        if current_atr_pct > vol_high_threshold:
            return MarketRegime.HIGH_VOLATILITY

        elif current_atr_pct < vol_low_threshold:
            return MarketRegime.LOW_VOLATILITY

        # Priority 2: Clear trend (EMA diff > 0.3% AND slope agrees)
        elif ema_diff > self.trend_threshold and ema_slope > 0:
            return MarketRegime.TRENDING_BULL

        elif ema_diff < -self.trend_threshold and ema_slope < 0:
            return MarketRegime.TRENDING_BEAR

        # Priority 3: Sideways (tight range, no trend)
        elif range_pct < 0.004:
            return MarketRegime.SIDEWAYS

        # Default: weak trend direction
        elif ema_diff > 0:
            return MarketRegime.TRENDING_BULL
        elif ema_diff < 0:
            return MarketRegime.TRENDING_BEAR
        else:
            return MarketRegime.SIDEWAYS
