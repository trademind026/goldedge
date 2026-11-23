"""
Task 13 — Entry Signal Quality Audit (research only).

Zero imports from `src/goldedge/`. Reads raw bid/ask ticks from PostgreSQL `market_ticks`.

Two-pass design:
  Pass 1 (screening): H0, H4a, application-faithful "best-signal-only" variants (H0_APP/H4a_APP),
                      13 isolated entry-quality hypothesis families x 2 bases (H0 / H4a),
                      parameter neighbours (W3 only) -> classification + pre-registered retention rule.
  Pass 2 (verification): retained candidates + controls, HWM-8% variants, 1.5x cost-stress variants,
                      plus a second, independently-implemented stream (t6c classes) for trade-by-trade reconciliation.

Outputs go to backtest_results/audit/task13_entry_signal_quality/.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd
import psycopg2
from scipy import stats

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import task6c_independent_validator as t6c
import task11_regime_reconstruction_and_diagnosis as t11p1
import task11a_independent_candidate_reconciler as t11a
import task12_session_analysis_and_diagnosis as t12p1
import task12_hypotheses_hwm_and_reconciliation as t12p2

ROOT = t12p1.ROOT
AUDIT_DIR = t12p1.AUDIT_DIR
FROZEN_V2_DIR = AUDIT_DIR / "frozen_v2_recovered"
OUT_DIR = AUDIT_DIR / "task13_entry_signal_quality"
OUT_DIR.mkdir(parents=True, exist_ok=True)

WINDOWS = [
    ("W1", datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc)),
    ("W2", datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc)),
    ("W3", datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 18, 18, 9, 59, tzinfo=timezone.utc)),
]

H0_NAME = "H0_BASELINE_V2"
H4A_NAME = "H4a_SCORE_CAP_080"
H0_APP = "H0_APP_STRICT"
H4A_APP = "H4a_APP_STRICT"


def _jdefault(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if (math.isnan(float(o)) or math.isinf(float(o))) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


def _dump(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, default=_jdefault), encoding="utf-8")


# =============================================================================
# Config
# =============================================================================

@dataclass
class EConfig(t12p2.SConfig):
    family: str = ""
    min_score: float = 0.60
    buy_max_score: float = 1.01
    app_strict: bool = False
    pred: Optional[Callable[[dict, str], bool]] = None
    param: Optional[float] = None
    ref_name: str = ""  # unprotected reference for HWM / cost variants


def _sgn(d: str) -> float:
    return 1.0 if d == "BUY" else -1.0


def make_pred(kind: str, v: float) -> Optional[Callable[[dict, str], bool]]:
    if kind == "ER_CAP":
        return lambda fx, d: fx["er"] < v
    if kind == "EXT_CAP":
        return lambda fx, d: _sgn(d) * fx["ext20"] <= v
    if kind == "ADX_MIN":
        return lambda fx, d: fx["adx"] >= v
    if kind == "ACCEL_ALIGN":
        return lambda fx, d: _sgn(d) * fx["kacc"] >= v
    if kind == "KV_CAP":
        return lambda fx, d: abs(fx["kv"]) <= v
    if kind == "LATE_CAP":
        return lambda fx, d: _sgn(d) * fx["ret6"] <= v
    if kind == "CONFIRM":
        def _p(fx: dict, d: str) -> bool:
            cp = fx["close_pos"] if d == "BUY" else 1.0 - fx["close_pos"]
            return _sgn(d) * fx["bar_dir"] > 0 and cp >= v
        return _p
    if kind == "ATR_EXP_CAP":
        return lambda fx, d: fx["atr_ratio"] <= v
    if kind == "SPREAD_CAP":
        return lambda fx, d: fx["spread"] <= v
    if kind == "BUY_EXT_CAP":
        return lambda fx, d: d != "BUY" or fx["ext20"] <= v
    if kind == "SELL_LOW_DIST":
        return lambda fx, d: d != "SELL" or fx["dist_lo20"] >= v
    return None  # MIN_SCORE / BUY_SCORE_CAP handled by config fields


# code, kind, main value, neighbours, description (pre-registered BEFORE any result was inspected)
FAMILIES: list[tuple[str, str, float, tuple[float, float], str]] = [
    ("E01", "ER_CAP", 0.75, (0.65, 0.85), "Block Kaufman ER >= v (efficiency saturation / exhaustion)"),
    ("E02", "EXT_CAP", 2.0, (1.5, 2.5), "Block direction-aligned (close-EMA20)/ATR > v (EMA overextension)"),
    ("E03", "ADX_MIN", 20.0, (15.0, 25.0), "Require Wilder ADX14 >= v (trend-strength confirmation)"),
    ("E04", "ACCEL_ALIGN", 0.0, (-0.005, 0.005), "Require direction-aligned Kalman acceleration >= v"),
    ("E05", "KV_CAP", 0.06, (0.045, 0.08), "Block |Kalman velocity| > v (momentum exhaustion)"),
    ("E06", "LATE_CAP", 2.5, (2.0, 3.0), "Block direction-aligned 30m (6x5m) move > v ATR (late trend entry)"),
    ("E07", "CONFIRM", 0.5, (0.4, 0.6), "Require signal bar closed with direction and in top/bottom v of its range"),
    ("E08", "ATR_EXP_CAP", 1.3, (1.2, 1.5), "Block ATR / mean(ATR,20) > v (volatility expansion)"),
    ("E09", "SPREAD_CAP", 0.12, (0.10, 0.15), "Block entry-tick spread > v USD"),
    ("E10", "BUY_EXT_CAP", 1.5, (1.0, 2.0), "BUY only: block (close-EMA20)/ATR > v"),
    ("E11", "SELL_LOW_DIST", 0.5, (0.3, 0.8), "SELL only: block when close within v ATR of 20-bar low (chasing lows)"),
    ("E12", "MIN_SCORE", 0.65, (0.62, 0.70), "Raise minimum score floor to v (calibration)"),
    ("E13", "BUY_SCORE_CAP", 0.70, (0.65, 0.75), "BUY only: score cap < v"),
]


def _make_cfg(name: str, group: str, base: str, kind: str, v: float, desc: str, family: str) -> EConfig:
    max_score = 0.80 if base == "H4a" else 1.01
    c = EConfig(name=name, group=group, base_variant=base, rule_desc=f"{desc} [v={v}] on {base}", max_score=max_score, family=family, param=v)
    if kind == "MIN_SCORE":
        c.min_score = v
    elif kind == "BUY_SCORE_CAP":
        c.buy_max_score = min(v, max_score)
    else:
        c.pred = make_pred(kind, v)
    return c


def build_configs(window: str) -> list[EConfig]:
    cfgs: list[EConfig] = [
        EConfig(H0_NAME, "control", "H0", "H0 baseline (score >= 0.60)"),
        EConfig(H4A_NAME, "control", "H4a", "H4a (0.60 <= score < 0.80)", max_score=0.80),
        EConfig(H0_APP, "app_semantics", "H0", "H0 with application best-signal-only gating", app_strict=True),
        EConfig(H4A_APP, "app_semantics", "H4a", "H4a with application best-signal-only gating", max_score=0.80, app_strict=True),
    ]
    for code, kind, v, nb, desc in FAMILIES:
        for base in ("H0", "H4a"):
            cfgs.append(_make_cfg(f"{code}_{base}", "hypothesis", base, kind, v, desc, code))
            if window == "W3":
                for i, nv in enumerate(nb):
                    cfgs.append(_make_cfg(f"{code}n{i + 1}_{base}", "neighbour", base, kind, nv, desc, code))
    return cfgs


# =============================================================================
# Features
# =============================================================================

def score_components(direction: str, regime: str, feat: dict) -> dict[str, float]:
    kv, ent, er, slp = feat["kalman_vel"], feat["shannon_entropy"], feat["kaufman_er"], feat["ema20_slope"]
    twd, h = feat["tick_weighted_price_dist"], feat["utc_hour"]
    sw = "SIDEWAYS" in regime.upper()
    if direction == "BUY":
        mom = t6c._score_band(kv, 0.008, 0.040, 0.002, 0.080) if sw else t6c._score_band(kv, 0.015, 0.060, 0.005, 0.120)
        slp_s = t6c._interp(slp, 0.00005, 0.00030)
        twd_s = t6c._score_band(twd, -0.0005, 0.003, -0.003, 0.006)
    else:
        mom = t6c._score_band(kv, -0.040, -0.008, -0.080, -0.002) if sw else t6c._score_band(kv, -0.060, -0.015, -0.120, -0.005)
        slp_s = t6c._interp(slp, -0.00005, -0.00030)
        twd_s = t6c._score_band(twd, -0.003, 0.0005, -0.006, 0.003)
    er_s = t6c._score_band(er, 0.20, 0.60, 0.08, 0.90)
    ent_s = t6c._score_band(ent, 2.50, 3.20, None, 3.60)
    sess = t6c._sess_score(h)
    raw = 0.35 * mom + 0.20 * er_s + 0.15 * ent_s + 0.15 * slp_s + 0.10 * twd_s + 0.05 * sess
    qs = min(1.0, max(0.0, raw))
    ka = feat["kalman_accel"]
    adj = max(-0.08, min(0.08, ka * 2.0 if direction == "BUY" else -ka * 2.0))
    qs = min(1.0, max(0.0, qs + adj))
    trr = feat["tick_rate_ratio"]
    mult = 1.0
    if trr < 0.70:
        mult = 0.75
    elif sw and trr < 1.10:
        mult = 0.85
    final = round(qs * mult, 4)
    return {"c_mom": mom, "c_er": er_s, "c_ent": ent_s, "c_slope": slp_s, "c_twd": twd_s, "c_sess": sess,
            "c_raw": raw, "c_accel_adj": adj, "c_trr_mult": mult, "c_final": final}


def compute_fx(df: pd.DataFrame, feat: dict, atr_v: float, regime: str, adx: float, eb: float, ea: float) -> dict[str, Any]:
    last = df.iloc[-1]
    atr = max(float(atr_v), 1e-9)
    close = float(last["close"])
    op = float(last["open"])
    hi = float(last["high"])
    lo = float(last["low"])
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])
    c = df["close"].values
    ret6 = (close - float(c[-7])) / atr if len(c) >= 7 else 0.0
    ret3 = (close - float(c[-4])) / atr if len(c) >= 4 else 0.0
    atr_tail = df["atr"].dropna().tail(20)
    atr_ratio = atr / float(atr_tail.mean()) if len(atr_tail) and float(atr_tail.mean()) > 1e-9 else 1.0
    rng = hi - lo
    close_pos = (close - lo) / rng if rng > 1e-9 else 0.5
    hi20 = float(df["high"].iloc[-21:-1].max())
    lo20 = float(df["low"].iloc[-21:-1].min())
    return {
        "kv": float(feat["kalman_vel"]), "kacc": float(feat["kalman_accel"]), "er": float(feat["kaufman_er"]),
        "ent": float(feat["shannon_entropy"]), "slope": float(feat["ema20_slope"]),
        "twd": float(feat["tick_weighted_price_dist"]), "trr": float(feat["tick_rate_ratio"]),
        "rsi": float(last["rsi"]) if pd.notna(last.get("rsi")) else 50.0,
        "atr": atr, "adx": float(adx), "spread": float(ea - eb), "regime": regime,
        "ext20": (close - ema20) / atr, "ext50": (close - ema50) / atr,
        "ret6": ret6, "ret3": ret3, "atr_ratio": atr_ratio,
        "bar_range_atr": rng / atr, "close_pos": close_pos,
        "bar_dir": 1.0 if close > op else (-1.0 if close < op else 0.0),
        "dist_hi20": (hi20 - close) / atr, "dist_lo20": (close - lo20) / atr,
        "utc_hour": int(feat["utc_hour"]),
    }


# =============================================================================
# Portfolio with causal excursion tracking
# =============================================================================

def _t13_update(pos: t11a.CleanPos, price: float, ts: datetime) -> None:
    st = pos.t13  # type: ignore[attr-defined]
    s = _sgn(pos.direction)
    exc = s * (price - pos.entry_price) / max(pos.atr, 1e-9)
    mins = (t11a._to_utc(ts) - t11a._to_utc(pos.entry_time)).total_seconds() / 60.0
    if exc > st["mfe"]:
        st["mfe"] = exc
        st["t_mfe"] = mins
    if exc < st["mae"]:
        st["mae"] = exc
        st["t_mae"] = mins
    for thr, key in ((0.5, "t_fav_05"), (1.0, "t_fav_10")):
        if exc >= thr and st[key] is None:
            st[key] = mins
    for thr, key in ((0.5, "t_adv_05"), (1.0, "t_adv_10")):
        if exc <= -thr and st[key] is None:
            st[key] = mins


class T13Portfolio(t12p2.SessionPortfolio):
    def _ratchet(self, pos: t11a.CleanPos, price: float, max_hold: int, ts_i: datetime) -> None:
        if hasattr(pos, "t13"):
            _t13_update(pos, price, ts_i)
        super()._ratchet(pos, price, max_hold, ts_i)

    def close_pos(self, pos: t11a.CleanPos, rsn: str, epx: float, ex_ts: datetime) -> None:
        st = getattr(pos, "t13", None)
        if st is not None:
            _t13_update(pos, epx, ex_ts)
        super().close_pos(pos, rsn, epx, ex_ts)
        if st is not None:
            t = self.trades[-1]
            t["score"] = st["feat"].get("score")
            t["strategy"] = pos.strategy
            for k, v in st["feat"].items():
                t[f"f_{k}"] = v
            t["mfe_atr"] = round(st["mfe"], 4)
            t["mae_atr"] = round(st["mae"], 4)
            t["t_mfe_min"] = st["t_mfe"]
            t["t_mae_min"] = st["t_mae"]
            t["t_fav_05_min"] = st["t_fav_05"]
            t["t_fav_10_min"] = st["t_fav_10"]
            t["t_adv_05_min"] = st["t_adv_05"]
            t["t_adv_10_min"] = st["t_adv_10"]
            t["hold_min"] = (t11a._to_utc(ex_ts) - t11a._to_utc(pos.entry_time)).total_seconds() / 60.0
            t["profit_then_loss"] = bool(st["mfe"] >= 1.0 and t["net_pnl"] <= 0)
            t["spread_usd"] = pos.entry_ask - pos.entry_bid


# =============================================================================
# Stream (one independent implementation of bars/regime/kalman/sizing)
# =============================================================================

class Stream:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        if kind == "A":
            self.k = t11a.CleanKalman2D()
            self.a5 = t11a.CleanBarAgg(5, 200)
            self.a1 = t11a.CleanBarAgg(60, 100)
            self.rd = t11a.CleanRegimeDetector()
            self.size = t11a._clean_size
        else:
            self.k = t6c.IndKalman2D()
            self.a5 = t6c.IndBarAggregator(5, 200)
            self.a1 = t6c.IndBarAggregator(60, 100)
            self.rd = t6c.IndRegimeDetector()
            self.size = t6c._ind_size
        self.regime = "UNKNOWN"
        self.closed = False
        self.df = pd.DataFrame()
        self.pfs: list[T13Portfolio] = []
        self.busy: list[bool] = []
        self.kx1 = 0.0
        self.kacc = 0.0


def _choose(c: EConfig, pf: T13Portfolio, scored: list, htf: int, regime: str, fx: dict, bar_idx: int,
            sp_usd: float, en_ts: datetime, sinfo: dict) -> tuple[Optional[tuple], str]:
    if c.app_strict:
        cand = [t for t in scored if c.min_score <= t[0] < c.max_score]
        if not cand:
            return None, "BELOW_MIN_OR_ABOVE_CAP"
        sc, st, d = cand[0]
        if (d == "BUY" and htf != 1) or (d == "SELL" and htf != -1):
            return None, "H1_GATE_BEST"
        if regime == "HIGH_VOLATILITY":
            return None, "REGIME_HIGH_VOL"
        if d == "BUY" and regime == "SIDEWAYS":
            return None, "REGIME_BUY_SIDEWAYS"
        if bar_idx - pf.last_cd_bar.get((st, d), -9999) < 15:
            return None, "COOLDOWN"
        return (sc, st, d), "ACCEPTED"
    if regime == "HIGH_VOLATILITY":
        return None, "REGIME_HIGH_VOL"
    saw_in_band = False
    for sc, st, d in scored:
        if not (c.min_score <= sc < c.max_score):
            continue
        saw_in_band = True
        if d == "BUY" and sc >= c.buy_max_score:
            continue
        if (d == "BUY" and htf != 1) or (d == "SELL" and htf != -1):
            continue
        if d == "BUY" and regime == "SIDEWAYS":
            continue
        if not t12p2._passes_session_filter(c, d, en_ts, sp_usd, sinfo):
            continue
        if c.pred is not None and not c.pred(fx, d):
            continue
        if bar_idx - pf.last_cd_bar.get((st, d), -9999) < 15:
            continue
        return (sc, st, d), "ACCEPTED"
    return None, ("FILTERED_OR_GATED" if saw_in_band else "BELOW_MIN_OR_ABOVE_CAP")


def evaluate_entries(S: Stream, bar_idx: int, bids: np.ndarray, asks: np.ndarray, ttimes: list, en_i: int,
                     counters: Optional[dict], funnel: Optional[list], wid: str) -> None:
    df = S.df
    c5_ts = S.a5.bars[-1]["time"]
    htf = t6c._ind_htf_trend([b["close"] for b in S.a1.bars])
    hour = c5_ts.hour
    if counters is not None:
        counters["closed_5m_bars_evaluated"] += 1
    sigs = t6c._ind_generate_signals(df, S.regime) if htf != 0 else []
    if counters is not None:
        if htf == 0:
            counters["h1_trend_zero"] += 1
        elif not (7 <= hour < 17):
            counters["outside_session_07_17_utc"] += 1
        elif not sigs:
            counters["no_strategy_trigger"] += 1
        else:
            counters["signal_bars"] += 1
            for st, d in sigs:
                key = f"{st}:{d}"
                counters["raw_signals_by_strategy_dir"][key] = counters["raw_signals_by_strategy_dir"].get(key, 0) + 1
    if not sigs:
        return
    lr = df.iloc[-1]
    cs_s = df["close"]
    feat = {
        "kalman_vel": S.kx1, "kalman_accel": S.kacc,
        "shannon_entropy": t6c._ind_shannon_entropy(cs_s.pct_change().dropna()) if len(cs_s) > 30 else 2.0,
        "kaufman_er": t6c._ind_kaufman_er(cs_s) if len(cs_s) > 15 else 0.3,
        "ema20_slope": float(lr["ema20_slope"]) if pd.notna(lr.get("ema20_slope")) else 0.0,
        "tick_weighted_price_dist": float(lr["tick_weighted_price_dist"]) if pd.notna(lr.get("tick_weighted_price_dist")) else 0.0,
        "tick_rate_ratio": float(lr["tick_rate_ratio"]) if pd.notna(lr.get("tick_rate_ratio")) else 1.0,
        "utc_hour": hour,
    }
    atr_v = float(df["atr"].iloc[-1]) if pd.notna(df["atr"].iloc[-1]) else 1.0
    eb, ea = float(bids[en_i]), float(asks[en_i])
    sp_usd = ea - eb
    en_ts = t11a._to_utc(ttimes[en_i])
    sinfo = t12p1.classify_dst_session(en_ts)
    adx = t11p1._compute_wilder_adx14(df)
    fx = compute_fx(df, feat, atr_v, S.regime, adx, eb, ea)
    scored = []
    comp_by_sig: dict[tuple[str, str], dict] = {}
    for st, d in sigs:
        sc = t6c._ind_score(d, S.regime, feat)
        comp = score_components(d, S.regime, feat)
        if abs(comp["c_final"] - sc) > 1e-9:
            raise RuntimeError(f"Score component replica mismatch {comp['c_final']} vs {sc}")
        comp_by_sig[(st, d)] = comp
        scored.append((sc, st, d))
    scored.sort(key=lambda x: x[0], reverse=True)

    statuses: dict[str, str] = {}
    for idx, pf in enumerate(S.pfs):
        c: EConfig = pf.cfg  # type: ignore[assignment]
        name = c.name
        if S.busy[idx]:
            statuses[name] = "POSITION_OPEN"
            continue
        if abs(min(0.0, pf.daily_pnl)) / max(pf.day_start_eq, 1.0) * 100.0 >= 3.0:
            statuses[name] = "DAILY_LOSS_LIMIT"
            continue
        if pf.daily_trades >= 8:
            statuses[name] = "MAX_TRADES_PER_DAY"
            continue
        if pf.consec_losses >= 3:
            statuses[name] = "MAX_CONSEC_LOSSES"
            continue
        chosen, why = _choose(c, pf, scored, htf, S.regime, fx, bar_idx, sp_usd, en_ts, sinfo)
        if chosen is None:
            statuses[name] = why
            continue
        if pf.halted:
            pf.prevented_signals_after_halt += 1
            statuses[name] = "HWM_HALT"
            continue
        sc, st, d = chosen
        raw_ep = ea if d == "BUY" else eb
        ep = (raw_ep + pf.slip_offset) if d == "BUY" else (raw_ep - pf.slip_offset)
        lot, sl_px, tp_px = S.size(pf.equity, ep, atr_v, d)
        if lot is None:
            statuses[name] = "SIZING_REJECT"
            continue
        e_comm = t12p2._side_comm(lot, ep, c.comm_rate_per_m)
        e_slip = pf.slip_offset * lot * 100.0
        if pf.open_pos is not None:
            pf.constraint_violations += 1
        pos = t11a.CleanPos(
            signal_time=c5_ts, direction=d, strategy=st, entry_time=ttimes[en_i],
            entry_bid=eb, entry_ask=ea, entry_price=ep, initial_sl=sl_px, sl=sl_px, tp=tp_px, atr=atr_v, lot=lot,
            entry_comm=e_comm, slip_usd=e_slip, peak_price=ep, last_swap_ts=ttimes[en_i],
        )
        ffeat = dict(fx)
        ffeat.update(comp_by_sig[(st, d)])
        ffeat["score"] = sc
        ffeat["dir_sign"] = _sgn(d)
        ffeat["htf"] = htf
        pos.t13 = {  # type: ignore[attr-defined]
            "feat": ffeat, "mfe": 0.0, "mae": 0.0, "t_mfe": 0.0, "t_mae": 0.0,
            "t_fav_05": None, "t_fav_10": None, "t_adv_05": None, "t_adv_10": None,
        }
        pf.open_pos = pos
        pf.mtm.update(pf.equity - sp_usd * lot * 100.0 - 2.0 * e_comm - e_slip)
        pf.last_cd_bar[(st, d)] = bar_idx
        pf.daily_trades += 1
        statuses[name] = "EXECUTED"
        if en_i + 1 < len(bids):
            ex = pf.scan_exit(bids, asks, ttimes, en_i + 1, False)
            if ex is not None:
                pf.close_pos(pf.open_pos, ex[0], ex[1], ex[2])
                pf.open_pos = None

    if funnel is not None:
        best = scored[0]
        bc = comp_by_sig[(best[1], best[2])]
        row = {
            "window": wid, "signal_bar_time": t11a._to_utc(c5_ts).isoformat(), "entry_tick_time": en_ts.isoformat(),
            "bar_idx": bar_idx, "regime": S.regime, "h1_trend": htf, "n_raw_signals": len(sigs),
            "raw_signals": ";".join(f"{st}:{d}:{sc:.4f}" for sc, st, d in scored),
            "best_strategy": best[1], "best_direction": best[2], "best_score": best[0],
            "entry_bid": eb, "entry_ask": ea,
            "status_H0_research": statuses.get(H0_NAME), "status_H0_application": statuses.get(H0_APP),
            "status_H4a_research": statuses.get(H4A_NAME), "status_H4a_application": statuses.get(H4A_APP),
        }
        row.update({f"fx_{k}": v for k, v in fx.items()})
        row.update(bc)
        funnel.append(row)


# =============================================================================
# Window runner
# =============================================================================

def _fetch(conn, s_dt: datetime, e_dt: datetime) -> pd.DataFrame:
    chunks = []
    cs = s_dt
    while cs < e_dt:
        ce = min(cs + timedelta(days=7), e_dt)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT time, bid, ask FROM market_ticks WHERE symbol = 'XAUUSD' AND time >= %s AND time < %s ORDER BY time ASC",
                (cs, ce),
            )
            rows = cur.fetchall()
        if rows:
            chunks.append(pd.DataFrame(rows, columns=["time", "bid", "ask"]))
        cs = ce
    if not chunks:
        return pd.DataFrame(columns=["time", "bid", "ask"])
    out = pd.concat(chunks, ignore_index=True)
    out["time"] = pd.to_datetime(out["time"], utc=True)
    return out


def new_counters() -> dict[str, Any]:
    return {"closed_5m_bars_evaluated": 0, "h1_trend_zero": 0, "outside_session_07_17_utc": 0,
            "no_strategy_trigger": 0, "signal_bars": 0, "raw_signals_by_strategy_dir": {}}


def run_window(conn, wid: str, start_dt: datetime, end_dt: datetime, configs: list[EConfig],
               verify_names: set[str], collect_funnel: bool) -> dict[str, Any]:
    A = Stream("A")
    A.pfs = [T13Portfolio(c, track_stops=(c.name in verify_names)) for c in configs]
    B: Optional[Stream] = None
    if verify_names:
        B = Stream("B")
        B.pfs = [T13Portfolio(c, track_stops=True) for c in configs if c.name in verify_names]
    streams = [A] + ([B] if B is not None else [])
    counters = new_counters() if collect_funnel else None
    funnel: Optional[list] = [] if collect_funnel else None
    bar_idx = -1
    last_valid: Optional[tuple[float, float, datetime]] = None

    w_df = _fetch(conn, start_dt - timedelta(days=4), start_dt)
    if not w_df.empty:
        w_df["minute"] = w_df["time"].dt.floor("min")
        for mts, grp in w_df.groupby("minute", sort=True):
            wb = grp["bid"].values.astype(np.float64)
            wa = grp["ask"].values.astype(np.float64)
            vi = np.flatnonzero((wb > 0.0) & (wa > 0.0) & (wb < wa))
            if len(vi) == 0:
                continue
            for S in streams:
                for j in vi:
                    S.k.update(float(wb[j]))
            vb = wb[vi]
            wc = {"time": mts.to_pydatetime(), "open": float(vb[0]), "high": float(vb.max()), "low": float(vb.min()),
                  "close": float(vb[-1]), "tick_count": int(len(vi))}
            for S in streams:
                S.a5.update(dict(wc))
                S.a1.update(dict(wc))
    del w_df

    end_ex = end_dt + timedelta(microseconds=1)
    cs = start_dt
    while cs < end_ex:
        ce = min(cs + timedelta(days=7), end_ex)
        print(f"[TASK 13 {wid}] {cs.strftime('%Y-%m-%d')} -> {ce.strftime('%Y-%m-%d')} "
              f"(H0={len(A.pfs[0].trades)}, H4a={len(A.pfs[1].trades)})", flush=True)
        t_df = _fetch(conn, cs, ce)
        cs = ce
        if t_df.empty:
            continue
        t_df["minute"] = t_df["time"].dt.floor("min")
        for mts, grp in t_df.groupby("minute", sort=True):
            bar_idx += 1
            m_py = mts.to_pydatetime()
            d_py = m_py.date()
            bids = grp["bid"].values.astype(np.float64)
            asks = grp["ask"].values.astype(np.float64)
            ttimes = [pd.Timestamp(x).to_pydatetime() for x in grp["time"].values]
            vm = (bids > 0.0) & (asks > 0.0) & (bids < asks)
            vi = np.flatnonzero(vm)
            if len(vi) > 0:
                lv = int(vi[-1])
                last_valid = (float(bids[lv]), float(asks[lv]), ttimes[lv])

            for S in streams:
                S.closed = False
                S.df = pd.DataFrame()
                if len(vi) > 0:
                    vb = bids[vi]
                    candle = {"time": m_py, "open": float(vb[0]), "high": float(vb.max()), "low": float(vb.min()),
                              "close": float(vb[-1]), "tick_count": int(len(vi))}
                    S.closed = S.a5.update(dict(candle))
                    S.a1.update(dict(candle))
                    if S.closed and len(S.a5.bars) >= 60:
                        S.df = t6c._ind_compute_indicators(list(S.a5.bars))
                        if not S.df.empty:
                            S.regime = S.rd.detect(S.df)
                S.busy = [False] * len(S.pfs)
                for idx, pf in enumerate(S.pfs):
                    pf.new_day(d_py)
                    if pf.open_pos is not None:
                        S.busy[idx] = True
                        pf.open_pos.bars_held += 1
                        ex = pf.scan_exit(bids, asks, ttimes, 0, True)
                        if ex is not None:
                            pf.close_pos(pf.open_pos, ex[0], ex[1], ex[2])
                            pf.open_pos = None

            if len(vi) == 0:
                continue
            en_i = int(vi[0])
            for S in streams:
                for j in range(en_i + 1):
                    if vm[j]:
                        S.k.update(float(bids[j]))
                S.kx1, S.kacc = S.k.x1, S.k.accel()
                for j in range(en_i + 1, len(bids)):
                    if vm[j]:
                        S.k.update(float(bids[j]))

            for S in streams:
                if S.closed and len(S.a5.bars) >= 60 and not S.df.empty:
                    is_a = S is A
                    evaluate_entries(S, bar_idx, bids, asks, ttimes, en_i,
                                     counters if is_a else None, funnel if is_a else None, wid)

    if last_valid is not None:
        lb, la, lt = last_valid
        for S in streams:
            for pf in S.pfs:
                if pf.open_pos is not None:
                    rc = lb if pf.open_pos.direction == "BUY" else la
                    epx = (rc - pf.slip_offset) if pf.open_pos.direction == "BUY" else (rc + pf.slip_offset)
                    pf.open_pos.slip_usd += pf.slip_offset * pf.open_pos.lot * 100.0
                    pf.close_pos(pf.open_pos, "END_OF_BACKTEST", epx, lt)
                    pf.open_pos = None

    return {
        "A": {pf.cfg.name: pf for pf in A.pfs},
        "B": {pf.cfg.name: pf for pf in B.pfs} if B is not None else {},
        "counters": counters, "funnel": funnel,
    }


# =============================================================================
# Statistics helpers
# =============================================================================

def summ(pf: T13Portfolio) -> dict[str, Any]:
    return t12p2._summ(pf.trades, pf.mtm)


def dir_pnl(pf: T13Portfolio, d: str) -> tuple[int, float]:
    t = [x for x in pf.trades if x["direction"] == d]
    return len(t), round(sum(x["net_pnl"] for x in t), 2)


def daily_series(pf: T13Portfolio) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in pf.trades:
        k = t["exit_time"][:10]
        out[k] = out.get(k, 0.0) + t["net_pnl"]
    return out


def paired_daily(cand: T13Portfolio, base: T13Portfolio, seed: int = 7) -> dict[str, Any]:
    a, b = daily_series(cand), daily_series(base)
    days = sorted(set(a) | set(b))
    diffs = np.array([a.get(d, 0.0) - b.get(d, 0.0) for d in days], dtype=float)
    p = 1.0
    nz = diffs[np.abs(diffs) > 1e-9]
    if len(nz) >= 5:
        try:
            p = float(stats.wilcoxon(nz).pvalue)
        except Exception:
            p = 1.0
    rng = np.random.default_rng(seed)
    if len(diffs) >= 2:
        means = rng.choice(diffs, size=(2000, len(diffs)), replace=True).mean(axis=1)
        lo, hi = np.percentile(means, [2.5, 97.5])
    else:
        lo = hi = float(diffs.mean()) if len(diffs) else 0.0
    return {"days": len(days), "days_nonzero_diff": int(len(nz)), "mean_daily_diff": round(float(diffs.mean()) if len(diffs) else 0.0, 3),
            "ci95_lo": round(float(lo), 3), "ci95_hi": round(float(hi), 3), "wilcoxon_p": round(p, 5)}


def ex_top(pf: T13Portfolio) -> dict[str, float]:
    p = sorted((t["net_pnl"] for t in pf.trades), reverse=True)
    ex_tr = round(sum(p[3:]), 2) if len(p) > 3 else 0.0
    d = sorted(daily_series(pf).values(), reverse=True)
    ex_dy = round(sum(d[3:]), 2) if len(d) > 3 else 0.0
    return {"ex_top3_trades_pnl": ex_tr, "ex_top3_days_pnl": ex_dy}


def bh(pvals: list[float]) -> list[float]:
    return t12p2._bh_fdr(pvals)
