"""
Pure-Python Domain ML Feature Governance Contract (Task G3.1).
Strict Hexagonal Architecture: ZERO pandas/sqlalchemy/infrastructure imports.

Explicitly enforces that unavailable or defective order-flow / Level II /
real-volume features (such as Lee-Ready midpoint aggressor `net_order_flow`
and `tick_momentum`) CANNOT enter Macro or Micro ML training/inference schemas.
"""

from __future__ import annotations

from typing import FrozenSet, Iterable, List, Optional

EXCLUDED_UNAVAILABLE_ML_FEATURES: FrozenSet[str] = frozenset(
    {
        "aggressor",
        "buy_vol",
        "sell_vol",
        "net_order_flow",
        "tick_momentum",
        "lee_ready_order_flow",
        "lee_ready_order_flow_and_l2_depth",
        "l2_order_book",
        "order_book_imbalance",
        "depth_imbalance",
        "real_volume",
        "vwap",
    }
)

ALLOWED_MACRO_ML_FEATURES: FrozenSet[str] = frozenset(
    {
        "ema20_ema50_spread_atr",
        "ema20_slope",
        "h1_ema20_ema50_trend",
        "rsi_14_5m",
        "atr_14_5m_pct_and_ratio",
        "adx_14_5m",
        "bb_width_and_position_20",
        "kaufman_er_20",
        "shannon_entropy_20",
        "regime_state_5m",
        "tick_weighted_price_dist",
        "tick_rate_ratio",
        "dist_to_20bar_swing_high_low_atr",
        "kalman_price_dist_vel_accel",
        "session_utc_cyclical_and_spread_bps",
    }
)

ALLOWED_MICRO_ML_FEATURES: FrozenSet[str] = frozenset(
    {
        "spread_usd_and_zscore_30t",
        "mid_return_5t_10t_30t",
        "quote_arrival_burst_ratio",
        "interarrival_time_stats_10t",
        "bid_ask_revision_imbalance_30t",
        "realized_vol_30t",
        "kalman_vel_accel_tick",
        "micro_range_position_30t",
    }
)


def validate_ml_feature_names(
    feature_names: Iterable[str],
    horizon: Optional[str] = None,
) -> List[str]:
    """
    Validate a candidate ML feature list against the GoldEdge feature contract.
    Raises ValueError immediately if any unavailable/defective order-flow, L2,
    or real-volume feature is present.
    """
    names = [str(f).strip() for f in feature_names]
    blocked = sorted({f for f in names if f.lower() in EXCLUDED_UNAVAILABLE_ML_FEATURES})
    if blocked:
        raise ValueError(
            f"Unavailable/defective ML features are strictly excluded from GoldEdge ML schemas: {blocked}"
        )
    if horizon is not None:
        h = horizon.strip().lower()
        if h == "macro":
            unknown = sorted({f for f in names if f not in ALLOWED_MACRO_ML_FEATURES})
            if unknown:
                raise ValueError(f"Unregistered Macro ML features: {unknown}")
        elif h == "micro":
            unknown = sorted({f for f in names if f not in ALLOWED_MICRO_ML_FEATURES})
            if unknown:
                raise ValueError(f"Unregistered Micro ML features: {unknown}")
        else:
            raise ValueError(f"Unknown ML horizon '{horizon}'; expected 'macro' or 'micro'.")
    return names
