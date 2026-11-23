"""
Task G1 Clean-Room Raw-Tick Execution Engine & Diagnostic Simulator (Memory-Lean Flat NumPy).

ZERO imports from `src/goldedge/`.
Independently queries raw `market_ticks` from PostgreSQL via psycopg2 into contiguous
flat NumPy arrays (~500 MB total for 20M ticks), reconstructs causal 1m/5m/1h OHLC bars,
pandas_ta indicators, regime hysteresis, signal engines, trade scorer, execution-price sizing,
broker commission/slippage/NY-17:00 swap, trailing SL ratchets, optional Kalman velocity exit,
early-failure exit, and 8% HWM kill-switch.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import math
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
try:
    import pandas_ta as ta
except ImportError:
    import pandas_ta_classic as ta

ROOT = Path(__file__).resolve().parent.parent.parent
if not (ROOT / "backtest_results").exists() and Path("/app/backtest_results").exists():
    ROOT = Path("/app")

_NY_TZ = ZoneInfo("America/New_York")
_CONTRACT_SIZE = 100.0
_PIP_SIZE = 0.10
_TICK_SIZE = 0.01
_TICK_VALUE = 1.0
_MIN_LOT = 0.01
_MAX_LOT = 50.0
_VOL_STEP = 0.01
_MARGIN_PER_LOT = 1000.0
_FUSION_COMM_PER_M = 12.0
_FUSION_SWAP_LONG_PIPS = -5.653
_FUSION_SWAP_SHORT_PIPS = 2.677
_FUSION_SLIP_PIPS = 0.0533
_NS_PER_SEC = 1_000_000_000
_NS_PER_MIN = 60 * _NS_PER_SEC


def ns_to_dt(ts_ns: int) -> datetime:
    sec = int(ts_ns) // _NS_PER_SEC
    us = (int(ts_ns) % _NS_PER_SEC) // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc).replace(microsecond=us)


def dt_to_ns(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(round(dt.timestamp() * _NS_PER_SEC))


# Precomputed NY 17:00 rollover table across 2026-07-01 .. 2026-12-31 for O(1) nanosecond checks
def _build_rollover_schedule() -> list[tuple[int, date, int]]:
    out: list[tuple[int, date, int]] = []
    d = date(2026, 7, 1)
    end_d = date(2026, 12, 31)
    while d <= end_d:
        wd = d.weekday()
        if wd not in (5, 6):
            r_utc = datetime(d.year, d.month, d.day, 17, 0, 0, tzinfo=_NY_TZ).astimezone(timezone.utc)
            out.append((int(r_utc.timestamp()) * _NS_PER_SEC, d, 3 if wd == 2 else 1))
        d += timedelta(days=1)
    return out


_ROLLOVER_TABLE = _build_rollover_schedule()


# ---------------------------------------------------------------------------
# Scalar 2D Kalman Filter & Entropy / ER
# ---------------------------------------------------------------------------

class CleanKalman2D:
    def __init__(self, q_p: float = 1e-4, q_v: float = 3e-5, r_base: float = 1e-2) -> None:
        self.q_p = q_p
        self.q_v = q_v
        self.r_base = r_base
        self.r_min = r_base / 3.0
        self.r_max = r_base * 3.0
        self.x0 = 0.0
        self.x1 = 0.0
        self.prev_x1 = 0.0
        self.p00 = 1.0
        self.p01 = 0.0
        self.p10 = 0.0
        self.p11 = 1.0
        self.idx = 0
        self.last_price: Optional[float] = None

    def update(self, price: float) -> tuple[float, float]:
        p = float(price)
        if self.idx == 0:
            self.prev_x1 = 0.0
            self.x0 = p
            self.x1 = 0.0
            self.last_price = p
            self.idx = 1
            return p, 0.0

        diff = abs(p - self.last_price) if self.last_price else 0.0
        self.last_price = p
        vf = diff if diff > 0.0001 else 0.0001
        r_i = self.r_base * vf
        if r_i < self.r_min:
            r_i = self.r_min
        elif r_i > self.r_max:
            r_i = self.r_max

        xm0 = self.x0 + self.x1
        xm1 = self.x1
        pm00 = ((self.p00 + self.p10) + (self.p01 + self.p11)) + self.q_p
        pm01 = self.p01 + self.p11
        pm10 = self.p10 + self.p11
        pm11 = self.p11 + self.q_v

        inv_s = 1.0 / (pm00 + r_i)
        k0 = pm00 * inv_s
        k1 = pm10 * inv_s
        y = p - xm0

        self.prev_x1 = self.x1
        self.x0 = xm0 + k0 * y
        self.x1 = xm1 + k1 * y

        omk0 = 1.0 - k0
        self.p00 = omk0 * pm00
        self.p01 = omk0 * pm01
        self.p10 = (-k1) * pm00 + pm10
        self.p11 = (-k1) * pm01 + pm11
        self.idx += 1
        return self.x0, self.x1

    def accel(self) -> float:
        return 0.0 if self.idx < 2 else (self.x1 - self.prev_x1)


def clean_shannon_entropy(returns: pd.Series, window: int = 30) -> float:
    if len(returns) < window:
        return 3.5
    recent = returns.iloc[-window:].values
    bins = [-np.inf, -0.000500, -0.000200, -0.000100, -0.000050, 0.0, 0.000050, 0.000100, 0.000200, 0.000500, np.inf]
    counts, _ = np.histogram(recent, bins=bins)
    tot = counts.sum()
    if tot <= 0:
        return 0.0
    probs = counts / tot
    probs = probs[probs > 0]
    return float(-(probs * np.log2(probs)).sum())


def clean_kaufman_er(series: pd.Series, window: int = 10) -> float:
    if len(series) <= window:
        return 0.0
    direction = abs(float(series.iloc[-1]) - float(series.iloc[-(window + 1)]))
    vol = float(series.diff().abs().iloc[-window:].sum())
    return 0.0 if vol == 0.0 else float(direction / vol)


# ---------------------------------------------------------------------------
# Causal Bar Aggregators & Technical Indicators
# ---------------------------------------------------------------------------

class CleanBarAgg:
    def __init__(self, period_minutes: int, maxlen: int) -> None:
        self.tf = period_minutes
        self.period_secs = period_minutes * 60
        self.bars: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.cur_bucket: Optional[int] = None
        self.cur_bar: Optional[dict[str, Any]] = None

    def update(self, c: dict[str, Any]) -> bool:
        ts = c["time"]
        bucket = (int(ts.timestamp()) // self.period_secs) * self.period_secs
        closed = False
        if self.cur_bucket is None or bucket != self.cur_bucket:
            if self.cur_bar is not None:
                self.bars.append(self.cur_bar)
                closed = True
            self.cur_bucket = bucket
            self.cur_bar = {
                "time": datetime.fromtimestamp(bucket, tz=timezone.utc),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "tick_count": int(c["tick_count"]),
                "tick_rate": round(float(c["tick_count"]) / float(self.tf), 4),
                "observed_1m_bars": 1,
                "is_feed_gap": True if self.tf > 1 else False,
            }
        else:
            b = self.cur_bar
            if c["high"] > b["high"]:
                b["high"] = float(c["high"])
            if c["low"] < b["low"]:
                b["low"] = float(c["low"])
            b["close"] = float(c["close"])
            b["tick_count"] += int(c["tick_count"])
            b["tick_rate"] = round(float(b["tick_count"]) / float(self.tf), 4)
            b["observed_1m_bars"] += 1
            b["is_feed_gap"] = b["observed_1m_bars"] < self.tf
        return closed


def clean_htf_trend(closes: list[float], fast: int = 20, slow: int = 50) -> tuple[int, float]:
    if len(closes) < slow:
        return 0, 0.0
    af = 2.0 / (fast + 1.0)
    as_ = 2.0 / (slow + 1.0)
    ef = float(closes[0])
    es = float(closes[0])
    for v in closes[1:]:
        fv = float(v)
        ef = af * fv + (1.0 - af) * ef
        es = as_ * fv + (1.0 - as_) * es
    spread_pct = ((ef - es) / es * 100.0) if es != 0 else 0.0
    if ef > es:
        return 1, spread_pct
    if ef < es:
        return -1, spread_pct
    return 0, 0.0


def clean_compute_indicators(bars: list[dict[str, Any]], compute_adx: bool = False) -> pd.DataFrame:
    df = pd.DataFrame(bars).set_index("time")
    if len(df) < 50:
        return df
    df["ema20"] = ta.ema(df["close"], length=20)
    df["ema50"] = ta.ema(df["close"], length=50)
    df["rsi"] = ta.rsi(df["close"], length=9)
    bb = ta.bbands(df["close"], length=20, std=2.0)
    if bb is not None:
        df["bollinger_lower"] = bb.iloc[:, 0]
        df["bollinger_mid"] = bb.iloc[:, 1]
        df["bollinger_upper"] = bb.iloc[:, 2]
        df["bb_width"] = (df["bollinger_upper"] - df["bollinger_lower"]) / df["bollinger_mid"]
    try:
        df["tick_weighted_price"] = ta.vwap(df["high"], df["low"], df["close"], df["tick_count"])
        df["tick_weighted_price_dist"] = (df["close"] - df["tick_weighted_price"]) / df["tick_weighted_price"]
    except Exception:
        cumcnt = df["tick_count"].cumsum()
        cumtpc = (df["close"] * df["tick_count"]).cumsum()
        df["tick_weighted_price"] = cumtpc / cumcnt.replace(0, np.nan)
        df["tick_weighted_price_dist"] = ((df["close"] - df["tick_weighted_price"]) / df["tick_weighted_price"].replace(0, np.nan)).fillna(0.0)
    df["tick_rate_sma20"] = ta.sma(df["tick_rate"], length=20)
    df["tick_rate_ratio"] = df["tick_rate"] / df["tick_rate_sma20"].replace(0, np.nan)
    df["ema20_slope"] = df["ema20"].diff(5) / df["ema20"].shift(5)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    if compute_adx:
        adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
        if adx_df is not None and not adx_df.empty:
            df["adx"] = adx_df.iloc[:, 0]
            df["dmp"] = adx_df.iloc[:, 1] if adx_df.shape[1] > 1 else np.nan
            df["dmn"] = adx_df.iloc[:, 2] if adx_df.shape[1] > 2 else np.nan
        else:
            df["adx"] = np.nan
    return df


class CleanRegimeDetector:
    def __init__(self) -> None:
        self.hist: deque[str] = deque(maxlen=3)
        self.committed: str = "UNKNOWN"
        self.last_raw: str = "UNKNOWN"
        self.bars_since_commit: int = 0

    def detect(self, df: pd.DataFrame) -> str:
        if df.empty or len(df) < 20:
            raw = "UNKNOWN"
        else:
            ema_s = df["ema20"].iloc[-1]
            ema_l = df["ema50"].iloc[-1]
            ema_sp = df["ema20"].iloc[-5] if len(df) >= 5 else ema_s
            ema_diff = (ema_s - ema_l) / ema_l
            ema_slope = (ema_s - ema_sp) / ema_sp
            tr = pd.concat([
                df["high"] - df["low"],
                (df["high"] - df["close"].shift(1)).abs(),
                (df["low"] - df["close"].shift(1)).abs(),
            ], axis=1).max(axis=1)
            atr_pct = tr.rolling(14).mean() / df["close"]
            cur_atr_pct = atr_pct.iloc[-1]
            atr_hist = atr_pct.dropna()
            if len(atr_hist) > 10:
                hi_th = np.percentile(atr_hist, 75)
                lo_th = np.percentile(atr_hist, 25)
            else:
                hi_th, lo_th = float("inf"), 0.0
            r_hi = df["high"].tail(20).max()
            r_lo = df["low"].tail(20).min()
            rng_pct = (r_hi - r_lo) / df["close"].iloc[-1]
            if cur_atr_pct > hi_th:
                raw = "HIGH_VOLATILITY"
            elif cur_atr_pct < lo_th:
                raw = "LOW_VOLATILITY"
            elif ema_diff > 0.003 and ema_slope > 0:
                raw = "TRENDING_BULL"
            elif ema_diff < -0.003 and ema_slope < 0:
                raw = "TRENDING_BEAR"
            elif rng_pct < 0.004:
                raw = "SIDEWAYS"
            elif ema_diff > 0:
                raw = "TRENDING_BULL"
            elif ema_diff < 0:
                raw = "TRENDING_BEAR"
            else:
                raw = "SIDEWAYS"

        self.last_raw = raw
        self.hist.append(raw)
        if len(self.hist) == 3 and len(set(self.hist)) == 1 and self.hist[-1] != self.committed:
            self.committed = self.hist[-1]
            self.bars_since_commit = 0
        else:
            self.bars_since_commit += 1
        return self.committed


_REGIME_STRATS = {
    "TRENDING_BULL": ["kinematic_kalman_trend", "vwap_momentum_breakout", "bullish_momentum"],
    "TRENDING_BEAR": ["kinematic_kalman_trend", "bearish_momentum"],
    "SIDEWAYS": ["statistical_mean_reversion", "bullish_momentum", "bearish_momentum"],
    "HIGH_VOLATILITY": ["session_orb", "bearish_momentum", "vwap_momentum_breakout", "bullish_momentum"],
    "LOW_VOLATILITY": ["statistical_mean_reversion", "vwap_momentum_breakout"],
    "UNKNOWN": ["kinematic_kalman_trend", "vwap_momentum_breakout", "bearish_momentum", "bullish_momentum"],
}


def clean_generate_signals(df: pd.DataFrame, regime: str) -> list[tuple[str, str]]:
    if df.empty:
        return []
    row = df.iloc[-1].to_dict()
    ts = df.index[-1]
    hour = ts.hour if hasattr(ts, "hour") else 12
    if not (7 <= hour < 17):
        return []

    strats = _REGIME_STRATS.get(regime, _REGIME_STRATS["UNKNOWN"])
    out: list[tuple[str, str]] = []

    if "kinematic_kalman_trend" in strats and len(df) >= 50:
        c = float(row.get("close", 0.0))
        o = float(row.get("open", c))
        h = float(row.get("high", c))
        l = float(row.get("low", c))
        e20 = float(row.get("ema20", 0.0))
        e50 = float(row.get("ema50", 0.0))
        e200 = float(row.get("ema200", 0.0))
        kp = float(row.get("kalman_price", e20))
        vz = float(row.get("kalman_velocity_z", row.get("kalman_vel_z", 0.0)))
        ac = float(row.get("kalman_accel", 0.0))
        cr = max(h - l, _PIP_SIZE * 0.5)
        if c > e50 > e200 and vz >= 1.5 and ac >= 0.0 and l <= max(e20, kp) + _PIP_SIZE * 2.0 and ((min(o, c) - l) / cr) >= 0.40:
            out.append(("kinematic_kalman_trend", "BUY"))
        if c < e50 < e200 and vz <= -1.5 and ac <= 0.0 and h >= min(e20, kp) - _PIP_SIZE * 2.0 and ((h - max(o, c)) / cr) >= 0.40:
            out.append(("kinematic_kalman_trend", "SELL"))

    if "statistical_mean_reversion" in strats and len(df) >= 25:
        rm = df["close"].rolling(20).mean().iloc[-1]
        rs = df["close"].rolling(20).std().iloc[-1]
        up3, lo3 = float(rm + 3.0 * rs), float(rm - 3.0 * rs)
        pr = df.iloc[-2]
        c = float(row.get("close", 0.0))
        rsi = float(row.get("rsi", 50.0))
        cv = float(row.get("kalman_velocity", row.get("kalman_velocity_z", 0.0)))
        pv = float(pr.get("kalman_velocity", pr.get("kalman_velocity_z", 0.0)))
        if float(pr.get("low", pr["close"])) <= lo3 and c > lo3 and rsi < 28 and cv > pv:
            out.append(("statistical_mean_reversion", "BUY"))
        if float(pr.get("high", pr["close"])) >= up3 and c < up3 and rsi > 72 and cv < pv:
            out.append(("statistical_mean_reversion", "SELL"))

    if "session_orb" in strats and len(df) >= 20:
        m = ts.minute if hasattr(ts, "minute") else 0
        if (hour == 7) or (hour == 8 and m <= 30) or (hour == 12 and m >= 30) or (hour == 13) or (hour == 14 and m == 0):
            c = float(row.get("close", 0.0))
            qr = float(row.get("tick_rate", 0.0) or 0.0)
            atr14 = float(row.get("atr", c * 0.0005))
            pr_df = df.iloc[:-1]
            r15 = pr_df.tail(15)
            oh, ol = float(r15["high"].max()), float(r15["low"].min())
            ra = float(pr_df["tick_rate"].tail(20).mean())
            if (oh - ol) < 0.75 * atr14 * 10.0 and ra > 0.0 and qr >= 2.0 * ra:
                if c > oh:
                    out.append(("session_orb", "BUY"))
                if c < ol:
                    out.append(("session_orb", "SELL"))

    if "vwap_momentum_breakout" in strats:
        req = ["close", "tick_weighted_price", "rsi", "ema20", "ema50"]
        if all(k in row and not pd.isna(row[k]) for k in req):
            trr = 0.0 if (row.get("tick_rate_ratio") is None or pd.isna(row.get("tick_rate_ratio"))) else float(row["tick_rate_ratio"])
            if (
                row["close"] > row["tick_weighted_price"]
                and row.get("ema20_slope", 0.0) > 0.00005
                and row["rsi"] > 60
                and trr > 2.5
                and row["ema20"] > row["ema50"]
            ):
                out.append(("vwap_momentum_breakout", "BUY"))

    if "bullish_momentum" in strats:
        req = ["close", "rsi", "ema20", "ema50", "tick_weighted_price"]
        if all(k in row and not pd.isna(row[k]) for k in req):
            trr = 0.0 if (row.get("tick_rate_ratio") is None or pd.isna(row.get("tick_rate_ratio"))) else float(row["tick_rate_ratio"])
            if (
                row["close"] > row["tick_weighted_price"]
                and row["rsi"] > 55
                and row["ema20"] > row["ema50"]
                and (trr > 2.0 or row.get("ema20_slope", 0.0) > 0.0002)
            ):
                out.append(("bullish_momentum", "BUY"))

    if "bearish_momentum" in strats:
        req = ["close", "rsi", "ema20", "ema50", "tick_weighted_price"]
        if all(k in row and not pd.isna(row[k]) for k in req):
            opx = row.get("open")
            if opx is None or pd.isna(opx) or float(row["close"]) < float(opx):
                trr = 0.0 if (row.get("tick_rate_ratio") is None or pd.isna(row.get("tick_rate_ratio"))) else float(row["tick_rate_ratio"])
                if (
                    row["close"] < row["tick_weighted_price"]
                    and row["rsi"] < 45
                    and row["ema20"] < row["ema50"]
                    and (trr > 2.0 or row.get("ema20_slope", 0.0) < -0.0002)
                ):
                    out.append(("bearish_momentum", "SELL"))

    return out


def _score_band(v: float, g0: float, g1: float, b0: Optional[float], b1: Optional[float]) -> float:
    if g0 <= v <= g1:
        return 1.0
    if v < g0:
        if b0 is None:
            return 1.0
        return 0.0 if v <= b0 else (v - b0) / (g0 - b0)
    if b1 is None:
        return 1.0
    return 0.0 if v >= b1 else (b1 - v) / (b1 - g1)


def _interp(v: float, lo: float, hi: float) -> float:
    if lo < hi:
        if v <= lo:
            return 0.0
        if v >= hi:
            return 1.0
        return (v - lo) / (hi - lo)
    if v >= lo:
        return 0.0
    if v <= hi:
        return 1.0
    return (v - lo) / (hi - lo)


def _sess_score(h: int) -> float:
    if 0 <= h < 7:
        return 0.0
    if 7 <= h < 9:
        return 0.4
    if 9 <= h < 12:
        return 0.7
    if 12 <= h < 17:
        return 1.0
    if 17 <= h < 20:
        return 0.7
    return 0.2


def clean_score_detailed(direction: str, regime: str, feat: dict[str, Any]) -> tuple[float, dict[str, float]]:
    kv = float(feat["kalman_vel"])
    ka = float(feat["kalman_accel"])
    ent = float(feat["shannon_entropy"])
    er = float(feat["kaufman_er"])
    slp = float(feat["ema20_slope"])
    twd = float(feat["tick_weighted_price_dist"])
    trr = float(feat["tick_rate_ratio"])
    h = int(feat["utc_hour"])
    sw = "SIDEWAYS" in regime.upper()

    if direction == "BUY":
        c_mom = _score_band(kv, 0.008, 0.040, 0.002, 0.080) if sw else _score_band(kv, 0.015, 0.060, 0.005, 0.120)
        c_er = _score_band(er, 0.20, 0.60, 0.08, 0.90)
        c_ent = _score_band(ent, 2.50, 3.20, None, 3.60)
        c_slp = _interp(slp, 0.00005, 0.00030)
        c_twd = _score_band(twd, -0.0005, 0.003, -0.003, 0.006)
        adj_raw = ka * 2.0
    else:
        c_mom = _score_band(kv, -0.040, -0.008, -0.080, -0.002) if sw else _score_band(kv, -0.060, -0.015, -0.120, -0.005)
        c_er = _score_band(er, 0.20, 0.60, 0.08, 0.90)
        c_ent = _score_band(ent, 2.50, 3.20, None, 3.60)
        c_slp = _interp(slp, -0.00005, -0.00030)
        c_twd = _score_band(twd, -0.003, 0.0005, -0.006, 0.003)
        adj_raw = -ka * 2.0

    c_ses = _sess_score(h)
    base_s = 0.35 * c_mom + 0.20 * c_er + 0.15 * c_ent + 0.15 * c_slp + 0.10 * c_twd + 0.05 * c_ses
    qs = min(1.0, max(0.0, base_s))
    accel_adj = max(-0.08, min(0.08, adj_raw))
    qs = min(1.0, max(0.0, qs + accel_adj))
    trr_mult = 1.0
    if trr < 0.70:
        trr_mult = 0.75
    elif sw and trr < 1.10:
        trr_mult = 0.85
    qs = round(qs * trr_mult, 4)

    comp = {
        "sub_mom_kalman": round(c_mom, 4),
        "sub_er": round(c_er, 4),
        "sub_entropy": round(c_ent, 4),
        "sub_ema_slope": round(c_slp, 4),
        "sub_twp_dist": round(c_twd, 4),
        "sub_session": round(c_ses, 4),
        "base_score_pre_accel": round(base_s, 4),
        "accel_adj": round(accel_adj, 4),
        "trr_mult": round(trr_mult, 4),
    }
    return qs, comp


# ---------------------------------------------------------------------------
# Flat Contiguous Span Representation (<550 MB for 20M ticks)
# ---------------------------------------------------------------------------

@dataclass
class MinuteSlice:
    minute_ts: datetime
    minute_epoch_s: int
    s: int
    e: int
    candle: Optional[dict[str, Any]]
    inverted_count: int
    zero_spread_count: int


@dataclass
class PreparedSpan:
    symbol: str
    start_dt: datetime
    end_dt: datetime
    warmup_start: datetime
    w_ts_ns: np.ndarray
    w_bids: np.ndarray
    w_asks: np.ndarray
    warmup_slices: list[MinuteSlice]
    r_ts_ns: np.ndarray
    r_bids: np.ndarray
    r_asks: np.ndarray
    replay_slices: list[MinuteSlice]
    chunk_boundary_splits: int = 0


def _fetch_flat_arrays(conn, symbol: str, s_dt: datetime, e_dt: datetime, chunk_days: Optional[int] = 7) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ts_parts: list[np.ndarray] = []
    b_parts: list[np.ndarray] = []
    a_parts: list[np.ndarray] = []

    windows: list[tuple[datetime, datetime]] = []
    if chunk_days is None:
        windows.append((s_dt, e_dt))
    else:
        cs = s_dt
        while cs < e_dt:
            ce = min(cs + timedelta(days=int(chunk_days)), e_dt)
            windows.append((cs, ce))
            cs = ce

    for ws, we in windows:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT (
                    EXTRACT(EPOCH FROM date_trunc('second', time))::bigint * 1000000000
                    + EXTRACT(MICROSECONDS FROM (time - date_trunc('second', time)))::bigint * 1000
                ) AS ts_ns,
                       bid::double precision,
                       ask::double precision
                FROM market_ticks
                WHERE symbol = %s AND time >= %s AND time < %s
                ORDER BY time ASC
                """,
                (symbol, ws, we),
            )
            rows = cur.fetchall()
        if not rows:
            continue
        ts_arr = np.fromiter((int(r[0]) for r in rows), dtype=np.int64, count=len(rows))
        b_arr = np.fromiter((float(r[1]) for r in rows), dtype=np.float64, count=len(rows))
        a_arr = np.fromiter((float(r[2]) for r in rows), dtype=np.float64, count=len(rows))
        ts_parts.append(ts_arr)
        b_parts.append(b_arr)
        a_parts.append(a_arr)
        del rows

    if not ts_parts:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64)
    if len(ts_parts) == 1:
        return ts_parts[0], b_parts[0], a_parts[0]
    return np.concatenate(ts_parts), np.concatenate(b_parts), np.concatenate(a_parts)


