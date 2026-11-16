import numpy as np
import pandas as pd
from typing import Optional, Tuple, List


class DynamicATRTripleBarrierLabeler:
    """
    1.2 Target Labeling: The Dynamic ATR Triple Barrier Method.
    (Marcos López de Prado: Advances in Financial Machine Learning)

    Upper Horizontal Barrier (U):
      BUY : Price_t + (2.0 * ATR_14)
      SELL: Price_t - (2.0 * ATR_14)
    Lower Horizontal Barrier (L):
      BUY : Price_t - (1.0 * ATR_14)
      SELL: Price_t + (1.0 * ATR_14)
    Vertical Time Barrier (T):
      Expiration after max_holding_bars (default: 30 completed M1 bars).

    Class Assignment:
      - Touches U first -> y = 1 (Win / Target reached)
      - Touches L first -> y = 0 (Loss / Stop-out)
      - Hits T first without touching U or L -> y = 0 (Stagnation penalty)
    """

    def __init__(
        self,
        atr_upper_mult: float = 2.0,
        atr_lower_mult: float = 1.0,
        max_holding_bars: int = 30,
        atr_period: int = 14,
    ):
        self.atr_upper_mult = atr_upper_mult
        self.atr_lower_mult = atr_lower_mult
        self.max_holding_bars = max_holding_bars
        self.atr_period = atr_period

    def label_candidate(
        self,
        df: pd.DataFrame,
        entry_idx: int,
        direction: int = 1,  # +1 = BUY, -1 = SELL
    ) -> int:
        """
        Evaluates future bars from entry_idx up to entry_idx + max_holding_bars.
        Returns: 1 for successful trade, 0 for failed trade or stagnation.
        """
        if entry_idx >= len(df) - 1:
            return 0

        entry_bar = df.iloc[entry_idx]
        entry_price = float(entry_bar["close"])

        # Calculate or extract ATR14
        if "atr" in entry_bar and not np.isnan(entry_bar["atr"]):
            atr = float(entry_bar["atr"])
        else:
            # Fallback local ATR calculation
            start_lookback = max(0, entry_idx - self.atr_period)
            sub = df.iloc[start_lookback:entry_idx + 1]
            tr = np.maximum(
                sub["high"] - sub["low"],
                np.maximum(
                    abs(sub["high"] - sub["close"].shift(1)),
                    abs(sub["low"] - sub["close"].shift(1))
                )
            )
            atr = float(tr.tail(self.atr_period).mean()) if len(tr) >= 2 else (entry_price * 0.0005)

        atr = max(atr, entry_price * 0.0001)

        # Set barriers
        if direction >= 0:  # BUY
            upper_barrier = entry_price + (self.atr_upper_mult * atr)
            lower_barrier = entry_price - (self.atr_lower_mult * atr)
        else:  # SELL
            upper_barrier = entry_price - (self.atr_upper_mult * atr)
            lower_barrier = entry_price + (self.atr_lower_mult * atr)

        # Scan forward bars
        end_idx = min(entry_idx + self.max_holding_bars + 1, len(df))
        future_bars = df.iloc[entry_idx + 1:end_idx]

        for _, bar in future_bars.iterrows():
            high_px = float(bar["high"])
            low_px = float(bar["low"])

            if direction >= 0:  # BUY
                hit_upper = high_px >= upper_barrier
                hit_lower = low_px <= lower_barrier

                if hit_upper and not hit_lower:
                    return 1
                if hit_lower and not hit_upper:
                    return 0
                if hit_upper and hit_lower:
                    # Intra-bar ambiguity: check if open is closer to one barrier
                    open_px = float(bar["open"])
                    if abs(open_px - upper_barrier) < abs(open_px - lower_barrier):
                        return 1
                    return 0
            else:  # SELL
                hit_upper = low_px <= upper_barrier   # Target for SELL
                hit_lower = high_px >= lower_barrier  # Stop for SELL

                if hit_upper and not hit_lower:
                    return 1
                if hit_lower and not hit_upper:
                    return 0
                if hit_upper and hit_lower:
                    open_px = float(bar["open"])
                    if abs(open_px - upper_barrier) < abs(open_px - lower_barrier):
                        return 1
                    return 0

        # Vertical barrier hit without touching U or L -> Opportunity cost penalty y = 0
        return 0

    def label_dataset(
        self,
        df: pd.DataFrame,
        signals_series: pd.Series,  # +1 for BUY, -1 for SELL, 0 for None
    ) -> pd.Series:
        """Labels all candidate signals in a dataset."""
        labels = pd.Series(index=df.index, dtype=float)
        for i, sig in enumerate(signals_series):
            if sig != 0 and not np.isnan(sig):
                labels.iloc[i] = self.label_candidate(df, i, direction=int(sig))
            else:
                labels.iloc[i] = np.nan
        return labels
