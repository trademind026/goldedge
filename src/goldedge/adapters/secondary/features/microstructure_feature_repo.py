import numpy as np
import pandas as pd
from typing import Any
from ....domain.ports.outbound.microstructure_repo import IMicrostructureFeatureRepository


def compute_forex_micro_features(tick_df: pd.DataFrame, window_seconds: int = 30) -> pd.DataFrame:
    """
    MODULE 3: FOREX MICROSTRUCTURE & ORDER FLOW FEATURES
    Computes microstructure features:
    - Spread expansion
    - Order flow tick momentum (Lee-Ready trade classification)
    - Tick intensity / burst ratio
    """
    if tick_df.empty:
        return pd.DataFrame()

    df = tick_df.copy()
    time_col = "timestamp" if "timestamp" in df.columns else ("time" if "time" in df.columns else df.columns[0])
    df[time_col] = pd.to_datetime(df[time_col])
    df = df.sort_values(time_col)

    # Price handling: use price or mid-price
    if "price" not in df.columns:
        df["price"] = (df["bid"] + df["ask"]) / 2.0

    # Spread
    df["spread"] = (df["ask"] - df["bid"]).clip(lower=0)

    # Aggressor classification (Lee-Ready)
    # price >= ask -> Buyer-initiated (1), price <= bid -> Seller-initiated (-1)
    df["aggressor"] = 0
    df.loc[df["price"] >= df["ask"], "aggressor"] = 1
    df.loc[df["price"] <= df["bid"], "aggressor"] = -1

    vol = df["volume"] if "volume" in df.columns else pd.Series(1, index=df.index)
    df["buy_vol"] = (df["aggressor"] == 1).astype(int) * vol
    df["sell_vol"] = (df["aggressor"] == -1).astype(int) * vol

    # Resample to 1-second snapshots
    df = df.set_index(time_col)
    agg = df.resample("1s").agg({
        "price": "last",
        "spread": "mean",
        "buy_vol": "sum",
        "sell_vol": "sum",
        "aggressor": "count"  # tick count
    }).dropna()

    agg = agg.rename(columns={"aggressor": "tick_count"})

    # Order Flow Imbalance (OFI)
    agg["net_order_flow"] = agg["buy_vol"] - agg["sell_vol"]
    agg["tick_momentum"] = agg["net_order_flow"].rolling(window=window_seconds, min_periods=1).sum()

    # Tick Burst
    short_ticks = agg["tick_count"].rolling(window=5, min_periods=1).sum()
    long_ticks = agg["tick_count"].rolling(window=window_seconds, min_periods=1).sum()
    agg["burst_ratio"] = (short_ticks / long_ticks.replace(0, np.nan)).fillna(1.0)

    return agg


class MicrostructureFeatureRepository(IMicrostructureFeatureRepository):
    """
    Adapter implementing IMicrostructureFeatureRepository port for live and historical ticks.
    """

    def compute_micro_features(self, tick_data: Any, window_seconds: int = 30) -> pd.DataFrame:
        if isinstance(tick_data, pd.DataFrame):
            return compute_forex_micro_features(tick_data, window_seconds=window_seconds)
        elif isinstance(tick_data, list):
            df = pd.DataFrame(tick_data)
            return compute_forex_micro_features(df, window_seconds=window_seconds)
        else:
            return pd.DataFrame()
