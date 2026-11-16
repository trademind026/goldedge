import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
import math
import numpy as np
import pandas as pd
from flask import Blueprint, request, jsonify
from dependency_injector.wiring import inject, Provide

from .....config.container import Container
from .....domain.ports.outbound.candle_repo import ICandleRepository
from .....domain.ports.outbound.market_data import IMarketDataProvider
from .....domain.models.pip_calculator import PipCalculator
from .....domain.models.bar import Bar
from .....adapters.secondary.features.forex_kalman_filter import apply_forex_adaptive_kalman
from .....gold.adapters.features.gold_kalman_filter import apply_gold_adaptive_kalman

logger = logging.getLogger("charts_api")

bp = Blueprint("charts", __name__, url_prefix="/api/charts")

TIMEFRAME_MAP = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1D"
}


def tag_session(dt: datetime) -> str:
    """Tags UTC timestamp with its 24/5 global trading session zone."""
    hour = dt.hour + (dt.minute / 60.0)
    if 12.0 <= hour < 16.0:
        return "LONDON_NY_OVERLAP"
    elif 7.0 <= hour < 16.0:
        return "LONDON"
    elif 12.0 <= hour < 21.0:
        return "NEW_YORK"
    elif 21.0 <= hour < 22.0:
        return "ROLLOVER"
    elif 0.0 <= hour < 9.0:
        return "TOKYO"
    return "SYDNEY"


