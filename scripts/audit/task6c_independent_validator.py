"""
Task 6C (Phases 2, 3, 5, 6) — Truly Independent Raw-Tick Full Pipeline Validator.

Architecture & Independence Guarantee:
  - Imports ZERO modules from `src/goldedge` (no TickRepository, no RunBacktestUseCase,
    no IndicatorService, no RegimeDetectorService, no SignalGeneratorService,
    no TradeScorer, no PositionSizingService, no BrokerCostService).
  - Connects directly via raw `psycopg2` cursor to `market_ticks` in TimescaleDB.
  - Independently reconstructs:
      1. Causal 1m/5m/1h OHLC + `tick_count`/`tick_rate` (`bid > 0 & ask > 0 & bid < ask`)
      2. Pure-scalar 2D Kalman Filter (Position, Velocity, Acceleration)
      3. Technical indicators, regime + 3-bar hysteresis, H1 EMA20/50 trend
      4. Strategy signal generation + quantitative scoring + risk/regime gates
      5. Execution-price risk sizing ($10k start, 1.0% max risk, Fusion $12/M comm)
      6. Tick-by-tick exits, BE/T1/T2/TIME_SQUEEZE trailing ratchets (logged per bar!),
         DST-aware NY 17:00 rollover & Wednesday 3x swap, slippage (`0.0533` pips),
         closed-equity & tick-MTM drawdowns, MAE/MFE, and daily/consecutive losses.
  - Reconciles trade-by-trade AND trailing-step-by-trailing-step against the actual
    GoldEdge application outputs across:
      * W1: 2026-08-24 -> 2026-11-16 20:01:23Z (118 trades)
      * W2: 2026-08-13 -> 2026-11-16 20:01:23Z (131 trades)
      * W3: 2026-08-17 -> 2026-11-18 18:00:00Z, Aug 13..16 warmup (143 trades)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
try:
    import pandas_ta as ta
except ImportError:
    import pandas_ta_classic as ta
import psycopg2

ROOT = Path(__file__).resolve().parent.parent.parent
if not (ROOT / "backtest_results").exists() and Path("/app/backtest_results").exists():
    ROOT = Path("/app")
AUDIT_DIR = ROOT / "backtest_results" / "audit"
AUDIT_DIR.mkdir(parents=True, exist_ok=True)

_NY_TZ = ZoneInfo("America/New_York")
_CONTRACT_SIZE = 100.0
_PIP_SIZE = 0.10
_TICK_SIZE = 0.01
_TICK_VALUE = 1.0
_MIN_LOT = 0.01
_MAX_LOT = 50.0
_VOL_STEP = 0.01
_MARGIN_PER_LOT = 1000.0
_COMM_RATE_PER_MILLION = 12.0
_SWAP_LONG_PIPS = -5.653
_SWAP_SHORT_PIPS = 2.677
_SLIP_PIPS = 0.0533
_SLIP_OFFSET = _SLIP_PIPS * _PIP_SIZE


# ---------------------------------------------------------------------------
# 1. Independent Scalar 2D Kalman Filter & Entropy / ER
# ---------------------------------------------------------------------------

class IndKalman2D:
    def __init__(self, q_p: float = 1e-4, q_v: float = 3e-5, r_base: float = 1e-2):
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


def _ind_shannon_entropy(returns: pd.Series, window: int = 30) -> float:
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


def _ind_kaufman_er(series: pd.Series, window: int = 10) -> float:
    if len(series) <= window:
        return 0.0
    direction = abs(float(series.iloc[-1]) - float(series.iloc[-(window + 1)]))
    vol = float(series.diff().abs().iloc[-window:].sum())
    return 0.0 if vol == 0.0 else float(direction / vol)


# ---------------------------------------------------------------------------
# 2. Independent Rollers, Indicators, Regime, Signals, Scorer
# ---------------------------------------------------------------------------

class IndBarAggregator:
    def __init__(self, period_minutes: int, maxlen: int):
        self.period_secs = period_minutes * 60
        self.bars: deque[dict] = deque(maxlen=maxlen)
        self.cur_bucket: Optional[int] = None
        self.cur_bar: Optional[dict] = None

    def update(self, c: dict) -> bool:
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
                "tick_rate": round(float(c["tick_count"]) / float(self.period_secs / 60.0), 4),
            }
        else:
            b = self.cur_bar
            if c["high"] > b["high"]:
                b["high"] = float(c["high"])
            if c["low"] < b["low"]:
                b["low"] = float(c["low"])
            b["close"] = float(c["close"])
            b["tick_count"] += int(c["tick_count"])
            b["tick_rate"] = round(float(b["tick_count"]) / float(self.period_secs / 60.0), 4)
        return closed


def _ind_htf_trend(closes: list[float], fast: int = 20, slow: int = 50) -> int:
    if len(closes) < slow:
        return 0
    af = 2.0 / (fast + 1.0)
    as_ = 2.0 / (slow + 1.0)
    ef = float(closes[0])
    es = float(closes[0])
    for v in closes[1:]:
        fv = float(v)
        ef = af * fv + (1.0 - af) * ef
        es = as_ * fv + (1.0 - as_) * es
    if ef > es:
        return 1
    if ef < es:
        return -1
    return 0


def _ind_compute_indicators(bars: list[dict]) -> pd.DataFrame:
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
    df["tick_weighted_price"] = ta.vwap(df["high"], df["low"], df["close"], df["tick_count"])
    df["tick_weighted_price_dist"] = (df["close"] - df["tick_weighted_price"]) / df["tick_weighted_price"]
    df["tick_rate_sma20"] = ta.sma(df["tick_rate"], length=20)
    df["tick_rate_ratio"] = df["tick_rate"] / df["tick_rate_sma20"].replace(0, np.nan)
    df["ema20_slope"] = df["ema20"].diff(5) / df["ema20"].shift(5)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    return df


class IndRegimeDetector:
    def __init__(self):
        self.hist: deque[str] = deque(maxlen=3)
        self.committed: str = "UNKNOWN"

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

        self.hist.append(raw)
        if len(self.hist) == 3 and len(set(self.hist)) == 1:
            self.committed = self.hist[-1]
        return self.committed


_REGIME_STRATS = {
    "TRENDING_BULL": ["kinematic_kalman_trend", "vwap_momentum_breakout", "bullish_momentum"],
    "TRENDING_BEAR": ["kinematic_kalman_trend", "bearish_momentum"],
    "SIDEWAYS": ["statistical_mean_reversion", "bullish_momentum", "bearish_momentum"],
    "HIGH_VOLATILITY": ["session_orb", "bearish_momentum", "vwap_momentum_breakout", "bullish_momentum"],
    "LOW_VOLATILITY": ["statistical_mean_reversion", "vwap_momentum_breakout"],
    "UNKNOWN": ["kinematic_kalman_trend", "vwap_momentum_breakout", "bearish_momentum", "bullish_momentum"],
}


def _ind_generate_signals(df: pd.DataFrame, regime: str) -> list[tuple[str, str]]:
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
        if v <= lo: return 0.0
        if v >= hi: return 1.0
        return (v - lo) / (hi - lo)
    if v >= lo: return 0.0
    if v <= hi: return 1.0
    return (v - lo) / (hi - lo)


def _sess_score(h: int) -> float:
    if 0 <= h < 7: return 0.0
    if 7 <= h < 9: return 0.4
    if 9 <= h < 12: return 0.7
    if 12 <= h < 17: return 1.0
    if 17 <= h < 20: return 0.7
    return 0.2


def _ind_score(direction: str, regime: str, feat: dict) -> float:
    kv = feat["kalman_vel"]
    ent = feat["shannon_entropy"]
    er = feat["kaufman_er"]
    slp = feat["ema20_slope"]
    twd = feat["tick_weighted_price_dist"]
    h = feat["utc_hour"]
    sw = "SIDEWAYS" in regime.upper()
    s = 0.0
    if direction == "BUY":
        mom = _score_band(kv, 0.008, 0.040, 0.002, 0.080) if sw else _score_band(kv, 0.015, 0.060, 0.005, 0.120)
        s += 0.35 * mom
        s += 0.20 * _score_band(er, 0.20, 0.60, 0.08, 0.90)
        s += 0.15 * _score_band(ent, 2.50, 3.20, None, 3.60)
        s += 0.15 * _interp(slp, 0.00005, 0.00030)
        s += 0.10 * _score_band(twd, -0.0005, 0.003, -0.003, 0.006)
        s += 0.05 * _sess_score(h)
    else:
        mom = _score_band(kv, -0.040, -0.008, -0.080, -0.002) if sw else _score_band(kv, -0.060, -0.015, -0.120, -0.005)
        s += 0.35 * mom
        s += 0.20 * _score_band(er, 0.20, 0.60, 0.08, 0.90)
        s += 0.15 * _score_band(ent, 2.50, 3.20, None, 3.60)
        s += 0.15 * _interp(slp, -0.00005, -0.00030)
        s += 0.10 * _score_band(twd, -0.003, 0.0005, -0.006, 0.003)
        s += 0.05 * _sess_score(h)

    qs = min(1.0, max(0.0, s))
    ka = feat["kalman_accel"]
    adj = max(-0.08, min(0.08, ka * 2.0 if direction == "BUY" else -ka * 2.0))
    qs = min(1.0, max(0.0, qs + adj))
    trr = feat["tick_rate_ratio"]
    if trr < 0.70:
        qs *= 0.75
    elif sw and trr < 1.10:
        qs *= 0.85
    qs = round(qs, 4)
    return qs  # w_ml=0.0, w_quant=1.0 in backtest evaluation


# ---------------------------------------------------------------------------
# 3. Independent Sizing, Costs, Rollover & Position Execution
# ---------------------------------------------------------------------------

def _ind_side_comm(lots: float, px: float) -> float:
    return round(abs(lots) * _CONTRACT_SIZE * float(px) * (_COMM_RATE_PER_MILLION / 1_000_000.0), 2)


def _ind_size(equity: float, entry_px: float, atr: float, direction: str) -> tuple[Optional[float], float, float]:
    sl_dist = atr * 1.5
    tp_dist = atr * 3.0
    sl_px = entry_px - sl_dist if direction == "BUY" else entry_px + sl_dist
    tp_px = entry_px + tp_dist if direction == "BUY" else entry_px - tp_dist
    max_risk = equity * (1.0 / 100.0)
    est_rt = round(2.0 * _ind_side_comm(1.0, entry_px), 2)
    loss_per_lot = (abs(entry_px - sl_px) / _TICK_SIZE) * _TICK_VALUE + est_rt
    if loss_per_lot <= 0 or _MIN_LOT * loss_per_lot > max_risk + 1e-6:
        return None, sl_px, tp_px
    raw = max_risk / loss_per_lot
    steps = math.floor((raw + 1e-12) / _VOL_STEP)
    lot = round(steps * _VOL_STEP, 2)
    while lot >= _MIN_LOT and (loss_per_lot * lot) > max_risk + 1e-6:
        lot = round(lot - _VOL_STEP, 2)
    if lot < _MIN_LOT:
        return None, sl_px, tp_px
    lot = min(lot, _MAX_LOT)
    margin = lot * _MARGIN_PER_LOT
    if margin > 0 and (equity / margin) * 100.0 < 300.0:
        max_aff = round(math.floor(((equity / 3.0) / _MARGIN_PER_LOT) / _VOL_STEP) * _VOL_STEP, 2)
        if max_aff < _MIN_LOT:
            return None, sl_px, tp_px
        lot = min(lot, max_aff)
    if loss_per_lot * lot > max_risk + 1e-6:
        return None, sl_px, tp_px
    return lot, sl_px, tp_px


def _ny_rollover_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 17, 0, 0, tzinfo=_NY_TZ).astimezone(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _rollovers_between(prev_u: datetime, curr_u: datetime) -> list[tuple[datetime, date, int]]:
    p = _as_utc(prev_u)
    c = _as_utc(curr_u)
    if c <= p:
        return []
    d = p.astimezone(_NY_TZ).date() - timedelta(days=1)
    end_d = c.astimezone(_NY_TZ).date() + timedelta(days=1)
    ev = []
    while d <= end_d:
        wd = d.weekday()
        if wd not in (5, 6):
            r = _ny_rollover_utc(d)
            if p < r <= c:
                ev.append((r, d, 3 if wd == 2 else 1))
        d += timedelta(days=1)
    return ev


@dataclass
class IndPos:
    direction: str
    strategy: str
    entry_time: datetime
    entry_bid: float
    entry_ask: float
    entry_price: float
    initial_sl: float
    sl: float
    tp: float
    atr: float
    lot: float
    entry_comm: float
    slip_usd: float
    peak_price: float
    worst_price: float
    last_swap_ts: datetime
    comm_exit: float = 0.0
    comm_total: float = 0.0
    swap_pnl: float = 0.0
    swap_cost: float = 0.0
    trailing_active: bool = False
    exit_reason_flag: str = ""
    bars_held: int = 0
    applied_rollovers: set[date] = field(default_factory=set)
    journey: list[dict] = field(default_factory=list)
    theo_sl: float = 0.0
    last_sl_gap: float = 0.0

    def __post_init__(self):
        self.comm_total = self.entry_comm
        self.theo_sl = self.sl

    def apply_rollovers(self, ts: datetime) -> None:
        evs = _rollovers_between(self.last_swap_ts, ts)
        for _r_utc, ldate, mult in evs:
            if ldate in self.applied_rollovers:
                continue
            pips = _SWAP_LONG_PIPS if self.direction == "BUY" else _SWAP_SHORT_PIPS
            s_pnl = round(pips * (_PIP_SIZE * _CONTRACT_SIZE) * self.lot * mult, 2)
            self.swap_pnl += s_pnl
            self.swap_cost = -self.swap_pnl
            self.applied_rollovers.add(ldate)
        if ts > self.last_swap_ts:
            self.last_swap_ts = ts

    def gross_at(self, px: float) -> float:
        diff = (px - self.entry_price) if self.direction == "BUY" else (self.entry_price - px)
        return diff * self.lot * _CONTRACT_SIZE

    def net_at(self, px: float, include_pending_exit_comm: bool = True) -> float:
        pend = _ind_side_comm(self.lot, px) if (include_pending_exit_comm and self.comm_exit == 0.0) else 0.0
        return self.gross_at(px) - self.comm_total - pend + self.swap_pnl

    def ratchet(self, price: float, max_hold: int) -> None:
        is_bull = self.direction == "BUY"
        if is_bull:
            if price > self.peak_price:
                self.peak_price = price
            gain = self.peak_price - self.entry_price
        else:
            if price < self.peak_price:
                self.peak_price = price
            gain = self.entry_price - self.peak_price

        if gain >= 2.5 * self.atr:
            self.trailing_active = True
            new_sl = (self.peak_price - 0.75 * self.atr) if is_bull else (self.peak_price + 0.75 * self.atr)
            if (is_bull and new_sl > self.sl) or ((not is_bull) and new_sl < self.sl):
                self.sl = new_sl
                self.exit_reason_flag = "TRAILING_SL"
        elif gain >= 1.6 * self.atr:
            self.trailing_active = True
            new_sl = (self.peak_price - 1.10 * self.atr) if is_bull else (self.peak_price + 1.10 * self.atr)
            if (is_bull and new_sl > self.sl) or ((not is_bull) and new_sl < self.sl):
                self.sl = new_sl
                self.exit_reason_flag = "TRAILING_SL"
        elif gain >= 1.0 * self.atr:
            be = 2.0 * _PIP_SIZE
            new_sl = (self.entry_price + be) if is_bull else (self.entry_price - be)
            if (is_bull and new_sl > self.sl) or ((not is_bull) and new_sl < self.sl):
                self.sl = new_sl
                self.exit_reason_flag = "BREAKEVEN"

        hold_pct = self.bars_held / max_hold if max_hold > 0 else 0.0
        if hold_pct >= 0.75:
            ratio = min((hold_pct - 0.75) / 0.25, 1.0)
            if is_bull:
                sq = self.sl + ratio * (price - self.sl)
                if sq > self.sl:
                    self.sl = sq
                    self.exit_reason_flag = "TIME_SQUEEZE"
            else:
                sq = self.sl - ratio * (self.sl - price)
                if sq < self.sl:
                    self.sl = sq
                    self.exit_reason_flag = "TIME_SQUEEZE"


class IndMtmTracker:
    def __init__(self, initial_cap: float):
        self.initial_cap = initial_cap
        self.hwm = initial_cap
        self.max_dd = 0.0
        self.max_dd_pct = 0.0
        self.max_dd_init_pct = 0.0
        self.peak_eq = initial_cap
        self.trough_eq = initial_cap

    def update(self, eq_fill: float) -> None:
        if eq_fill > self.hwm:
            self.hwm = eq_fill
        dd = eq_fill - self.hwm
        dd_pct = abs(dd) / self.hwm * 100.0 if self.hwm > 0 else 0.0
        dd_init = abs(dd) / self.initial_cap * 100.0 if self.initial_cap > 0 else 0.0
        if dd < self.max_dd:
            self.max_dd = dd
            self.peak_eq = self.hwm
            self.trough_eq = eq_fill
        if dd_pct > self.max_dd_pct:
            self.max_dd_pct = dd_pct
        if dd_init > self.max_dd_init_pct:
            self.max_dd_init_pct = dd_init


def _run_independent_window(conn, wid: str, start_dt: datetime, end_dt: datetime) -> dict:
    warmup_start = start_dt - timedelta(days=4)

    def _fetch_ticks(s_dt: datetime, e_dt: datetime) -> pd.DataFrame:
        chunks = []
        cs = s_dt
        while cs < e_dt:
            ce = min(cs + timedelta(days=7), e_dt)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT time, bid, ask
                    FROM market_ticks
                    WHERE symbol = 'XAUUSD' AND time >= %s AND time < %s
                    ORDER BY time ASC
                    """,
                    (cs, ce),
                )
                rows = cur.fetchall()
            if rows:
                chunks.append(pd.DataFrame(rows, columns=["time", "bid", "ask"]))
            cs = ce
        if not chunks:
            return pd.DataFrame(columns=["time", "bid", "ask"])
        df = pd.concat(chunks, ignore_index=True)
        df["time"] = pd.to_datetime(df["time"], utc=True)
        return df

    # Warmup
    kalman = IndKalman2D()
    agg_5m = IndBarAggregator(5, 200)
    agg_1h = IndBarAggregator(60, 100)

    w_df = _fetch_ticks(warmup_start, start_dt)
    if not w_df.empty:
        w_df["minute"] = w_df["time"].dt.floor("min")
        for mts, grp in w_df.groupby("minute", sort=True):
            wb = grp["bid"].values.astype(np.float64)
            wa = grp["ask"].values.astype(np.float64)
            vm = (wb > 0.0) & (wa > 0.0) & (wb < wa)
            vi = np.flatnonzero(vm)
            if len(vi) == 0:
                continue
            for j in vi:
                kalman.update(float(wb[j]))
            vb = wb[vi]
            wc = {"time": mts.to_pydatetime(), "open": float(vb[0]), "high": float(vb.max()), "low": float(vb.min()), "close": float(vb[-1]), "tick_count": int(len(vi))}
            agg_5m.update(wc)
            agg_1h.update(wc)
    del w_df

    # Replay
    open_pos: Optional[IndPos] = None
    trades: list[dict] = []
    equity = 10000.0
    day_start_eq = 10000.0
    mtm = IndMtmTracker(10000.0)
    daily_pnl = 0.0
    daily_trades = 0
    consec_losses = 0
    current_day: Optional[date] = None
    current_regime = "UNKNOWN"
    regime_det = IndRegimeDetector()
    last_cd_bar: dict[tuple[str, str], int] = {}
    bar_idx = -1
    last_valid: Optional[tuple[float, float, datetime]] = None

    def _close_pos(pos: IndPos, reason: str, exit_px: float, exit_ts: datetime) -> None:
        nonlocal equity, daily_pnl, consec_losses
        pos.apply_rollovers(exit_ts)
        c_ex = _ind_side_comm(pos.lot, exit_px)
        pos.comm_exit = c_ex
        pos.comm_total = round(pos.entry_comm + c_ex, 2)
        gross = pos.gross_at(exit_px)
        net = gross - pos.comm_total + pos.swap_pnl
        equity += net
        daily_pnl += net
        consec_losses = 0 if net > 0 else consec_losses + 1
        mtm.update(equity)

        is_bull = pos.direction == "BUY"
        mae_price_diff = (pos.entry_price - pos.worst_price) if is_bull else (pos.worst_price - pos.entry_price)
        mfe_price_diff = (pos.peak_price - pos.entry_price) if is_bull else (pos.entry_price - pos.peak_price)
        trades.append({
            "entry_time": pos.entry_time.isoformat(),
            "exit_time": exit_ts.isoformat(),
            "direction": pos.direction,
            "strategy": pos.strategy,
            "entry_bid": pos.entry_bid,
            "entry_ask": pos.entry_ask,
            "entry_price": pos.entry_price,
            "exit_price": exit_px,
            "sl": pos.initial_sl,
            "final_sl": pos.sl,
            "theoretical_stop_price": pos.theo_sl,
            "tp": pos.tp,
            "qty": pos.lot,
            "gross_pnl": gross,
            "pnl": net,
            "commission_entry_usd": pos.entry_comm,
            "commission_exit_usd": pos.comm_exit,
            "commission_usd": pos.comm_total,
            "slippage_usd": pos.slip_usd,
            "swap_usd": pos.swap_cost,
            "swap_pnl_usd": pos.swap_pnl,
            "exit_reason": reason,
            "atr": pos.atr,
            "mae_usd": round(max(0.0, mae_price_diff) * pos.lot * _CONTRACT_SIZE, 2),
            "mfe_usd": round(max(0.0, mfe_price_diff) * pos.lot * _CONTRACT_SIZE, 2),
            "mae_atr": round(max(0.0, mae_price_diff) / max(pos.atr, 1e-9), 4),
            "mfe_atr": round(max(0.0, mfe_price_diff) / max(pos.atr, 1e-9), 4),
            "journey": pos.journey,
        })

    end_ex = end_dt + timedelta(microseconds=1)
    cs = start_dt
    while cs < end_ex:
        ce = min(cs + timedelta(days=7), end_ex)
        t_df = _fetch_ticks(cs, ce)
        cs = ce
        if t_df.empty:
            continue
        t_df["minute"] = t_df["time"].dt.floor("min")
        for mts, grp in t_df.groupby("minute", sort=True):
            bar_idx += 1
            m_py = mts.to_pydatetime()
            if current_day != m_py.date():
                current_day = m_py.date()
                daily_pnl = 0.0
                daily_trades = 0
                consec_losses = 0
                day_start_eq = max(equity, 1.0)

            bids = grp["bid"].values.astype(np.float64)
            asks = grp["ask"].values.astype(np.float64)
            ttimes = [pd.Timestamp(x).to_pydatetime() for x in grp["time"].values]
            vm = (bids > 0.0) & (asks > 0.0) & (bids < asks)
            vi = np.flatnonzero(vm)
            if len(vi) > 0:
                lv = int(vi[-1])
                last_valid = (float(bids[lv]), float(asks[lv]), ttimes[lv])

            closed_5m = False
            df_ind = pd.DataFrame()
            if len(vi) > 0:
                vb = bids[vi]
                candle = {"time": m_py, "open": float(vb[0]), "high": float(vb.max()), "low": float(vb.min()), "close": float(vb[-1]), "tick_count": int(len(vi))}
                closed_5m = agg_5m.update(candle)
                agg_1h.update(candle)
                if closed_5m and len(agg_5m.bars) >= 60:
                    df_ind = _ind_compute_indicators(list(agg_5m.bars))
                    if not df_ind.empty:
                        current_regime = regime_det.detect(df_ind)

            def _scan_exit(pos: IndPos, start_i: int, eval_to: bool) -> tuple[bool, str, float, int]:
                is_bull = pos.direction == "BUY"
                e_hr = pos.entry_time.hour
                eff_mh = 60 if 7 <= e_hr < 20 else 90
                best_q = None
                last_q = None
                for i in range(start_i, len(bids)):
                    b = float(bids[i])
                    a = float(asks[i])
                    ts_i = ttimes[i]
                    if b <= 0.0 or a <= 0.0 or b >= a:
                        continue
                    kalman.update(b)
                    pos.apply_rollovers(ts_i)
                    eq_q = b if is_bull else a
                    last_q = eq_q
                    if is_bull and eq_q < pos.worst_price:
                        pos.worst_price = eq_q
                    elif (not is_bull) and eq_q > pos.worst_price:
                        pos.worst_price = eq_q
                    if best_q is None or (is_bull and eq_q > best_q) or (not is_bull and eq_q < best_q):
                        best_q = eq_q

                    if is_bull and b <= pos.sl:
                        rsn = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
                        fpx = b - _SLIP_OFFSET
                        pos.slip_usd += _SLIP_OFFSET * pos.lot * _CONTRACT_SIZE
                        pos.theo_sl = float(pos.sl)
                        pos.last_sl_gap = pos.net_at(fpx) - pos.net_at(pos.theo_sl)
                        mtm.update(equity + pos.net_at(fpx))
                        return True, rsn, fpx, i
                    if (not is_bull) and a >= pos.sl:
                        rsn = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
                        fpx = a + _SLIP_OFFSET
                        pos.slip_usd += _SLIP_OFFSET * pos.lot * _CONTRACT_SIZE
                        pos.theo_sl = float(pos.sl)
                        pos.last_sl_gap = pos.net_at(fpx) - pos.net_at(pos.theo_sl)
                        mtm.update(equity + pos.net_at(fpx))
                        return True, rsn, fpx, i
                    if is_bull and b >= pos.tp:
                        fpx = float(pos.tp)
                        mtm.update(equity + pos.net_at(fpx))
                        return True, "TARGET", fpx, i
                    if (not is_bull) and a <= pos.tp:
                        fpx = float(pos.tp)
                        mtm.update(equity + pos.net_at(fpx))
                        return True, "TARGET", fpx, i
                    mtm.update(equity + pos.net_at(eq_q))
                    pos.ratchet(eq_q, eff_mh)

                if eval_to and pos.bars_held >= eff_mh:
                    bpx = last_q if last_q is not None else pos.entry_price
                    epx = (bpx - _SLIP_OFFSET) if is_bull else (bpx + _SLIP_OFFSET)
                    if last_q is not None:
                        pos.slip_usd += _SLIP_OFFSET * pos.lot * _CONTRACT_SIZE
                    return True, "TIMEOUT", epx, len(bids) - 1

                if best_q is not None:
                    pos.ratchet(best_q, eff_mh)
                return False, "", 0.0, len(bids) - 1

            if open_pos is not None:
                open_pos.bars_held += 1
                exited, rsn, epx, lidx = _scan_exit(open_pos, 0, True)
                cur_px = epx if exited else float(bids[-1])
                open_pos.journey.append({
                    "time": m_py.strftime("%Y-%m-%d %H:%M:%S"),
                    "price": round(cur_px, 2),
                    "sl": round(open_pos.sl, 2),
                    "bars_held": open_pos.bars_held,
                })
                if exited:
                    for j in range(lidx + 1, len(bids)):
                        if vm[j]:
                            kalman.update(float(bids[j]))
                    _close_pos(open_pos, rsn, epx, ttimes[lidx])
                    open_pos = None
                continue

            if len(vi) == 0:
                continue
            en_i = int(vi[0])
            for j in range(en_i + 1):
                if vm[j]:
                    kalman.update(float(bids[j]))
            k_until = en_i + 1

            def _flush_k():
                for j in range(k_until, len(bids)):
                    if vm[j]:
                        kalman.update(float(bids[j]))

            if not closed_5m or len(agg_5m.bars) < 60 or df_ind.empty:
                _flush_k()
                continue
            if abs(min(0.0, daily_pnl)) / max(day_start_eq, 1.0) * 100.0 >= 3.0 or daily_trades >= 8 or consec_losses >= 3:
                _flush_k()
                continue
            htf = _ind_htf_trend([b["close"] for b in agg_1h.bars])
            if htf == 0:
                _flush_k()
                continue
            sigs = _ind_generate_signals(df_ind, current_regime)
            if not sigs:
                _flush_k()
                continue

            lr = df_ind.iloc[-1]
            cs_s = df_ind["close"]
            c5_ts = agg_5m.bars[-1]["time"]
            feat = {
                "kalman_vel": kalman.x1,
                "kalman_accel": kalman.accel(),
                "shannon_entropy": _ind_shannon_entropy(cs_s.pct_change().dropna()) if len(cs_s) > 30 else 2.0,
                "kaufman_er": _ind_kaufman_er(cs_s) if len(cs_s) > 15 else 0.3,
                "ema20_slope": float(lr["ema20_slope"]) if pd.notna(lr.get("ema20_slope")) else 0.0,
                "tick_weighted_price_dist": float(lr["tick_weighted_price_dist"]) if pd.notna(lr.get("tick_weighted_price_dist")) else 0.0,
                "tick_rate_ratio": float(lr["tick_rate_ratio"]) if pd.notna(lr.get("tick_rate_ratio")) else 1.0,
                "utc_hour": c5_ts.hour,
            }
            scored = [( _ind_score(d, current_regime, feat), st, d) for st, d in sigs]
            scored = [x for x in scored if x[0] >= 0.60]
            scored.sort(key=lambda x: x[0], reverse=True)
            if not scored:
                _flush_k()
                continue
            sc, st, d = scored[0]
            if (d == "BUY" and htf != 1) or (d == "SELL" and htf != -1):
                _flush_k()
                continue
            if current_regime == "HIGH_VOLATILITY" or (d == "BUY" and current_regime == "SIDEWAYS"):
                _flush_k()
                continue
            if bar_idx - last_cd_bar.get((st, d), -9999) < 15:
                _flush_k()
                continue

            atr_v = float(df_ind["atr"].iloc[-1]) if pd.notna(df_ind["atr"].iloc[-1]) else 1.0
            eb, ea = float(bids[en_i]), float(asks[en_i])
            raw_ep = ea if d == "BUY" else eb
            ep = (raw_ep + _SLIP_OFFSET) if d == "BUY" else (raw_ep - _SLIP_OFFSET)
            lot, sl_px, tp_px = _ind_size(equity, ep, atr_v, d)
            if lot is None:
                _flush_k()
                continue

            e_comm = _ind_side_comm(lot, ep)
            e_slip = _SLIP_OFFSET * lot * _CONTRACT_SIZE
            open_pos = IndPos(
                direction=d, strategy=st, entry_time=ttimes[en_i],
                entry_bid=eb, entry_ask=ea, entry_price=ep,
                initial_sl=sl_px, sl=sl_px, tp=tp_px, atr=atr_v, lot=lot,
                entry_comm=e_comm, slip_usd=e_slip,
                peak_price=ep, worst_price=ep, last_swap_ts=ttimes[en_i],
            )
            mtm.update(equity - (ea - eb) * lot * _CONTRACT_SIZE - 2.0 * e_comm - e_slip)
            last_cd_bar[(st, d)] = bar_idx
            daily_trades += 1

            if en_i + 1 < len(bids):
                exited, rsn, epx, lidx = _scan_exit(open_pos, en_i + 1, False)
                if exited:
                    open_pos.journey.append({"time": m_py.strftime("%Y-%m-%d %H:%M:%S"), "price": round(epx, 2), "sl": round(open_pos.sl, 2), "bars_held": 0})
                    for j in range(lidx + 1, len(bids)):
                        if vm[j]:
                            kalman.update(float(bids[j]))
                    _close_pos(open_pos, rsn, epx, ttimes[lidx])
                    open_pos = None

    if open_pos is not None and last_valid is not None:
        lb, la, lt = last_valid
        rc = lb if open_pos.direction == "BUY" else la
        epx = (rc - _SLIP_OFFSET) if open_pos.direction == "BUY" else (rc + _SLIP_OFFSET)
        open_pos.slip_usd += _SLIP_OFFSET * open_pos.lot * _CONTRACT_SIZE
        _close_pos(open_pos, "END_OF_BACKTEST", epx, lt)
        open_pos = None

    # Closed DD
    ceq = 10000.0
    chwm = 10000.0
    c_dd = 0.0
    c_dd_pct = 0.0
    for tr in trades:
        ceq += tr["pnl"]
        if ceq > chwm:
            chwm = ceq
        dd = ceq - chwm
        if dd < c_dd:
            c_dd = dd
        pct = abs(dd) / chwm * 100.0 if chwm > 0 else 0.0
        if pct > c_dd_pct:
            c_dd_pct = pct

    return {
        "window_id": wid,
        "trades": trades,
        "closed_max_dd_usd": round(c_dd, 4),
        "closed_max_dd_pct": round(c_dd_pct, 4),
        "mtm_max_dd_usd": round(mtm.max_dd, 4),
        "mtm_max_dd_pct": round(mtm.max_dd_pct, 4),
        "mtm_max_dd_initial_pct": round(mtm.max_dd_init_pct, 4),
        "final_equity": round(equity, 4),
    }


