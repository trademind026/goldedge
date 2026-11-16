import numpy as np
import pandas as pd
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone, timedelta
from ...domain.models.pip_calculator import PipCalculator


class MicrostructureFeatureEngine:
    """
    STAGE 3.2 & MODEL 2: Microstructure Feature Engineering Matrix (16 Features).
    Computed on rolling tick packets (30-second sliding window):
    1. Order Flow Imbalance (OFI) Metrics (4 Features)
    2. Order Book Depth & Spread Dynamics (4 Features)
    3. Tick Arrival Velocity & Aggression (4 Features)
    4. Micro-Momentum & Wick Exhaustion (4 Features)
    """

    FEATURE_NAMES: List[str] = [
        # 1. Order Flow Imbalance (OFI) Metrics
        "OFI_10s",
        "OFI_30s",
        "OFI_Acceleration",
        "Delta_Cumulative",
        # 2. Order Book Depth & Spread Dynamics
        "Top_Depth_Skew",
        "Spread_Pips",
        "Spread_ZScore",
        "Liquidity_Ratio",
        # 3. Tick Arrival Velocity & Aggression
        "Tick_Arrival_Rate",
        "Inter_Tick_Time",
        "Size_Spike_Ratio",
        "Burst_Intensity",
        # 4. Micro-Momentum & Wick Exhaustion
        "Tick_Momentum",
        "Wick_Penetration",
        "Tape_Sweep_Flag",
        "Micro_Entropy",
    ]

    @staticmethod
    def _shannon_entropy_directions(directions: np.ndarray) -> float:
        """Shannon entropy of tick price direction (+1, -1, 0)."""
        if len(directions) < 5:
            return 1.5
        vals, counts = np.unique(directions, return_counts=True)
        probs = counts / counts.sum()
        probs = probs[probs > 0]
        return float(-np.sum(probs * np.log2(probs)))

    def compute_features(
        self,
        ticks_df: pd.DataFrame,
        symbol: str,
        current_time: Optional[datetime] = None,
        rolling_spread_mean: Optional[float] = None,
        rolling_spread_std: Optional[float] = None,
    ) -> Dict[str, float]:
        """
        Computes 16 microstructure features from a DataFrame of recent ticks.
        Required columns in ticks_df:
          - time / timestamp
          - bid, ask (and optional price)
          - volume / tick_volume (default 1.0)
          - optional bid_qty, ask_qty (default 100.0)
        """
        if ticks_df.empty or len(ticks_df) < 5:
            return {feat: 0.0 for feat in self.FEATURE_NAMES}

        df = ticks_df.copy()
        time_col = "timestamp" if "timestamp" in df.columns else ("time" if "time" in df.columns else df.columns[0])
        if not pd.api.types.is_datetime64_any_dtype(df[time_col]):
            df[time_col] = pd.to_datetime(df[time_col])

        if "bid" in df.columns:
            df["bid"] = pd.to_numeric(df["bid"], errors="coerce")
        if "ask" in df.columns:
            df["ask"] = pd.to_numeric(df["ask"], errors="coerce")
        if "price" not in df.columns or df["price"].isna().all():
            df["price"] = (df["bid"] + df["ask"]) / 2.0
        else:
            df["price"] = pd.to_numeric(df["price"], errors="coerce").fillna((df["bid"] + df["ask"]) / 2.0)
        if "volume" not in df.columns or df["volume"].isna().all():
            df["volume"] = 1.0
        else:
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(1.0)
        if "bid_qty" not in df.columns:
            df["bid_qty"] = 100.0
        if "ask_qty" not in df.columns:
            df["ask_qty"] = 100.0

        pip_unit = PipCalculator.get_pip_unit(symbol)

        last_time = df[time_col].iloc[-1]
        t10 = last_time - pd.Timedelta(seconds=10)
        t30 = last_time - pd.Timedelta(seconds=30)
        t60 = last_time - pd.Timedelta(seconds=60)

        # ── Aggressor Quote Rule ───────────────────────────────────────
        # Is_Buy_k = 1 if Price_k >= Ask_k else 0 if Price_k <= Bid_k else (1 if Price_k >= Price_{k-1} else 0)
        px = df["price"].values
        bid = df["bid"].values
        ask = df["ask"].values
        vol = df["volume"].values
        n = len(df)

        is_buy = np.zeros(n)
        for i in range(n):
            if px[i] >= ask[i]:
                is_buy[i] = 1.0
            elif px[i] <= bid[i]:
                is_buy[i] = 0.0
            else:
                # Tick test
                is_buy[i] = 1.0 if (i > 0 and px[i] >= px[i - 1]) else 0.0

        df["is_buy"] = is_buy
        df["buy_vol"] = is_buy * vol
        df["sell_vol"] = (1.0 - is_buy) * vol

        # ── 1. ORDER FLOW IMBALANCE (OFI) METRICS ─────────────────────
        eps = 1e-6
        # OFI 10s
        sub10 = df[df[time_col] >= t10]
        b10 = sub10["buy_vol"].sum()
        s10 = sub10["sell_vol"].sum()
        ofi_10s = float((b10 - s10) / (b10 + s10 + eps))

        # OFI 30s
        sub30 = df[df[time_col] >= t30]
        b30 = sub30["buy_vol"].sum()
        s30 = sub30["sell_vol"].sum()
        ofi_30s = float((b30 - s30) / (b30 + s30 + eps))

        # OFI Acceleration
        ofi_accel = ofi_10s - ofi_30s

        # Cumulative Volume Delta (60s)
        sub60 = df[df[time_col] >= t60]
        delta_cumulative = float(sub60["buy_vol"].sum() - sub60["sell_vol"].sum())

        # ── 2. ORDER BOOK DEPTH & SPREAD DYNAMICS ─────────────────────
        curr_bid = float(df["bid"].iloc[-1])
        curr_ask = float(df["ask"].iloc[-1])
        curr_bid_qty = float(df["bid_qty"].iloc[-1])
        curr_ask_qty = float(df["ask_qty"].iloc[-1])
        total_depth = curr_bid_qty + curr_ask_qty

        top_depth_skew = float((curr_bid_qty - curr_ask_qty) / (total_depth + eps))

        spread_raw = max(curr_ask - curr_bid, 0.0)
        spread_pips = float(spread_raw / pip_unit)

        # Spread Z-Score
        if rolling_spread_mean is not None and rolling_spread_std is not None and rolling_spread_std > 1e-6:
            spread_zscore = float((spread_pips - rolling_spread_mean) / rolling_spread_std)
        else:
            spreads = (df["ask"] - df["bid"]) / pip_unit
            s_mean = spreads.mean()
            s_std = spreads.std()
            spread_zscore = float((spread_pips - s_mean) / s_std) if s_std > 1e-4 else 0.0

        curr_vol = float(df["volume"].iloc[-1])
        liquidity_ratio = float(total_depth / (curr_vol + eps))

        # ── 3. TICK ARRIVAL VELOCITY & AGGRESSION ─────────────────────
        # Tick arrival rate in last 10 seconds (ticks per second)
        tick_arrival_rate = float(len(sub10) / 10.0)

        # Inter-tick time of last 5 ticks in milliseconds
        if len(df) >= 5:
            last5_times = df[time_col].tail(5)
            diffs = last5_times.diff().dropna().dt.total_seconds() * 1000.0
            inter_tick_time = float(diffs.mean()) if len(diffs) > 0 else 100.0
        else:
            inter_tick_time = 100.0

        # Size spike ratio
        vol_window_mean = float(sub30["volume"].mean()) if len(sub30) > 0 else curr_vol
        size_spike_ratio = float(curr_vol / (vol_window_mean + eps))

        # Burst intensity: consecutive trades on same side
        last_side = is_buy[-1]
        burst_intensity = 0
        for b in reversed(is_buy):
            if b == last_side:
                burst_intensity += 1
            else:
                break
        burst_intensity = float(burst_intensity)

        # ── 4. MICRO-MOMENTUM & WICK EXHAUSTION ───────────────────────
        # Tick momentum: price change over last 20 ticks in pips
        lookback_px = float(df["price"].iloc[-min(20, len(df))])
        tick_momentum = float((px[-1] - lookback_px) / pip_unit)

        # Wick penetration: distance price traveled beyond ask/bid
        curr_px = px[-1]
        wick_penetration = float(max(curr_px - curr_ask, curr_bid - curr_px, 0.0) / pip_unit)

        # Tape sweep flag: consecutive price jump > 1.5 spread
        recent_px_diffs = np.abs(np.diff(px[-min(5, len(px)):])) if len(px) >= 3 else np.array([0.0])
        tape_sweep_flag = 1.0 if np.any(recent_px_diffs > (1.5 * spread_raw)) else 0.0

        # Micro Entropy: Shannon entropy of tick directions
        px_diffs = np.diff(px[-min(30, len(px)):])
        dirs = np.sign(px_diffs)
        micro_entropy = self._shannon_entropy_directions(dirs)

        features = {
            "OFI_10s": round(ofi_10s, 4),
            "OFI_30s": round(ofi_30s, 4),
            "OFI_Acceleration": round(ofi_accel, 4),
            "Delta_Cumulative": round(delta_cumulative, 2),
            "Top_Depth_Skew": round(top_depth_skew, 4),
            "Spread_Pips": round(spread_pips, 2),
            "Spread_ZScore": round(spread_zscore, 3),
            "Liquidity_Ratio": round(liquidity_ratio, 2),
            "Tick_Arrival_Rate": round(tick_arrival_rate, 2),
            "Inter_Tick_Time": round(inter_tick_time, 1),
            "Size_Spike_Ratio": round(size_spike_ratio, 2),
            "Burst_Intensity": round(burst_intensity, 1),
            "Tick_Momentum": round(tick_momentum, 2),
            "Wick_Penetration": round(wick_penetration, 2),
            "Tape_Sweep_Flag": round(tape_sweep_flag, 1),
            "Micro_Entropy": round(micro_entropy, 4),
        }

        # Clean NaNs
        for k, v in features.items():
            if np.isnan(v) or np.isinf(v):
                features[k] = 0.0

        return features