def _index_minutes(ts_ns: np.ndarray, bids: np.ndarray, asks: np.ndarray) -> tuple[list[MinuteSlice], int]:
    n = len(ts_ns)
    if n == 0:
        return [], 0
    min_ns = (ts_ns // _NS_PER_MIN) * _NS_PER_MIN
    changes = np.flatnonzero(min_ns[1:] != min_ns[:-1]) + 1
    starts = np.concatenate(([0], changes))
    ends = np.concatenate((changes, [n]))

    valid_all = (bids > 0.0) & (asks > 0.0) & (bids < asks)
    inv_all = (bids > 0.0) & (asks > 0.0) & (bids > asks)
    zs_all = (bids > 0.0) & (asks > 0.0) & (bids == asks)

    slices: list[MinuteSlice] = []
    seen_min: set[int] = set()
    dup_splits = 0
    for k in range(len(starts)):
        s = int(starts[k])
        e = int(ends[k])
        m_ep_s = int(min_ns[s] // _NS_PER_SEC)
        if m_ep_s in seen_min:
            dup_splits += 1
        seen_min.add(m_ep_s)
        m_dt = datetime.fromtimestamp(m_ep_s, tz=timezone.utc)
        vm = valid_all[s:e]
        vb = bids[s:e][vm]
        tc = int(len(vb))
        candle = None
        if tc > 0:
            candle = {
                "time": pd.Timestamp(m_dt),
                "open": float(vb[0]),
                "high": float(vb.max()),
                "low": float(vb.min()),
                "close": float(vb[-1]),
                "tick_count": tc,
                "tick_volume": tc,
                "tick_rate": float(tc),
                "real_volume": None,
            }
        slices.append(
            MinuteSlice(
                minute_ts=m_dt,
                minute_epoch_s=m_ep_s,
                s=s,
                e=e,
                candle=candle,
                inverted_count=int(inv_all[s:e].sum()),
                zero_spread_count=int(zs_all[s:e].sum()),
            )
        )
    return slices, dup_splits


def load_prepared_span(
    conn,
    symbol: str,
    start_dt: datetime,
    end_dt: datetime,
    chunk_days: Optional[int] = 7,
) -> PreparedSpan:
    warmup_start = start_dt - timedelta(days=4)
    w_ts, w_b, w_a = _fetch_flat_arrays(conn, symbol, warmup_start, start_dt, chunk_days=None)
    w_slices, _ = _index_minutes(w_ts, w_b, w_a)

    end_ex = end_dt + timedelta(microseconds=1)
    r_ts, r_b, r_a = _fetch_flat_arrays(conn, symbol, start_dt, end_ex, chunk_days=chunk_days)
    r_slices, splits = _index_minutes(r_ts, r_b, r_a)

    return PreparedSpan(
        symbol=symbol,
        start_dt=start_dt,
        end_dt=end_dt,
        warmup_start=warmup_start,
        w_ts_ns=w_ts,
        w_bids=w_b,
        w_asks=w_a,
        warmup_slices=w_slices,
        r_ts_ns=r_ts,
        r_bids=r_b,
        r_asks=r_a,
        replay_slices=r_slices,
        chunk_boundary_splits=splits,
    )


def slice_prepared_span(
    full_span: PreparedSpan,
    split_start_dt: datetime,
    split_end_dt: datetime,
) -> PreparedSpan:
    """
    Zero-copy chronological sub-window view over `full_span` (`2026-08-13 .. 2026-11-19`)
    putting `[split_start_dt - 4d, split_start_dt)` into `warmup_slices` and
    `[split_start_dt, split_end_dt]` into `replay_slices`.
    Matches `RunBacktestUseCase.execute(start=split_start_dt, end=split_end_dt)` 1-for-1
    with zero extra array allocations.
    """
    wu_start_dt = split_start_dt - timedelta(days=4)
    wu_s = int(wu_start_dt.timestamp())
    re_s = int(split_start_dt.timestamp())
    re_end_s = int(split_end_dt.timestamp())

    wu_slices = [ms for ms in full_span.replay_slices if wu_s <= ms.minute_epoch_s < re_s]
    rep_slices = [ms for ms in full_span.replay_slices if re_s <= ms.minute_epoch_s <= re_end_s]

    return PreparedSpan(
        symbol=full_span.symbol,
        start_dt=split_start_dt,
        end_dt=split_end_dt,
        warmup_start=wu_start_dt,
        w_ts_ns=full_span.r_ts_ns,
        w_bids=full_span.r_bids,
        w_asks=full_span.r_asks,
        warmup_slices=wu_slices,
        r_ts_ns=full_span.r_ts_ns,
        r_bids=full_span.r_bids,
        r_asks=full_span.r_asks,
        replay_slices=rep_slices,
        chunk_boundary_splits=0,
    )


# ---------------------------------------------------------------------------
# Simulation Config, Position, HWM & MTM Trackers
# ---------------------------------------------------------------------------

@dataclass
class CleanSimConfig:
    initial_capital: float = 10000.0
    risk_pct: float = 1.0
    max_daily_loss_pct: float = 3.0
    max_trades_per_day: int = 8
    max_consecutive_losses: int = 3
    max_hold_bars: int = 120
    sl_atr_mult: float = 1.5
    tp_atr_mult: float = 3.0
    min_score: float = 0.60
    max_score: Optional[float] = None
    block_high_vol: bool = True
    block_buy_sideways: bool = True
    block_sell_sideways: bool = False
    use_kalman_exit: bool = True
    kalman_vel_thresh: float = 2.5
    kalman_rate_norm: bool = False
    kalman_min_hold_bars: int = 0
    session_close_utc: Optional[int] = 15
    early_fail_mins: Optional[int] = 15
    early_fail_min_mfe_atr: Optional[float] = 0.4
    cost_mode: str = "zero"                     # "zero" | "fusion_ctrader_demo"
    cost_multiplier: float = 1.0
    max_hwm_dd_pct: Optional[float] = 8.0
    adx_min: Optional[float] = None
    ema20_max_dist_atr: Optional[float] = None
    cooldown_bars: int = 15
    # Bug-parity switches for Phase 1 pre-fix vs post-fix reconciliation
    bug_invert_before_kalman: bool = False
    bug_kalman_before_sl_tp: bool = False
    bug_skip_mtm_on_kalman_exit: bool = False
    bug_len_minus_1_on_timeout_idx: bool = False
    compute_adx_features: bool = False
    record_signals: bool = False
    slice_start_dt: Optional[datetime] = None   # Filter replay execution to [slice_start_dt, slice_end_dt] while warming from span.start_dt!
    slice_end_dt: Optional[datetime] = None


class CleanHwmKillSwitch:
    def __init__(self, max_dd_pct: float = 8.0, initial_equity: float = 10000.0) -> None:
        self.max_dd_pct = float(max_dd_pct)
        self.hwm = float(initial_equity)
        self.current_equity = float(initial_equity)
        self.drawdown_pct = 0.0
        self.max_seen_dd_pct = 0.0
        self.halted = False
        self.trigger_ns: Optional[int] = None
        self.trigger_hwm: Optional[float] = None
        self.trigger_equity: Optional[float] = None
        self.trigger_dd_pct: Optional[float] = None
        self.trades_prevented = 0

    def evaluate(self, equity_val: float, ts_ns: int) -> bool:
        eq = float(equity_val)
        self.current_equity = eq
        if self.halted:
            if self.hwm > 0:
                self.drawdown_pct = max(0.0, (self.hwm - eq) / self.hwm * 100.0)
            return False
        if eq > self.hwm:
            self.hwm = eq
        dd_pct = max(0.0, (self.hwm - eq) / self.hwm * 100.0) if self.hwm > 0 else 0.0
        self.drawdown_pct = dd_pct
        if dd_pct > self.max_seen_dd_pct:
            self.max_seen_dd_pct = dd_pct
        if round(dd_pct, 10) >= self.max_dd_pct:
            self.halted = True
            self.trigger_ns = int(ts_ns)
            self.trigger_hwm = self.hwm
            self.trigger_equity = eq
            self.trigger_dd_pct = dd_pct
            return True
        return False


class CleanMtmTracker:
    def __init__(self, initial_cap: float, start_ns: int) -> None:
        self.initial_cap = float(initial_cap)
        self.hwm = float(initial_cap)
        self.hwm_ns = int(start_ns)
        self.max_dd = 0.0
        self.max_dd_pct = 0.0
        self.max_dd_init_pct = 0.0
        self.peak_eq = float(initial_cap)
        self.peak_ns = int(start_ns)
        self.trough_eq = float(initial_cap)
        self.trough_ns = int(start_ns)
        self.sl_slip_events = 0
        self.worst_sl_gap_usd = 0.0

    def update(self, eq_fill: float, ts_ns: int, gap_usd: float = 0.0) -> None:
        if gap_usd < -1e-6:
            self.sl_slip_events += 1
            if gap_usd < self.worst_sl_gap_usd:
                self.worst_sl_gap_usd = float(gap_usd)
        if eq_fill > self.hwm:
            self.hwm = float(eq_fill)
            self.hwm_ns = int(ts_ns)
        dd = float(eq_fill) - self.hwm
        if dd < self.max_dd:
            self.max_dd = dd
            self.max_dd_init_pct = abs(dd) / self.initial_cap * 100.0 if self.initial_cap > 0 else 0.0
            self.peak_eq = self.hwm
            self.peak_ns = self.hwm_ns
            self.trough_eq = float(eq_fill)
            self.trough_ns = int(ts_ns)
        if self.hwm > 0:
            pct = max(0.0, -dd / self.hwm * 100.0)
            if pct > self.max_dd_pct:
                self.max_dd_pct = pct


def _side_comm(lots: float, px: float, cfg: CleanSimConfig) -> float:
    if cfg.cost_mode == "zero":
        return 0.0
    rate = _FUSION_COMM_PER_M * cfg.cost_multiplier
    return round(abs(lots) * _CONTRACT_SIZE * float(px) * (rate / 1_000_000.0), 2)


def _slip_offset(cfg: CleanSimConfig) -> float:
    if cfg.cost_mode == "zero":
        return 0.0
    return (_FUSION_SLIP_PIPS * cfg.cost_multiplier) * _PIP_SIZE


def _size_position(equity: float, entry_px: float, atr: float, direction: str, cfg: CleanSimConfig) -> tuple[Optional[float], float, float, Optional[str]]:
    if equity <= 0 or entry_px <= 0:
        return None, 0.0, 0.0, "non_positive_eq_or_px"
    sl_dist = atr * cfg.sl_atr_mult
    tp_dist = atr * cfg.tp_atr_mult
    if sl_dist <= 0:
        return None, 0.0, 0.0, "zero_sl_dist"
    sl_px = (entry_px - sl_dist) if direction == "BUY" else (entry_px + sl_dist)
    tp_px = (entry_px + tp_dist) if direction == "BUY" else (entry_px - tp_dist)
    max_risk = equity * (cfg.risk_pct / 100.0)
    est_rt = round(2.0 * _side_comm(1.0, entry_px, cfg), 2) if cfg.cost_mode != "zero" else 0.0
    loss_per_lot = (abs(entry_px - sl_px) / _TICK_SIZE) * _TICK_VALUE + est_rt
    if loss_per_lot <= 0:
        return None, sl_px, tp_px, "zero_loss_per_lot"
    if _MIN_LOT * loss_per_lot > max_risk + 1e-6:
        return None, sl_px, tp_px, "min_lot_exceeds_risk"
    raw = max_risk / loss_per_lot
    steps = math.floor((raw + 1e-12) / _VOL_STEP)
    lot = round(steps * _VOL_STEP, 2)
    while lot >= _MIN_LOT and (loss_per_lot * lot) > max_risk + 1e-6:
        lot = round(lot - _VOL_STEP, 2)
    if lot < _MIN_LOT:
        return None, sl_px, tp_px, "below_min_lot"
    lot = min(lot, _MAX_LOT)
    margin = lot * _MARGIN_PER_LOT
    if margin > 0 and (equity / margin) * 100.0 < 300.0:
        max_aff = round(math.floor(((equity / 3.0) / _MARGIN_PER_LOT) / _VOL_STEP) * _VOL_STEP, 2)
        if max_aff < _MIN_LOT:
            return None, sl_px, tp_px, "margin_level_below_300"
        lot = min(lot, max_aff)
    if loss_per_lot * lot > max_risk + 1e-6:
        return None, sl_px, tp_px, "exceeds_risk_after_cap"
    return lot, sl_px, tp_px, None


@dataclass
class CleanPos:
    trade_id: int
    direction: str
    strategy: str
    regime: str
    score: float
    entry_ns: int
    entry_bid: float
    entry_ask: float
    entry_price: float
    initial_sl: float
    sl: float
    tp: float
    atr: float
    lot: float
    planned_risk_usd: float
    planned_risk_pct: float
    entry_comm: float
    slip_usd: float
    peak_price: float
    peak_ns: int
    worst_price: float
    worst_ns: int
    last_swap_ns: int
    entry_ctx: dict[str, Any] = field(default_factory=dict)
    comm_exit: float = 0.0
    comm_total: float = 0.0
    swap_pnl: float = 0.0
    swap_cost: float = 0.0
    trailing_active: bool = False
    exit_reason_flag: str = ""
    bars_held: int = 0
    theo_sl: float = 0.0
    last_sl_gap: float = 0.0
    sl_updates: list[dict[str, Any]] = field(default_factory=list)
    be_triggered: bool = False
    t1_triggered: bool = False
    t2_triggered: bool = False
    kalman_tighten_triggered: bool = False
    journey: list[dict[str, Any]] = field(default_factory=list)
    prev_minute_valid_ticks: int = 60
    next_roll_idx: int = 0

    def __post_init__(self) -> None:
        self.comm_total = self.entry_comm
        self.theo_sl = self.sl
        # Advance rollover pointer to first rollover strictly > entry_ns
        idx = 0
        while idx < len(_ROLLOVER_TABLE) and _ROLLOVER_TABLE[idx][0] <= self.last_swap_ns:
            idx += 1
        self.next_roll_idx = idx

    def apply_rollovers(self, ts_ns: int, cfg: CleanSimConfig) -> None:
        if cfg.cost_mode == "zero":
            return
        while self.next_roll_idx < len(_ROLLOVER_TABLE) and _ROLLOVER_TABLE[self.next_roll_idx][0] <= ts_ns:
            _r_ns, _ldate, mult = _ROLLOVER_TABLE[self.next_roll_idx]
            pips = _FUSION_SWAP_LONG_PIPS if self.direction == "BUY" else _FUSION_SWAP_SHORT_PIPS
            s_pnl = round(pips * (_PIP_SIZE * _CONTRACT_SIZE) * self.lot * mult * cfg.cost_multiplier, 2)
            self.swap_pnl += s_pnl
            self.swap_cost = -self.swap_pnl
            self.next_roll_idx += 1

    def gross_at(self, px: float) -> float:
        diff = (px - self.entry_price) if self.direction == "BUY" else (self.entry_price - px)
        return diff * self.lot * _CONTRACT_SIZE

    def net_at(self, px: float, cfg: CleanSimConfig, include_pending_exit_comm: bool = True) -> float:
        pend = _side_comm(self.lot, px, cfg) if (include_pending_exit_comm and self.comm_exit == 0.0) else 0.0
        return self.gross_at(px) - self.comm_total - pend + self.swap_pnl

    def ratchet(self, price: float, ts_ns: int, max_hold: int) -> None:
        is_bull = self.direction == "BUY"
        if is_bull:
            if price > self.peak_price:
                self.peak_price = price
                self.peak_ns = ts_ns
            gain = self.peak_price - self.entry_price
        else:
            if price < self.peak_price:
                self.peak_price = price
                self.peak_ns = ts_ns
            gain = self.entry_price - self.peak_price

        old_sl = self.sl
        stage = None
        if gain >= 2.5 * self.atr:
            self.trailing_active = True
            new_sl = (self.peak_price - 0.75 * self.atr) if is_bull else (self.peak_price + 0.75 * self.atr)
            if (is_bull and new_sl > self.sl) or ((not is_bull) and new_sl < self.sl):
                self.sl = new_sl
                self.exit_reason_flag = "TRAILING_SL"
                self.t2_triggered = True
                stage = "T2"
        elif gain >= 1.6 * self.atr:
            self.trailing_active = True
            new_sl = (self.peak_price - 1.10 * self.atr) if is_bull else (self.peak_price + 1.10 * self.atr)
            if (is_bull and new_sl > self.sl) or ((not is_bull) and new_sl < self.sl):
                self.sl = new_sl
                self.exit_reason_flag = "TRAILING_SL"
                self.t1_triggered = True
                stage = "T1"
        elif gain >= 1.0 * self.atr:
            be = 2.0 * _PIP_SIZE
            new_sl = (self.entry_price + be) if is_bull else (self.entry_price - be)
            if (is_bull and new_sl > self.sl) or ((not is_bull) and new_sl < self.sl):
                self.sl = new_sl
                self.exit_reason_flag = "BREAKEVEN"
                self.be_triggered = True
                stage = "BE"

        if stage is not None and abs(self.sl - old_sl) > 1e-9:
            self.sl_updates.append({"ts": ns_to_dt(ts_ns).isoformat(), "stage": stage, "sl": round(self.sl, 5)})

        hold_pct = self.bars_held / max_hold if max_hold > 0 else 0.0
        if hold_pct >= 0.75:
            ratio = min((hold_pct - 0.75) / 0.25, 1.0)
            old_sq = self.sl
            changed = False
            if is_bull:
                sq = self.sl + ratio * (price - self.sl)
                if sq > self.sl:
                    self.sl = sq
                    self.exit_reason_flag = "TIME_SQUEEZE"
                    changed = True
            else:
                sq = self.sl - ratio * (self.sl - price)
                if sq < self.sl:
                    self.sl = sq
                    self.exit_reason_flag = "TIME_SQUEEZE"
                    changed = True
            if changed and abs(self.sl - old_sq) > 1e-9:
                self.sl_updates.append({"ts": ns_to_dt(ts_ns).isoformat(), "stage": "TIME_SQUEEZE", "sl": round(self.sl, 5)})


@dataclass
class SpanCache:
    r_start_off: int
    k_vel_all: np.ndarray
    signal_minutes: dict[int, dict[str, Any]]
    bars_processed: int
    inverted_skipped: int
    zero_spread_skipped: int


def precompute_span_cache(span: PreparedSpan, compute_adx: bool = True) -> SpanCache:
    """
    Causal precomputation over `span` matching post-E1 `run_clean_backtest` bit-for-bit.
    Precomputes per-tick valid `CleanKalman2D` velocity and closed-5m indicator/signal/feature
    packets so repeated hypothesis sweeps skip redundant pandas_ta & flat Kalman loops.
    """
    kalman = CleanKalman2D()
    agg_5m = CleanBarAgg(5, 200)
    agg_1h = CleanBarAgg(60, 100)
    regime_det = CleanRegimeDetector()
    current_regime = "UNKNOWN"

    w_bids = span.w_bids
    w_asks = span.w_asks
    for ms in span.warmup_slices:
        for j in range(ms.s, ms.e):
            b = float(w_bids[j])
            a = float(w_asks[j])
            if b > 0.0 and a > 0.0 and b < a:
                kalman.update(b)
        if ms.candle is not None:
            agg_5m.update(ms.candle)
            agg_1h.update(ms.candle)

    r_start_off = span.replay_slices[0].s if span.replay_slices else 0
    r_end_off = span.replay_slices[-1].e if span.replay_slices else 0
    k_vel_all = np.zeros(max(0, r_end_off - r_start_off), dtype=np.float64)

    r_bids = span.r_bids
    r_asks = span.r_asks
    signal_minutes: dict[int, dict[str, Any]] = {}
    bars_processed = 0
    inverted_skipped = 0
    zero_spread_skipped = 0

    def _sf(v: Any, dflt: float) -> float:
        try:
            f = float(v)
            return dflt if math.isnan(f) else f
        except Exception:
            return dflt

    for idx_m, ms in enumerate(span.replay_slices):
        inverted_skipped += ms.inverted_count
        zero_spread_skipped += ms.zero_spread_count
        s_i, e_i = ms.s, ms.e
        bids = r_bids[s_i:e_i]
        asks = r_asks[s_i:e_i]
        valid_mask = (bids > 0.0) & (asks > 0.0) & (bids < asks)
        valid_idxs = np.flatnonzero(valid_mask)

        en_i = int(valid_idxs[0]) if len(valid_idxs) > 0 else -1
        en_k = None
        for j in valid_idxs:
            ji = int(j)
            kp, kv = kalman.update(float(bids[ji]))
            k_vel_all[(s_i + ji) - r_start_off] = kv
            if ji == en_i:
                en_k = (kp, kv, kalman.accel())

        closed_5m = False
        df_ind = pd.DataFrame()
        if ms.candle is not None:
            closed_5m = agg_5m.update(ms.candle)
            agg_1h.update(ms.candle)
            bars_processed += 1
            if closed_5m and len(agg_5m.bars) >= 60:
                df_ind = clean_compute_indicators(list(agg_5m.bars), compute_adx=compute_adx)
                if not df_ind.empty:
                    current_regime = regime_det.detect(df_ind)

        if en_i >= 0 and en_k is not None and closed_5m and len(agg_5m.bars) >= 60 and not df_ind.empty:
            htf_trend, htf_spread_pct = clean_htf_trend([b["close"] for b in agg_1h.bars])
            if htf_trend != 0:
                sigs = clean_generate_signals(df_ind, current_regime)
                if sigs:
                    lr = df_ind.iloc[-1]
                    cs_s = df_ind["close"]
                    c5_ts = agg_5m.bars[-1]["time"]
                    feat = {
                        "kalman_pos": en_k[0],
                        "kalman_vel": en_k[1],
                        "kalman_accel": en_k[2],
                        "shannon_entropy": clean_shannon_entropy(cs_s.pct_change().dropna()) if len(cs_s) > 30 else 2.0,
                        "kaufman_er": clean_kaufman_er(cs_s) if len(cs_s) > 15 else 0.3,
                        "ema20_slope": _sf(lr.get("ema20_slope"), 0.0),
                        "tick_weighted_price_dist": _sf(lr.get("tick_weighted_price_dist"), 0.0),
                        "tick_rate_ratio": _sf(lr.get("tick_rate_ratio"), 1.0),
                        "rsi": _sf(lr.get("rsi"), 50.0),
                        "utc_hour": c5_ts.hour,
                    }
                    atr_v = _sf(lr.get("atr"), 1.0)
                    ema20_v = _sf(lr.get("ema20"), 0.0)
                    ema50_v = _sf(lr.get("ema50"), 0.0)
                    adx_v = _sf(lr.get("adx"), np.nan) if "adx" in df_ind.columns else np.nan
                    ema20_dist_atr = ((_sf(lr.get("close"), 0.0) - ema20_v) / max(atr_v, 1e-9)) if ema20_v > 0 else 0.0
                    bb_u = _sf(lr.get("bollinger_upper"), np.nan)
                    bb_m = _sf(lr.get("bollinger_mid"), np.nan)
                    bb_l = _sf(lr.get("bollinger_lower"), np.nan)
                    bb_pos = ((_sf(lr.get("close"), 0.0) - bb_l) / max(bb_u - bb_l, 1e-9)) if not (math.isnan(bb_u) or math.isnan(bb_l)) else np.nan
                    swing20_hi = float(df_ind["high"].tail(20).max())
                    swing20_lo = float(df_ind["low"].tail(20).min())

                    signal_minutes[idx_m] = {
                        "en_i": en_i,
                        "n_valid_ticks": int(len(valid_idxs)),
                        "regime": current_regime,
                        "raw_regime": regime_det.last_raw,
                        "bars_since_commit": regime_det.bars_since_commit,
                        "htf_trend": htf_trend,
                        "htf_spread_pct": htf_spread_pct,
                        "sigs": sigs,
                        "feat": feat,
                        "atr": atr_v,
                        "ema20": ema20_v,
                        "ema50": ema50_v,
                        "adx": adx_v,
                        "dmp": _sf(lr.get("dmp"), np.nan) if "dmp" in df_ind.columns else np.nan,
                        "dmn": _sf(lr.get("dmn"), np.nan) if "dmn" in df_ind.columns else np.nan,
                        "ema20_dist_atr": ema20_dist_atr,
                        "bb_upper": bb_u,
                        "bb_mid": bb_m,
                        "bb_lower": bb_l,
                        "bb_pos": bb_pos,
                        "swing20_hi": swing20_hi,
                        "swing20_lo": swing20_lo,
                        "entry_5m_tick_rate": _sf(lr.get("tick_rate"), 0.0),
                        "entry_5m_is_feed_gap": bool(agg_5m.bars[-1].get("is_feed_gap", False)),
                    }

    return SpanCache(
        r_start_off=r_start_off,
        k_vel_all=k_vel_all,
        signal_minutes=signal_minutes,
        bars_processed=bars_processed,
        inverted_skipped=inverted_skipped,
        zero_spread_skipped=zero_spread_skipped,
    )


def run_clean_backtest(
    span: PreparedSpan,
    cfg: CleanSimConfig,
    cache: Optional[SpanCache] = None,
) -> dict[str, Any]:
    """Run causal tick-by-tick simulation over flat NumPy span (optionally accelerated via SpanCache)."""
    if cache is not None:
        assert not cfg.bug_invert_before_kalman, "SpanCache requires post-E1 clean quote ordering"
        assert cfg.slice_start_dt is None and cfg.slice_end_dt is None, "Use slice_prepared_span with SpanCache"

    kalman = CleanKalman2D() if cache is None else None
    agg_5m = CleanBarAgg(5, 200) if cache is None else None
    agg_1h = CleanBarAgg(60, 100) if cache is None else None

    if cache is None:
        w_bids = span.w_bids
        w_asks = span.w_asks
        for ms in span.warmup_slices:
            s, e = ms.s, ms.e
            for j in range(s, e):
                b = float(w_bids[j])
                a = float(w_asks[j])
                if b > 0.0 and a > 0.0 and b < a:
                    kalman.update(b)
            if ms.candle is not None:
                agg_5m.update(ms.candle)
                agg_1h.update(ms.candle)

    slice_start_ns = dt_to_ns(cfg.slice_start_dt) if cfg.slice_start_dt is not None else None
    slice_end_ns = dt_to_ns(cfg.slice_end_dt) if cfg.slice_end_dt is not None else None

    r_ts = span.r_ts_ns
    r_bids = span.r_bids
    r_asks = span.r_asks

    open_pos: Optional[CleanPos] = None
    closed_trades: list[dict[str, Any]] = []
    signals_log: list[dict[str, Any]] = []
    equity = float(cfg.initial_capital)
    day_start_eq = float(cfg.initial_capital)
    mtm_start_ns = slice_start_ns if slice_start_ns is not None else dt_to_ns(span.start_dt)
    mtm = CleanMtmTracker(cfg.initial_capital, mtm_start_ns)
    ks = CleanHwmKillSwitch(cfg.max_hwm_dd_pct, cfg.initial_capital) if cfg.max_hwm_dd_pct is not None else None

    daily_pnl = 0.0
    daily_trades = 0
    consec_losses = 0
    current_day: Optional[date] = None
    current_regime = "UNKNOWN"
    regime_det = CleanRegimeDetector()
    last_cd_bar: dict[tuple[str, str], int] = {}
    bar_idx = -1
    bars_processed = 0
    signals_seen = 0
    signals_passed = 0
    inverted_skipped = 0
    zero_spread_skipped = 0
    last_valid: Optional[tuple[float, float, int]] = None
    slip_off = _slip_offset(cfg)
    trade_counter = 0

    def _close_pos(pos: CleanPos, reason: str, exit_px: float, exit_ns: int, exit_bid: float, exit_ask: float, exit_min_ticks: int) -> None:
        nonlocal equity, daily_pnl, consec_losses
        pos.apply_rollovers(exit_ns, cfg)
        c_ex = _side_comm(pos.lot, exit_px, cfg)
        pos.comm_exit = c_ex
        pos.comm_total = round(pos.entry_comm + c_ex, 6)
        gross = pos.gross_at(exit_px)
        net = gross - pos.comm_total + pos.swap_pnl
        eq_before = equity
        equity += net
        daily_pnl += net
        consec_losses = 0 if net > 0 else consec_losses + 1
        mtm.update(equity, exit_ns)
        if ks is not None:
            ks.evaluate(equity, exit_ns)

        realized_loss_pct = (net / eq_before * 100.0) if (eq_before > 0 and net < 0) else 0.0
        is_bull = pos.direction == "BUY"
        mae_price_diff = (pos.entry_price - pos.worst_price) if is_bull else (pos.worst_price - pos.entry_price)
        mfe_price_diff = (pos.peak_price - pos.entry_price) if is_bull else (pos.entry_price - pos.peak_price)
        mae_atr = max(0.0, mae_price_diff) / max(pos.atr, 1e-9)
        mfe_atr = max(0.0, mfe_price_diff) / max(pos.atr, 1e-9)
        r_mult = (net / pos.planned_risk_usd) if pos.planned_risk_usd > 0 else 0.0
        entry_dt = ns_to_dt(pos.entry_ns)
        exit_dt = ns_to_dt(exit_ns)

        closed_trades.append({
            "trade_id": pos.trade_id,
            "entry_time": entry_dt,
            "exit_time": exit_dt,
            "entry_ns": pos.entry_ns,
            "exit_ns": exit_ns,
            "hold_secs": (exit_ns - pos.entry_ns) / float(_NS_PER_SEC),
            "hold_bars": pos.bars_held,
            "symbol": span.symbol,
            "direction": pos.direction,
            "strategy": pos.strategy,
            "regime": pos.regime,
            "score": pos.score,
            "entry_bid": pos.entry_bid,
            "entry_ask": pos.entry_ask,
            "entry_price": pos.entry_price,
            "entry_spread_usd": round(pos.entry_ask - pos.entry_bid, 4),
            "exit_bid": exit_bid,
            "exit_ask": exit_ask,
            "exit_price": exit_px,
            "exit_spread_usd": round(exit_ask - exit_bid, 4),
            "sl": pos.initial_sl,
            "final_sl": pos.sl,
            "theoretical_stop_price": pos.theo_sl,
            "tp": pos.tp,
            "atr": pos.atr,
            "qty": pos.lot,
            "gross_pnl": gross,
            "pnl": net,
            "commission_entry_usd": pos.entry_comm,
            "commission_exit_usd": pos.comm_exit,
            "commission_usd": pos.comm_total,
            "slippage_usd": pos.slip_usd,
            "swap_usd": pos.swap_cost,
            "swap_pnl_usd": pos.swap_pnl,
            "planned_risk_usd": pos.planned_risk_usd,
            "planned_risk_pct": pos.planned_risk_pct,
            "realized_loss_pct": realized_loss_pct,
            "sl_gap_usd": pos.last_sl_gap,
            "exit_reason": reason,
            "r_multiple": r_mult,
            "mfe_usd": max(0.0, mfe_price_diff) * pos.lot * _CONTRACT_SIZE,
            "mae_usd": max(0.0, mae_price_diff) * pos.lot * _CONTRACT_SIZE,
            "mfe_atr": mfe_atr,
            "mae_atr": mae_atr,
            "time_to_mfe_secs": (pos.peak_ns - pos.entry_ns) / float(_NS_PER_SEC),
            "time_to_mae_secs": (pos.worst_ns - pos.entry_ns) / float(_NS_PER_SEC),
            "be_triggered": pos.be_triggered,
            "t1_triggered": pos.t1_triggered,
            "t2_triggered": pos.t2_triggered,
            "kalman_tighten_triggered": pos.kalman_tighten_triggered,
            "trailing_updates_count": len(pos.sl_updates),
            "sl_updates": pos.sl_updates,
            "exit_minute_valid_ticks": exit_min_ticks,
            "entry_ctx": pos.entry_ctx,
            "journey": pos.journey,
        })

    for idx_m, ms in enumerate(span.replay_slices):
        m_py = ms.minute_ts
        m_ns = int(ms.minute_epoch_s) * _NS_PER_SEC
        if slice_end_ns is not None and m_ns > slice_end_ns and open_pos is None:
            break

        s_i, e_i = ms.s, ms.e
        bids = r_bids[s_i:e_i]
        asks = r_asks[s_i:e_i]
        ts_arr = r_ts[s_i:e_i]

        # If before slice_start_dt: warm indicators, Kalman & regime causally without taking trades
        if cache is None and slice_start_ns is not None and m_ns < slice_start_ns:
            for k in range(len(bids)):
                b = float(bids[k])
                a = float(asks[k])
                if b > 0.0 and a > 0.0 and b < a:
                    kalman.update(b)
            if ms.candle is not None:
                c5 = agg_5m.update(ms.candle)
                agg_1h.update(ms.candle)
                if c5 and len(agg_5m.bars) >= 60:
                    df_w = clean_compute_indicators(list(agg_5m.bars), compute_adx=False)
                    if not df_w.empty:
                        current_regime = regime_det.detect(df_w)
            continue

        bar_idx += 1
        if current_day != m_py.date():
            current_day = m_py.date()
            daily_pnl = 0.0
            daily_trades = 0
            consec_losses = 0
            day_start_eq = max(equity, 1.0)

        if cache is None:
            inverted_skipped += ms.inverted_count
            zero_spread_skipped += ms.zero_spread_count
            valid_mask = (bids > 0.0) & (asks > 0.0) & (bids < asks)
            valid_idxs = np.flatnonzero(valid_mask)
            if len(valid_idxs) > 0:
                lv = int(valid_idxs[-1])
                last_valid = (float(bids[lv]), float(asks[lv]), int(ts_arr[lv]))

            closed_5m = False
            df_ind = pd.DataFrame()
            if ms.candle is not None:
                closed_5m = agg_5m.update(ms.candle)
                agg_1h.update(ms.candle)
                bars_processed += 1
                if closed_5m and len(agg_5m.bars) >= 60:
                    df_ind = clean_compute_indicators(list(agg_5m.bars), compute_adx=cfg.compute_adx_features)
                    if not df_ind.empty:
                        current_regime = regime_det.detect(df_ind)
        else:
            # Fast skip when flat and minute has no precomputed candidate signals
            if open_pos is None and idx_m not in cache.signal_minutes:
                continue
            valid_mask = (bids > 0.0) & (asks > 0.0) & (bids < asks)
            valid_idxs = np.flatnonzero(valid_mask)
            if len(valid_idxs) > 0:
                lv = int(valid_idxs[-1])
                last_valid = (float(bids[lv]), float(asks[lv]), int(ts_arr[lv]))

        def _scan_exit(pos: CleanPos, start_k: int, eval_to: bool, cur_min_ticks: int) -> tuple[bool, str, float, int, float, float]:
            is_bull = pos.direction == "BUY"
            e_hr = ns_to_dt(pos.entry_ns).hour
            eff_mh = min(cfg.max_hold_bars, 60 if 7 <= e_hr < 20 else 90)
            if cfg.kalman_rate_norm:
                eff_rate = max(float(pos.prev_minute_valid_ticks), 60.0)
                atr_per_tick = max(pos.atr / eff_rate, 0.01)
            else:
                atr_per_tick = max(pos.atr / 60.0, 0.01)

            best_q: Optional[float] = None
            last_q: Optional[float] = None
            last_v_idx: Optional[int] = None
            last_vb = pos.entry_bid
            last_va = pos.entry_ask
            last_best_ns = pos.entry_ns

            for k in range(start_k, len(bids)):
                b = float(bids[k])
                a = float(asks[k])
                t_ns = int(ts_arr[k])

                if cfg.bug_invert_before_kalman:
                    _, k_vel = kalman.update(b)
                    if b <= 0.0 or a <= 0.0 or b >= a:
                        continue
                else:
                    if b <= 0.0 or a <= 0.0 or b >= a:
                        continue
                    k_vel = float(cache.k_vel_all[(s_i + k) - cache.r_start_off]) if cache is not None else kalman.update(b)[1]

                pos.apply_rollovers(t_ns, cfg)
                exec_q = b if is_bull else a
                last_q = exec_q
                last_v_idx = k
                last_vb = b
                last_va = a
                last_best_ns = t_ns

                if is_bull and exec_q < pos.worst_price:
                    pos.worst_price = exec_q
                    pos.worst_ns = t_ns
                elif (not is_bull) and exec_q > pos.worst_price:
                    pos.worst_price = exec_q
                    pos.worst_ns = t_ns

                if best_q is None or (is_bull and exec_q > best_q) or (not is_bull and exec_q < best_q):
                    best_q = exec_q

                vel_norm = k_vel / atr_per_tick

                def _check_kalman_block() -> Optional[tuple[bool, str, float, int, float, float]]:
                    if not cfg.use_kalman_exit:
                        return None
                    if pos.bars_held >= cfg.kalman_min_hold_bars:
                        if is_bull and vel_norm <= -cfg.kalman_vel_thresh:
                            ex_px = exec_q - slip_off
                            pos.slip_usd += slip_off * pos.lot * _CONTRACT_SIZE
                            if not cfg.bug_skip_mtm_on_kalman_exit:
                                fill_unr = pos.net_at(ex_px, cfg)
                                mtm.update(equity + fill_unr, t_ns)
                            return True, "KALMAN_VELOCITY_REVERSAL", ex_px, k, b, a
                        elif (not is_bull) and vel_norm >= cfg.kalman_vel_thresh:
                            ex_px = exec_q + slip_off
                            pos.slip_usd += slip_off * pos.lot * _CONTRACT_SIZE
                            if not cfg.bug_skip_mtm_on_kalman_exit:
                                fill_unr = pos.net_at(ex_px, cfg)
                                mtm.update(equity + fill_unr, t_ns)
                            return True, "KALMAN_VELOCITY_REVERSAL", ex_px, k, b, a

                    gain_k = (b - pos.entry_price) if is_bull else (pos.entry_price - a)
                    if gain_k >= pos.atr * 1.5:
                        if (is_bull and vel_norm < -0.5) or ((not is_bull) and vel_norm > 0.5):
                            tight_sl = (b - pos.atr * 0.75) if is_bull else (a + pos.atr * 0.75)
                            if (is_bull and tight_sl > pos.sl) or ((not is_bull) and tight_sl < pos.sl):
                                pos.sl = tight_sl
                                pos.trailing_active = True
                                pos.exit_reason_flag = "KALMAN_TIGHTEN"
                                pos.kalman_tighten_triggered = True
                                pos.sl_updates.append({"ts": ns_to_dt(t_ns).isoformat(), "stage": "KALMAN_TIGHTEN", "sl": round(pos.sl, 5)})
                    return None

                if cfg.bug_kalman_before_sl_tp:
                    k_res = _check_kalman_block()
                    if k_res is not None:
                        return k_res

                if is_bull and b <= pos.sl:
                    rsn = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
                    theo = float(pos.sl)
                    fpx = b - slip_off
                    if slip_off > 0.0:
                        pos.slip_usd += slip_off * pos.lot * _CONTRACT_SIZE
                    gap_usd = pos.net_at(fpx, cfg) - pos.net_at(theo, cfg)
                    pos.theo_sl = theo
                    pos.last_sl_gap = gap_usd
                    mtm.update(equity + pos.net_at(fpx, cfg), t_ns, gap_usd=gap_usd)
                    return True, rsn, fpx, k, b, a
                if (not is_bull) and a >= pos.sl:
                    rsn = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
                    theo = float(pos.sl)
                    fpx = a + slip_off
                    if slip_off > 0.0:
                        pos.slip_usd += slip_off * pos.lot * _CONTRACT_SIZE
                    gap_usd = pos.net_at(fpx, cfg) - pos.net_at(theo, cfg)
                    pos.theo_sl = theo
                    pos.last_sl_gap = gap_usd
                    mtm.update(equity + pos.net_at(fpx, cfg), t_ns, gap_usd=gap_usd)
                    return True, rsn, fpx, k, b, a

                if is_bull and b >= pos.tp:
                    fpx = float(pos.tp)
                    mtm.update(equity + pos.net_at(fpx, cfg), t_ns)
                    return True, "TARGET", fpx, k, b, a
                if (not is_bull) and a <= pos.tp:
                    fpx = float(pos.tp)
                    mtm.update(equity + pos.net_at(fpx, cfg), t_ns)
                    return True, "TARGET", fpx, k, b, a

                if not cfg.bug_kalman_before_sl_tp:
                    k_res = _check_kalman_block()
                    if k_res is not None:
                        return k_res

                raw_unr = pos.net_at(exec_q, cfg)
                mtm.update(equity + raw_unr, t_ns)

                if ks is not None and ks.evaluate(equity + raw_unr, t_ns):
                    fpx = (exec_q - slip_off) if is_bull else (exec_q + slip_off)
                    if slip_off > 0.0:
                        pos.slip_usd += slip_off * pos.lot * _CONTRACT_SIZE
                    mtm.update(equity + pos.net_at(fpx, cfg), t_ns)
                    return True, "HWM_KILL_SWITCH_8PCT", fpx, k, b, a

                pos.ratchet(exec_q, t_ns, eff_mh)

            pos.prev_minute_valid_ticks = cur_min_ticks

            if eval_to:
                ret_idx = (len(bids) - 1) if cfg.bug_len_minus_1_on_timeout_idx else (last_v_idx if last_v_idx is not None else len(bids) - 1)
                if cfg.early_fail_mins is not None and cfg.early_fail_min_mfe_atr is not None and pos.bars_held == cfg.early_fail_mins:
                    gain_abs = (pos.peak_price - pos.entry_price) if is_bull else (pos.entry_price - pos.peak_price)
                    mfe_atr = gain_abs / max(pos.atr, 1e-9)
                    if mfe_atr < cfg.early_fail_min_mfe_atr:
                        base_px = last_q if last_q is not None else pos.entry_price
                        ex_px = (base_px - slip_off) if is_bull else (base_px + slip_off)
                        if slip_off > 0.0 and last_q is not None:
                            pos.slip_usd += slip_off * pos.lot * _CONTRACT_SIZE
                        return True, f"EARLY_FAIL_{cfg.early_fail_mins}M", ex_px, ret_idx, last_vb, last_va

                if pos.bars_held >= eff_mh:
                    base_px = last_q if last_q is not None else pos.entry_price
                    ex_px = (base_px - slip_off) if is_bull else (base_px + slip_off)
                    if slip_off > 0.0 and last_q is not None:
                        pos.slip_usd += slip_off * pos.lot * _CONTRACT_SIZE
                    return True, "TIMEOUT", ex_px, ret_idx, last_vb, last_va

            if best_q is not None:
                pos.ratchet(best_q, last_best_ns, eff_mh)

            return False, "", 0.0, len(bids) - 1, last_vb, last_va

        if open_pos is not None:
            open_pos.bars_held += 1
            exited, rsn, epx, lidx, ex_b, ex_a = _scan_exit(open_pos, 0, True, len(valid_idxs))
            cur_px = epx if exited else float(bids[-1])
            open_pos.journey.append({
                "time": m_py.strftime("%Y-%m-%d %H:%M:%S"),
                "price": round(cur_px, 2),
                "sl": round(open_pos.sl, 2),
                "bars_held": open_pos.bars_held,
            })
            if exited:
                if cache is None:
                    for j in range(lidx + 1, len(bids)):
                        if valid_mask[j]:
                            kalman.update(float(bids[j]))
                _close_pos(open_pos, rsn, epx, int(ts_arr[lidx]), ex_b, ex_a, len(valid_idxs))
                open_pos = None
            continue

        if slice_end_ns is not None and m_ns > slice_end_ns:
            continue

        if cache is None:
            if len(valid_idxs) == 0:
                continue

            en_i = int(valid_idxs[0])
            for j in range(en_i + 1):
                if valid_mask[j]:
                    kalman.update(float(bids[j]))
            k_until = en_i + 1

            def _flush_k() -> None:
                for j in range(k_until, len(bids)):
                    if valid_mask[j]:
                        kalman.update(float(bids[j]))

            if not closed_5m or len(agg_5m.bars) < 60 or df_ind.empty:
                _flush_k()
                continue
            if cfg.session_close_utc is not None and m_py.hour >= cfg.session_close_utc:
                _flush_k()
                continue

            daily_loss_pct = abs(min(0.0, daily_pnl)) / max(day_start_eq, 1.0) * 100.0
            if daily_loss_pct >= cfg.max_daily_loss_pct or daily_trades >= cfg.max_trades_per_day or consec_losses >= cfg.max_consecutive_losses:
                _flush_k()
                continue

            htf_trend, htf_spread_pct = clean_htf_trend([b["close"] for b in agg_1h.bars])
            if htf_trend == 0:
                _flush_k()
                continue

            sigs = clean_generate_signals(df_ind, current_regime)
            if not sigs:
                _flush_k()
                continue

            signals_seen += len(sigs)
            lr = df_ind.iloc[-1]
            cs_s = df_ind["close"]
            c5_ts = agg_5m.bars[-1]["time"]

            def _sf(v: Any, dflt: float) -> float:
                try:
                    f = float(v)
                    return dflt if math.isnan(f) else f
                except Exception:
                    return dflt

            feat = {
                "kalman_pos": kalman.x0,
                "kalman_vel": kalman.x1,
                "kalman_accel": kalman.accel(),
                "shannon_entropy": clean_shannon_entropy(cs_s.pct_change().dropna()) if len(cs_s) > 30 else 2.0,
                "kaufman_er": clean_kaufman_er(cs_s) if len(cs_s) > 15 else 0.3,
                "ema20_slope": _sf(lr.get("ema20_slope"), 0.0),
                "tick_weighted_price_dist": _sf(lr.get("tick_weighted_price_dist"), 0.0),
                "tick_rate_ratio": _sf(lr.get("tick_rate_ratio"), 1.0),
                "rsi": _sf(lr.get("rsi"), 50.0),
                "utc_hour": c5_ts.hour,
            }
            atr_v = _sf(lr.get("atr"), 1.0)
            ema20_v = _sf(lr.get("ema20"), 0.0)
            ema50_v = _sf(lr.get("ema50"), 0.0)
            adx_v = _sf(lr.get("adx"), np.nan) if "adx" in df_ind.columns else np.nan
            dmp_v = _sf(lr.get("dmp"), np.nan) if "dmp" in df_ind.columns else np.nan
            dmn_v = _sf(lr.get("dmn"), np.nan) if "dmn" in df_ind.columns else np.nan
            ema20_dist_atr = ((_sf(lr.get("close"), 0.0) - ema20_v) / max(atr_v, 1e-9)) if ema20_v > 0 else 0.0
            bb_u = _sf(lr.get("bollinger_upper"), np.nan)
            bb_m = _sf(lr.get("bollinger_mid"), np.nan)
            bb_l = _sf(lr.get("bollinger_lower"), np.nan)
            bb_pos = ((_sf(lr.get("close"), 0.0) - bb_l) / max(bb_u - bb_l, 1e-9)) if not (math.isnan(bb_u) or math.isnan(bb_l)) else np.nan
            swing20_hi = float(df_ind["high"].tail(20).max())
            swing20_lo = float(df_ind["low"].tail(20).min())
            entry_5m_tr = _sf(lr.get("tick_rate"), 0.0)
            entry_5m_gap = bool(agg_5m.bars[-1].get("is_feed_gap", False))
            raw_reg_entry = regime_det.last_raw
            reg_bars_since = regime_det.bars_since_commit
        else:
            meta = cache.signal_minutes[idx_m]
            en_i = meta["en_i"]
            def _flush_k() -> None:
                return None

            if cfg.session_close_utc is not None and m_py.hour >= cfg.session_close_utc:
                continue

            daily_loss_pct = abs(min(0.0, daily_pnl)) / max(day_start_eq, 1.0) * 100.0
            if daily_loss_pct >= cfg.max_daily_loss_pct or daily_trades >= cfg.max_trades_per_day or consec_losses >= cfg.max_consecutive_losses:
                continue

            current_regime = meta["regime"]
            htf_trend = meta["htf_trend"]
            htf_spread_pct = meta["htf_spread_pct"]
            sigs = meta["sigs"]
            signals_seen += len(sigs)
            feat = meta["feat"]
            atr_v = meta["atr"]
            ema20_v = meta["ema20"]
            ema50_v = meta["ema50"]
            adx_v = meta["adx"]
            dmp_v = meta["dmp"]
            dmn_v = meta["dmn"]
            ema20_dist_atr = meta["ema20_dist_atr"]
            bb_u = meta["bb_upper"]
            bb_m = meta["bb_mid"]
            bb_l = meta["bb_lower"]
            bb_pos = meta["bb_pos"]
            swing20_hi = meta["swing20_hi"]
            swing20_lo = meta["swing20_lo"]
            entry_5m_tr = meta["entry_5m_tick_rate"]
            entry_5m_gap = meta["entry_5m_is_feed_gap"]
            raw_reg_entry = meta["raw_regime"]
            reg_bars_since = meta["bars_since_commit"]

        scored: list[tuple[float, str, str, dict[str, float]]] = []
        for st_name, dir_val in sigs:
            sc_val, comp = clean_score_detailed(dir_val, current_regime, feat)
            if sc_val >= cfg.min_score:
                scored.append((sc_val, st_name, dir_val, comp))
        scored.sort(key=lambda x: x[0], reverse=True)

        if not scored:
            if cfg.record_signals:
                et_iso = ns_to_dt(int(ts_arr[en_i])).isoformat()
                for st_name, dir_val in sigs:
                    sc_v, _ = clean_score_detailed(dir_val, current_regime, feat)
                    signals_log.append({"time": et_iso, "strategy": st_name, "direction": dir_val, "regime": current_regime, "score": sc_v, "reason": "SCORE_BELOW_MIN"})
            _flush_k()
            continue

        sc, st, d, comp = scored[0]

        def _reject(rsn: str) -> None:
            if cfg.record_signals:
                signals_log.append({"time": ns_to_dt(int(ts_arr[en_i])).isoformat(), "strategy": st, "direction": d, "regime": current_regime, "score": sc, "reason": rsn})
            _flush_k()

        if (d == "BUY" and htf_trend != 1) or (d == "SELL" and htf_trend != -1):
            _reject("H1_TREND_GATE")
            continue

        if (cfg.block_high_vol and current_regime == "HIGH_VOLATILITY") or (cfg.block_buy_sideways and d == "BUY" and current_regime == "SIDEWAYS") or (cfg.block_sell_sideways and d == "SELL" and current_regime == "SIDEWAYS"):
            _reject("REGIME_GATE")
            continue

        if cfg.max_score is not None and sc >= cfg.max_score:
            _reject("SCORE_CAP_GATE")
            continue

        if cfg.adx_min is not None and (math.isnan(adx_v) or adx_v < cfg.adx_min):
            _reject("ADX_MIN_GATE")
            continue

        if cfg.ema20_max_dist_atr is not None:
            signed_dist = ema20_dist_atr if d == "BUY" else -ema20_dist_atr
            if signed_dist > cfg.ema20_max_dist_atr:
                _reject("EMA20_EXT_GATE")
                continue

        if bar_idx - last_cd_bar.get((st, d), -9999) < cfg.cooldown_bars:
            _reject("COOLDOWN_GATE")
            continue

        if ks is not None and ks.halted:
            ks.trades_prevented += 1
            _reject("HWM_KILL_SWITCH_8PCT")
            continue

        eb, ea = float(bids[en_i]), float(asks[en_i])
        raw_ep = ea if d == "BUY" else eb
        ep = (raw_ep + slip_off) if d == "BUY" else (raw_ep - slip_off)
        lot, sl_px, tp_px, size_err = _size_position(equity, ep, atr_v, d, cfg)
        if lot is None:
            _reject(f"SIZING_{size_err}")
            continue

        e_comm = _side_comm(lot, ep, cfg)
        est_rt_comm = 2.0 * e_comm
        e_slip = slip_off * lot * _CONTRACT_SIZE
        planned_risk_usd = abs(ep - sl_px) * lot * _CONTRACT_SIZE + est_rt_comm
        planned_risk_pct = (planned_risk_usd / equity * 100.0) if equity > 0 else 0.0

        entry_ctx = {
            "ema20": ema20_v,
            "ema50": ema50_v,
            "ema_spread_pct": ((ema20_v - ema50_v) / ema50_v * 100.0) if ema50_v > 0 else 0.0,
            "ema20_slope": feat["ema20_slope"],
            "ema20_dist_atr": ema20_dist_atr,
            "rsi": feat["rsi"],
            "bb_upper": bb_u,
            "bb_mid": bb_m,
            "bb_lower": bb_l,
            "bb_pos": bb_pos,
            "adx": adx_v,
            "dmp": dmp_v,
            "dmn": dmn_v,
            "kaufman_er": feat["kaufman_er"],
            "shannon_entropy": feat["shannon_entropy"],
            "tick_rate_ratio": feat["tick_rate_ratio"],
            "tick_weighted_price_dist": feat["tick_weighted_price_dist"],
            "entry_5m_tick_rate": entry_5m_tr,
            "entry_5m_is_feed_gap": entry_5m_gap,
            "htf_h1_trend": htf_trend,
            "htf_h1_ema_spread_pct": round(htf_spread_pct, 4),
            "kalman_pos": feat["kalman_pos"],
            "kalman_vel": feat["kalman_vel"],
            "kalman_accel": feat["kalman_accel"],
            "kalman_vel_norm_at_entry": feat["kalman_vel"] / max(atr_v / 60.0, 0.01),
            "raw_regime_at_entry": raw_reg_entry,
            "regime_bars_since_commit": reg_bars_since,
            "dist_to_20bar_high_atr": (swing20_hi - raw_ep) / max(atr_v, 1e-9),
            "dist_to_20bar_low_atr": (raw_ep - swing20_lo) / max(atr_v, 1e-9),
            "entry_minute_valid_ticks": int(len(valid_idxs)),
            "utc_hour": feat["utc_hour"],
            **comp,
        }

        trade_counter += 1
        entry_ns = int(ts_arr[en_i])
        open_pos = CleanPos(
            trade_id=trade_counter,
            direction=d,
            strategy=st,
            regime=current_regime,
            score=sc,
            entry_ns=entry_ns,
            entry_bid=eb,
            entry_ask=ea,
            entry_price=ep,
            initial_sl=sl_px,
            sl=sl_px,
            tp=tp_px,
            atr=atr_v,
            lot=lot,
            planned_risk_usd=planned_risk_usd,
            planned_risk_pct=planned_risk_pct,
            entry_comm=e_comm,
            slip_usd=e_slip,
            peak_price=ep,
            peak_ns=entry_ns,
            worst_price=ep,
            worst_ns=entry_ns,
            last_swap_ns=entry_ns,
            entry_ctx=entry_ctx,
            prev_minute_valid_ticks=len(valid_idxs),
        )
        init_unr = -(ea - eb) * lot * _CONTRACT_SIZE - est_rt_comm - e_slip
        mtm.update(equity + init_unr, entry_ns)
        last_cd_bar[(st, d)] = bar_idx
        daily_trades += 1
        signals_passed += 1

        if cfg.record_signals:
            signals_log.append({"time": ns_to_dt(entry_ns).isoformat(), "strategy": st, "direction": d, "regime": current_regime, "score": sc, "reason": "EXECUTED"})

        if en_i + 1 < len(bids):
            exited, rsn, epx, lidx, ex_b, ex_a = _scan_exit(open_pos, en_i + 1, False, len(valid_idxs))
            if exited:
                open_pos.journey.append({"time": m_py.strftime("%Y-%m-%d %H:%M:%S"), "price": round(epx, 2), "sl": round(open_pos.sl, 2), "bars_held": 0})
                if cache is None:
                    for j in range(lidx + 1, len(bids)):
                        if valid_mask[j]:
                            kalman.update(float(bids[j]))
                _close_pos(open_pos, rsn, epx, int(ts_arr[lidx]), ex_b, ex_a, len(valid_idxs))
                open_pos = None

    if cache is not None:
        bars_processed = cache.bars_processed
        inverted_skipped = cache.inverted_skipped
        zero_spread_skipped = cache.zero_spread_skipped

    if open_pos is not None and last_valid is not None:
        lb, la, lns = last_valid
        rc = lb if open_pos.direction == "BUY" else la
        epx = (rc - slip_off) if open_pos.direction == "BUY" else (rc + slip_off)
        if slip_off > 0.0:
            open_pos.slip_usd += slip_off * open_pos.lot * _CONTRACT_SIZE
        _close_pos(open_pos, "END_OF_BACKTEST", epx, lns, lb, la, open_pos.prev_minute_valid_ticks)
        open_pos = None

    pnls = [float(t["pnl"]) for t in closed_trades]
    n = len(closed_trades)
    wins = sum(1 for p in pnls if round(p, 2) > 0.0)
    losses = sum(1 for p in pnls if round(p, 2) < 0.0)
    bes = n - wins - losses
    total_pnl = sum(pnls)
    gross_win = sum(p for p in pnls if round(p, 2) > 0.0)
    gross_loss = sum(p for p in pnls if round(p, 2) < 0.0)
    pf = abs(gross_win / gross_loss) if gross_loss != 0.0 else (999.0 if gross_win > 0 else 0.0)

    ceq = cfg.initial_capital
    chwm = cfg.initial_capital
    closed_dd = 0.0
    closed_dd_pct = 0.0
    for p in pnls:
        ceq += p
        if ceq > chwm:
            chwm = ceq
        dd = ceq - chwm
        if dd < closed_dd:
            closed_dd = dd
        pct = abs(dd) / chwm * 100.0 if chwm > 0 else 0.0
        if pct > closed_dd_pct:
            closed_dd_pct = pct

    buy_trades = [t for t in closed_trades if t["direction"] == "BUY"]
    sell_trades = [t for t in closed_trades if t["direction"] == "SELL"]
    exit_breakdown: dict[str, dict[str, Any]] = {}
    for t in closed_trades:
        eb = exit_breakdown.setdefault(t["exit_reason"], {"count": 0, "wins": 0, "pnl": 0.0})
        eb["count"] += 1
        if round(t["pnl"], 2) > 0.0:
            eb["wins"] += 1
        eb["pnl"] += float(t["pnl"])
    for eb in exit_breakdown.values():
        eb["pnl"] = round(eb["pnl"], 4)

    hwm_summary = None
    if ks is not None:
        hwm_summary = {
            "halted": ks.halted,
            "trigger_time": ns_to_dt(ks.trigger_ns).isoformat() if ks.trigger_ns else None,
            "trigger_hwm": ks.trigger_hwm,
            "trigger_equity": ks.trigger_equity,
            "trigger_drawdown_pct": ks.trigger_dd_pct,
            "trades_prevented": ks.trades_prevented,
            "max_seen_dd_pct": ks.max_seen_dd_pct,
        }

    return {
        "trades": n,
        "wins": wins,
        "losses": losses,
        "breakevens": bes,
        "win_rate_pct": (wins / n * 100.0) if n > 0 else 0.0,
        "pnl": total_pnl,
        "gross_pnl": sum(float(t["gross_pnl"]) for t in closed_trades),
        "commission_usd": sum(float(t["commission_usd"]) for t in closed_trades),
        "slippage_usd": sum(float(t["slippage_usd"]) for t in closed_trades),
        "swap_usd": sum(float(t["swap_usd"]) for t in closed_trades),
        "final_equity": equity,
        "pf": pf,
        "expectancy": (total_pnl / n) if n > 0 else 0.0,
        "closed_max_dd": closed_dd,
        "closed_max_dd_pct": closed_dd_pct,
        "mtm_max_dd": mtm.max_dd,
        "mtm_max_dd_pct": mtm.max_dd_pct,
        "mtm_max_dd_initial_pct": mtm.max_dd_init_pct,
        "mtm_peak_equity": mtm.peak_eq,
        "mtm_trough_equity": mtm.trough_eq,
        "mtm_peak_time": str(ns_to_dt(mtm.peak_ns)),
        "mtm_trough_time": str(ns_to_dt(mtm.trough_ns)),
        "buy_trades": len(buy_trades),
        "buy_wins": sum(1 for t in buy_trades if round(t["pnl"], 2) > 0.0),
        "buy_pnl": sum(float(t["pnl"]) for t in buy_trades),
        "sell_trades": len(sell_trades),
        "sell_wins": sum(1 for t in sell_trades if round(t["pnl"], 2) > 0.0),
        "sell_pnl": sum(float(t["pnl"]) for t in sell_trades),
        "bars_processed": bars_processed,
        "signals_seen": signals_seen,
        "signals_passed": signals_passed,
        "inverted_quotes_skipped": inverted_skipped,
        "zero_spread_ticks_skipped": zero_spread_skipped,
        "exit_breakdown": exit_breakdown,
        "hwm_kill_switch": hwm_summary,
        "trade_list": closed_trades,
        "signals_log": signals_log,
    }
