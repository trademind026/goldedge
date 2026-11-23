import numpy as np
import pandas as pd
from typing import Any
from ....domain.ports.outbound.microstructure_repo import IMicrostructureFeatureRepository


def compute_forex_micro_features(
    tick_df: pd.DataFrame,
    window_seconds: int = 30,
    include_disabled_order_flow: bool = False,
) -> pd.DataFrame:
    """
    MODULE 3: FOREX QUOTE-ARRIVAL MICROSTRUCTURE FEATURES (QUOTE_ARRIVAL_SEMANTICS_V1)

    Computes valid top-of-book quote microstructure features:
    - price (last valid midpoint)
    - spread (mean bid/ask spread)
    - tick_count (valid quote arrival count per 1s bucket)
    - burst_ratio (5s vs window_seconds quote-arrival burst ratio)

    IMPORTANT (Task G3.1):
    GoldEdge XAUUSD spot data contains top-of-book bid/ask quotes only (no executed
    trade prints, real_volume IS NULL, no Level II order book). Lee-Ready aggressor
    classification (`price >= ask` / `price <= bid`) evaluates to 0 on 100% of valid
    midpoint quotes and MUST NOT be fed into ML pipelines as order-flow pressure.
    Accordingly, `buy_vol`, `sell_vol`, `net_order_flow`, and `tick_momentum` are
    explicitly disabled and excluded unless a caller passes real trade prints
    (currently unsupported -> raises ValueError if `include_disabled_order_flow=True`).
    """
    if include_disabled_order_flow:
        raise ValueError(
            "Lee-Ready order-flow features (buy_vol, sell_vol, net_order_flow, tick_momentum) "
            "are disabled in GoldEdge because XAUUSD spot data has no executed trade prints or L2 book."
        )

    if tick_df.empty:
        return pd.DataFrame()

    df = tick_df.copy()
    time_col = "timestamp" if "timestamp" in df.columns else ("time" if "time" in df.columns else df.columns[0])
    df[time_col] = pd.to_datetime(df[time_col])
    df = df.sort_values(time_col)

    # Strict valid quote filter (bid > 0 & ask > 0 & bid < ask)
    if "bid" in df.columns and "ask" in df.columns:
        df = df[
            df["bid"].notna()
            & df["ask"].notna()
            & (df["bid"] > 0.0)
            & (df["ask"] > 0.0)
            & (df["bid"] < df["ask"])
        ].copy()
        if df.empty:
            return pd.DataFrame()

    # Price handling: use midpoint
    if "price" not in df.columns:
        df["price"] = (df["bid"] + df["ask"]) / 2.0

    # Spread
    df["spread"] = (df["ask"] - df["bid"]).clip(lower=0)
    df["_tick_unit"] = 1

    # Resample to 1-second snapshots (quote-arrival semantics only)
    df = df.set_index(time_col)
    agg = df.resample("1s").agg(
        {
            "price": "last",
            "spread": "mean",
            "_tick_unit": "count",
        }
    ).dropna(subset=["price"])

    agg = agg.rename(columns={"_tick_unit": "tick_count"})

    # Quote-arrival burst ratio (NOT traded volume)
    short_ticks = agg["tick_count"].rolling(window=5, min_periods=1).sum()
    long_ticks = agg["tick_count"].rolling(window=window_seconds, min_periods=1).sum()
    agg["burst_ratio"] = (short_ticks / long_ticks.replace(0, np.nan)).fillna(1.0)

    return agg


class MicrostructureFeatureRepository(IMicrostructureFeatureRepository):
    """
    Adapter implementing IMicrostructureFeatureRepository port for live and historical ticks.
    Emits valid quote-arrival features only; defective Lee-Ready order-flow columns are excluded.
    """

    def compute_micro_features(self, tick_data: Any, window_seconds: int = 30) -> pd.DataFrame:
        if isinstance(tick_data, pd.DataFrame):
            return compute_forex_micro_features(tick_data, window_seconds=window_seconds)
        elif isinstance(tick_data, list):
            df = pd.DataFrame(tick_data)
            return compute_forex_micro_features(df, window_seconds=window_seconds)
        else:
            return pd.DataFrame()
