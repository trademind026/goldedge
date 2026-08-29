import numpy as np
import pandas as pd
import logging
from ..models.regime import MarketRegime

logger = logging.getLogger(__name__)

class RegimeDetectorService:
    """
    Detects market regime from a DataFrame of OHLCV candles enriched with indicators.
    Adapted from the AI-trader reference architecture.
    """

    def __init__(
        self,
        vol_high_pct: float = 75,
        vol_low_pct: float = 25,
        trend_threshold: float = 0.002,
        atr_period: int = 14
    ):
        self.vol_high_pct = vol_high_pct
        self.vol_low_pct = vol_low_pct
        self.trend_threshold = trend_threshold
        self.atr_period = atr_period

    def detect(self, df: pd.DataFrame) -> MarketRegime:
        """
        Takes a DataFrame containing at least 'high', 'low', 'close', 'ema20', 'ema50'.
        Returns the detected MarketRegime.
        """
        if df.empty or len(df) < 20:
            return MarketRegime.UNKNOWN

        # ── EMA Trend ────────────────────────────────────────────────────────
        ema_s = df["ema20"].iloc[-1]
        ema_l = df["ema50"].iloc[-1]
        ema_s_prev = df["ema20"].iloc[-5] if len(df) >= 5 else ema_s

        ema_diff = (ema_s - ema_l) / ema_l
        ema_slope = (ema_s - ema_s_prev) / ema_s_prev

        # ── ATR / Volatility ─────────────────────────────────────────────────
        # True Range
        tr1 = df["high"] - df["low"]
        tr2 = (df["high"] - df["close"].shift(1)).abs()
        tr3 = (df["low"] - df["close"].shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        atr = tr.rolling(self.atr_period).mean()
        atr_pct = atr / df["close"]  # ATR as % of price

        current_atr_pct = atr_pct.iloc[-1]
        atr_history = atr_pct.dropna()
        
        if len(atr_history) > 10:
            vol_high_threshold = np.percentile(atr_history, self.vol_high_pct)
            vol_low_threshold = np.percentile(atr_history, self.vol_low_pct)
        else:
            vol_high_threshold = float('inf')
            vol_low_threshold = 0.0

        # ── Price Range Compression (sideways detection) ─────────────────────
        recent_high = df["high"].tail(20).max()
        recent_low = df["low"].tail(20).min()
        range_pct = (recent_high - recent_low) / df["close"].iloc[-1]

        # ── Classification Logic ─────────────────────────────────────────────

        # Priority 1: Extreme volatility
        if current_atr_pct > vol_high_threshold:
            regime = MarketRegime.HIGH_VOLATILITY
        elif current_atr_pct < vol_low_threshold:
            regime = MarketRegime.LOW_VOLATILITY
        # Priority 2: Clear trend
        elif ema_diff > self.trend_threshold and ema_slope > 0:
            regime = MarketRegime.TRENDING_BULL
        elif ema_diff < -self.trend_threshold and ema_slope < 0:
            regime = MarketRegime.TRENDING_BEAR
        # Priority 3: Sideways (range-bound)
        elif range_pct < 0.005:  # Tweaked threshold for generic assets
            regime = MarketRegime.SIDEWAYS
        # Default
        elif ema_diff > 0:
            regime = MarketRegime.TRENDING_BULL
        elif ema_diff < 0:
            regime = MarketRegime.TRENDING_BEAR
        else:
            regime = MarketRegime.SIDEWAYS

        logger.debug(
            f"Regime: {regime.value} "
            f"(ema_diff={ema_diff:.4f}, atr%={current_atr_pct:.4f}, range%={range_pct:.4f})"
        )
        return regime
