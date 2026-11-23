"""
Task 11 (Phases 6, 7, 8, 10) — Independent Raw-Tick Regime Hypothesis Testing,
Multi-Window W1/W2/W3 Robustness, Parameter Sensitivity, Cost Stress,
Outlier Checks, BH-FDR Validation, and Reconciliation.

Zero imports from `src/goldedge`. Direct `psycopg2` access to `market_ticks`.
Produces deliverables #8–#11:
  - task11_regime_hypothesis_results.csv
  - task11_parameter_sensitivity.csv
  - task11_statistical_validation.json
  - task11_independent_reconciliation.json
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy import stats
import psycopg2

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import task6c_independent_validator as t6c
import task11_regime_reconstruction_and_diagnosis as t11p1

ROOT = t6c.ROOT
AUDIT_DIR = t6c.AUDIT_DIR
FROZEN_V2_DIR = AUDIT_DIR / "frozen_v2_recovered"


@dataclass
class RConfig:
    name: str
    group: str
    description: str
    max_score_trend: float = 1.01
    max_score_sideways: float = 1.01
    min_adx: float = 0.0
    max_raw_atr_pctl: float = 101.0
    min_sideways_er: float = 0.0
    min_bars_since_transition: int = 0
    block_buy_sideways_to_trend: bool = False
    buy_be_atr: float = 1.0
    buy_t1_trigger_atr: float = 1.6
    buy_t1_dist_atr: float = 1.1
    max_hwm_dd_pct: Optional[float] = None
    slip_pips: float = 0.0533
    comm_rate_per_m: float = 12.0


def _side_comm(lot: float, px: float, rate: float) -> float:
    return round(lot * t6c._CONTRACT_SIZE * px * (rate / 1_000_000.0), 2)


class RPortfolio:
    def __init__(self, cfg: RConfig):
        self.cfg = cfg
        self.slip_offset = cfg.slip_pips * t6c._PIP_SIZE
        self.open_pos: Optional[t6c.IndPos] = None
        self.trades: list[dict[str, Any]] = []
        self.equity = 10000.0
        self.day_start_eq = 10000.0
        self.mtm = t6c.IndMtmTracker(10000.0)
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.consec_losses = 0
        self.current_day: Optional[date] = None
        self.last_cd_bar: dict[tuple[str, str], int] = {}
        self.halted = False

    def new_day(self, d: date) -> None:
        if self.current_day != d:
            self.current_day = d
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.consec_losses = 0
            self.day_start_eq = max(self.equity, 1.0)

    def _net_at(self, pos: t6c.IndPos, px: float) -> float:
        pend = _side_comm(pos.lot, px, self.cfg.comm_rate_per_m) if pos.comm_exit == 0.0 else 0.0
        return pos.gross_at(px) - pos.comm_total - pend + pos.swap_pnl

    def _ratchet(self, pos: t6c.IndPos, price: float, max_hold: int) -> None:
        if pos.direction == "SELL":
            pos.ratchet(price, max_hold)
            return
        if price > pos.peak_price:
            pos.peak_price = price
        gain = pos.peak_price - pos.entry_price
        if gain >= 2.5 * pos.atr:
            pos.trailing_active = True
            new_sl = pos.peak_price - 0.75 * pos.atr
            if new_sl > pos.sl:
                pos.sl = new_sl
                pos.exit_reason_flag = "TRAILING_SL"
        elif gain >= self.cfg.buy_t1_trigger_atr * pos.atr:
            pos.trailing_active = True
            new_sl = pos.peak_price - self.cfg.buy_t1_dist_atr * pos.atr
            if new_sl > pos.sl:
                pos.sl = new_sl
                pos.exit_reason_flag = "TRAILING_SL"
        elif gain >= self.cfg.buy_be_atr * pos.atr:
            new_sl = pos.entry_price + 2.0 * t6c._PIP_SIZE
            if new_sl > pos.sl:
                pos.sl = new_sl
                pos.exit_reason_flag = "BREAKEVEN"
        hold_pct = pos.bars_held / max_hold if max_hold > 0 else 0.0
        if hold_pct >= 0.75:
            ratio = min((hold_pct - 0.75) / 0.25, 1.0)
            sq = pos.sl + ratio * (price - pos.sl)
            if sq > pos.sl:
                pos.sl = sq
                pos.exit_reason_flag = "TIME_SQUEEZE"

    def close_pos(self, pos: t6c.IndPos, reason: str, exit_px: float, exit_ts: datetime) -> None:
        pos.apply_rollovers(exit_ts)
        c_ex = _side_comm(pos.lot, exit_px, self.cfg.comm_rate_per_m)
        pos.comm_exit = c_ex
        pos.comm_total = round(pos.entry_comm + c_ex, 2)
        gross = pos.gross_at(exit_px)
        net = gross - pos.comm_total + pos.swap_pnl
        self.equity += net
        self.daily_pnl += net
        self.consec_losses = 0 if net > 0 else self.consec_losses + 1
        self.mtm.update(self.equity)
        e_utc = t6c._as_utc(pos.entry_time)
        self.trades.append({
            "entry_time": e_utc.isoformat(),
            "exit_time": t6c._as_utc(exit_ts).isoformat(),
            "direction": pos.direction,
            "gross_pnl": gross,
            "pnl": net,
            "exit_reason": reason,
            "subperiod": t11p1._subperiod(e_utc),
        })

    def scan_exit(self, bids: np.ndarray, asks: np.ndarray, ttimes: list[datetime], start_i: int, eval_to: bool) -> Optional[tuple[str, float, datetime]]:
        pos = self.open_pos
        if pos is None:
            return None
        is_bull = pos.direction == "BUY"
        eff_mh = 60 if 7 <= pos.entry_time.hour < 20 else 90
        best_q = None
        last_q = None
        for i in range(start_i, len(bids)):
            b = float(bids[i])
            a = float(asks[i])
            ts_i = ttimes[i]
            if b <= 0.0 or a <= 0.0 or b >= a:
                continue
            pos.apply_rollovers(ts_i)
            eq_q = b if is_bull else a
            last_q = eq_q
            if best_q is None or (is_bull and eq_q > best_q) or (not is_bull and eq_q < best_q):
                best_q = eq_q
            if is_bull and b <= pos.sl:
                rsn = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
                fpx = b - self.slip_offset
                pos.slip_usd += self.slip_offset * pos.lot * t6c._CONTRACT_SIZE
                self.mtm.update(self.equity + self._net_at(pos, fpx))
                return rsn, fpx, ts_i
            if (not is_bull) and a >= pos.sl:
                rsn = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
                fpx = a + self.slip_offset
                pos.slip_usd += self.slip_offset * pos.lot * t6c._CONTRACT_SIZE
                self.mtm.update(self.equity + self._net_at(pos, fpx))
                return rsn, fpx, ts_i
            if is_bull and b >= pos.tp:
                fpx = float(pos.tp)
                self.mtm.update(self.equity + self._net_at(pos, fpx))
                return "TARGET", fpx, ts_i
            if (not is_bull) and a <= pos.tp:
                fpx = float(pos.tp)
                self.mtm.update(self.equity + self._net_at(pos, fpx))
                return "TARGET", fpx, ts_i
            cur_eq = self.equity + self._net_at(pos, eq_q)
            self.mtm.update(cur_eq)
            if self.cfg.max_hwm_dd_pct is not None:
                dd_pct = max(0.0, (self.mtm.hwm - cur_eq) / self.mtm.hwm * 100.0) if self.mtm.hwm > 0 else 0.0
                if round(dd_pct, 10) >= self.cfg.max_hwm_dd_pct:
                    self.halted = True
                    fpx = (b - self.slip_offset) if is_bull else (a + self.slip_offset)
                    pos.slip_usd += self.slip_offset * pos.lot * t6c._CONTRACT_SIZE
                    self.mtm.update(self.equity + self._net_at(pos, fpx))
                    return "HWM_KILL_SWITCH_8PCT", fpx, ts_i
            self._ratchet(pos, eq_q, eff_mh)
        if eval_to and pos.bars_held >= eff_mh:
            bpx = last_q if last_q is not None else pos.entry_price
            epx = (bpx - self.slip_offset) if is_bull else (bpx + self.slip_offset)
            if last_q is not None:
                pos.slip_usd += self.slip_offset * pos.lot * t6c._CONTRACT_SIZE
            return "TIMEOUT", epx, ttimes[-1]
        if best_q is not None:
            self._ratchet(pos, best_q, eff_mh)
        return None


def _run_window(conn, wid: str, start_dt: datetime, end_dt: datetime, configs: list[RConfig]) -> list[RPortfolio]:
    pfs = [RPortfolio(c) for c in configs]
    warmup_start = start_dt - timedelta(days=4)

    def _fetch(s_dt: datetime, e_dt: datetime) -> pd.DataFrame:
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

    kalman = t6c.IndKalman2D()
    agg_5m = t6c.IndBarAggregator(5, 200)
    agg_1h = t6c.IndBarAggregator(60, 100)
    regime_det = t6c.IndRegimeDetector()
    current_regime = "UNKNOWN"
    last_trans_type = "NONE"
    bars_since_trans = 999
    cur_adx = 0.0
    cur_atr_pctl = 50.0
    bar_idx = -1
    last_valid: Optional[tuple[float, float, datetime]] = None

    w_df = _fetch(warmup_start, start_dt)
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

    end_ex = end_dt + timedelta(microseconds=1)
    cs = start_dt
    while cs < end_ex:
        ce = min(cs + timedelta(days=7), end_ex)
        print(f"[TASK 11 {wid}] Streaming {cs.strftime('%Y-%m-%d')} -> {ce.strftime('%Y-%m-%d')} (H0={len(pfs[0].trades)})...", flush=True)
        t_df = _fetch(cs, ce)
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

            closed_5m = False
            df_ind = pd.DataFrame()
            if len(vi) > 0:
                vb = bids[vi]
                candle = {"time": m_py, "open": float(vb[0]), "high": float(vb.max()), "low": float(vb.min()), "close": float(vb[-1]), "tick_count": int(len(vi))}
                closed_5m = agg_5m.update(candle)
                agg_1h.update(candle)
                if closed_5m and len(agg_5m.bars) >= 60:
                    df_ind = t6c._ind_compute_indicators(list(agg_5m.bars))
                    if not df_ind.empty:
                        new_r = regime_det.detect(df_ind)
                        if new_r != current_regime:
                            last_trans_type = t11p1._classify_transition(current_regime, new_r)
                            current_regime = new_r
                            bars_since_trans = 0
                        else:
                            bars_since_trans += 1
                        cur_adx = t11p1._compute_wilder_adx14(df_ind)
                        tr_s = pd.concat([
                            df_ind["high"] - df_ind["low"],
                            (df_ind["high"] - df_ind["close"].shift(1)).abs(),
                            (df_ind["low"] - df_ind["close"].shift(1)).abs(),
                        ], axis=1).max(axis=1)
                        atr_pct_s = (tr_s.rolling(14).mean() / df_ind["close"]).dropna()
                        cur_atr_pctl = float((atr_pct_s <= float(atr_pct_s.iloc[-1])).mean() * 100.0) if len(atr_pct_s) > 1 else 50.0

            entered_or_open = [False] * len(pfs)
            for p_idx, pf in enumerate(pfs):
                pf.new_day(d_py)
                if pf.open_pos is not None:
                    entered_or_open[p_idx] = True
                    pf.open_pos.bars_held += 1
                    ex = pf.scan_exit(bids, asks, ttimes, 0, True)
                    if ex is not None:
                        rsn, epx, ex_ts = ex
                        pf.close_pos(pf.open_pos, rsn, epx, ex_ts)
                        pf.open_pos = None

            if len(vi) == 0:
                continue
            en_i = int(vi[0])
            for j in range(en_i + 1):
                if vm[j]:
                    kalman.update(float(bids[j]))
            k_snap_x1 = kalman.x1
            k_snap_acc = kalman.accel()
            for j in range(en_i + 1, len(bids)):
                if vm[j]:
                    kalman.update(float(bids[j]))

            if not closed_5m or len(agg_5m.bars) < 60 or df_ind.empty:
                continue
            htf = t6c._ind_htf_trend([b["close"] for b in agg_1h.bars])
            if htf == 0:
                continue
            sigs = t6c._ind_generate_signals(df_ind, current_regime)
            if not sigs:
                continue

            lr = df_ind.iloc[-1]
            cs_s = df_ind["close"]
            c5_ts = agg_5m.bars[-1]["time"]
            ent_v = t6c._ind_shannon_entropy(cs_s.pct_change().dropna()) if len(cs_s) > 30 else 2.0
            er_v = t6c._ind_kaufman_er(cs_s) if len(cs_s) > 15 else 0.3
            feat = {
                "kalman_vel": k_snap_x1,
                "kalman_accel": k_snap_acc,
                "shannon_entropy": ent_v,
                "kaufman_er": er_v,
                "ema20_slope": float(lr["ema20_slope"]) if pd.notna(lr.get("ema20_slope")) else 0.0,
                "tick_weighted_price_dist": float(lr["tick_weighted_price_dist"]) if pd.notna(lr.get("tick_weighted_price_dist")) else 0.0,
                "tick_rate_ratio": float(lr["tick_rate_ratio"]) if pd.notna(lr.get("tick_rate_ratio")) else 1.0,
                "utc_hour": c5_ts.hour,
            }
            atr_v = float(df_ind["atr"].iloc[-1]) if pd.notna(df_ind["atr"].iloc[-1]) else 1.0
            eb, ea = float(bids[en_i]), float(asks[en_i])
            scored = [(t6c._ind_score(d, current_regime, feat), st, d) for st, d in sigs]
            scored.sort(key=lambda x: x[0], reverse=True)

            for p_idx, pf in enumerate(pfs):
                if entered_or_open[p_idx] or pf.halted:
                    continue
                c = pf.cfg
                if abs(min(0.0, pf.daily_pnl)) / max(pf.day_start_eq, 1.0) * 100.0 >= 3.0 or pf.daily_trades >= 8 or pf.consec_losses >= 3:
                    continue
                if current_regime == "HIGH_VOLATILITY":
                    continue
                if cur_adx < c.min_adx:
                    continue
                if cur_atr_pctl > c.max_raw_atr_pctl:
                    continue
                if bars_since_trans < c.min_bars_since_transition:
                    continue
                if current_regime == "SIDEWAYS" and er_v < c.min_sideways_er:
                    continue

                max_sc = c.max_score_sideways if current_regime == "SIDEWAYS" else c.max_score_trend
                chosen = None
                for sc, st, d in scored:
                    if not (0.60 <= sc < max_sc):
                        continue
                    if (d == "BUY" and htf != 1) or (d == "SELL" and htf != -1):
                        continue
                    if d == "BUY" and current_regime == "SIDEWAYS":
                        continue
                    if d == "BUY" and c.block_buy_sideways_to_trend and last_trans_type == "SIDEWAYS_TO_TREND":
                        continue
                    if bar_idx - pf.last_cd_bar.get((st, d), -9999) < 15:
                        continue
                    chosen = (sc, st, d)
                    break

                if chosen is None:
                    continue
                sc, st, d = chosen
                raw_ep = ea if d == "BUY" else eb
                ep = (raw_ep + pf.slip_offset) if d == "BUY" else (raw_ep - pf.slip_offset)
                lot, sl_px, tp_px = t6c._ind_size(pf.equity, ep, atr_v, d)
                if lot is None:
                    continue

                e_comm = _side_comm(lot, ep, c.comm_rate_per_m)
                e_slip = pf.slip_offset * lot * t6c._CONTRACT_SIZE
                pf.open_pos = t6c.IndPos(
                    direction=d, strategy=st, entry_time=ttimes[en_i],
                    entry_bid=eb, entry_ask=ea, entry_price=ep,
                    initial_sl=sl_px, sl=sl_px, tp=tp_px, atr=atr_v, lot=lot,
                    entry_comm=e_comm, slip_usd=e_slip,
                    peak_price=ep, worst_price=ep, last_swap_ts=ttimes[en_i],
                )
                pf.mtm.update(pf.equity - (ea - eb) * lot * t6c._CONTRACT_SIZE - 2.0 * e_comm - e_slip)
                pf.last_cd_bar[(st, d)] = bar_idx
                pf.daily_trades += 1

                if en_i + 1 < len(bids):
                    ex = pf.scan_exit(bids, asks, ttimes, en_i + 1, False)
                    if ex is not None:
                        rsn, epx, ex_ts = ex
                        pf.close_pos(pf.open_pos, rsn, epx, ex_ts)
                        pf.open_pos = None

    if last_valid is not None:
        lb, la, lt = last_valid
        for pf in pfs:
            if pf.open_pos is not None:
                rc = lb if pf.open_pos.direction == "BUY" else la
                epx = (rc - pf.slip_offset) if pf.open_pos.direction == "BUY" else (rc + pf.slip_offset)
                pf.open_pos.slip_usd += pf.slip_offset * pf.open_pos.lot * t6c._CONTRACT_SIZE
                pf.close_pos(pf.open_pos, "END_OF_BACKTEST", epx, lt)
                pf.open_pos = None
    return pfs


def _stats(trades: list[dict], mtm: Optional[t6c.IndMtmTracker] = None) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        out = {"n": 0, "wr_pct": 0.0, "net_pnl": 0.0, "pf": 0.0, "exp": 0.0, "closed_dd": 0.0}
        if mtm is not None:
            out["mtm_dd_pct"] = round(mtm.max_dd_pct, 2)
        return out
    p = np.array([t["pnl"] for t in trades], dtype=float)
    w = p[p > 0]
    l = p[p <= 0]
    pf = float(w.sum() / abs(l.sum())) if len(l) and l.sum() < 0 else (999.0 if len(w) and w.sum() > 0 else 0.0)
    ceq = np.cumsum(p) + 10000.0
    hwm = np.maximum.accumulate(np.insert(ceq, 0, 10000.0))[1:]
    c_dd = float((ceq - hwm).min())
    out = {
        "n": int(n),
        "wr_pct": round(len(w) / n * 100.0, 2),
        "net_pnl": round(float(p.sum()), 2),
        "pf": round(pf, 4),
        "exp": round(float(p.mean()), 2),
        "closed_dd": round(c_dd, 2),
    }
    if mtm is not None:
        out["mtm_dd_pct"] = round(mtm.max_dd_pct, 2)
    return out


def _ex_top_3(trades: list[dict]) -> dict[str, Any]:
    sp = sorted([float(t["pnl"]) for t in trades], reverse=True)
    rem = sp[3:] if len(sp) > 3 else []
    w = sum(x for x in rem if x > 0)
    l = abs(sum(x for x in rem if x <= 0))
    return {"remaining_net_pnl": round(sum(rem), 2), "remaining_pf": round(w / l, 4) if l > 0 else 0.0}


def _bh_fdr(pvals: list[float]) -> list[float]:
    m = len(pvals)
    if m == 0:
        return []
    indexed = sorted(enumerate(pvals), key=lambda x: x[1])
    out = [1.0] * m
    prev = 1.0
    for rev_i, (idx, pv) in enumerate(reversed(indexed)):
        rank = m - rev_i
        q = min(prev, (pv * m) / rank)
        out[idx] = round(min(1.0, q), 5)
        prev = q
    return out


def main() -> None:
    w3_configs = [
        # Core controls
        RConfig("H0_BASELINE_V2", "control", "Unchanged frozen V2 baseline"),
        RConfig("H8_H4a_ALL_REGIMES", "control", "H4a (score < 0.80) across all regimes", max_score_trend=0.80, max_score_sideways=0.80),
        # Individual regime hypotheses on H0 (Phase 6)
        RConfig("R1_ADX20_ON_H0", "regime_h0", "Require ADX_14 >= 20 on H0", min_adx=20.0),
        RConfig("R2_NO_RAW_HIVOL_ON_H0", "regime_h0", "Block raw atr_pctl_200 > 75% on H0", max_raw_atr_pctl=75.0),
        RConfig("R3_SIDEWAYS_ER25_ON_H0", "regime_h0", "Block low-efficiency SIDEWAYS (ER < 0.25) on H0", min_sideways_er=0.25),
        RConfig("R4_NO_POST_TRANS3_ON_H0", "regime_h0", "Block entries <= 3 bars after regime transition on H0", min_bars_since_transition=4),
        RConfig("R5_NO_SW_TO_BULL_BUY_H0", "regime_h0", "Block BUY after SIDEWAYS->TREND transition on H0", block_buy_sideways_to_trend=True),
        RConfig("R6_TREND_CAP080_SW_OPEN", "regime_h0", "Regime-specific: score < 0.80 in TREND only, uncapped in SIDEWAYS", max_score_trend=0.80, max_score_sideways=1.01),
        # Individual regime hypotheses on H4a (Phase 6)
        RConfig("R7_H4a_PLUS_ADX20", "regime_h4a", "H4a + ADX_14 >= 20", max_score_trend=0.80, max_score_sideways=0.80, min_adx=20.0),
        RConfig("R8_H4a_PLUS_NO_RAW_HIVOL", "regime_h4a", "H4a + block raw atr_pctl_200 > 75%", max_score_trend=0.80, max_score_sideways=0.80, max_raw_atr_pctl=75.0),
        RConfig("R9_H4a_PLUS_NO_POST_TRANS3", "regime_h4a", "H4a + block <= 3 bars after regime transition", max_score_trend=0.80, max_score_sideways=0.80, min_bars_since_transition=4),
        RConfig("R10_H4a_NO_SW_TO_BULL_BUY", "regime_h4a", "H4a + block BUY after SIDEWAYS->TREND transition", max_score_trend=0.80, max_score_sideways=0.80, block_buy_sideways_to_trend=True),
        RConfig("R11_H4a_PLUS_H7_REF", "combo_ref", "H4a + BUY fast BE 0.75/T1 1.2 (Task 10 combo ref)", max_score_trend=0.80, max_score_sideways=0.80, buy_be_atr=0.75, buy_t1_trigger_atr=1.2, buy_t1_dist_atr=0.8),
        # Separate Task 8 HWM 8% protected variants
        RConfig("H0_HWM8", "hwm_protected", "H0 + 8% HWM Kill-Switch", max_hwm_dd_pct=8.0),
        RConfig("H4a_HWM8", "hwm_protected", "H4a + 8% HWM Kill-Switch", max_score_trend=0.80, max_score_sideways=0.80, max_hwm_dd_pct=8.0),
        RConfig("R1_ADX20_HWM8", "hwm_protected", "R1 (ADX>=20 on H0) + 8% HWM Kill-Switch", min_adx=20.0, max_hwm_dd_pct=8.0),
        RConfig("R10_H4a_NOSW_HWM8", "hwm_protected", "R10 (H4a + no SW->BULL BUY) + 8% HWM Kill-Switch", max_score_trend=0.80, max_score_sideways=0.80, block_buy_sideways_to_trend=True, max_hwm_dd_pct=8.0),
        # Cost stress (1.5x slip + $15/M comm)
        RConfig("H4a_COST_STRESS", "cost_stress", "H4a under 1.5x slip & $15/M comm", max_score_trend=0.80, max_score_sideways=0.80, slip_pips=0.080, comm_rate_per_m=15.0),
        RConfig("R10_COST_STRESS", "cost_stress", "R10 under 1.5x slip & $15/M comm", max_score_trend=0.80, max_score_sideways=0.80, block_buy_sideways_to_trend=True, slip_pips=0.080, comm_rate_per_m=15.0),
        # Sensitivity sweep (Phase 7)
        RConfig("SENS_ADX15_H0", "sensitivity", "ADX >= 15 on H0", min_adx=15.0),
        RConfig("SENS_ADX25_H0", "sensitivity", "ADX >= 25 on H0", min_adx=25.0),
        RConfig("SENS_ADX30_H0", "sensitivity", "ADX >= 30 on H0", min_adx=30.0),
        RConfig("SENS_VOL70_H0", "sensitivity", "Raw ATR pctl <= 70 on H0", max_raw_atr_pctl=70.0),
        RConfig("SENS_VOL80_H0", "sensitivity", "Raw ATR pctl <= 80 on H0", max_raw_atr_pctl=80.0),
        RConfig("SENS_VOL85_H0", "sensitivity", "Raw ATR pctl <= 85 on H0", max_raw_atr_pctl=85.0),
        RConfig("SENS_TRANS1_H0", "sensitivity", "Block <= 1 bar post-transition on H0", min_bars_since_transition=2),
        RConfig("SENS_TRANS2_H0", "sensitivity", "Block <= 2 bars post-transition on H0", min_bars_since_transition=3),
        RConfig("SENS_TRANS5_H0", "sensitivity", "Block <= 5 bars post-transition on H0", min_bars_since_transition=6),
    ]

    mw_names = {"H0_BASELINE_V2", "H8_H4a_ALL_REGIMES", "R1_ADX20_ON_H0", "R2_NO_RAW_HIVOL_ON_H0", "R4_NO_POST_TRANS3_ON_H0", "R5_NO_SW_TO_BULL_BUY_H0", "R6_TREND_CAP080_SW_OPEN", "R7_H4a_PLUS_ADX20", "R10_H4a_NO_SW_TO_BULL_BUY", "R11_H4a_PLUS_H7_REF"}
    mw_configs = [c for c in w3_configs if c.name in mw_names]

    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    conn = psycopg2.connect(db_url)
    try:
        w3_pfs = _run_window(conn, "W3", datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc), w3_configs)
        w1_pfs = _run_window(conn, "W1", datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc), mw_configs)
        w2_pfs = _run_window(conn, "W2", datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc), mw_configs)
    finally:
        conn.close()

    # Reconciliation across W1, W2, W3
    recon = {}
    for wid, pfs, fn in [("W1", w1_pfs, "w1_trades.csv"), ("W2", w2_pfs, "w2_trades.csv"), ("W3", w3_pfs, "w3_trades.csv")]:
        fdf = pd.read_csv(FROZEN_V2_DIR / fn)
        h0 = pfs[0]
        mism = 0
        max_d = 0.0
        for i in range(max(len(fdf), len(h0.trades))):
            if i >= len(fdf) or i >= len(h0.trades):
                mism += 1
                continue
            d = abs(float(fdf.iloc[i]["pnl"]) - h0.trades[i]["pnl"])
            max_d = max(max_d, d)
            if d > 0.02 or str(fdf.iloc[i]["direction"]) != h0.trades[i]["direction"]:
                mism += 1
        recon[wid] = {"frozen_trades": int(len(fdf)), "sim_trades": int(len(h0.trades)), "mismatch_count": mism, "max_pnl_diff_usd": round(max_d, 6)}
    (AUDIT_DIR / "task11_independent_reconciliation.json").write_text(json.dumps(recon, indent=2), encoding="utf-8")

    # Build W3 hypothesis results & sensitivity tables
    h0_w3 = w3_pfs[0]
    h0_keys = {(t["entry_time"], t["direction"]) for t in h0_w3.trades}
    h0_pnls = [t["pnl"] for t in h0_w3.trades]

    hyp_rows = []
    sens_rows = []
    pvals = []
    for pf in w3_pfs:
        c = pf.cfg
        keys = {(t["entry_time"], t["direction"]) for t in pf.trades}
        ret = len(keys & h0_keys)
        rem = len(h0_keys - keys)
        new = len(keys - h0_keys)
        ov = _stats(pf.trades, pf.mtm)
        buy_s = _stats([t for t in pf.trades if t["direction"] == "BUY"])
        sell_s = _stats([t for t in pf.trades if t["direction"] == "SELL"])
        p1 = _stats([t for t in pf.trades if t["subperiod"] == "P1"])
        p2 = _stats([t for t in pf.trades if t["subperiod"] == "P2"])
        p3 = _stats([t for t in pf.trades if t["subperiod"] == "P3"])
        ex3 = _ex_top_3(pf.trades)
        pnls = [t["pnl"] for t in pf.trades]
        mw_p = float(stats.mannwhitneyu(pnls, h0_pnls, alternative="two-sided").pvalue) if c.name != "H0_BASELINE_V2" and len(pnls) >= 5 else 1.0

        row = {
            "hypothesis": c.name, "group": c.group,
            "n": ov["n"], "ret": ret, "rem": rem, "new": new,
            "wr_pct": ov["wr_pct"], "net_pnl": ov["net_pnl"], "pf": ov["pf"], "exp": ov["exp"],
            "closed_dd": ov["closed_dd"], "mtm_dd_pct": ov["mtm_dd_pct"],
            "buy_n": buy_s["n"], "buy_pnl": buy_s["net_pnl"], "buy_pf": buy_s["pf"],
            "sell_n": sell_s["n"], "sell_pnl": sell_s["net_pnl"], "sell_pf": sell_s["pf"],
            "P1": p1["net_pnl"], "P2": p2["net_pnl"], "P3": p3["net_pnl"],
            "ex_top3_pnl": ex3["remaining_net_pnl"], "ex_top3_pf": ex3["remaining_pf"],
            "mw_p": round(mw_p, 5),
        }
        if c.group == "sensitivity" or c.name in ("R1_ADX20_ON_H0", "R2_NO_RAW_HIVOL_ON_H0", "R4_NO_POST_TRANS3_ON_H0"):
            sens_rows.append(row)
        if c.group != "sensitivity":
            hyp_rows.append(row)
            if c.name != "H0_BASELINE_V2":
                pvals.append(mw_p)

    qs = _bh_fdr(pvals)
    hyp_rows[0]["mw_q_fdr"] = 1.0
    for r, qv in zip(hyp_rows[1:], qs):
        r["mw_q_fdr"] = qv

    pd.DataFrame(hyp_rows).to_csv(AUDIT_DIR / "task11_regime_hypothesis_results.csv", index=False)
    pd.DataFrame(sens_rows).to_csv(AUDIT_DIR / "task11_parameter_sensitivity.csv", index=False)

    # Multi-window statistical validation JSON
    w1_map = {pf.cfg.name: pf for pf in w1_pfs}
    w2_map = {pf.cfg.name: pf for pf in w2_pfs}
    w3_map = {pf.cfg.name: pf for pf in w3_pfs}
    stat_val = {"multi_window": {}, "outliers_ex_top_3": {}}
    for name in [c.name for c in mw_configs]:
        stat_val["multi_window"][name] = {}
        for wid, mdict in [("W1", w1_map), ("W2", w2_map), ("W3", w3_map)]:
            pf = mdict[name]
            stat_val["multi_window"][name][wid] = {
                "overall": _stats(pf.trades, pf.mtm),
                "buy": _stats([t for t in pf.trades if t["direction"] == "BUY"]),
                "sell": _stats([t for t in pf.trades if t["direction"] == "SELL"]),
            }
        stat_val["outliers_ex_top_3"][name] = _ex_top_3(w3_map[name].trades)

    (AUDIT_DIR / "task11_statistical_validation.json").write_text(json.dumps(stat_val, indent=2), encoding="utf-8")

    print("\n[TASK 11 PART 2 COMPLETE]")
    print("Reconciliation:", json.dumps(recon, indent=2))
    print("\nW3 Regime Hypothesis Results:")
    print(pd.DataFrame(hyp_rows).to_string(index=False))
    print("\nMulti-Window W1/W2/W3 Summary:")
    for name, d in stat_val["multi_window"].items():
        w1, w2, w3 = d["W1"]["overall"], d["W2"]["overall"], d["W3"]["overall"]
        print(f"  {name:26s} | W1: n={w1['n']:3d} pnl={w1['net_pnl']:7.1f} pf={w1['pf']:.3f} dd={w1['mtm_dd_pct']:5.2f}% | W2: n={w2['n']:3d} pnl={w2['net_pnl']:7.1f} pf={w2['pf']:.3f} dd={w2['mtm_dd_pct']:5.2f}% | W3: n={w3['n']:3d} pnl={w3['net_pnl']:7.1f} pf={w3['pf']:.3f} dd={w3['mtm_dd_pct']:5.2f}%")


if __name__ == "__main__":
    main()