def _reconcile_window(wid: str, ind_res: dict) -> dict:
    app_df = pd.read_csv(AUDIT_DIR / f"task6c_app_{wid.lower()}_trades.csv")
    app_j = json.loads((AUDIT_DIR / f"task6c_app_{wid.lower()}_journeys.json").read_text(encoding="utf-8"))
    ind_trades = ind_res["trades"]

    rows = []
    mismatches = 0
    trail_step_mismatches = 0
    max_pnl_diff = 0.0
    n = max(len(app_df), len(ind_trades))
    for idx in range(n):
        a = app_df.iloc[idx].to_dict() if idx < len(app_df) else None
        it = ind_trades[idx] if idx < len(ind_trades) else None
        aj = app_j.get(str(idx), []) if a is not None else []
        ij = it["journey"] if it is not None else []

        if a is None or it is None:
            mismatches += 1
            rows.append({"trade_idx": idx + 1, "status": "COUNT_MISMATCH"})
            continue

        a_et = pd.to_datetime(a["entry_time"], utc=True)
        i_et = pd.to_datetime(it["entry_time"], utc=True)
        a_xt = pd.to_datetime(a["exit_time"], utc=True)
        i_xt = pd.to_datetime(it["exit_time"], utc=True)
        t_ok = (
            a_et == i_et
            and a_xt == i_xt
            and str(a["direction"]) == it["direction"]
            and str(a["strategy"]) == it["strategy"]
            and str(a["exit_reason"]) == it["exit_reason"]
            and abs(float(a["qty"]) - it["qty"]) <= 1e-6
            and abs(float(a["entry_price"]) - it["entry_price"]) <= 1e-4
            and abs(float(a["exit_price"]) - it["exit_price"]) <= 1e-4
            and abs(float(a["sl"]) - it["sl"]) <= 1e-4
            and abs(float(a["tp"]) - it["tp"]) <= 1e-4
            and abs(float(a["commission_usd"]) - it["commission_usd"]) <= 0.015
            and abs(float(a["slippage_usd"]) - it["slippage_usd"]) <= 0.015
            and abs(float(a["swap_usd"]) - it["swap_usd"]) <= 0.015
            and abs(float(a["gross_pnl"]) - it["gross_pnl"]) <= 0.02
            and abs(float(a["pnl"]) - it["pnl"]) <= 0.02
        )
        # Reconcile EVERY trailing-stop update in journeys!
        j_diff = len(aj) != len(ij)
        if not j_diff:
            for s_a, s_i in zip(aj, ij):
                if s_a["time"] != s_i["time"] or abs(float(s_a["sl"]) - float(s_i["sl"])) > 0.02 or int(s_a["bars_held"]) != int(s_i["bars_held"]):
                    j_diff = True
                    trail_step_mismatches += 1
                    break
        else:
            trail_step_mismatches += 1

        if not t_ok or j_diff:
            mismatches += 1
            if mismatches <= 5:
                print(
                    f"  [MISMATCH {wid} #{idx+1}] app=({a_et}, {a_xt}, {a['direction']}, {a['strategy']}, {a['exit_reason']}, px={a['entry_price']}->{a['exit_price']}, qty={a['qty']}, pnl={a['pnl']:.4f}, j={len(aj)}) "
                    f"vs ind=({i_et}, {i_xt}, {it['direction']}, {it['strategy']}, {it['exit_reason']}, px={it['entry_price']}->{it['exit_price']}, qty={it['qty']}, pnl={it['pnl']:.4f}, j={len(ij)})",
                    flush=True,
                )
        pdiff = abs(float(a["pnl"]) - it["pnl"])
        max_pnl_diff = max(max_pnl_diff, pdiff)
        rows.append({
            "trade_idx": idx + 1,
            "entry_time": it["entry_time"],
            "exit_time": it["exit_time"],
            "direction": it["direction"],
            "strategy": it["strategy"],
            "entry_bid": it["entry_bid"],
            "entry_ask": it["entry_ask"],
            "app_entry_px": float(a["entry_price"]),
            "ind_entry_px": it["entry_price"],
            "app_exit_px": float(a["exit_price"]),
            "ind_exit_px": it["exit_price"],
            "qty": it["qty"],
            "app_reason": a["exit_reason"],
            "ind_reason": it["exit_reason"],
            "app_comm": float(a["commission_usd"]),
            "ind_comm": it["commission_usd"],
            "app_slip": float(a["slippage_usd"]),
            "ind_slip": it["slippage_usd"],
            "app_swap": float(a["swap_usd"]),
            "ind_swap": it["swap_usd"],
            "app_pnl": round(float(a["pnl"]), 4),
            "ind_pnl": round(it["pnl"], 4),
            "pnl_diff": round(pdiff, 6),
            "journey_steps": len(ij),
            "journey_exact_match": not j_diff,
            "mae_usd": it["mae_usd"],
            "mfe_usd": it["mfe_usd"],
            "mae_atr": it["mae_atr"],
            "mfe_atr": it["mfe_atr"],
            "status": "MATCH" if (t_ok and not j_diff) else "MISMATCH",
        })

    pd.DataFrame(rows).to_csv(AUDIT_DIR / f"task6c_reconciliation_{wid.lower()}.csv", index=False)
    app_summary = json.loads((AUDIT_DIR / "task6c_app_summary.json").read_text(encoding="utf-8"))[wid]
    return {
        "window_id": wid,
        "app_trades": int(len(app_df)),
        "ind_trades": int(len(ind_trades)),
        "mismatch_count": mismatches,
        "trailing_journey_mismatches": trail_step_mismatches,
        "max_pnl_diff_usd": round(max_pnl_diff, 6),
        "app_final_equity": app_summary["final_equity_usd"],
        "ind_final_equity": ind_res["final_equity"],
        "app_closed_dd_usd": app_summary["closed_max_dd_usd"],
        "ind_closed_dd_usd": ind_res["closed_max_dd_usd"],
        "app_mtm_dd_usd": app_summary["mtm_max_dd_usd"],
        "ind_mtm_dd_usd": ind_res["mtm_max_dd_usd"],
    }


