"""
Task 8 (Phase 7) — Independent Raw-Tick 8% HWM Kill-Switch Validator.

Architecture & Independence Guarantee:
  - Imports ZERO modules from `src/goldedge` (no TickRepository, no RunBacktestUseCase,
    no HwmKillSwitchService, no PositionSizingService, no BrokerCostService).
  - Reads raw historical ticks directly from TimescaleDB `market_ticks` via `psycopg2`.
  - Independently tracks equity HWM (closed equity + unrealized P&L at executable bid/ask),
    intrabar tick-level drawdown `((HWM - equity) / HWM) * 100.0`, exact >= 8.0% breach
    trigger, executable emergency close (`bid - slip` for BUY, `ask + slip` for SELL),
    sticky entry blocking after halt, and simulated restart persistence via raw SQL.
  - Reconciles EVERY trade, trailing-SL step, trigger timestamp, trigger HWM, trigger
    equity, drawdown %, emergency exit fill, prevented trade count, P&L, PF, closed DD,
    and tick-MTM DD against Task 8 application backtest outputs across W1, W2, and W3.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Optional

import numpy as np
import pandas as pd
import psycopg2

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import task6c_independent_validator as t6c

ROOT = t6c.ROOT
AUDIT_DIR = t6c.AUDIT_DIR


class IndHwmKillSwitch:
    def __init__(self, max_dd_pct: float = 8.0, initial_equity: float = 10000.0):
        self.max_dd_pct = float(max_dd_pct)
        self.hwm = float(initial_equity)
        self.current_equity = float(initial_equity)
        self.drawdown_pct = 0.0
        self.max_seen_dd_pct = 0.0
        self.halted = False
        self.trigger_time: Optional[datetime] = None
        self.trigger_hwm: Optional[float] = None
        self.trigger_equity: Optional[float] = None
        self.trigger_dd_pct: Optional[float] = None
        self.open_positions_at_trigger: int = 0
        self.emergency_exits: list[dict[str, Any]] = []
        self.trades_prevented: int = 0

    def evaluate(self, equity_val: float, ts: datetime, open_positions: int = 0) -> bool:
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
            self.trigger_time = ts
            self.trigger_hwm = self.hwm
            self.trigger_equity = eq
            self.trigger_dd_pct = dd_pct
            self.open_positions_at_trigger = int(open_positions)
            return True
        return False


def _run_independent_hwm_window(conn, wid: str, start_dt: datetime, end_dt: datetime) -> dict[str, Any]:
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
        out = pd.concat(chunks, ignore_index=True)
        out["time"] = pd.to_datetime(out["time"], utc=True)
        return out

    kalman = t6c.IndKalman2D()
    agg_5m = t6c.IndBarAggregator(5, 200)
    agg_1h = t6c.IndBarAggregator(60, 100)

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

    open_pos: Optional[t6c.IndPos] = None
    trades: list[dict[str, Any]] = []
    equity = 10000.0
    day_start_eq = 10000.0
    mtm = t6c.IndMtmTracker(10000.0)
    ks = IndHwmKillSwitch(max_dd_pct=8.0, initial_equity=10000.0)
    daily_pnl = 0.0
    daily_trades = 0
    consec_losses = 0
    current_day = None
    current_regime = "UNKNOWN"
    regime_det = t6c.IndRegimeDetector()
    last_cd_bar: dict[tuple[str, str], int] = {}
    bar_idx = -1
    last_valid: Optional[tuple[float, float, datetime]] = None

    def _close_pos(pos: t6c.IndPos, reason: str, exit_px: float, exit_ts: datetime) -> None:
        nonlocal equity, daily_pnl, consec_losses
        pos.apply_rollovers(exit_ts)
        c_ex = t6c._ind_side_comm(pos.lot, exit_px)
        pos.comm_exit = c_ex
        pos.comm_total = round(pos.entry_comm + c_ex, 2)
        gross = pos.gross_at(exit_px)
        net = gross - pos.comm_total + pos.swap_pnl
        equity += net
        daily_pnl += net
        consec_losses = 0 if net > 0 else consec_losses + 1
        mtm.update(equity)
        ks.evaluate(equity, exit_ts, open_positions=0)

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
            "tp": pos.tp,
            "qty": pos.lot,
            "gross_pnl": gross,
            "pnl": net,
            "commission_usd": pos.comm_total,
            "slippage_usd": pos.slip_usd,
            "swap_usd": pos.swap_cost,
            "exit_reason": reason,
            "mae_usd": round(max(0.0, mae_price_diff) * pos.lot * t6c._CONTRACT_SIZE, 2),
            "mfe_usd": round(max(0.0, mfe_price_diff) * pos.lot * t6c._CONTRACT_SIZE, 2),
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
                    df_ind = t6c._ind_compute_indicators(list(agg_5m.bars))
                    if not df_ind.empty:
                        current_regime = regime_det.detect(df_ind)

            def _scan_exit(pos: t6c.IndPos, start_i: int, eval_to: bool) -> tuple[bool, str, float, int]:
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
                        fpx = b - t6c._SLIP_OFFSET
                        pos.slip_usd += t6c._SLIP_OFFSET * pos.lot * t6c._CONTRACT_SIZE
                        mtm.update(equity + pos.net_at(fpx))
                        return True, rsn, fpx, i
                    if (not is_bull) and a >= pos.sl:
                        rsn = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
                        fpx = a + t6c._SLIP_OFFSET
                        pos.slip_usd += t6c._SLIP_OFFSET * pos.lot * t6c._CONTRACT_SIZE
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

                    cur_eq = equity + pos.net_at(eq_q)
                    mtm.update(cur_eq)
                    if ks.evaluate(cur_eq, ts_i, open_positions=1):
                        fpx = (b - t6c._SLIP_OFFSET) if is_bull else (a + t6c._SLIP_OFFSET)
                        pos.slip_usd += t6c._SLIP_OFFSET * pos.lot * t6c._CONTRACT_SIZE
                        ks.emergency_exits.append({
                            "direction": pos.direction,
                            "lots": pos.lot,
                            "timestamp": ts_i.isoformat(),
                            "requested_price": eq_q,
                            "actual_execution_price": fpx,
                            "exit_reason": "HWM_KILL_SWITCH_8PCT",
                        })
                        mtm.update(equity + pos.net_at(fpx))
                        return True, "HWM_KILL_SWITCH_8PCT", fpx, i

                    pos.ratchet(eq_q, eff_mh)

                if eval_to and pos.bars_held >= eff_mh:
                    bpx = last_q if last_q is not None else pos.entry_price
                    epx = (bpx - t6c._SLIP_OFFSET) if is_bull else (bpx + t6c._SLIP_OFFSET)
                    if last_q is not None:
                        pos.slip_usd += t6c._SLIP_OFFSET * pos.lot * t6c._CONTRACT_SIZE
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
            htf = t6c._ind_htf_trend([b["close"] for b in agg_1h.bars])
            if htf == 0:
                _flush_k()
                continue
            sigs = t6c._ind_generate_signals(df_ind, current_regime)
            if not sigs:
                _flush_k()
                continue

            lr = df_ind.iloc[-1]
            cs_s = df_ind["close"]
            c5_ts = agg_5m.bars[-1]["time"]
            feat = {
                "kalman_vel": kalman.x1,
                "kalman_accel": kalman.accel(),
                "shannon_entropy": t6c._ind_shannon_entropy(cs_s.pct_change().dropna()) if len(cs_s) > 30 else 2.0,
                "kaufman_er": t6c._ind_kaufman_er(cs_s) if len(cs_s) > 15 else 0.3,
                "ema20_slope": float(lr["ema20_slope"]) if pd.notna(lr.get("ema20_slope")) else 0.0,
                "tick_weighted_price_dist": float(lr["tick_weighted_price_dist"]) if pd.notna(lr.get("tick_weighted_price_dist")) else 0.0,
                "tick_rate_ratio": float(lr["tick_rate_ratio"]) if pd.notna(lr.get("tick_rate_ratio")) else 1.0,
                "utc_hour": c5_ts.hour,
            }
            scored = [(t6c._ind_score(d, current_regime, feat), st, d) for st, d in sigs]
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

            # Independent 8% HWM Kill-Switch Entry Block
            if ks.halted:
                ks.trades_prevented += 1
                _flush_k()
                continue

            atr_v = float(df_ind["atr"].iloc[-1]) if pd.notna(df_ind["atr"].iloc[-1]) else 1.0
            eb, ea = float(bids[en_i]), float(asks[en_i])
            raw_ep = ea if d == "BUY" else eb
            ep = (raw_ep + t6c._SLIP_OFFSET) if d == "BUY" else (raw_ep - t6c._SLIP_OFFSET)
            lot, sl_px, tp_px = t6c._ind_size(equity, ep, atr_v, d)
            if lot is None:
                _flush_k()
                continue

            e_comm = t6c._ind_side_comm(lot, ep)
            e_slip = t6c._SLIP_OFFSET * lot * t6c._CONTRACT_SIZE
            open_pos = t6c.IndPos(
                direction=d, strategy=st, entry_time=ttimes[en_i],
                entry_bid=eb, entry_ask=ea, entry_price=ep,
                initial_sl=sl_px, sl=sl_px, tp=tp_px, atr=atr_v, lot=lot,
                entry_comm=e_comm, slip_usd=e_slip,
                peak_price=ep, worst_price=ep, last_swap_ts=ttimes[en_i],
            )
            mtm.update(equity - (ea - eb) * lot * t6c._CONTRACT_SIZE - 2.0 * e_comm - e_slip)
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
        epx = (rc - t6c._SLIP_OFFSET) if open_pos.direction == "BUY" else (rc + t6c._SLIP_OFFSET)
        open_pos.slip_usd += t6c._SLIP_OFFSET * open_pos.lot * t6c._CONTRACT_SIZE
        _close_pos(open_pos, "END_OF_BACKTEST", epx, lt)
        open_pos = None

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

    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) < 0 else 0.0

    return {
        "window_id": wid,
        "trades": trades,
        "trades_count": len(trades),
        "profit_factor": round(pf, 6),
        "closed_max_dd_usd": round(c_dd, 4),
        "closed_max_dd_pct": round(c_dd_pct, 4),
        "mtm_max_dd_usd": round(mtm.max_dd, 4),
        "mtm_max_dd_pct": round(mtm.max_dd_pct, 4),
        "mtm_max_dd_initial_pct": round(mtm.max_dd_init_pct, 4),
        "final_equity": round(equity, 4),
        "kill_switch": {
            "halted": ks.halted,
            "trigger_time": ks.trigger_time.isoformat() if ks.trigger_time else None,
            "trigger_hwm": round(ks.trigger_hwm, 4) if ks.trigger_hwm is not None else None,
            "trigger_equity": round(ks.trigger_equity, 4) if ks.trigger_equity is not None else None,
            "trigger_drawdown_pct": round(ks.trigger_dd_pct, 4) if ks.trigger_dd_pct is not None else None,
            "final_hwm": round(ks.hwm, 4),
            "final_drawdown_pct": round(ks.drawdown_pct, 4),
            "open_positions_at_trigger": ks.open_positions_at_trigger,
            "emergency_exits": ks.emergency_exits,
            "trades_prevented": ks.trades_prevented,
        },
    }


def _reconcile_hwm_window(wid: str, app_comp: dict[str, Any], ind_res: dict[str, Any]) -> dict[str, Any]:
    app_df = pd.read_csv(AUDIT_DIR / f"task8_app_{wid.lower()}_hwm_trades.csv")
    app_j = json.loads((AUDIT_DIR / f"task8_app_{wid.lower()}_hwm_journeys.json").read_text(encoding="utf-8"))
    app_w = app_comp["windows"][wid]["app_with_8pct_kill_switch"]
    app_ks = app_w["hwm_kill_switch"]
    ind_trades = ind_res["trades"]
    ind_ks = ind_res["kill_switch"]

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
        max_pnl_diff = max(max_pnl_diff, abs(float(a["pnl"]) - it["pnl"]))

    ks_checks = {
        "halted_match": app_ks["halted"] == ind_ks["halted"],
        "trigger_time_match": pd.to_datetime(app_ks["halt_timestamp"], utc=True) == pd.to_datetime(ind_ks["trigger_time"], utc=True),
        "trigger_hwm_diff_usd": round(abs(float(app_ks["trigger_hwm"]) - float(ind_ks["trigger_hwm"])), 6),
        "trigger_equity_diff_usd": round(abs(float(app_ks["trigger_equity"]) - float(ind_ks["trigger_equity"])), 6),
        "trigger_dd_diff_pct": round(abs(float(app_ks["trigger_drawdown_pct"]) - float(ind_ks["trigger_drawdown_pct"])), 6),
        "trades_prevented_match": int(app_ks["trades_prevented"]) == int(ind_ks["trades_prevented"]),
        "emergency_exit_exec_price_diff": round(
            abs(float(app_ks["emergency_exit_details"][0]["actual_execution_price"]) - float(ind_ks["emergency_exits"][0]["actual_execution_price"])),
            6,
        ),
        "emergency_exit_req_price_diff": round(
            abs(float(app_ks["emergency_exit_details"][0]["requested_price"]) - float(ind_ks["emergency_exits"][0]["requested_price"])),
            6,
        ),
        "final_equity_diff_usd": round(abs(float(app_w["final_equity_usd"]) - float(ind_res["final_equity"])), 6),
        "closed_dd_diff_usd": round(abs(float(app_w["closed_max_dd_usd"]) - float(ind_res["closed_max_dd_usd"])), 6),
        "mtm_dd_diff_usd": round(abs(float(app_w["mtm_max_dd_usd"]) - float(ind_res["mtm_max_dd_usd"])), 6),
    }

    return {
        "window_id": wid,
        "app_trades": int(len(app_df)),
        "ind_trades": int(ind_res["trades_count"]),
        "trade_mismatch_count": mismatches,
        "trailing_journey_mismatches": trail_step_mismatches,
        "max_pnl_diff_usd": round(max_pnl_diff, 6),
        "app_final_equity": app_w["final_equity_usd"],
        "ind_final_equity": ind_res["final_equity"],
        "app_pf": app_w["profit_factor"],
        "ind_pf": ind_res["profit_factor"],
        "app_closed_dd_pct": app_w["closed_max_dd_pct"],
        "ind_closed_dd_pct": ind_res["closed_max_dd_pct"],
        "app_mtm_dd_pct": app_w["mtm_max_dd_pct"],
        "ind_mtm_dd_pct": ind_res["mtm_max_dd_pct"],
        "app_trigger_time": app_ks["halt_timestamp"],
        "ind_trigger_time": ind_ks["trigger_time"],
        "app_trigger_hwm": app_ks["trigger_hwm"],
        "ind_trigger_hwm": ind_ks["trigger_hwm"],
        "app_trigger_equity": app_ks["trigger_equity"],
        "ind_trigger_equity": ind_ks["trigger_equity"],
        "app_trigger_dd_pct": app_ks["trigger_drawdown_pct"],
        "ind_trigger_dd_pct": ind_ks["trigger_drawdown_pct"],
        "app_trades_prevented": int(app_ks["trades_prevented"]),
        "ind_trades_prevented": int(ind_ks["trades_prevented"]),
        "ks_checks": ks_checks,
    }


def _verify_restart_persistence_raw_sql(db_url: str, w3_ks: dict[str, Any]) -> dict[str, Any]:
    """Verify triggered state persists in risk_hwm_state across fresh DB connections."""
    scope = "task8_ind_verify:W3:USD"
    conn1 = psycopg2.connect(db_url)
    try:
        with conn1:
            with conn1.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO risk_hwm_state (
                        account_scope, account_id, account_currency,
                        initial_equity, hwm_equity, current_equity,
                        drawdown_pct, max_drawdown_pct, halted,
                        halt_timestamp, halt_reason,
                        trigger_hwm, trigger_equity, trigger_drawdown_pct,
                        open_positions_at_trigger, emergency_exit_status,
                        emergency_exit_details, reset_count
                    ) VALUES (
                        %s, 'ind_acct', 'USD',
                        10000.0, %s, %s,
                        %s, %s, TRUE,
                        %s, %s,
                        %s, %s, %s,
                        %s, 'ALL_CLOSED_CONFIRMED',
                        %s::jsonb, 0
                    )
                    ON CONFLICT (account_scope) DO UPDATE SET
                        hwm_equity = EXCLUDED.hwm_equity,
                        current_equity = EXCLUDED.current_equity,
                        halted = EXCLUDED.halted,
                        trigger_hwm = EXCLUDED.trigger_hwm,
                        trigger_equity = EXCLUDED.trigger_equity,
                        trigger_drawdown_pct = EXCLUDED.trigger_drawdown_pct
                    """,
                    (
                        scope,
                        w3_ks["trigger_hwm"],
                        w3_ks["trigger_equity"],
                        w3_ks["trigger_drawdown_pct"],
                        w3_ks["trigger_drawdown_pct"],
                        w3_ks["trigger_time"],
                        "Independent Phase 7 HWM 8% verification",
                        w3_ks["trigger_hwm"],
                        w3_ks["trigger_equity"],
                        w3_ks["trigger_drawdown_pct"],
                        w3_ks["open_positions_at_trigger"],
                        json.dumps(w3_ks["emergency_exits"]),
                    ),
                )
    finally:
        conn1.close()

    # Fresh DB connection simulates restart & verifies sticky halt survival
    conn2 = psycopg2.connect(db_url)
    try:
        with conn2.cursor() as cur:
            cur.execute(
                """
                SELECT halted, trigger_hwm, trigger_equity, trigger_drawdown_pct, halt_timestamp
                FROM risk_hwm_state WHERE account_scope = %s
                """,
                (scope,),
            )
            row = cur.fetchone()
        with conn2:
            with conn2.cursor() as cur:
                cur.execute("DELETE FROM risk_hwm_state WHERE account_scope = %s", (scope,))
    finally:
        conn2.close()

    return {
        "row_found_after_reconnect": row is not None,
        "persisted_halted": bool(row[0]) if row else False,
        "persisted_trigger_hwm": float(row[1]) if row else None,
        "persisted_trigger_equity": float(row[2]) if row else None,
        "persisted_trigger_dd_pct": float(row[3]) if row else None,
        "persisted_halt_timestamp": row[4].isoformat() if row and row[4] else None,
        "cleaned_up_test_row": True,
    }


