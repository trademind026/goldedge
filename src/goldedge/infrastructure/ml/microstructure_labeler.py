import numpy as np
import pandas as pd
from typing import Optional


class MicrostructureExplosiveBreakoutLabeler:
    """
    2.2 Target Labeling: The Explosive Micro-Breakout Formula.
    Predicts zero-slippage entry velocity over the next 60 seconds:

      y_micro = 1 <=> (Delta_Price_60s / Spread >= 2.5) AND (Adverse Excursion_60s <= 0.5 * Spread)

    Target = 1 (Clean Breakout): Price moves favorably by at least 2.5x spread
             within 60s without pulling back more than 0.5x spread.
    Target = 0 (Fakeout / Trap): Price stalls, pulls back into spread, or moves against trade.
    """

    def __init__(
        self,
        favorable_spread_mult: float = 2.5,
        max_adverse_spread_mult: float = 0.5,
        horizon_seconds: float = 60.0,
    ):
        self.favorable_spread_mult = favorable_spread_mult
        self.max_adverse_spread_mult = max_adverse_spread_mult
        self.horizon_seconds = horizon_seconds

    def label_ticks(
        self,
        ticks_df: pd.DataFrame,
        indices: list,
        direction: int = 1,
    ) -> np.ndarray:
        """
        Fast vectorized labeling of multiple ticks using numpy searchsorted.
        Avoids redundant DataFrame copies and executes in sub-second time.
        """
        time_col = "timestamp" if "timestamp" in ticks_df.columns else ("time" if "time" in ticks_df.columns else ticks_df.columns[0])
        times = pd.to_datetime(ticks_df[time_col]).values
        bids = pd.to_numeric(ticks_df["bid"], errors="coerce").values
        asks = pd.to_numeric(ticks_df["ask"], errors="coerce").values
        if "price" in ticks_df.columns:
            prices = pd.to_numeric(ticks_df["price"], errors="coerce").values
            mask_nan = np.isnan(prices)
            if np.any(mask_nan):
                prices[mask_nan] = (bids[mask_nan] + asks[mask_nan]) / 2.0
        else:
            prices = (bids + asks) / 2.0

        horizon_ns = np.timedelta64(int(self.horizon_seconds * 1e9), "ns")
        labels = np.zeros(len(indices), dtype=int)

        for out_idx, tick_idx in enumerate(indices):
            if tick_idx >= len(prices) - 1:
                labels[out_idx] = 0
                continue

            entry_time = times[tick_idx]
            entry_px = prices[tick_idx]
            spread = max(asks[tick_idx] - bids[tick_idx], 1e-5)

            favorable_threshold = self.favorable_spread_mult * spread
            adverse_cap = self.max_adverse_spread_mult * spread

            cutoff_time = entry_time + horizon_ns
            end_idx = np.searchsorted(times, cutoff_time, side="right")

            if end_idx <= tick_idx + 1:
                labels[out_idx] = 0
                continue

            future_pxs = prices[tick_idx + 1:end_idx]
            if len(future_pxs) == 0:
                labels[out_idx] = 0
                continue

            if direction >= 0:
                favorable_moves = future_pxs - entry_px
                adverse_moves = entry_px - future_pxs
            else:
                favorable_moves = entry_px - future_pxs
                adverse_moves = future_pxs - entry_px

            max_adverse = np.max(adverse_moves)
            max_favorable = np.max(favorable_moves)

            if max_favorable >= favorable_threshold and max_adverse <= adverse_cap:
                labels[out_idx] = 1
            else:
                labels[out_idx] = 0

        return labels

    def label_tick(
        self,
        ticks_df: pd.DataFrame,
        tick_idx: int,
        direction: int = 1,  # +1 = BUY, -1 = SELL
    ) -> int:
        """
        Labels a single tick based on future price trajectory within horizon_seconds.
        """
        res = self.label_ticks(ticks_df, [tick_idx], direction=direction)
        return int(res[0])