def main():
    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    conn = psycopg2.connect(db_url)
    try:
        windows = [
            ("W1", datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc)),
            ("W2", datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc)),
            ("W3", datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 18, 18, 0, 0, tzinfo=timezone.utc)),
        ]
        summary = {}
        perf_rows = []
        app_sum_all = json.loads((AUDIT_DIR / "task6c_app_summary.json").read_text(encoding="utf-8"))

        for wid, s_dt, e_dt in windows:
            print(f"[TASK 6C IND] Running independent raw-tick validator for {wid}...", flush=True)
            ind_res = _run_independent_window(conn, wid, s_dt, e_dt)
            rec = _reconcile_window(wid, ind_res)
            summary[wid] = rec

            # Build rich Performance & Risk metrics table
            ind_t = ind_res["trades"]
            df_i = pd.DataFrame(ind_t)
            buy_df = df_i[df_i["direction"] == "BUY"]
            sell_df = df_i[df_i["direction"] == "SELL"]
            a_sum = app_sum_all[wid]
            perf_rows.append({
                "window_id": wid,
                "trades": a_sum["trades"],
                "wins": a_sum["wins"],
                "losses": a_sum["losses"],
                "breakevens": a_sum["breakevens"],
                "win_rate_pct": a_sum["win_rate_pct"],
                "gross_pnl_usd": a_sum["gross_pnl_usd"],
                "commission_usd": a_sum["commission_usd"],
                "slippage_usd": a_sum["slippage_usd"],
                "swap_usd": a_sum["swap_usd"],
                "net_pnl_usd": a_sum["net_pnl_usd"],
                "final_equity_usd": a_sum["final_equity_usd"],
                "profit_factor": a_sum["profit_factor"],
                "closed_max_dd_usd": a_sum["closed_max_dd_usd"],
                "closed_max_dd_pct": a_sum["closed_max_dd_pct"],
                "mtm_max_dd_usd": a_sum["mtm_max_dd_usd"],
                "mtm_max_dd_pct": a_sum["mtm_max_dd_pct"],
                "mtm_max_dd_initial_pct": a_sum["mtm_max_dd_initial_pct"],
                "buy_trades": a_sum["buy_trades"],
                "buy_wins": int((buy_df["pnl"].round(2) > 0).sum()),
                "buy_net_pnl_usd": a_sum["buy_net_pnl_usd"],
                "sell_trades": a_sum["sell_trades"],
                "sell_wins": int((sell_df["pnl"].round(2) > 0).sum()),
                "sell_net_pnl_usd": a_sum["sell_net_pnl_usd"],
                "max_consecutive_losses": a_sum["max_consecutive_losses"],
                "worst_daily_pnl_usd": a_sum["worst_daily_pnl_usd"],
                "best_daily_pnl_usd": a_sum["best_daily_pnl_usd"],
                "avg_mae_usd": round(float(df_i["mae_usd"].mean()), 2),
                "max_mae_usd": round(float(df_i["mae_usd"].max()), 2),
                "avg_mfe_usd": round(float(df_i["mfe_usd"].mean()), 2),
                "max_mfe_usd": round(float(df_i["mfe_usd"].max()), 2),
                "avg_mae_atr": round(float(df_i["mae_atr"].mean()), 4),
                "avg_mfe_atr": round(float(df_i["mfe_atr"].mean()), 4),
            })

        pd.DataFrame(perf_rows).to_csv(AUDIT_DIR / "task6c_performance_and_risk.csv", index=False)
        (AUDIT_DIR / "task6c_validation_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print("\n[TASK 6C RECONCILIATION SUMMARY]")
        print(json.dumps(summary, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