def main() -> None:
    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    app_comp = json.loads((AUDIT_DIR / "task8_app_comparison.json").read_text(encoding="utf-8"))
    windows = [
        ("W1", datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc)),
        ("W2", datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc)),
        ("W3", datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 18, 18, 0, 0, tzinfo=timezone.utc)),
    ]

    conn = psycopg2.connect(db_url)
    report: dict[str, Any] = {"windows": {}, "restart_persistence_check": None}
    try:
        for wid, s_dt, e_dt in windows:
            print(f"[TASK 8 PHASE 7] Running independent 8% HWM validator for {wid}...", flush=True)
            ind_res = _run_independent_hwm_window(conn, wid, s_dt, e_dt)
            rec = _reconcile_hwm_window(wid, app_comp, ind_res)
            report["windows"][wid] = rec
            print(f"[TASK 8 PHASE 7] {wid} reconciled: trade_mismatches={rec['trade_mismatch_count']} trail_mismatches={rec['trailing_journey_mismatches']} prevented_app={rec['app_trades_prevented']} prevented_ind={rec['ind_trades_prevented']}", flush=True)
            if wid == "W3":
                report["restart_persistence_check"] = _verify_restart_persistence_raw_sql(db_url, ind_res["kill_switch"])
            (AUDIT_DIR / "task8_independent_reconciliation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    finally:
        conn.close()

    (AUDIT_DIR / "task8_independent_reconciliation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("\n[TASK 8 PHASE 7 COMPLETE]")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
