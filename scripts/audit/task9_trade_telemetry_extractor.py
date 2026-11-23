"""
Task 9 (Phases 1 & 2) — Baseline Fingerprint Verification, Independent Raw-Tick
Reproduction, and Full Per-Trade + Candidate-Signal Telemetry Extraction (W3 V2).

Zero imports from `src/goldedge`. Reads `market_ticks` directly via `psycopg2`,
verifies frozen SHA-256 hashes, reproduces the exact 143 trades of
RECOVERED_FULL_HISTORY_BASELINE_V2 (W3: 2026-08-17 -> 2026-11-18, Aug 13-16 warmup),
reconciles 143/143 trades and trailing journeys (0 mismatches), and writes:
  - backtest_results/audit/task9_phase1_verification.json
  - backtest_results/audit/task9_trades_enriched_w3.csv (143 executed trades, 75+ features)
  - backtest_results/audit/task9_candidate_signals_w3.csv (all generated signals + gate reasons)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
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
FROZEN_V2_DIR = AUDIT_DIR / "frozen_v2_recovered"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_frozen_baselines() -> dict[str, Any]:
    manifest = json.loads((FROZEN_V2_DIR / "task6c_frozen_manifest.json").read_text(encoding="utf-8"))
    app_sum = manifest["application_summary"]
    t4 = manifest["prior_frozen_task4_preserved"]

    frozen_111 = ROOT / "backtest_results" / "frozen_111_baseline_trades.csv"
    w1_csv = FROZEN_V2_DIR / "w1_trades.csv"
    w2_csv = FROZEN_V2_DIR / "w2_trades.csv"
    w3_csv = FROZEN_V2_DIR / "w3_trades.csv"
    w3_jrn = FROZEN_V2_DIR / "w3_journeys.json"

    checks = {
        "frozen_111_csv_sha256_match": _sha256_file(frozen_111) == t4["frozen_111_baseline_trades_sha256"],
        "v2_w1_csv_sha256_match": _sha256_file(w1_csv) == app_sum["W1"]["trades_csv_sha256"],
        "v2_w2_csv_sha256_match": _sha256_file(w2_csv) == app_sum["W2"]["trades_csv_sha256"],
        "v2_w3_csv_sha256_match": _sha256_file(w3_csv) == app_sum["W3"]["trades_csv_sha256"],
        "v2_w3_journeys_sha256_match": _sha256_file(w3_jrn) == app_sum["W3"]["journeys_json_sha256"],
        "v2_w3_trade_count": int(len(pd.read_csv(w3_csv))),
    }
    task8_comp_path = AUDIT_DIR / "task8_app_comparison.json"
    task8_info = None
    if task8_comp_path.exists():
        t8 = json.loads(task8_comp_path.read_text(encoding="utf-8"))
        w3_t8 = t8["windows"]["W3"]["app_with_8pct_kill_switch"]
        task8_info = {
            "trades": w3_t8["trades"],
            "net_pnl_usd": w3_t8["net_pnl_usd"],
            "profit_factor": w3_t8["profit_factor"],
            "mtm_max_dd_pct": w3_t8["mtm_max_dd_pct"],
            "halt_timestamp": w3_t8["hwm_kill_switch"]["halt_timestamp"],
            "trades_prevented": w3_t8["trades_prevented_by_kill_switch"],
        }
    return {
        "baseline_version": manifest["baseline_version"],
        "hash_checks": checks,
        "all_hashes_verified": all(v is True for k, v in checks.items() if k.endswith("_match")) and checks["v2_w3_trade_count"] == 143,
        "v2_w3_summary": app_sum["W3"],
        "task8_protected_w3_summary": task8_info,
    }


def _score_decomposed(direction: str, regime: str, feat: dict[str, float]) -> dict[str, float]:
    kv = feat["kalman_vel"]
    ent = feat["shannon_entropy"]
    er = feat["kaufman_er"]
    slp = feat["ema20_slope"]
    twd = feat["tick_weighted_price_dist"]
    h = int(feat["utc_hour"])
    sw = "SIDEWAYS" in regime.upper()
    if direction == "BUY":
        c_vel = t6c._score_band(kv, 0.008, 0.040, 0.002, 0.080) if sw else t6c._score_band(kv, 0.015, 0.060, 0.005, 0.120)
        c_slp = t6c._interp(slp, 0.00005, 0.00030)
        c_twd = t6c._score_band(twd, -0.0005, 0.003, -0.003, 0.006)
        raw_acc = feat["kalman_accel"] * 2.0
    else:
        c_vel = t6c._score_band(kv, -0.040, -0.008, -0.080, -0.002) if sw else t6c._score_band(kv, -0.060, -0.015, -0.120, -0.005)
        c_slp = t6c._interp(slp, -0.00005, -0.00030)
        c_twd = t6c._score_band(twd, -0.003, 0.0005, -0.006, 0.003)
        raw_acc = -feat["kalman_accel"] * 2.0
    c_er = t6c._score_band(er, 0.20, 0.60, 0.08, 0.90)
    c_ent = t6c._score_band(ent, 2.50, 3.20, None, 3.60)
    c_sess = t6c._sess_score(h)

    base = min(1.0, max(0.0, 0.35 * c_vel + 0.20 * c_er + 0.15 * c_ent + 0.15 * c_slp + 0.10 * c_twd + 0.05 * c_sess))
    acc_adj = max(-0.08, min(0.08, raw_acc))
    post_acc = min(1.0, max(0.0, base + acc_adj))
    trr = feat["tick_rate_ratio"]
    tmult = 1.0
    if trr < 0.70:
        tmult = 0.75
    elif sw and trr < 1.10:
        tmult = 0.85
    final_s = round(post_acc * tmult, 4)
    return {
        "comp_kalman_vel": round(c_vel, 4),
        "comp_kaufman_er": round(c_er, 4),
        "comp_entropy": round(c_ent, 4),
        "comp_ema20_slope": round(c_slp, 4),
        "comp_twp_dist": round(c_twd, 4),
        "comp_session": round(c_sess, 4),
        "base_score_pre_accel": round(base, 4),
        "comp_accel_adj": round(acc_adj, 4),
        "comp_tick_mult": tmult,
        "score_computed": final_s,
    }


def _htf_ema_details(closes: list[float], fast: int = 20, slow: int = 50) -> tuple[int, float, float]:
    if len(closes) < slow:
        return 0, 0.0, 0.0
    af = 2.0 / (fast + 1.0)
    as_ = 2.0 / (slow + 1.0)
    ef = float(closes[0])
    es = float(closes[0])
    for v in closes[1:]:
        fv = float(v)
        ef = af * fv + (1.0 - af) * ef
        es = as_ * fv + (1.0 - as_) * es
    tr = 1 if ef > es else (-1 if ef < es else 0)
    return tr, ef, es


def _atr_percentile(df: pd.DataFrame) -> float:
    if len(df) < 20:
        return 50.0
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr_pct = (tr.rolling(14).mean() / df["close"]).dropna()
    if len(atr_pct) <= 1:
        return 50.0
    cur = float(atr_pct.iloc[-1])
    return round(float((atr_pct <= cur).mean() * 100.0), 2)


def _utc_session(hour: int) -> str:
    if 0 <= hour < 7:
        return "Asia"
    if 7 <= hour < 9:
        return "London_PreOpen_07_08"
    if 9 <= hour < 12:
        return "London_Core_09_11"
    if 12 <= hour < 17:
        return "LDN_NY_Overlap_12_16"
    return "Late_NY_17_23"


def _subperiod_label(dt: datetime) -> str:
    # Equal-ish 3 chronological thirds across Aug 17 -> Nov 18 (~1 month each)
    if dt < datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc):
        return "P1_Aug17_Sep16"
    if dt < datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc):
        return "P2_Sep17_Oct16"
    return "P3_Oct17_Nov18"


def _extract_w3(conn) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    start_dt = datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(2026, 11, 18, 18, 0, 0, tzinfo=timezone.utc)
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
    open_meta: dict[str, Any] = {}
    trades: list[dict[str, Any]] = []
    candidate_signals: list[dict[str, Any]] = []

    equity = 10000.0
    day_start_eq = 10000.0
    mtm = t6c.IndMtmTracker(10000.0)
    daily_pnl = 0.0
    daily_trades = 0
    consec_losses = 0
    current_day: Optional[date] = None
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

        is_bull = pos.direction == "BUY"
        mae_price_diff = (pos.entry_price - pos.worst_price) if is_bull else (pos.worst_price - pos.entry_price)
        mfe_price_diff = (pos.peak_price - pos.entry_price) if is_bull else (pos.entry_price - pos.peak_price)
        mae_usd = round(max(0.0, mae_price_diff) * pos.lot * t6c._CONTRACT_SIZE, 4)
        mfe_usd = round(max(0.0, mfe_price_diff) * pos.lot * t6c._CONTRACT_SIZE, 4)
        mae_atr = round(max(0.0, mae_price_diff) / max(pos.atr, 1e-9), 4)
        mfe_atr = round(max(0.0, mfe_price_diff) / max(pos.atr, 1e-9), 4)
        hold_mins = round((exit_ts - pos.entry_time).total_seconds() / 60.0, 4)

        be_reached = int(mfe_atr >= 1.0)
        t1_reached = int(mfe_atr >= 1.6)
        t2_reached = int(mfe_atr >= 2.5)
        e_hr = pos.entry_time.hour
        eff_mh = 60 if 7 <= e_hr < 20 else 90
        tsq_reached = int(pos.bars_held >= int(0.75 * eff_mh))

        t_mfe = open_meta.get("time_to_mfe_mins", 0.0)
        t_mae = open_meta.get("time_to_mae_mins", 0.0)
        mfe_15m_atr = open_meta.get("mfe_15m_atr", mfe_atr)
        mae_15m_atr = open_meta.get("mae_15m_atr", mae_atr)

        row = dict(open_meta)
        row.update({
            "trade_idx": len(trades) + 1,
            "entry_time": pos.entry_time.isoformat(),
            "exit_time": exit_ts.isoformat(),
            "holding_mins": hold_mins,
            "bars_held": pos.bars_held,
            "effective_max_hold": eff_mh,
            "entry_price": pos.entry_price,
            "exit_price": exit_px,
            "sl": pos.initial_sl,
            "final_sl": pos.sl,
            "tp": pos.tp,
            "qty": pos.lot,
            "gross_pnl": gross,
            "commission_usd": pos.comm_total,
            "slippage_usd": pos.slip_usd,
            "swap_usd": pos.swap_cost,
            "pnl": net,
            "equity_after": round(equity, 4),
            "exit_reason": reason,
            "mae_usd": mae_usd,
            "mfe_usd": mfe_usd,
            "mae_atr": mae_atr,
            "mfe_atr": mfe_atr,
            "mae_r": round(mae_atr / 1.5, 4),
            "mfe_r": round(mfe_atr / 1.5, 4),
            "mfe_15m_atr": round(mfe_15m_atr, 4),
            "mae_15m_atr": round(mae_15m_atr, 4),
            "time_to_mfe_mins": round(t_mfe, 4),
            "time_to_mae_mins": round(t_mae, 4),
            "mfe_before_mae": int(t_mfe < t_mae) if (mfe_atr > 0 or mae_atr > 0) else 0,
            "be_reached": be_reached,
            "t1_reached": t1_reached,
            "t2_reached": t2_reached,
            "time_squeeze_reached": tsq_reached,
            "was_profitable_05atr": int(mfe_atr >= 0.5),
            "reversed_from_05atr_profit": int(mfe_atr >= 0.5 and net < 0.0),
            "reversed_from_10atr_profit": int(mfe_atr >= 1.0 and net < 0.0),
            "mfe_captured_ratio": round(net / mfe_usd, 4) if mfe_usd > 0.0 else 0.0,
            "trail_steps_count": len(pos.journey),
            "sl_gap_usd": round(pos.last_sl_gap, 4),
            "journey": pos.journey,
        })
        trades.append(row)

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
                    mins_el = (ts_i - pos.entry_time).total_seconds() / 60.0

                    # Track excursion timing & 15m progress
                    fav_diff = (eq_q - pos.entry_price) if is_bull else (pos.entry_price - eq_q)
                    adv_diff = (pos.entry_price - eq_q) if is_bull else (eq_q - pos.entry_price)
                    fav_atr = max(0.0, fav_diff) / max(pos.atr, 1e-9)
                    adv_atr = max(0.0, adv_diff) / max(pos.atr, 1e-9)
                    if fav_atr > open_meta.get("_best_fav_atr", -1.0):
                        open_meta["_best_fav_atr"] = fav_atr
                        open_meta["time_to_mfe_mins"] = mins_el
                    if adv_atr > open_meta.get("_best_adv_atr", -1.0):
                        open_meta["_best_adv_atr"] = adv_atr
                        open_meta["time_to_mae_mins"] = mins_el
                    if mins_el <= 15.0:
                        if fav_atr > open_meta.get("mfe_15m_atr", 0.0):
                            open_meta["mfe_15m_atr"] = fav_atr
                        if adv_atr > open_meta.get("mae_15m_atr", 0.0):
                            open_meta["mae_15m_atr"] = adv_atr

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
                        pos.theo_sl = float(pos.sl)
                        pos.last_sl_gap = pos.net_at(fpx) - pos.net_at(pos.theo_sl)
                        mtm.update(equity + pos.net_at(fpx))
                        return True, rsn, fpx, i
                    if (not is_bull) and a >= pos.sl:
                        rsn = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
                        fpx = a + t6c._SLIP_OFFSET
                        pos.slip_usd += t6c._SLIP_OFFSET * pos.lot * t6c._CONTRACT_SIZE
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
                    epx = (bpx - t6c._SLIP_OFFSET) if is_bull else (bpx + t6c._SLIP_OFFSET)
                    if last_q is not None:
                        pos.slip_usd += t6c._SLIP_OFFSET * pos.lot * t6c._CONTRACT_SIZE
                    return True, "TIMEOUT", epx, len(bids) - 1

                if best_q is not None:
                    pos.ratchet(best_q, eff_mh)
                return False, "", 0.0, len(bids) - 1

            # Check if a signal fires while open_pos is active (for candidate telemetry only) or normal close
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
                    open_meta = {}
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

            htf, htf_e20, htf_e50 = _htf_ema_details([b["close"] for b in agg_1h.bars])
            sigs = t6c._ind_generate_signals(df_ind, current_regime)
            if not sigs:
                _flush_k()
                continue

            lr = df_ind.iloc[-1]
            cs_s = df_ind["close"]
            c5_bar = agg_5m.bars[-1]
            c5_ts = c5_bar["time"]
            atr_v = float(df_ind["atr"].iloc[-1]) if pd.notna(df_ind["atr"].iloc[-1]) else 1.0
            close_v = float(lr["close"])
            open_v = float(lr["open"])
            high_v = float(lr["high"])
            low_v = float(lr["low"])
            e20_v = float(lr["ema20"]) if pd.notna(lr.get("ema20")) else close_v
            e50_v = float(lr["ema50"]) if pd.notna(lr.get("ema50")) else close_v
            twp_v = float(lr["tick_weighted_price"]) if pd.notna(lr.get("tick_weighted_price")) else close_v
            bb_u = float(lr["bollinger_upper"]) if pd.notna(lr.get("bollinger_upper")) else close_v
            bb_l = float(lr["bollinger_lower"]) if pd.notna(lr.get("bollinger_lower")) else close_v
            bb_w = max(bb_u - bb_l, 1e-9)
            rsi_v = float(lr["rsi"]) if pd.notna(lr.get("rsi")) else 50.0
            ent_v = t6c._ind_shannon_entropy(cs_s.pct_change().dropna()) if len(cs_s) > 30 else 2.0
            er_v = t6c._ind_kaufman_er(cs_s) if len(cs_s) > 15 else 0.3
            slp_v = float(lr["ema20_slope"]) if pd.notna(lr.get("ema20_slope")) else 0.0
            twd_v = float(lr["tick_weighted_price_dist"]) if pd.notna(lr.get("tick_weighted_price_dist")) else 0.0
            trr_v = float(lr["tick_rate_ratio"]) if pd.notna(lr.get("tick_rate_ratio")) else 1.0

            h10 = float(df_ind["high"].tail(10).max())
            l10 = float(df_ind["low"].tail(10).min())
            h20 = float(df_ind["high"].tail(20).max())
            l20 = float(df_ind["low"].tail(20).min())
            prev_30m_ret = (close_v - float(cs_s.iloc[-7])) / max(atr_v, 1e-9) if len(cs_s) >= 7 else 0.0
            prev_1h_ret = (close_v - float(cs_s.iloc[-13])) / max(atr_v, 1e-9) if len(cs_s) >= 13 else 0.0
            prev_4h_ret = (close_v - float(cs_s.iloc[-49])) / max(atr_v, 1e-9) if len(cs_s) >= 49 else 0.0

            feat = {
                "kalman_vel": kalman.x1,
                "kalman_accel": kalman.accel(),
                "shannon_entropy": ent_v,
                "kaufman_er": er_v,
                "ema20_slope": slp_v,
                "tick_weighted_price_dist": twd_v,
                "tick_rate_ratio": trr_v,
                "utc_hour": c5_ts.hour,
            }

            eb, ea = float(bids[en_i]), float(asks[en_i])
            spr_usd = round(ea - eb, 4)
            atr_pctl = _atr_percentile(df_ind)

            def _build_sig_row(st_n: str, d_n: str, sc_n: float, comp_n: dict[str, float], gate_status: str) -> dict[str, Any]:
                k_aligned = int((kalman.x1 > 0) if d_n == "BUY" else (kalman.x1 < 0))
                k_acc_aligned = int((feat["kalman_accel"] > 0) if d_n == "BUY" else (feat["kalman_accel"] < 0))
                cr = max(high_v - low_v, 1e-9)
                is_bull_c = int(close_v > open_v)
                return {
                    "signal_time": c5_ts.isoformat(),
                    "candidate_entry_time": ttimes[en_i].isoformat(),
                    "utc_hour": int(c5_ts.hour),
                    "session": _utc_session(int(c5_ts.hour)),
                    "subperiod": _subperiod_label(c5_ts),
                    "month": c5_ts.strftime("%Y-%m"),
                    "day": c5_ts.strftime("%Y-%m-%d"),
                    "split_t4_dev_holdout": "DEV_Aug17_Oct16" if c5_ts < datetime(2026, 10, 17, tzinfo=timezone.utc) else "HOLDOUT_Oct17_Nov18",
                    "direction": d_n,
                    "strategy": st_n,
                    "regime": current_regime,
                    "gate_status": gate_status,
                    "score": sc_n,
                    **comp_n,
                    "htf_trend": htf,
                    "htf_ema20": round(htf_e20, 4),
                    "htf_ema50": round(htf_e50, 4),
                    "htf_ema_spread_atr": round((htf_e20 - htf_e50) / max(atr_v, 1e-9), 4),
                    "entry_bid": eb,
                    "entry_ask": ea,
                    "entry_spread_usd": spr_usd,
                    "entry_spread_pips": round(spr_usd / t6c._PIP_SIZE, 2),
                    "quote_arrival_1m": int(len(vi)),
                    "bar5m_tick_count": int(c5_bar["tick_count"]),
                    "tick_rate_ratio": round(trr_v, 4),
                    "kalman_price": round(kalman.x0, 4),
                    "kalman_vel": round(kalman.x1, 6),
                    "kalman_accel": round(feat["kalman_accel"], 6),
                    "kalman_dir": 1 if kalman.x1 > 0 else (-1 if kalman.x1 < 0 else 0),
                    "kalman_aligned": k_aligned,
                    "kalman_accel_aligned": k_acc_aligned,
                    "kalman_dist_atr": round((close_v - kalman.x0) / max(atr_v, 1e-9), 4),
                    "ema20": round(e20_v, 4),
                    "ema50": round(e50_v, 4),
                    "ema20_slope": round(slp_v, 7),
                    "ema_spread_atr": round((e20_v - e50_v) / max(atr_v, 1e-9), 4),
                    "dist_ema20_atr": round((close_v - e20_v) / max(atr_v, 1e-9), 4),
                    "dist_ema50_atr": round((close_v - e50_v) / max(atr_v, 1e-9), 4),
                    "tick_weighted_price": round(twp_v, 4),
                    "twp_dist_frac": round(twd_v, 6),
                    "dist_twp_atr": round((close_v - twp_v) / max(atr_v, 1e-9), 4),
                    "rsi": round(rsi_v, 4),
                    "atr": round(atr_v, 4),
                    "atr_pct": round(atr_v / max(close_v, 1e-9), 6),
                    "atr_pctl_hist": atr_pctl,
                    "kaufman_er": round(er_v, 4),
                    "shannon_entropy": round(ent_v, 4),
                    "bb_pos": round((close_v - bb_l) / bb_w, 4),
                    "bar5m_open": open_v,
                    "bar5m_high": high_v,
                    "bar5m_low": low_v,
                    "bar5m_close": close_v,
                    "bar5m_bull_candle": is_bull_c,
                    "bar5m_candle_aligned": is_bull_c if d_n == "BUY" else int(close_v < open_v),
                    "bar5m_body_atr": round(abs(close_v - open_v) / max(atr_v, 1e-9), 4),
                    "bar5m_signed_body_atr": round((close_v - open_v) / max(atr_v, 1e-9), 4),
                    "bar5m_upper_wick_atr": round((high_v - max(open_v, close_v)) / max(atr_v, 1e-9), 4),
                    "bar5m_lower_wick_atr": round((min(open_v, close_v) - low_v) / max(atr_v, 1e-9), 4),
                    "bar5m_close_pos": round((close_v - low_v) / cr, 4),
                    "dist_10bar_high_atr": round((h10 - close_v) / max(atr_v, 1e-9), 4),
                    "dist_10bar_low_atr": round((close_v - l10) / max(atr_v, 1e-9), 4),
                    "dist_20bar_high_atr": round((h20 - close_v) / max(atr_v, 1e-9), 4),
                    "dist_20bar_low_atr": round((close_v - l20) / max(atr_v, 1e-9), 4),
                    "prev_30m_ret_atr": round(prev_30m_ret, 4),
                    "prev_1h_ret_atr": round(prev_1h_ret, 4),
                    "prev_4h_ret_atr": round(prev_4h_ret, 4),
                }

            # Daily risk gate check
            daily_dd = abs(min(0.0, daily_pnl)) / max(day_start_eq, 1.0) * 100.0
            daily_blocked = daily_dd >= 3.0 or daily_trades >= 8 or consec_losses >= 3

            scored_all = []
            for st_n, d_n in sigs:
                comp = _score_decomposed(d_n, current_regime, feat)
                sc_n = t6c._ind_score(d_n, current_regime, feat)
                scored_all.append((sc_n, st_n, d_n, comp))
            scored_all.sort(key=lambda x: x[0], reverse=True)

            # Determine exact gate status for every candidate signal
            accepted_sig = None
            for sc_n, st_n, d_n, comp in scored_all:
                if daily_blocked:
                    g_stat = "BLOCK_DAILY_LIMIT"
                elif htf == 0:
                    g_stat = "BLOCK_H1_WARMUP"
                elif sc_n < 0.60:
                    g_stat = "BLOCK_SCORE_LT_060"
                elif (d_n == "BUY" and htf != 1) or (d_n == "SELL" and htf != -1):
                    g_stat = "BLOCK_H1_COUNTER"
                elif current_regime == "HIGH_VOLATILITY":
                    g_stat = "BLOCK_HIGH_VOL"
                elif d_n == "BUY" and current_regime == "SIDEWAYS":
                    g_stat = "BLOCK_BUY_SIDEWAYS"
                elif bar_idx - last_cd_bar.get((st_n, d_n), -9999) < 15:
                    g_stat = "BLOCK_COOLDOWN"
                elif accepted_sig is None:
                    g_stat = "ACCEPTED"
                    accepted_sig = (sc_n, st_n, d_n, comp)
                else:
                    g_stat = "BLOCK_LOWER_RANK"
                candidate_signals.append(_build_sig_row(st_n, d_n, sc_n, comp, g_stat))

            if daily_blocked or htf == 0 or accepted_sig is None:
                _flush_k()
                continue

            sc, st, d, comp = accepted_sig
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
            open_meta = _build_sig_row(st, d, sc, comp, "ACCEPTED")
            open_meta["_best_fav_atr"] = 0.0
            open_meta["_best_adv_atr"] = 0.0
            open_meta["mfe_15m_atr"] = 0.0
            open_meta["mae_15m_atr"] = 0.0
            open_meta["time_to_mfe_mins"] = 0.0
            open_meta["time_to_mae_mins"] = 0.0

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
                    open_meta = {}

    if open_pos is not None and last_valid is not None:
        lb, la, lt = last_valid
        rc = lb if open_pos.direction == "BUY" else la
        epx = (rc - t6c._SLIP_OFFSET) if open_pos.direction == "BUY" else (rc + t6c._SLIP_OFFSET)
        open_pos.slip_usd += t6c._SLIP_OFFSET * open_pos.lot * t6c._CONTRACT_SIZE
        _close_pos(open_pos, "END_OF_BACKTEST", epx, lt)

    # Reconcile 143/143 against frozen w3_trades.csv and w3_journeys.json
    app_df = pd.read_csv(FROZEN_V2_DIR / "w3_trades.csv")
    app_j = json.loads((FROZEN_V2_DIR / "w3_journeys.json").read_text(encoding="utf-8"))
    mismatches = 0
    trail_mismatches = 0
    max_pnl_diff = 0.0
    n = max(len(app_df), len(trades))
    for idx in range(n):
        a = app_df.iloc[idx].to_dict() if idx < len(app_df) else None
        it = trades[idx] if idx < len(trades) else None
        aj = app_j.get(str(idx), []) if a is not None else []
        ij = it["journey"] if it is not None else []
        if a is None or it is None:
            mismatches += 1
            continue
        ok = (
            pd.to_datetime(a["entry_time"], utc=True) == pd.to_datetime(it["entry_time"], utc=True)
            and pd.to_datetime(a["exit_time"], utc=True) == pd.to_datetime(it["exit_time"], utc=True)
            and str(a["direction"]) == it["direction"]
            and str(a["exit_reason"]) == it["exit_reason"]
            and abs(float(a["qty"]) - it["qty"]) <= 1e-6
            and abs(float(a["pnl"]) - it["pnl"]) <= 0.02
            and abs(float(a["entry_price"]) - it["entry_price"]) <= 1e-4
            and abs(float(a["exit_price"]) - it["exit_price"]) <= 1e-4
        )
        j_diff = len(aj) != len(ij)
        if not j_diff:
            for sa, si in zip(aj, ij):
                if sa["time"] != si["time"] or abs(float(sa["sl"]) - float(si["sl"])) > 0.02:
                    j_diff = True
                    trail_mismatches += 1
                    break
        else:
            trail_mismatches += 1
        if not ok or j_diff:
            mismatches += 1
        max_pnl_diff = max(max_pnl_diff, abs(float(a["pnl"]) - it["pnl"]))

    repro_summary = {
        "frozen_trades": int(len(app_df)),
        "extracted_trades": int(len(trades)),
        "trade_mismatch_count": mismatches,
        "trail_step_mismatches": trail_mismatches,
        "max_pnl_diff_usd": round(max_pnl_diff, 6),
        "final_equity": round(equity, 4),
        "mtm_max_dd_usd": round(mtm.max_dd, 4),
        "mtm_max_dd_pct": round(mtm.max_dd_pct, 4),
        "candidate_signals_total": len(candidate_signals),
    }

    # Drop private temp keys & journey list from CSV export
    cleaned_trades = []
    for t in trades:
        row = {k: v for k, v in t.items() if k != "journey" and not k.startswith("_")}
        cleaned_trades.append(row)

    return cleaned_trades, candidate_signals, repro_summary


def main() -> None:
    phase1 = _verify_frozen_baselines()
    if not phase1["all_hashes_verified"]:
        raise RuntimeError(f"Frozen baseline verification FAILED: {phase1}")

    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    conn = psycopg2.connect(db_url)
    try:
        trades, candidates, repro = _extract_w3(conn)
    finally:
        conn.close()

    phase1["independent_w3_reproduction"] = repro
    (AUDIT_DIR / "task9_phase1_verification.json").write_text(json.dumps(phase1, indent=2), encoding="utf-8")
    pd.DataFrame(trades).to_csv(AUDIT_DIR / "task9_trades_enriched_w3.csv", index=False)
    pd.DataFrame(candidates).to_csv(AUDIT_DIR / "task9_candidate_signals_w3.csv", index=False)
    print(json.dumps(phase1, indent=2))


if __name__ == "__main__":
    main()
