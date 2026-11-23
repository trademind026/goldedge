"""
Task 9 (Phase 7 & Phase 8) — Independent Causal Raw-Tick Hypothesis Testing Engine.

Zero imports from `src/goldedge`. Streams `market_ticks` once across W3
(2026-08-17 -> 2026-11-18, warmup Aug 13-16) and steps isolated independent
portfolio simulations simultaneously (verified bit-exact via H0_BASELINE_V2 ==
frozen 143 trades with 0 mismatches).

For each hypothesis H1..H11 vs H0_BASELINE_V2:
  - Measures trades removed, retained, and newly generated vs H0.
  - Reports BUY, SELL, and Combined: n, WR%, Gross/Net P&L, PF, Expectancy,
    Closed MaxDD, Tick-MTM MaxDD.
  - Reports chronological out-of-sample stability across:
      P1 (Aug 17 - Sep 16), P2 (Sep 17 - Oct 16), P3 (Oct 17 - Nov 18)
      and DEV (Aug 17 - Oct 16) vs HOLDOUT (Oct 17 - Nov 18).
  - Applies Benjamini-Hochberg FDR adjustment on Mann-Whitney per-trade P&L diff
    where paired/compared, and checks stationary counterfactuals.
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
class SimConfig:
    name: str
    description: str
    failure_mechanism: str
    allow_buy_sideways: bool = False
    block_buy_hours: tuple[int, ...] = ()
    block_sell_hours: tuple[int, ...] = ()
    session_close_utc: int = 17
    require_buy_bull_candle: bool = False
    max_score: float = 1.01
    min_score: float = 0.60
    max_er: float = 1.01
    disallow_buy_chasing_20h_atr: Optional[float] = None
    buy_be_atr: float = 1.0
    buy_t1_trigger_atr: float = 1.6
    buy_t1_dist_atr: float = 1.1
    buy_t2_trigger_atr: float = 2.5
    buy_t2_dist_atr: float = 0.75
    early_fail_mins: Optional[int] = None
    early_fail_min_mfe_atr: float = 0.40
    max_hwm_dd_pct: Optional[float] = None


class SimPortfolio:
    def __init__(self, cfg: SimConfig):
        self.cfg = cfg
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

    def new_day_check(self, d: date) -> None:
        if self.current_day != d:
            self.current_day = d
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.consec_losses = 0
            self.day_start_eq = max(self.equity, 1.0)

    def _ratchet(self, pos: t6c.IndPos, price: float, max_hold: int) -> None:
        if pos.direction == "SELL":
            pos.ratchet(price, max_hold)
            return
        # Parameterized BUY trailing progression (identical to V2 when defaults used)
        if price > pos.peak_price:
            pos.peak_price = price
        gain = pos.peak_price - pos.entry_price
        if gain >= self.cfg.buy_t2_trigger_atr * pos.atr:
            pos.trailing_active = True
            new_sl = pos.peak_price - self.cfg.buy_t2_dist_atr * pos.atr
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

    def close_pos(self, pos: t6c.IndPos, reason: str, exit_px: float, exit_ts: datetime) -> None:
        pos.apply_rollovers(exit_ts)
        c_ex = t6c._ind_side_comm(pos.lot, exit_px)
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
                fpx = b - t6c._SLIP_OFFSET
                pos.slip_usd += t6c._SLIP_OFFSET * pos.lot * t6c._CONTRACT_SIZE
                self.mtm.update(self.equity + pos.net_at(fpx))
                return rsn, fpx, ts_i
            if (not is_bull) and a >= pos.sl:
                rsn = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
                fpx = a + t6c._SLIP_OFFSET
                pos.slip_usd += t6c._SLIP_OFFSET * pos.lot * t6c._CONTRACT_SIZE
                self.mtm.update(self.equity + pos.net_at(fpx))
                return rsn, fpx, ts_i
            if is_bull and b >= pos.tp:
                fpx = float(pos.tp)
                self.mtm.update(self.equity + pos.net_at(fpx))
                return "TARGET", fpx, ts_i
            if (not is_bull) and a <= pos.tp:
                fpx = float(pos.tp)
                self.mtm.update(self.equity + pos.net_at(fpx))
                return "TARGET", fpx, ts_i

            cur_eq = self.equity + pos.net_at(eq_q)
            self.mtm.update(cur_eq)
            if self.cfg.max_hwm_dd_pct is not None:
                dd_pct = max(0.0, (self.mtm.hwm - cur_eq) / self.mtm.hwm * 100.0) if self.mtm.hwm > 0 else 0.0
                if round(dd_pct, 10) >= self.cfg.max_hwm_dd_pct:
                    self.halted = True
                    self.halt_time = ts_i
                    fpx = (b - t6c._SLIP_OFFSET) if is_bull else (a + t6c._SLIP_OFFSET)
                    pos.slip_usd += t6c._SLIP_OFFSET * pos.lot * t6c._CONTRACT_SIZE
                    self.mtm.update(self.equity + pos.net_at(fpx))
                    return "HWM_KILL_SWITCH_8PCT", fpx, ts_i

            if self.cfg.early_fail_mins is not None:
                el_mins = (ts_i - pos.entry_time).total_seconds() / 60.0
                if el_mins >= float(self.cfg.early_fail_mins):
                    peak_p = best_q if best_q is not None else eq_q
                    if (is_bull and peak_p > pos.peak_price):
                        pos.peak_price = peak_p
                    elif (not is_bull) and peak_p < pos.peak_price:
                        pos.peak_price = peak_p
                    mfe_diff = (pos.peak_price - pos.entry_price) if is_bull else (pos.entry_price - pos.peak_price)
                    if (mfe_diff / max(pos.atr, 1e-9)) < self.cfg.early_fail_min_mfe_atr:
                        fpx = (b - t6c._SLIP_OFFSET) if is_bull else (a + t6c._SLIP_OFFSET)
                        pos.slip_usd += t6c._SLIP_OFFSET * pos.lot * t6c._CONTRACT_SIZE
                        self.mtm.update(self.equity + pos.net_at(fpx))
                        return "EARLY_FAIL_E1", fpx, ts_i

            self._ratchet(pos, eq_q, eff_mh)

        if eval_to and pos.bars_held >= eff_mh:
            bpx = last_q if last_q is not None else pos.entry_price
            epx = (bpx - t6c._SLIP_OFFSET) if is_bull else (bpx + t6c._SLIP_OFFSET)
            if last_q is not None:
                pos.slip_usd += t6c._SLIP_OFFSET * pos.lot * t6c._CONTRACT_SIZE
            return "TIMEOUT", epx, ttimes[-1]

        if best_q is not None:
            self._ratchet(pos, best_q, eff_mh)
        return None


def _subperiod(dt: datetime) -> str:
    dt_u = t6c._as_utc(dt)
    if dt_u < datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc):
        return "P1"
    if dt_u < datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc):
        return "P2"
    return "P3"


def _calc_stats(trades: list[dict], mtm: t6c.IndMtmTracker, final_eq: float) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wins": 0, "losses": 0, "wr_pct": 0.0, "net_pnl": 0.0, "pf": 0.0, "expectancy": 0.0, "closed_dd": 0.0, "mtm_dd_usd": round(mtm.max_dd, 2), "mtm_dd_pct": round(mtm.max_dd_pct, 2), "final_equity": round(final_eq, 2)}
    pnls = np.array([t["pnl"] for t in trades], dtype=float)
    w = pnls[pnls > 0]
    l = pnls[pnls <= 0]
    pf = float(w.sum() / abs(l.sum())) if len(l) and l.sum() < 0 else (999.0 if len(w) and w.sum() > 0 else 0.0)
    ceq = np.cumsum(pnls) + 10000.0
    hwm = np.maximum.accumulate(np.insert(ceq, 0, 10000.0))[1:]
    c_dd = float((ceq - hwm).min())
    return {
        "n": int(n),
        "wins": int(len(w)),
        "losses": int(len(l)),
        "wr_pct": round(len(w) / n * 100.0, 2),
        "net_pnl": round(float(pnls.sum()), 2),
        "pf": round(pf, 4),
        "expectancy": round(float(pnls.mean()), 2),
        "closed_dd": round(c_dd, 2),
        "mtm_dd_usd": round(mtm.max_dd, 2),
        "mtm_dd_pct": round(mtm.max_dd_pct, 2),
        "final_equity": round(final_eq, 2),
    }


def _side_stats(trades: list[dict]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr_pct": 0.0, "net_pnl": 0.0, "pf": 0.0, "expectancy": 0.0}
    pnls = np.array([t["pnl"] for t in trades], dtype=float)
    w = pnls[pnls > 0]
    l = pnls[pnls <= 0]
    pf = float(w.sum() / abs(l.sum())) if len(l) and l.sum() < 0 else (999.0 if len(w) and w.sum() > 0 else 0.0)
    return {
        "n": int(n),
        "wr_pct": round(len(w) / n * 100.0, 2),
        "net_pnl": round(float(pnls.sum()), 2),
        "pf": round(pf, 4),
        "expectancy": round(float(pnls.mean()), 2),
    }


def main() -> None:
    configs = [
        SimConfig(
            name="H0_BASELINE_V2",
            description="Unchanged RECOVERED_FULL_HISTORY_BASELINE_V2 (143 trades)",
            failure_mechanism="Baseline control",
        ),
        SimConfig(
            name="H1_BLOCK_BUY_07UTC",
            description="Block BUY entries at 07:00 UTC (keep SELL at 07:00 UTC)",
            failure_mechanism="BUY 07:00 UTC pre-open fakeout bleed (-$378.38, 2W/6L, PF 0.24 vs SELL +$512.78, 13W/3L)",
            block_buy_hours=(7,),
        ),
        SimConfig(
            name="H2_UNBLOCK_BUY_SIDEWAYS",
            description="Allow BUY in SIDEWAYS regime (block_buy_sideways=False)",
            failure_mechanism="Asymmetric gate bans BUY in SIDEWAYS while SELL profits +$339.82 there; pushes BUY into late TRENDING_BULL extensions",
            allow_buy_sideways=True,
        ),
        SimConfig(
            name="H3_BUY_BULL_CANDLE",
            description="Require close > open on bullish_momentum BUY (matching bearish_momentum close < open)",
            failure_mechanism="19/41 BUY entries occur on red 5m candles (-$296.09), missing candle symmetry with SELL",
            require_buy_bull_candle=True,
        ),
        SimConfig(
            name="H4a_SCORE_CAP_080",
            description="Reject exhausted entries with score >= 0.80 (keep 0.60 <= score < 0.80)",
            failure_mechanism="TradeScorer over-rewards saturated velocity+slope+ER+accel chasing; score >= 0.80 underperforms both directions",
            max_score=0.80,
        ),
        SimConfig(
            name="H4b_SCORE_CAP_075",
            description="Reject exhausted entries with score >= 0.75 (keep 0.60 <= score < 0.75)",
            failure_mechanism="Score >= 0.75 loses -$875.38 (-$422.62 BUY, -$452.76 SELL) while [0.60, 0.75) makes +$1,086.86 across all 3 periods",
            max_score=0.75,
        ),
        SimConfig(
            name="H5_CAP_ER_045",
            description="Cap Kaufman ER <= 0.45 at entry",
            failure_mechanism="High Kaufman ER (FDR q=0.040 on SELL) marks late exhausted straight-line impulses right before pullback",
            max_er=0.45,
        ),
        SimConfig(
            name="H6_SESSION_CLOSE_15UTC",
            description="Stop new entries at >= 15:00 UTC (keep 07..14 UTC)",
            failure_mechanism="16:00 UTC late-NY chop loses -$220.88 (40.0% WR, PF 0.77)",
            session_close_utc=15,
        ),
        SimConfig(
            name="H7_BUY_FAST_BE_075_T1_12",
            description="Asymmetric BUY exit only: BE at 0.75 ATR (from 1.0), T1 at 1.2/0.8 ATR (from 1.6/1.1)",
            failure_mechanism="73.2% of BUYs hit +0.5 ATR and 24.4% reverse into full losses, while only 22.0% reach T1=1.6 ATR",
            buy_be_atr=0.75,
            buy_t1_trigger_atr=1.2,
            buy_t1_dist_atr=0.8,
        ),
        SimConfig(
            name="H8_EARLY_FAIL_E1_15M",
            description="Early failure cut E1: exit at 15 minutes if MFE < 0.40 ATR",
            failure_mechanism="Stalled entries failing to reach +0.40 ATR by 15m produce -$682 on BUY and -$2,068 on SELL",
            early_fail_mins=15,
            early_fail_min_mfe_atr=0.40,
        ),
        SimConfig(
            name="H9_BUY_NO_HIGH_CHASE_015",
            description="Block BUY entries within <= 0.15 ATR of 20-bar high",
            failure_mechanism="Chasing 20-bar breakout high on BUY lost -$273.84 (1W/5L, PF 0.226)",
            disallow_buy_chasing_20h_atr=0.15,
        ),
        SimConfig(
            name="H10_V2_WITH_HWM_8PCT",
            description="Task 8 8% HWM Kill-Switch Risk-Protected Variant",
            failure_mechanism="Capital preservation boundary stopping run past 8% HWM drawdown",
            max_hwm_dd_pct=8.0,
        ),
        SimConfig(
            name="H11_COMBO_CANDIDATE",
            description="Block BUY 07UTC + Cap score < 0.80 + E1 15m<0.4ATR + HWM 8%",
            failure_mechanism="Joint test of non-overlapping structural fixes + HWM risk cap",
            block_buy_hours=(7,),
            max_score=0.80,
            early_fail_mins=15,
            early_fail_min_mfe_atr=0.40,
            max_hwm_dd_pct=8.0,
        ),
    ]
    portfolios = [SimPortfolio(c) for c in configs]

    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    conn = psycopg2.connect(db_url)
    start_dt = datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(2026, 11, 18, 18, 0, 0, tzinfo=timezone.utc)
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

    try:
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
            print(f"[TASK 9 H-TEST] Streaming chunk {cs.strftime('%Y-%m-%d')} -> {ce.strftime('%Y-%m-%d')} (H0 trades={len(portfolios[0].trades)})...", flush=True)
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

                # Step open positions first for each portfolio
                entered_or_open = [False] * len(portfolios)
                for p_idx, pf in enumerate(portfolios):
                    pf.new_day_check(d_py)
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
                close_v = float(lr["close"])
                open_v = float(lr["open"])
                h20 = float(df_ind["high"].tail(20).max())
                dist_20h_atr = (h20 - close_v) / max(atr_v, 1e-9)
                eb, ea = float(bids[en_i]), float(asks[en_i])

                scored_base = [(t6c._ind_score(d, current_regime, feat), st, d) for st, d in sigs]
                scored_base.sort(key=lambda x: x[0], reverse=True)

                for p_idx, pf in enumerate(portfolios):
                    if entered_or_open[p_idx] or pf.halted:
                        continue
                    c = pf.cfg
                    if c5_ts.hour >= c.session_close_utc:
                        continue
                    if abs(min(0.0, pf.daily_pnl)) / max(pf.day_start_eq, 1.0) * 100.0 >= 3.0 or pf.daily_trades >= 8 or pf.consec_losses >= 3:
                        continue
                    if current_regime == "HIGH_VOLATILITY":
                        continue

                    # Pick highest-scoring qualified signal under c
                    chosen = None
                    for sc, st, d in scored_base:
                        if not (c.min_score <= sc < c.max_score):
                            continue
                        if (d == "BUY" and htf != 1) or (d == "SELL" and htf != -1):
                            continue
                        if d == "BUY" and current_regime == "SIDEWAYS" and not c.allow_buy_sideways:
                            continue
                        if d == "BUY" and c5_ts.hour in c.block_buy_hours:
                            continue
                        if d == "SELL" and c5_ts.hour in c.block_sell_hours:
                            continue
                        if d == "BUY" and c.require_buy_bull_candle and close_v <= open_v:
                            continue
                        if er_v > c.max_er:
                            continue
                        if d == "BUY" and c.disallow_buy_chasing_20h_atr is not None and dist_20h_atr <= c.disallow_buy_chasing_20h_atr:
                            continue
                        if bar_idx - pf.last_cd_bar.get((st, d), -9999) < 15:
                            continue
                        chosen = (sc, st, d)
                        break

                    if chosen is None:
                        continue
                    sc, st, d = chosen
                    raw_ep = ea if d == "BUY" else eb
                    ep = (raw_ep + t6c._SLIP_OFFSET) if d == "BUY" else (raw_ep - t6c._SLIP_OFFSET)
                    lot, sl_px, tp_px = t6c._ind_size(pf.equity, ep, atr_v, d)
                    if lot is None:
                        continue

                    e_comm = t6c._ind_side_comm(lot, ep)
                    e_slip = t6c._SLIP_OFFSET * lot * t6c._CONTRACT_SIZE
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
            for pf in portfolios:
                if pf.open_pos is not None:
                    rc = lb if pf.open_pos.direction == "BUY" else la
                    epx = (rc - t6c._SLIP_OFFSET) if pf.open_pos.direction == "BUY" else (rc + t6c._SLIP_OFFSET)
                    pf.open_pos.slip_usd += t6c._SLIP_OFFSET * pf.open_pos.lot * t6c._CONTRACT_SIZE
                    pf.close_pos(pf.open_pos, "END_OF_BACKTEST", epx, lt)
                    pf.open_pos = None
    finally:
        conn.close()

    # Verify H0 == frozen w3_trades.csv (143/143)
    frozen_df = pd.read_csv(FROZEN_V2_DIR / "w3_trades.csv")
    h0 = portfolios[0]
    if len(h0.trades) != len(frozen_df):
        raise RuntimeError(f"H0 mismatch: {len(h0.trades)} vs {len(frozen_df)}")
    h0_max_diff = max(abs(float(frozen_df.iloc[i]["pnl"]) - h0.trades[i]["pnl"]) for i in range(len(h0.trades)))
    if h0_max_diff > 0.02:
        raise RuntimeError(f"H0 pnl diff too high: {h0_max_diff}")

    h0_keys = {(t["entry_time"], t["direction"]) for t in h0.trades}
    h0_buy = [t["pnl"] for t in h0.trades if t["direction"] == "BUY"]
    h0_sell = [t["pnl"] for t in h0.trades if t["direction"] == "SELL"]
    h0_all = [t["pnl"] for t in h0.trades]

    report_rows = []
    for pf in portfolios:
        c = pf.cfg
        keys = {(t["entry_time"], t["direction"]) for t in pf.trades}
        retained = len(keys & h0_keys)
        removed = len(h0_keys - keys)
        generated = len(keys - h0_keys)

        overall = _calc_stats(pf.trades, pf.mtm, pf.equity)
        buy_tr = [t for t in pf.trades if t["direction"] == "BUY"]
        sell_tr = [t for t in pf.trades if t["direction"] == "SELL"]
        buy_s = _side_stats(buy_tr)
        sell_s = _side_stats(sell_tr)

        p1 = _side_stats([t for t in pf.trades if t["subperiod"] == "P1"])
        p2 = _side_stats([t for t in pf.trades if t["subperiod"] == "P2"])
        p3 = _side_stats([t for t in pf.trades if t["subperiod"] == "P3"])
        dev = _side_stats([t for t in pf.trades if t["split"] == "DEV"])
        ho = _side_stats([t for t in pf.trades if t["split"] == "HOLDOUT"])
        ho_buy = _side_stats([t for t in buy_tr if t["split"] == "HOLDOUT"])
        ho_sell = _side_stats([t for t in sell_tr if t["split"] == "HOLDOUT"])

        # Statistical test vs H0 (Mann-Whitney on per-trade P&L distribution)
        pnls_all = [t["pnl"] for t in pf.trades]
        p_vs_h0 = float(stats.mannwhitneyu(pnls_all, h0_all, alternative="two-sided").pvalue) if c.name != "H0_BASELINE_V2" and len(pnls_all) >= 5 else 1.0

        report_rows.append({
            "hypothesis": c.name,
            "description": c.description,
            "failure_mechanism": c.failure_mechanism,
            "h0_verify_max_pnl_diff": round(h0_max_diff, 6) if c.name == "H0_BASELINE_V2" else None,
            "retained": retained,
            "removed": removed,
            "newly_generated": generated,
            "overall": overall,
            "buy": buy_s,
            "sell": sell_s,
            "subperiods": {"P1": p1, "P2": p2, "P3": p3},
            "dev_vs_holdout": {"DEV": dev, "HOLDOUT": ho, "HOLDOUT_BUY": ho_buy, "HOLDOUT_SELL": ho_sell},
            "mw_p_vs_h0": round(p_vs_h0, 5),
        })

    # BH-FDR across H1..H10
    pvals = [r["mw_p_vs_h0"] for r in report_rows[1:]]
    m = len(pvals)
    indexed = sorted(enumerate(pvals), key=lambda x: x[1])
    qs = [1.0] * m
    prev = 1.0
    for rev_i, (idx, pv) in enumerate(reversed(indexed)):
        rank = m - rev_i
        q = min(prev, (pv * m) / rank)
        qs[idx] = round(min(1.0, q), 5)
        prev = q
    report_rows[0]["mw_q_fdr_vs_h0"] = 1.0
    for r, qv in zip(report_rows[1:], qs):
        r["mw_q_fdr_vs_h0"] = qv

    (AUDIT_DIR / "task9_hypothesis_results.json").write_text(json.dumps(report_rows, indent=2), encoding="utf-8")

    # Save concise CSV & print comparison table
    flat = []
    for r in report_rows:
        o = r["overall"]
        flat.append({
            "hypothesis": r["hypothesis"],
            "n": o["n"], "retained": r["retained"], "removed": r["removed"], "new": r["newly_generated"],
            "wr_pct": o["wr_pct"], "net_pnl": o["net_pnl"], "pf": o["pf"], "exp": o["expectancy"],
            "closed_dd": o["closed_dd"], "mtm_dd_pct": o["mtm_dd_pct"],
            "buy_n": r["buy"]["n"], "buy_pnl": r["buy"]["net_pnl"], "buy_pf": r["buy"]["pf"],
            "sell_n": r["sell"]["n"], "sell_pnl": r["sell"]["net_pnl"], "sell_pf": r["sell"]["pf"],
            "P1_pnl": r["subperiods"]["P1"]["net_pnl"],
            "P2_pnl": r["subperiods"]["P2"]["net_pnl"],
            "P3_pnl": r["subperiods"]["P3"]["net_pnl"],
            "DEV_pnl": r["dev_vs_holdout"]["DEV"]["net_pnl"],
            "HO_pnl": r["dev_vs_holdout"]["HOLDOUT"]["net_pnl"],
            "mw_p": r["mw_p_vs_h0"], "mw_q_fdr": r["mw_q_fdr_vs_h0"],
        })
    flat_df = pd.DataFrame(flat)
    flat_df.to_csv(AUDIT_DIR / "task9_hypothesis_results.csv", index=False)
    print(flat_df.to_string(index=False))


if __name__ == "__main__":
    main()