@bp.route("/data", methods=["GET"])
@inject
def api_charts_data(
    candle_repo: ICandleRepository = Provide[Container.candle_repo],
    market_data: Optional[IMarketDataProvider] = Provide[Container.market_data_provider]
):
    """
    Multi-Timeframe Quantitative Chart Data Endpoint.
    Overlays:
    - 2D Kinematic Kalman Filter (price state x_1 & normalized velocity Z-score)
    - Rolling Shannon Entropy (Chop vs Trend Meter)
    - EMA 20 & EMA 50, ATR in pips, Order Flow Momentum
    - Session Shading Tagging (London/NY overlap, Rollover, etc.)
    """
    symbol = request.args.get("symbol", "EURUSD").upper().replace("/", "").replace("_", "")
    tf_arg = request.args.get("timeframe", "1m").lower()
    limit = int(request.args.get("limit", 300))
    limit = max(20, min(limit, 1000))

    pip_unit = PipCalculator.get_pip_unit(symbol)
    resample_tf = TIMEFRAME_MAP.get(tf_arg, "1min")

    # 1. Fetch real bars from Candle Repository (DB) or Broker (Market Data Provider)
    raw_bars: List[Bar] = []
    try:
        raw_bars = candle_repo.get_latest_bars(symbol, count=limit * 5 + 60)
    except Exception as e:
        logger.debug(f"DB candle query exception for {symbol}: {e}")

    # Fallback to Live Broker Historical Data
    if not raw_bars and market_data is not None and hasattr(market_data, "fetch_historical_bars"):
        try:
            raw_bars = market_data.fetch_historical_bars(symbol, "M1", count=limit * 5 + 60)
        except Exception as e:
            logger.debug(f"Broker historical candle query exception for {symbol}: {e}")

    # STRICT: If no data in DB and no data in broker, return 404. ZERO FAKE/SYNTHETIC DATA.
    if not raw_bars:
        return jsonify({
            "error": f"No candle data available for '{symbol}'. Ingest historical data or connect broker."
        }), 404

    records = []
    for b in raw_bars:
        spread_val = float(b.spread * 0.1) if b.spread is not None else 1.0
        records.append({
            "timestamp": b.time,
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "tick_volume": int(b.tick_volume),
            "spread_pips": spread_val
        })

    df = pd.DataFrame(records)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    # 2. Multi-Timeframe Resampling
    if tf_arg != "1m" and len(df) > 1:
        df = df.set_index("timestamp").resample(resample_tf).agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "tick_volume": "sum",
            "spread_pips": "mean"
        }).dropna().reset_index()

    if df.empty:
        return jsonify({"error": f"No resampled data available for {symbol} on {tf_arg}"}), 404

    # 3. 2D Kinematic Kalman Filter
    is_gold = "XAU" in symbol or "GOLD" in symbol
    if is_gold:
        kalman_df = apply_gold_adaptive_kalman(df["close"], df["tick_volume"])
    else:
        kalman_df = apply_forex_adaptive_kalman(
            price_series=df["close"],
            volume_series=df["tick_volume"],
            spread_series=df["spread_pips"],
            q_price=1e-4,
            q_velocity=1e-5,
            r_base=1.5 * pip_unit
        )

    df["kalman_price"] = kalman_df["kalman_price"].values
    raw_vel = kalman_df["kalman_velocity"]
    vel_std = raw_vel.rolling(20, min_periods=5).std().replace(0, np.nan)
    df["kalman_velocity_z"] = (raw_vel / vel_std).fillna(0.0).clip(-3.0, 3.0).round(3)

    # 4. Rolling Shannon Entropy of Returns (Market Noise vs Signal)
    returns = df["close"].pct_change().fillna(0.0)
    static_bins = [-np.inf, -0.0005, -0.0002, -0.0001, 0.0, 0.0001, 0.0002, 0.0005, np.inf]

    def calc_entropy(window):
        counts, _ = np.histogram(window, bins=static_bins)
        if counts.sum() == 0:
            return 1.5
        probs = counts / counts.sum()
        probs = probs[probs > 0]
        return float(-np.sum(probs * np.log2(probs)))

    df["entropy"] = returns.rolling(20, min_periods=8).apply(calc_entropy, raw=True).fillna(1.5).clip(0.0, 3.0).round(2)

    # 5. Standard Technical Indicators
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean().round(5 if not is_gold and "JPY" not in symbol else 2)
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean().round(5 if not is_gold and "JPY" not in symbol else 2)

    # ATR in Pips
    tr = np.maximum(
        df["high"] - df["low"],
        np.maximum((df["high"] - df["close"].shift(1)).abs(), (df["low"] - df["close"].shift(1)).abs())
    )
    df["atr_pips"] = (tr.rolling(14, min_periods=1).mean() / pip_unit).fillna(1.0).round(1)

    # Order flow momentum proxy (aggressor tick imbalance)
    df["order_flow_momentum"] = (
        (np.sign(returns).fillna(0) * np.log1p(df["tick_volume"]))
        .rolling(10, min_periods=1)
        .mean()
        .fillna(0.0)
        .round(2)
    )

    # 6. Session Zone Tagging
    df["session"] = df["timestamp"].apply(tag_session)

    # Trim to requested limit
    df = df.tail(limit).copy().reset_index(drop=True)
    latest_bar = df.iloc[-1]

    # Determine Regime
    curr_vel = float(latest_bar["kalman_velocity_z"])
    curr_entropy = float(latest_bar["entropy"])
    curr_close = float(latest_bar["close"])
    curr_ema20 = float(latest_bar["ema20"])

    if curr_entropy > 2.3:
        regime = "HIGH_VOLATILITY"
    elif curr_vel > 0.5 and curr_close > curr_ema20:
        regime = "TRENDING_BULL"
    elif curr_vel < -0.5 and curr_close < curr_ema20:
        regime = "TRENDING_BEAR"
    else:
        regime = "SIDEWAYS"

    # Format bars for JSON
    bars: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        bars.append({
            "timestamp": row["timestamp"].isoformat(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "tick_volume": int(row["tick_volume"]),
            "spread_pips": float(row["spread_pips"]),
            "kalman_price": round(float(row["kalman_price"]), 5 if not is_gold and "JPY" not in symbol else 2),
            "kalman_velocity_z": float(row["kalman_velocity_z"]),
            "entropy": float(row["entropy"]),
            "order_flow_momentum": float(row["order_flow_momentum"]),
            "ema20": float(row["ema20"]),
            "ema50": float(row["ema50"]),
            "atr_pips": float(row["atr_pips"]),
            "session": str(row["session"])
        })

    last_close = float(latest_bar["close"])
    last_spread_pips = float(latest_bar["spread_pips"])
    bid = last_close
    ask = round(last_close + (last_spread_pips * pip_unit), 5 if not is_gold and "JPY" not in symbol else 2)

    return jsonify({
        "symbol": symbol,
        "timeframe": tf_arg,
        "pip_unit": pip_unit,
        "current_quote": {
            "bid": bid,
            "ask": ask,
            "spread_pips": round(last_spread_pips, 1),
            "tick_time": latest_bar["timestamp"].isoformat()
        },
        "bars": bars,
        "statistics": {
            "volatility_atr_pips": round(float(latest_bar["atr_pips"]), 1),
            "current_regime": regime,
            "current_kalman_velocity": round(curr_vel, 2),
            "current_entropy": round(curr_entropy, 2),
            "session_status": str(latest_bar["session"])
        }
    })