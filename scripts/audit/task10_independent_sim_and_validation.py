"""
Task 10 (Phases 3, 4, 5, 6, 7, 8, 10) — Independent Raw-Tick Multi-Window
Hypothesis Testing, Sensitivity Sweep, Cost Stress, Statistical Validation,
and Frozen Baseline Reconciliation.

Zero imports from `src/goldedge`. Connects directly to TimescaleDB `market_ticks`
via `psycopg2`. Produces deliverables #7–#11:
  - task10_hypothesis_results.json
  - task10_hypothesis_results.csv
  - task10_parameter_sensitivity.csv
  - task10_statistical_validation.json
  - task10_independent_reconciliation.json
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

ROOT = t6c.ROOT
AUDIT_DIR = t6c.AUDIT_DIR
FROZEN_V2_DIR = AUDIT_DIR / "frozen_v2_recovered"


@dataclass
class T10Config:
    name: str
    group: str
    description: str
    max_score: float = 1.01
    min_score: float = 0.60
    max_er: float = 1.01
    accel_mode: str = "original"  # original | remove | penalize_extreme | mod_bonus_pen_extreme | with_er_ext
    accel_extreme_thresh: float = 0.015
    block_buy_hours: tuple[int, ...] = ()
    buy_be_atr: float = 1.0
    buy_t1_trigger_atr: float = 1.6
    buy_t1_dist_atr: float = 1.1
    max_hwm_dd_pct: Optional[float] = None
    slip_pips: float = 0.0533
    comm_rate_per_million: float = 12.0


def _score_with_accel_mode(direction: str, regime: str, feat: dict, cfg: T10Config) -> float:
    kv = feat["kalman_vel"]
    ent = feat["shannon_entropy"]
    er = feat["kaufman_er"]
    slp = feat["ema20_slope"]
    twd = feat["tick_weighted_price_dist"]
    h = feat["utc_hour"]
    sw = "SIDEWAYS" in regime.upper()
    s = 0.0
    if direction == "BUY":
        mom = t6c._score_band(kv, 0.008, 0.040, 0.002, 0.080) if sw else t6c._score_band(kv, 0.015, 0.060, 0.005, 0.120)
        s += 0.35 * mom
        s += 0.20 * t6c._score_band(er, 0.20, 0.60, 0.08, 0.90)
        s += 0.15 * t6c._score_band(ent, 2.50, 3.20, None, 3.60)
        s += 0.15 * t6c._interp(slp, 0.00005, 0.00030)
        s += 0.10 * t6c._score_band(twd, -0.0005, 0.003, -0.003, 0.006)
        s += 0.05 * t6c._sess_score(h)
        aligned_acc = feat["kalman_accel"]
    else:
        mom = t6c._score_band(kv, -0.040, -0.008, -0.080, -0.002) if sw else t6c._score_band(kv, -0.060, -0.015, -0.120, -0.005)
        s += 0.35 * mom
        s += 0.20 * t6c._score_band(er, 0.20, 0.60, 0.08, 0.90)
        s += 0.15 * t6c._score_band(ent, 2.50, 3.20, None, 3.60)
        s += 0.15 * t6c._interp(slp, -0.00005, -0.00030)
        s += 0.10 * t6c._score_band(twd, -0.003, 0.0005, -0.006, 0.003)
        s += 0.05 * t6c._sess_score(h)
        aligned_acc = -feat["kalman_accel"]

    qs = min(1.0, max(0.0, s))
    if cfg.accel_mode == "original":
        adj = max(-0.08, min(0.08, aligned_acc * 2.0))
    elif cfg.accel_mode == "remove":
        adj = 0.0
    elif cfg.accel_mode == "penalize_extreme":
        adj = -0.12 if aligned_acc > cfg.accel_extreme_thresh else 0.0
    elif cfg.accel_mode == "mod_bonus_pen_extreme":
        if -0.02 <= aligned_acc <= 0.005:
            adj = 0.04
        elif aligned_acc > cfg.accel_extreme_thresh:
            adj = -0.12
        else:
            adj = 0.0
    else:
        adj = max(-0.08, min(0.08, aligned_acc * 2.0))

    qs = min(1.0, max(0.0, qs + adj))
    trr = feat["tick_rate_ratio"]
    if trr < 0.70:
        qs *= 0.75
    elif sw and trr < 1.10:
        qs *= 0.85
    return round(qs, 4)


def _side_comm(lot: float, px: float, rate_per_m: float) -> float:
    return round(lot * t6c._CONTRACT_SIZE * px * (rate_per_m / 1_000_000.0), 2)


def _subperiod(dt: datetime) -> str:
    d = t6c._as_utc(dt)
    if d < datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc):
        return "P1"
    if d < datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc):
        return "P2"
    return "P3"


class T10Portfolio:
    def __init__(self, cfg: T10Config):
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
        self.halt_time: Optional[datetime] = None

    def new_day(self, d: date) -> None:
        if self.current_day != d:
            self.current_day = d
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.consec_losses = 0
            self.day_start_eq = max(self.equity, 1.0)

    def _net_at(self, pos: t6c.IndPos, px: float) -> float:
        pend = _side_comm(pos.lot, px, self.cfg.comm_rate_per_million) if pos.comm_exit == 0.0 else 0.0
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
            be = 2.0 * t6c._PIP_SIZE
            new_sl = pos.entry_price + be
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

    def close_pos(self, pos: t6c.IndPos, reason: str, exit_px: float, exit_ts: datetime, entry_score: float) -> None:
        pos.apply_rollovers(exit_ts)
        c_ex = _side_comm(pos.lot, exit_px, self.cfg.comm_rate_per_million)
        pos.comm_exit = c_ex
        pos.comm_total = round(pos.entry_comm + c_ex, 2)
        gross = pos.gross_at(exit_px)
        net = gross - pos.comm_total + pos.swap_pnl
        self.equity += net
        self.daily_pnl += net
        self.consec_losses = 0 if net > 0 else self.consec_losses + 1
        self.mtm.update(self.equity)
        e_utc = t6c._as_utc(pos.entry_time)
        x_utc = t6c._as_utc(exit_ts)
        self.trades.append({
            "entry_time": e_utc.isoformat(),
            "exit_time": x_utc.isoformat(),
            "direction": pos.direction,
            "strategy": pos.strategy,
            "entry_price": pos.entry_price,
            "exit_price": exit_px,
            "qty": pos.lot,
            "score": entry_score,
            "gross_pnl": gross,
            "pnl": net,
            "commission_usd": pos.comm_total,
            "slippage_usd": pos.slip_usd,
            "exit_reason": reason,
            "subperiod": _subperiod(e_utc),
            "split": "DEV" if e_utc < datetime(2026, 10, 17, tzinfo=timezone.utc) else "HOLDOUT",
        })

    def scan_exit(self, bids: np.ndarray, asks: np.ndarray, ttimes: list[datetime], start_i: int, eval_to: bool) -> Optional[tuple[str, float, datetime]]:
        pos = self.open_pos
        if pos is None:
            return None
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
                    self.halt_time = ts_i
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


def _run_window(conn, wid: str, start_dt: datetime, end_dt: datetime, configs: list[T10Config]) -> list[T10Portfolio]:
    portfolios = [T10Portfolio(c) for c in configs]
    open_scores = [0.0] * len(configs)
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
    current_regime = "UNKNOWN"
    regime_det = t6c.IndRegimeDetector()
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
        print(f"[TASK 10 {wid}] Streaming {cs.strftime('%Y-%m-%d')} -> {ce.strftime('%Y-%m-%d')} (H0={len(portfolios[0].trades)})...", flush=True)
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
                        current_regime = regime_det.detect(df_ind)

            entered_or_open = [False] * len(portfolios)
            for p_idx, pf in enumerate(portfolios):
                pf.new_day(d_py)
                if pf.open_pos is not None:
                    entered_or_open[p_idx] = True
                    pf.open_pos.bars_held += 1
                    ex = pf.scan_exit(bids, asks, ttimes, 0, True)
                    if ex is not None:
                        rsn, epx, ex_ts = ex
                        pf.close_pos(pf.open_pos, rsn, epx, ex_ts, open_scores[p_idx])
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
            close_v = float(lr["close"])
            e20_v = float(lr["ema20"]) if pd.notna(lr.get("ema20")) else close_v
            eb, ea = float(bids[en_i]), float(asks[en_i])

            for p_idx, pf in enumerate(portfolios):
                if entered_or_open[p_idx] or pf.halted:
                    continue
                c = pf.cfg
                if abs(min(0.0, pf.daily_pnl)) / max(pf.day_start_eq, 1.0) * 100.0 >= 3.0 or pf.daily_trades >= 8 or pf.consec_losses >= 3:
                    continue
                if current_regime == "HIGH_VOLATILITY":
                    continue

                scored = [(_score_with_accel_mode(d, current_regime, feat, c), st, d) for st, d in sigs]
                scored.sort(key=lambda x: x[0], reverse=True)
                chosen = None
                for sc, st, d in scored:
                    if not (c.min_score <= sc < c.max_score):
                        continue
                    if (d == "BUY" and htf != 1) or (d == "SELL" and htf != -1):
                        continue
                    if d == "BUY" and current_regime == "SIDEWAYS":
                        continue
                    if d == "BUY" and c5_ts.hour in c.block_buy_hours:
                        continue
                    if er_v > c.max_er:
                        continue
                    if c.accel_mode == "with_er_ext":
                        al_acc = k_snap_acc if d == "BUY" else -k_snap_acc
                        al_ext = ((close_v - e20_v) if d == "BUY" else (e20_v - close_v)) / max(atr_v, 1e-9)
                        if al_acc > 0.0 and (er_v > 0.45 or al_ext > 1.5):
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

                e_comm = _side_comm(lot, ep, c.comm_rate_per_million)
                e_slip = pf.slip_offset * lot * t6c._CONTRACT_SIZE
                pf.open_pos = t6c.IndPos(
                    direction=d, strategy=st, entry_time=ttimes[en_i],
                    entry_bid=eb, entry_ask=ea, entry_price=ep,
                    initial_sl=sl_px, sl=sl_px, tp=tp_px, atr=atr_v, lot=lot,
                    entry_comm=e_comm, slip_usd=e_slip,
                    peak_price=ep, worst_price=ep, last_swap_ts=ttimes[en_i],
                )
                open_scores[p_idx] = sc
                pf.mtm.update(pf.equity - (ea - eb) * lot * t6c._CONTRACT_SIZE - 2.0 * e_comm - e_slip)
                pf.last_cd_bar[(st, d)] = bar_idx
                pf.daily_trades += 1

                if en_i + 1 < len(bids):
                    ex = pf.scan_exit(bids, asks, ttimes, en_i + 1, False)
                    if ex is not None:
                        rsn, epx, ex_ts = ex
                        pf.close_pos(pf.open_pos, rsn, epx, ex_ts, open_scores[p_idx])
                        pf.open_pos = None

    if last_valid is not None:
        lb, la, lt = last_valid
        for p_idx, pf in enumerate(portfolios):
            if pf.open_pos is not None:
                rc = lb if pf.open_pos.direction == "BUY" else la
                epx = (rc - pf.slip_offset) if pf.open_pos.direction == "BUY" else (rc + pf.slip_offset)
                pf.open_pos.slip_usd += pf.slip_offset * pf.open_pos.lot * t6c._CONTRACT_SIZE
                pf.close_pos(pf.open_pos, "END_OF_BACKTEST", epx, lt, open_scores[p_idx])
                pf.open_pos = None

    return portfolios


def _stats_block(trades: list[dict], mtm: Optional[t6c.IndMtmTracker] = None, final_eq: Optional[float] = None) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        out = {"n": 0, "wins": 0, "losses": 0, "wr_pct": 0.0, "gross_pnl": 0.0, "net_pnl": 0.0, "pf": 0.0, "expectancy": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "closed_dd": 0.0, "ci95_low": 0.0, "ci95_high": 0.0}
        if mtm is not None:
            out["mtm_dd_usd"] = round(mtm.max_dd, 2)
            out["mtm_dd_pct"] = round(mtm.max_dd_pct, 2)
            out["final_equity"] = round(final_eq or 10000.0, 2)
        return out
    pnls = np.array([t["pnl"] for t in trades], dtype=float)
    gross = float(sum(t["gross_pnl"] for t in trades))
    w = pnls[pnls > 0]
    l = pnls[pnls <= 0]
    pf = float(w.sum() / abs(l.sum())) if len(l) and l.sum() < 0 else (999.0 if len(w) and w.sum() > 0 else 0.0)
    ceq = np.cumsum(pnls) + 10000.0
    hwm = np.maximum.accumulate(np.insert(ceq, 0, 10000.0))[1:]
    c_dd = float((ceq - hwm).min())
    exp = float(pnls.mean())
    std = float(pnls.std(ddof=1)) if n > 1 else 0.0
    se = std / math.sqrt(n) if n > 1 else 0.0
    tc = float(stats.t.ppf(0.975, df=n - 1)) if n > 1 else 0.0
    out = {
        "n": int(n),
        "wins": int(len(w)),
        "losses": int(len(l)),
        "wr_pct": round(len(w) / n * 100.0, 2),
        "gross_pnl": round(gross, 2),
        "net_pnl": round(float(pnls.sum()), 2),
        "pf": round(pf, 4),
        "expectancy": round(exp, 2),
        "avg_win": round(float(w.mean()), 2) if len(w) else 0.0,
        "avg_loss": round(float(l.mean()), 2) if len(l) else 0.0,
        "closed_dd": round(c_dd, 2),
        "std_pnl": round(std, 2),
        "ci95_low": round(exp - tc * se, 2),
        "ci95_high": round(exp + tc * se, 2),
    }
    if mtm is not None:
        out["mtm_dd_usd"] = round(mtm.max_dd, 2)
        out["mtm_dd_pct"] = round(mtm.max_dd_pct, 2)
        out["final_equity"] = round(final_eq or (10000.0 + float(pnls.sum())), 2)
    return out


def _outlier_check(trades: list[dict]) -> dict[str, Any]:
    if not trades:
        return {}
    sp = sorted([float(t["pnl"]) for t in trades], reverse=True)
    res = {}
    for k in (1, 2, 3):
        rem = sp[k:]
        w = sum(x for x in rem if x > 0)
        l = abs(sum(x for x in rem if x <= 0))
        res[f"ex_top_{k}"] = {
            "removed_sum": round(sum(sp[:k]), 2),
            "remaining_net_pnl": round(sum(rem), 2),
            "remaining_pf": round(w / l, 4) if l > 0 else 0.0,
        }
    return res


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
        # Main hypotheses (Phase 3)
        T10Config("H0_BASELINE_V2", "main", "Frozen V2 control (unchanged)"),
        T10Config("H4a_SCORE_CAP_080", "main", "Reject entries with score >= 0.80", max_score=0.80),
        T10Config("H5_ER_CAP_045", "main", "Reject entries with Kaufman ER > 0.45", max_er=0.45),
        T10Config("H4a_PLUS_H5", "main", "Combined exhaustion filter: score < 0.80 AND ER <= 0.45", max_score=0.80, max_er=0.45),
        T10Config("H4b_SCORE_CAP_075", "main", "Tighter score cap: score < 0.75", max_score=0.75),
        # Acceleration alternatives (Phase 4)
        T10Config("ACCEL_REMOVE", "accel", "Remove Kalman acceleration bonus completely", accel_mode="remove"),
        T10Config("ACCEL_PENALIZE_EXTREME", "accel", "Zero accel bonus; penalize extreme aligned accel > 0.015 (-0.12)", accel_mode="penalize_extreme", accel_extreme_thresh=0.015),
        T10Config("ACCEL_MOD_BONUS_PEN_EXTREME", "accel", "Reward pullback/mod accel [-0.02..0.005] (+0.04), penalize >0.015 (-0.12)", accel_mode="mod_bonus_pen_extreme", accel_extreme_thresh=0.015),
        T10Config("ACCEL_WITH_ER_AND_EXT", "accel", "Block aligned accel > 0 when ER > 0.45 or EMA20 ext > 1.5 ATR", accel_mode="with_er_ext"),
        # Secondary hypotheses (Phase 6)
        T10Config("H1_BLOCK_BUY_07UTC", "secondary", "Block BUY entries during 07:00-07:59 UTC", block_buy_hours=(7,)),
        T10Config("H7_BUY_FAST_BE_075_T1_12", "secondary", "BUY-specific BE 0.75 ATR and T1 1.2/0.8 ATR", buy_be_atr=0.75, buy_t1_trigger_atr=1.2, buy_t1_dist_atr=0.8),
        # Justified combinations (Phase 8)
        T10Config("H4a_PLUS_H7", "combo", "Score < 0.80 + BUY fast BE 0.75/T1 1.2", max_score=0.80, buy_be_atr=0.75, buy_t1_trigger_atr=1.2, buy_t1_dist_atr=0.8),
        T10Config("H4a_PLUS_H1", "combo", "Score < 0.80 + Block BUY 07 UTC", max_score=0.80, block_buy_hours=(7,)),
        # Task 8 8% HWM protected views (kept strictly separate!)
        T10Config("H0_WITH_HWM_8PCT", "hwm_protected", "Baseline V2 + 8% HWM Kill-Switch", max_hwm_dd_pct=8.0),
        T10Config("H4a_WITH_HWM_8PCT", "hwm_protected", "H4a (score < 0.80) + 8% HWM Kill-Switch", max_score=0.80, max_hwm_dd_pct=8.0),
        T10Config("H5_WITH_HWM_8PCT", "hwm_protected", "H5 (ER <= 0.45) + 8% HWM Kill-Switch", max_er=0.45, max_hwm_dd_pct=8.0),
        T10Config("H4a_PLUS_H5_WITH_HWM_8PCT", "hwm_protected", "H4a+H5 + 8% HWM Kill-Switch", max_score=0.80, max_er=0.45, max_hwm_dd_pct=8.0),
        T10Config("H4a_PLUS_H7_WITH_HWM_8PCT", "hwm_protected", "H4a+H7 + 8% HWM Kill-Switch", max_score=0.80, buy_be_atr=0.75, buy_t1_trigger_atr=1.2, buy_t1_dist_atr=0.8, max_hwm_dd_pct=8.0),
        # Cost stress (1.5x slippage 0.080p + $15/M comm)
        T10Config("H0_COST_STRESS", "cost_stress", "H0 under 1.5x slippage (0.080p) & $15/M comm", slip_pips=0.080, comm_rate_per_million=15.0),
        T10Config("H4a_COST_STRESS", "cost_stress", "H4a under 1.5x slippage & $15/M comm", max_score=0.80, slip_pips=0.080, comm_rate_per_million=15.0),
        T10Config("H5_COST_STRESS", "cost_stress", "H5 under 1.5x slippage & $15/M comm", max_er=0.45, slip_pips=0.080, comm_rate_per_million=15.0),
        T10Config("H4a_PLUS_H5_COST_STRESS", "cost_stress", "H4a+H5 under 1.5x slippage & $15/M comm", max_score=0.80, max_er=0.45, slip_pips=0.080, comm_rate_per_million=15.0),
        T10Config("H4a_PLUS_H7_COST_STRESS", "cost_stress", "H4a+H7 under 1.5x slippage & $15/M comm", max_score=0.80, buy_be_atr=0.75, buy_t1_trigger_atr=1.2, buy_t1_dist_atr=0.8, slip_pips=0.080, comm_rate_per_million=15.0),
        # Sensitivity sweep (Phase 7 #4)
        T10Config("SENS_SCORE_078", "sensitivity", "Score cap 0.78", max_score=0.78),
        T10Config("SENS_SCORE_082", "sensitivity", "Score cap 0.82", max_score=0.82),
        T10Config("SENS_SCORE_085", "sensitivity", "Score cap 0.85", max_score=0.85),
        T10Config("SENS_SCORE_090", "sensitivity", "Score cap 0.90", max_score=0.90),
        T10Config("SENS_ER_040", "sensitivity", "ER cap 0.40", max_er=0.40),
        T10Config("SENS_ER_050", "sensitivity", "ER cap 0.50", max_er=0.50),
        T10Config("SENS_ER_055", "sensitivity", "ER cap 0.55", max_er=0.55),
        T10Config("SENS_ACCEL_005", "sensitivity", "Penalize aligned accel > 0.005", accel_mode="penalize_extreme", accel_extreme_thresh=0.005),
        T10Config("SENS_ACCEL_010", "sensitivity", "Penalize aligned accel > 0.010", accel_mode="penalize_extreme", accel_extreme_thresh=0.010),
        T10Config("SENS_ACCEL_020", "sensitivity", "Penalize aligned accel > 0.020", accel_mode="penalize_extreme", accel_extreme_thresh=0.020),
    ]

    mw_names = {"H0_BASELINE_V2", "H4a_SCORE_CAP_080", "H5_ER_CAP_045", "H4a_PLUS_H5", "H4b_SCORE_CAP_075", "ACCEL_PENALIZE_EXTREME", "ACCEL_WITH_ER_AND_EXT", "H1_BLOCK_BUY_07UTC", "H7_BUY_FAST_BE_075_T1_12", "H4a_PLUS_H7", "H4a_PLUS_H1"}
    mw_configs = [c for c in w3_configs if c.name in mw_names]

    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    conn = psycopg2.connect(db_url)
    try:
        w3_pfs = _run_window(conn, "W3", datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc), w3_configs)
        w1_pfs = _run_window(conn, "W1", datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc), mw_configs)
        w2_pfs = _run_window(conn, "W2", datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc), mw_configs)
    finally:
        conn.close()

    # Reconcile H0 across W1, W2, W3 against frozen V2 CSVs
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

    # Cross-check W3 H4a and H5 against Task 9 results
    t9_h = {r["hypothesis"]: r for r in json.loads((AUDIT_DIR / "task9_hypothesis_results.json").read_text(encoding="utf-8"))}
    w3_map = {pf.cfg.name: pf for pf in w3_pfs}
    recon["task9_cross_check"] = {
        "H4a_pnl_diff_vs_task9": round(abs(w3_map["H4a_SCORE_CAP_080"].equity - 10000.0 - t9_h["H4a_SCORE_CAP_080"]["overall"]["net_pnl"]), 4),
        "H5_pnl_diff_vs_task9": round(abs(w3_map["H5_ER_CAP_045"].equity - 10000.0 - t9_h["H5_CAP_ER_045"]["overall"]["net_pnl"]), 4),
        "H1_pnl_diff_vs_task9": round(abs(w3_map["H1_BLOCK_BUY_07UTC"].equity - 10000.0 - t9_h["H1_BLOCK_BUY_07UTC"]["overall"]["net_pnl"]), 4),
        "H7_pnl_diff_vs_task9": round(abs(w3_map["H7_BUY_FAST_BE_075_T1_12"].equity - 10000.0 - t9_h["H7_BUY_FAST_BE_075_T1_12"]["overall"]["net_pnl"]), 4),
    }
    (AUDIT_DIR / "task10_independent_reconciliation.json").write_text(json.dumps(recon, indent=2), encoding="utf-8")

    # Build W3 hypothesis results & statistical validation
    h0_w3 = w3_pfs[0]
    h0_keys = {(t["entry_time"], t["direction"]) for t in h0_w3.trades}
    h0_pnls = [t["pnl"] for t in h0_w3.trades]

    hyp_results = []
    sens_rows = []
    stat_val = {"multi_window": {}, "outlier_sensitivity": {}, "power_analysis": {}}

    for pf in w3_pfs:
        c = pf.cfg
        keys = {(t["entry_time"], t["direction"]) for t in pf.trades}
        ret = len(keys & h0_keys)
        rem = len(h0_keys - keys)
        new = len(keys - h0_keys)

        ov = _stats_block(pf.trades, pf.mtm, pf.equity)
        buy_tr = [t for t in pf.trades if t["direction"] == "BUY"]
        sell_tr = [t for t in pf.trades if t["direction"] == "SELL"]
        buy_s = _stats_block(buy_tr)
        sell_s = _stats_block(sell_tr)
        p1 = _stats_block([t for t in pf.trades if t["subperiod"] == "P1"])
        p2 = _stats_block([t for t in pf.trades if t["subperiod"] == "P2"])
        p3 = _stats_block([t for t in pf.trades if t["subperiod"] == "P3"])
        dev = _stats_block([t for t in pf.trades if t["split"] == "DEV"])
        ho = _stats_block([t for t in pf.trades if t["split"] == "HOLDOUT"])

        pnls = [t["pnl"] for t in pf.trades]
        mw_p = float(stats.mannwhitneyu(pnls, h0_pnls, alternative="two-sided").pvalue) if c.name != "H0_BASELINE_V2" and len(pnls) >= 5 else 1.0
        # Two-sample t-test p-value & required N for 80% power
        if c.name != "H0_BASELINE_V2" and len(pnls) >= 5:
            diff_m = ov["expectancy"] - 1.48
            sd_p = ov.get("std_pnl", 90.0) or 90.0
            req_n_80 = int(math.ceil(2.0 * ((1.96 + 0.84) * sd_p / max(abs(diff_m), 0.1)) ** 2))
        else:
            req_n_80 = 0

        entry = {
            "hypothesis": c.name,
            "group": c.group,
            "description": c.description,
            "retained": ret,
            "removed": rem,
            "newly_generated": new,
            "halted_by_hwm": pf.halted,
            "overall": ov,
            "buy": buy_s,
            "sell": sell_s,
            "subperiods": {"P1": p1, "P2": p2, "P3": p3},
            "dev_vs_holdout": {"DEV": dev, "HOLDOUT": ho},
            "mw_p_vs_h0": round(mw_p, 5),
            "req_n_per_arm_80pct_power": req_n_80,
            "outliers": _outlier_check(pf.trades),
        }
        if c.group == "sensitivity" or c.name in ("H4b_SCORE_CAP_075", "H4a_SCORE_CAP_080", "H5_ER_CAP_045", "ACCEL_PENALIZE_EXTREME"):
            sens_rows.append({
                "hypothesis": c.name,
                "max_score": c.max_score,
                "max_er": c.max_er,
                "accel_mode": c.accel_mode,
                "accel_thresh": c.accel_extreme_thresh,
                "n": ov["n"],
                "wr_pct": ov["wr_pct"],
                "net_pnl": ov["net_pnl"],
                "pf": ov["pf"],
                "expectancy": ov["expectancy"],
                "closed_dd": ov["closed_dd"],
                "mtm_dd_pct": ov["mtm_dd_pct"],
                "buy_pnl": buy_s["net_pnl"],
                "sell_pnl": sell_s["net_pnl"],
                "P1_pnl": p1["net_pnl"],
                "P2_pnl": p2["net_pnl"],
                "P3_pnl": p3["net_pnl"],
            })
        if c.group != "sensitivity":
            hyp_results.append(entry)

    # BH-FDR on non-sensitivity hypotheses vs H0
    pvals = [r["mw_p_vs_h0"] for r in hyp_results[1:]]
    qs = _bh_fdr(pvals)
    hyp_results[0]["mw_q_fdr_vs_h0"] = 1.0
    for r, qv in zip(hyp_results[1:], qs):
        r["mw_q_fdr_vs_h0"] = qv

    # Multi-window summary (W1, W2, W3)
    w1_map = {pf.cfg.name: pf for pf in w1_pfs}
    w2_map = {pf.cfg.name: pf for pf in w2_pfs}
    for name in [c.name for c in mw_configs]:
        stat_val["multi_window"][name] = {}
        for wid, mdict in [("W1", w1_map), ("W2", w2_map), ("W3", w3_map)]:
            pf = mdict[name]
            stat_val["multi_window"][name][wid] = {
                "overall": _stats_block(pf.trades, pf.mtm, pf.equity),
                "buy": _stats_block([t for t in pf.trades if t["direction"] == "BUY"]),
                "sell": _stats_block([t for t in pf.trades if t["direction"] == "SELL"]),
            }
        stat_val["outlier_sensitivity"][name] = _outlier_check(w3_map[name].trades)

    (AUDIT_DIR / "task10_hypothesis_results.json").write_text(json.dumps(hyp_results, indent=2), encoding="utf-8")
    pd.DataFrame(sens_rows).to_csv(AUDIT_DIR / "task10_parameter_sensitivity.csv", index=False)
    (AUDIT_DIR / "task10_statistical_validation.json").write_text(json.dumps(stat_val, indent=2), encoding="utf-8")

    flat = []
    for r in hyp_results:
        o = r["overall"]
        flat.append({
            "hypothesis": r["hypothesis"],
            "group": r["group"],
            "n": o["n"], "ret": r["retained"], "rem": r["removed"], "new": r["newly_generated"],
            "wr_pct": o["wr_pct"], "gross_pnl": o["gross_pnl"], "net_pnl": o["net_pnl"], "pf": o["pf"], "exp": o["expectancy"],
            "avg_w": o["avg_win"], "avg_l": o["avg_loss"],
            "closed_dd": o["closed_dd"], "mtm_dd_pct": o["mtm_dd_pct"],
            "buy_n": r["buy"]["n"], "buy_pnl": r["buy"]["net_pnl"], "buy_pf": r["buy"]["pf"],
            "sell_n": r["sell"]["n"], "sell_pnl": r["sell"]["net_pnl"], "sell_pf": r["sell"]["pf"],
            "P1": r["subperiods"]["P1"]["net_pnl"],
            "P2": r["subperiods"]["P2"]["net_pnl"],
            "P3": r["subperiods"]["P3"]["net_pnl"],
            "mw_p": r["mw_p_vs_h0"], "mw_q": r["mw_q_fdr_vs_h0"],
        })
    flat_df = pd.DataFrame(flat)
    flat_df.to_csv(AUDIT_DIR / "task10_hypothesis_results.csv", index=False)
    print("\n[TASK 10 SIMULATION & VALIDATION COMPLETE]")
    print("Reconciliation:", json.dumps(recon, indent=2))
    print("\nHypothesis Comparison (W3):")
    print(flat_df.to_string(index=False))
    print("\nParameter Sensitivity (W3):")
    print(pd.DataFrame(sens_rows).to_string(index=False))


if __name__ == "__main__":
    main()
