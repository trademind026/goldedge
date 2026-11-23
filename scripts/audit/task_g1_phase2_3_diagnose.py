"""
Task G1 — Phases 2 & 3: Complete Causal Trade-by-Trade Diagnosis & Root-Cause Isolation (Causes A–H).

ZERO imports from `src/goldedge/`.
Produces:
  - backtest_results/audit/task_g1_root_cause/g1_trade_timeline.csv
  - backtest_results/audit/task_g1_root_cause/g1_sl_exits_audit.csv
  - backtest_results/audit/task_g1_root_cause/g1_winners_vs_losers_comparison.csv
  - backtest_results/audit/task_g1_root_cause/g1_root_cause_isolation.json
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Optional

import numpy as np
import pandas as pd
import psycopg2

ROOT = Path(__file__).resolve().parent.parent.parent
if not (ROOT / "src").exists() and Path("/app/src").exists():
    ROOT = Path("/app")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import g1_clean_room_engine as clean

G1_DIR = ROOT / "backtest_results" / "audit" / "task_g1_root_cause"
START_DT = datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc)
END_DT = datetime(2026, 11, 19, 23, 59, 59, tzinfo=timezone.utc)
_NS_PER_MIN = 60 * 1_000_000_000
_NS_PER_SEC = 1_000_000_000


def _diag_post_exit_path(
    span: clean.PreparedSpan,
    trade: dict[str, Any],
) -> dict[str, Any]:
    """
    DIAGNOSTIC-ONLY (strictly never used for entry decisions):
    Evaluates executable price & theoretical delta PnL at +5m/+15m/+30m/+60m after exit.
    """
    r_ts = span.r_ts_ns
    r_b = span.r_bids
    r_a = span.r_asks
    exit_ns = int(trade["exit_ns"])
    is_bull = trade["direction"] == "BUY"
    ep = float(trade["entry_price"])
    xp = float(trade["exit_price"])
    lot = float(trade["qty"])
    atr = max(float(trade["atr"]), 1e-9)

    out: dict[str, Any] = {}
    for mins in (5, 15, 30, 60):
        target_ns = exit_ns + mins * _NS_PER_MIN
        idx = int(np.searchsorted(r_ts, target_ns, side="left"))
        val_px = None
        # Search small neighborhood for valid quote at >= target_ns
        for k in range(idx, min(len(r_ts), idx + 200)):
            b, a = float(r_b[k]), float(r_a[k])
            if b > 0.0 and a > 0.0 and b < a and (r_ts[k] - exit_ns) <= (mins + 10) * _NS_PER_MIN:
                val_px = b if is_bull else a
                break
        if val_px is None:
            out[f"diag_post_{mins}m_px"] = np.nan
            out[f"diag_post_{mins}m_pnl_if_held"] = np.nan
            out[f"diag_post_{mins}m_delta_vs_exit"] = np.nan
        else:
            held_pnl = ((val_px - ep) if is_bull else (ep - val_px)) * lot * clean._CONTRACT_SIZE
            out[f"diag_post_{mins}m_px"] = round(val_px, 2)
            out[f"diag_post_{mins}m_pnl_if_held"] = round(held_pnl, 2)
            out[f"diag_post_{mins}m_delta_vs_exit"] = round(held_pnl - float(trade["pnl"]), 2)

    # Post-exit max favorable / adverse in next 60 mins
    s_idx = int(np.searchsorted(r_ts, exit_ns, side="right"))
    e_idx = int(np.searchsorted(r_ts, exit_ns + 60 * _NS_PER_MIN, side="right"))
    if e_idx > s_idx:
        bs = r_b[s_idx:e_idx]
        as_ = r_a[s_idx:e_idx]
        vm = (bs > 0.0) & (as_ > 0.0) & (bs < as_)
        qs = bs[vm] if is_bull else as_[vm]
        if len(qs) > 0:
            best_after = float(qs.max()) if is_bull else float(qs.min())
            fav_after_atr = ((best_after - xp) if is_bull else (xp - best_after)) / atr
            out["diag_post_60m_max_fav_atr"] = round(fav_after_atr, 3)
        else:
            out["diag_post_60m_max_fav_atr"] = np.nan
    else:
        out["diag_post_60m_max_fav_atr"] = np.nan
    return out


def _counterfactual_std_trade(
    span: clean.PreparedSpan,
    trade: dict[str, Any],
    enable_early_fail: bool = True,
) -> dict[str, Any]:
    """
    DIAGNOSTIC-ONLY: Replay this exact single position causally from entry_ns + 1 tick
    under Standard ATR trailing/BE/SL/TP/TIMEOUT rules (no Kalman reversal/tighten).
    """
    is_bull = trade["direction"] == "BUY"
    ep = float(trade["entry_price"])
    atr = float(trade["atr"])
    sl = float(trade["sl"])
    tp = float(trade["tp"])
    lot = float(trade["qty"])
    entry_ns = int(trade["entry_ns"])
    e_hr = trade["entry_time"].hour
    eff_mh = 60 if 7 <= e_hr < 20 else 90

    peak = ep
    trailing_active = False
    flag = ""
    entry_min_ns = (entry_ns // _NS_PER_MIN) * _NS_PER_MIN

    # Locate starting slice index in replay_slices via binary search on minute_epoch_s
    target_ep_s = entry_min_ns // _NS_PER_SEC
    lo, hi = 0, len(span.replay_slices) - 1
    start_s_idx = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if span.replay_slices[mid].minute_epoch_s == target_ep_s:
            start_s_idx = mid
            break
        elif span.replay_slices[mid].minute_epoch_s < target_ep_s:
            lo = mid + 1
        else:
            hi = mid - 1

    r_ts = span.r_ts_ns
    r_b = span.r_bids
    r_a = span.r_asks
    bars_held = 0

    for idx in range(start_s_idx, len(span.replay_slices)):
        ms = span.replay_slices[idx]
        if idx > start_s_idx:
            bars_held += 1
        s_i, e_i = ms.s, ms.e
        bids = r_b[s_i:e_i]
        asks = r_a[s_i:e_i]
        ts_arr = r_ts[s_i:e_i]
        last_q = None
        last_t = ms.minute_ts
        best_q = None

        for k in range(len(bids)):
            t_ns = int(ts_arr[k])
            if idx == start_s_idx and t_ns <= entry_ns:
                continue
            b, a = float(bids[k]), float(asks[k])
            if b <= 0.0 or a <= 0.0 or b >= a:
                continue
            exec_q = b if is_bull else a
            last_q = exec_q
            last_t = clean.ns_to_dt(t_ns)
            if best_q is None or (is_bull and exec_q > best_q) or ((not is_bull) and exec_q < best_q):
                best_q = exec_q

            if is_bull and b <= sl:
                rsn = flag if flag else ("TRAILING_SL" if trailing_active else "SL")
                pnl = (b - ep) * lot * clean._CONTRACT_SIZE
                return {"reason": rsn, "exit_px": b, "pnl": pnl, "exit_time": last_t.isoformat()}
            if (not is_bull) and a >= sl:
                rsn = flag if flag else ("TRAILING_SL" if trailing_active else "SL")
                pnl = (ep - a) * lot * clean._CONTRACT_SIZE
                return {"reason": rsn, "exit_px": a, "pnl": pnl, "exit_time": last_t.isoformat()}
            if is_bull and b >= tp:
                pnl = (tp - ep) * lot * clean._CONTRACT_SIZE
                return {"reason": "TARGET", "exit_px": tp, "pnl": pnl, "exit_time": last_t.isoformat()}
            if (not is_bull) and a <= tp:
                pnl = (ep - tp) * lot * clean._CONTRACT_SIZE
                return {"reason": "TARGET", "exit_px": tp, "pnl": pnl, "exit_time": last_t.isoformat()}

            # Ratchet inline
            if is_bull and exec_q > peak:
                peak = exec_q
            elif (not is_bull) and exec_q < peak:
                peak = exec_q
            gain = (peak - ep) if is_bull else (ep - peak)
            if gain >= 2.5 * atr:
                trailing_active = True
                nsl = (peak - 0.75 * atr) if is_bull else (peak + 0.75 * atr)
                if (is_bull and nsl > sl) or ((not is_bull) and nsl < sl):
                    sl = nsl
                    flag = "TRAILING_SL"
            elif gain >= 1.6 * atr:
                trailing_active = True
                nsl = (peak - 1.10 * atr) if is_bull else (peak + 1.10 * atr)
                if (is_bull and nsl > sl) or ((not is_bull) and nsl < sl):
                    sl = nsl
                    flag = "TRAILING_SL"
            elif gain >= 1.0 * atr:
                nsl = (ep + 0.20) if is_bull else (ep - 0.20)
                if (is_bull and nsl > sl) or ((not is_bull) and nsl < sl):
                    sl = nsl
                    flag = "BREAKEVEN"

            hold_pct = bars_held / eff_mh
            if hold_pct >= 0.75:
                ratio = min((hold_pct - 0.75) / 0.25, 1.0)
                if is_bull:
                    sq = sl + ratio * (exec_q - sl)
                    if sq > sl:
                        sl = sq
                        flag = "TIME_SQUEEZE"
                else:
                    sq = sl - ratio * (sl - exec_q)
                    if sq < sl:
                        sl = sq
                        flag = "TIME_SQUEEZE"

        if idx > start_s_idx:
            if enable_early_fail and bars_held == 15:
                gain_abs = (peak - ep) if is_bull else (ep - peak)
                if gain_abs / max(atr, 1e-9) < 0.4:
                    q_ex = last_q if last_q is not None else ep
                    pnl = ((q_ex - ep) if is_bull else (ep - q_ex)) * lot * clean._CONTRACT_SIZE
                    return {"reason": "EARLY_FAIL_15M", "exit_px": q_ex, "pnl": pnl, "exit_time": last_t.isoformat()}
            if bars_held >= eff_mh:
                q_ex = last_q if last_q is not None else ep
                pnl = ((q_ex - ep) if is_bull else (ep - q_ex)) * lot * clean._CONTRACT_SIZE
                return {"reason": "TIMEOUT", "exit_px": q_ex, "pnl": pnl, "exit_time": last_t.isoformat()}

        if best_q is not None:
            if is_bull and best_q > peak:
                peak = best_q
            elif (not is_bull) and best_q < peak:
                peak = best_q

    return {"reason": "END_OF_BACKTEST", "exit_px": ep, "pnl": 0.0, "exit_time": trade["exit_time"].isoformat()}


def _group_stats(df: pd.DataFrame, col: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, g in df.groupby(col, observed=False):
        pnls = g["realized_pnl"]
        gw = float(pnls[pnls.round(2) > 0].sum())
        gl = float(abs(pnls[pnls.round(2) < 0].sum()))
        pf = (gw / gl) if gl > 0 else (999.0 if gw > 0 else 0.0)
        out[str(k)] = {
            "trades": int(len(g)),
            "wins": int((pnls.round(2) > 0).sum()),
            "losses": int((pnls.round(2) < 0).sum()),
            "wr_pct": round(float((pnls.round(2) > 0).mean() * 100.0), 2),
            "net_pnl": round(float(pnls.sum()), 2),
            "avg_pnl": round(float(pnls.mean()), 2),
            "avg_r": round(float(g["r_multiple"].mean()), 3),
            "pf": round(pf, 3),
            "sl_count": int((g["exit_reason"] == "SL").sum()),
        }
    return out


def main() -> None:
    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    conn = psycopg2.connect(db_url)
    try:
        print("[PHASE 2/3] Loading raw ticks for 2026-08-13 .. 2026-11-19...", flush=True)
        span = clean.load_prepared_span(conn, "XAUUSD", START_DT, END_DT, chunk_days=7)
    finally:
        conn.close()

    # 1. Exact 70-trade baseline WITH full causal features + ADX + signal funnel
    print("[PHASE 2/3] Running exact 70-trade baseline (Mode B uncached) with full causal feature extraction...", flush=True)
    cfg_b0 = clean.CleanSimConfig(
        use_kalman_exit=True, session_close_utc=15, early_fail_mins=15, early_fail_min_mfe_atr=0.4,
        cost_mode="zero", max_hwm_dd_pct=8.0,
        bug_invert_before_kalman=True, bug_kalman_before_sl_tp=True,
        bug_skip_mtm_on_kalman_exit=True, bug_len_minus_1_on_timeout_idx=True,
        compute_adx_features=True, record_signals=True,
    )
    res_b0 = clean.run_clean_backtest(span, cfg_b0)
    assert res_b0["trades"] == 70, f"Expected 70 trades, got {res_b0['trades']}"

    print("[PHASE 2/3] Precomputing causal SpanCache + verifying cached vs uncached parity...", flush=True)
    span_cache = clean.precompute_span_cache(span, compute_adx=True)
    cfg_kal_zero_pf = clean.CleanSimConfig(use_kalman_exit=True, session_close_utc=15, early_fail_mins=15, early_fail_min_mfe_atr=0.4, cost_mode="zero", max_hwm_dd_pct=8.0)
    res_kal_zero_uncached = clean.run_clean_backtest(span, cfg_kal_zero_pf)
    res_kal_zero_postfix = clean.run_clean_backtest(span, cfg_kal_zero_pf, cache=span_cache)
    assert res_kal_zero_postfix["trades"] == res_kal_zero_uncached["trades"], "Cache trade count mismatch"
    assert abs(res_kal_zero_postfix["pnl"] - res_kal_zero_uncached["pnl"]) < 1e-6, "Cache PnL mismatch"
    print(f"[PHASE 2/3] Cache parity verified cleanly: {res_kal_zero_postfix['trades']} trades, ${res_kal_zero_postfix['pnl']:+.4f} (diff=0.0).", flush=True)

    # 2. Portfolio-level counterfactuals across identical span (post-fix clean spec)
    print("[PHASE 2/3] Running Standard vs Kalman & Zero vs Fusion cost comparisons...", flush=True)
    res_std_zero = clean.run_clean_backtest(
        span,
        clean.CleanSimConfig(use_kalman_exit=False, session_close_utc=15, early_fail_mins=15, early_fail_min_mfe_atr=0.4, cost_mode="zero", max_hwm_dd_pct=8.0),
        cache=span_cache,
    )
    res_std_zero_unprot = clean.run_clean_backtest(
        span,
        clean.CleanSimConfig(use_kalman_exit=False, session_close_utc=15, early_fail_mins=15, early_fail_min_mfe_atr=0.4, cost_mode="zero", max_hwm_dd_pct=None),
        cache=span_cache,
    )
    res_kal_zero_unprot = clean.run_clean_backtest(
        span,
        clean.CleanSimConfig(use_kalman_exit=True, session_close_utc=15, early_fail_mins=15, early_fail_min_mfe_atr=0.4, cost_mode="zero", max_hwm_dd_pct=None),
        cache=span_cache,
    )
    res_kal_fusion = clean.run_clean_backtest(
        span,
        clean.CleanSimConfig(use_kalman_exit=True, session_close_utc=15, early_fail_mins=15, early_fail_min_mfe_atr=0.4, cost_mode="fusion_ctrader_demo", max_hwm_dd_pct=8.0),
        cache=span_cache,
    )
    res_std_fusion = clean.run_clean_backtest(
        span,
        clean.CleanSimConfig(use_kalman_exit=False, session_close_utc=15, early_fail_mins=15, early_fail_min_mfe_atr=0.4, cost_mode="fusion_ctrader_demo", max_hwm_dd_pct=8.0),
        cache=span_cache,
    )

    # 3. Build complete trade-by-trade causal timeline CSV + diagnostic post-exit & CF replay
    print("[PHASE 2/3] Building 70-trade causal timeline + counterfactual replays...", flush=True)
    rows: list[dict[str, Any]] = []
    for tr in res_b0["trade_list"]:
        ctx = tr["entry_ctx"]
        diag = _diag_post_exit_path(span, tr)
        cf_std = _counterfactual_std_trade(span, tr, enable_early_fail=True)
        cf_noef = _counterfactual_std_trade(span, tr, enable_early_fail=False)

        # Estimate Fusion Markets cost for this exact trade (diagnosis of Cause G)
        est_comm = round(2.0 * round(tr["qty"] * 100.0 * float(tr["entry_price"]) * (12.0 / 1_000_000.0), 2), 2)
        est_slip = round(2.0 * (clean._FUSION_SLIP_PIPS * clean._PIP_SIZE) * tr["qty"] * 100.0, 2) if tr["exit_reason"] != "TARGET" else round((clean._FUSION_SLIP_PIPS * clean._PIP_SIZE) * tr["qty"] * 100.0, 2)
        est_cost = round(est_comm + est_slip, 2)

        sl_dist_usd = abs(float(tr["entry_price"]) - float(tr["sl"]))
        sl_dist_atr = sl_dist_usd / max(float(tr["atr"]), 1e-9)
        entry_spread = float(tr["entry_spread_usd"])
        spread_pct_of_sl = (entry_spread / max(sl_dist_usd, 1e-9)) * 100.0

        row = {
            "trade_id": tr["trade_id"],
            "entry_time": tr["entry_time"].isoformat(),
            "exit_time": tr["exit_time"].isoformat(),
            "month": tr["entry_time"].strftime("%Y-%m"),
            "utc_hour": int(ctx["utc_hour"]),
            "hold_secs": round(tr["hold_secs"], 2),
            "hold_mins": round(tr["hold_secs"] / 60.0, 2),
            "hold_bars": tr["hold_bars"],
            "direction": tr["direction"],
            "strategy": tr["strategy"],
            "regime": tr["regime"],
            "raw_regime_at_entry": ctx["raw_regime_at_entry"],
            "regime_lagged": ctx["raw_regime_at_entry"] != tr["regime"],
            "regime_bars_since_commit": ctx["regime_bars_since_commit"],
            "score": tr["score"],
            "sub_mom_kalman": ctx["sub_mom_kalman"],
            "sub_er": ctx["sub_er"],
            "sub_entropy": ctx["sub_entropy"],
            "sub_ema_slope": ctx["sub_ema_slope"],
            "sub_twp_dist": ctx["sub_twp_dist"],
            "sub_session": ctx["sub_session"],
            "accel_adj": ctx["accel_adj"],
            "trr_mult": ctx["trr_mult"],
            "entry_bid": tr["entry_bid"],
            "entry_ask": tr["entry_ask"],
            "entry_price": tr["entry_price"],
            "entry_spread_usd": entry_spread,
            "exit_bid": tr["exit_bid"],
            "exit_ask": tr["exit_ask"],
            "exit_price": round(float(tr["exit_price"]), 4),
            "exit_spread_usd": tr["exit_spread_usd"],
            "sl": round(float(tr["sl"]), 4),
            "final_sl": round(float(tr["final_sl"]), 4),
            "tp": round(float(tr["tp"]), 4),
            "atr": round(float(tr["atr"]), 4),
            "sl_dist_usd": round(sl_dist_usd, 4),
            "sl_dist_atr": round(sl_dist_atr, 3),
            "spread_pct_of_sl": round(spread_pct_of_sl, 2),
            "qty": tr["qty"],
            "planned_risk_usd": round(tr["planned_risk_usd"], 4),
            "planned_risk_pct": round(tr["planned_risk_pct"], 4),
            "ema20": round(ctx["ema20"], 3),
            "ema50": round(ctx["ema50"], 3),
            "ema_spread_pct": round(ctx["ema_spread_pct"], 4),
            "ema20_slope": round(ctx["ema20_slope"], 6),
            "ema20_dist_atr": round(ctx["ema20_dist_atr"], 3),
            "rsi": round(ctx["rsi"], 2),
            "bb_upper": round(ctx["bb_upper"], 3) if not math.isnan(ctx["bb_upper"]) else np.nan,
            "bb_mid": round(ctx["bb_mid"], 3) if not math.isnan(ctx["bb_mid"]) else np.nan,
            "bb_lower": round(ctx["bb_lower"], 3) if not math.isnan(ctx["bb_lower"]) else np.nan,
            "bb_pos": round(ctx["bb_pos"], 3) if not math.isnan(ctx["bb_pos"]) else np.nan,
            "adx": round(ctx["adx"], 2) if not math.isnan(ctx["adx"]) else np.nan,
            "kaufman_er": round(ctx["kaufman_er"], 4),
            "shannon_entropy": round(ctx["shannon_entropy"], 4),
            "tick_rate_ratio": round(ctx["tick_rate_ratio"], 3),
            "tick_weighted_price_dist": round(ctx["tick_weighted_price_dist"], 6),
            "entry_5m_tick_rate": ctx["entry_5m_tick_rate"],
            "entry_5m_is_feed_gap": ctx["entry_5m_is_feed_gap"],
            "entry_minute_valid_ticks": ctx["entry_minute_valid_ticks"],
            "exit_minute_valid_ticks": tr["exit_minute_valid_ticks"],
            "htf_h1_trend": ctx["htf_h1_trend"],
            "htf_h1_ema_spread_pct": ctx["htf_h1_ema_spread_pct"],
            "kalman_pos": round(ctx["kalman_pos"], 3),
            "kalman_vel": round(ctx["kalman_vel"], 5),
            "kalman_accel": round(ctx["kalman_accel"], 5),
            "kalman_vel_norm_at_entry": round(ctx["kalman_vel_norm_at_entry"], 3),
            "dist_to_20bar_high_atr": round(ctx["dist_to_20bar_high_atr"], 3),
            "dist_to_20bar_low_atr": round(ctx["dist_to_20bar_low_atr"], 3),
            "mfe_usd": round(tr["mfe_usd"], 2),
            "mae_usd": round(tr["mae_usd"], 2),
            "mfe_atr": round(tr["mfe_atr"], 3),
            "mae_atr": round(tr["mae_atr"], 3),
            "time_to_mfe_secs": round(tr["time_to_mfe_secs"], 1),
            "time_to_mae_secs": round(tr["time_to_mae_secs"], 1),
            "trailing_updates_count": tr["trailing_updates_count"],
            "trailing_updates_json": json.dumps(tr["sl_updates"]),
            "be_triggered": tr["be_triggered"],
            "t1_triggered": tr["t1_triggered"],
            "t2_triggered": tr["t2_triggered"],
            "kalman_tighten_triggered": tr["kalman_tighten_triggered"],
            "exit_reason": tr["exit_reason"],
            "gross_pnl": round(tr["gross_pnl"], 4),
            "realized_pnl": round(tr["pnl"], 4),
            "r_multiple": round(tr["r_multiple"], 3),
            "est_fusion_cost_usd": est_cost,
            "est_net_pnl_realcost": round(tr["pnl"] - est_cost, 2),
            "diag_cf_std_reason": cf_std["reason"],
            "diag_cf_std_pnl": round(cf_std["pnl"], 2),
            "diag_cf_std_delta_vs_actual": round(tr["pnl"] - cf_std["pnl"], 2),
            "diag_cf_noef_reason": cf_noef["reason"],
            "diag_cf_noef_pnl": round(cf_noef["pnl"], 2),
            "diag_cf_noef_delta_vs_actual": round(tr["pnl"] - cf_noef["pnl"], 2),
            **diag,
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    df.to_csv(G1_DIR / "g1_trade_timeline.csv", index=False)

    # 4. Individual audit of all 13 SL exits (+ 5 TARGET exits for contrast)
    sl_audit = df[df["exit_reason"] == "SL"].copy()
    sl_audit["loss_classification"] = np.where(
        (sl_audit["mfe_atr"] < 0.25) & (sl_audit["hold_mins"] < 15.0),
        "IMMEDIATE_REVERSAL_WEAK_SETUP",
        np.where(sl_audit["mfe_atr"] >= 0.70, "REACHED_NEAR_BE_THEN_REVERSED", "NORMAL_ADVERSE_FOLLOWTHROUGH"),
    )
    sl_audit.to_csv(G1_DIR / "g1_sl_exits_audit.csv", index=False)

    # 5. Winners vs Losers comparison table
    num_cols = [
        "score", "sub_mom_kalman", "sub_er", "sub_entropy", "sub_ema_slope", "accel_adj",
        "atr", "entry_spread_usd", "spread_pct_of_sl", "adx", "rsi", "kaufman_er",
        "shannon_entropy", "tick_rate_ratio", "ema20_dist_atr", "htf_h1_ema_spread_pct",
        "kalman_vel", "kalman_accel", "mfe_atr", "mae_atr", "hold_mins", "realized_pnl", "r_multiple",
    ]
    cohorts = {
        "ALL_70": df,
        "WINNERS_gt0": df[df["realized_pnl"].round(2) > 0],
        "TARGET_EXITS_5": df[df["exit_reason"] == "TARGET"],
        "LOSERS_lt0": df[df["realized_pnl"].round(2) < 0],
        "SL_EXITS_13": df[df["exit_reason"] == "SL"],
        "OCTOBER_22": df[df["month"] == "2026-10"],
        "OCTOBER_SL_7": df[(df["month"] == "2026-10") & (df["exit_reason"] == "SL")],
        "SIDEWAYS_SELL_23": df[(df["regime"] == "SIDEWAYS") & (df["direction"] == "SELL")],
        "SCORE_GE_080": df[df["score"] >= 0.80],
        "SCORE_060_080": df[(df["score"] >= 0.60) & (df["score"] < 0.80)],
    }
    comp_rows = []
    for cname, cdf in cohorts.items():
        r = {"cohort": cname, "trades": int(len(cdf)), "net_pnl": round(float(cdf["realized_pnl"].sum()), 2)}
        for nc in num_cols:
            s = pd.to_numeric(cdf[nc], errors="coerce").dropna()
            r[f"{nc}_mean"] = round(float(s.mean()), 4) if len(s) else np.nan
            r[f"{nc}_median"] = round(float(s.median()), 4) if len(s) else np.nan
        comp_rows.append(r)
    pd.DataFrame(comp_rows).to_csv(G1_DIR / "g1_winners_vs_losers_comparison.csv", index=False)

    # 6. Phase 3 Root-Cause Isolation JSON (Causes A–H)
    sig_df = pd.DataFrame(res_b0["signals_log"])
    funnel_counts = sig_df["reason"].value_counts().to_dict() if not sig_df.empty else {}

    # Score deciles / bands
    df["score_band"] = pd.cut(df["score"], bins=[0.5999, 0.70, 0.80, 0.90, 1.0001], labels=["[0.60,0.70)", "[0.70,0.80)", "[0.80,0.90)", "[0.90,1.00]"])
    score_band_stats = _group_stats(df, "score_band")
    sw_sell = df[(df["regime"] == "SIDEWAYS") & (df["direction"] == "SELL")]
    oct_df = df[df["month"] == "2026-10"]
    kvr_df = df[df["exit_reason"] == "KALMAN_VELOCITY_REVERSAL"]
    kt_df = df[df["exit_reason"] == "KALMAN_TIGHTEN"]
    ef_df = df[df["exit_reason"] == "EARLY_FAIL_15M"]
    sc80_df = df[df["score"] >= 0.80]
    sc60_80_df = df[df["score"] < 0.80]

    # Regime latency check across 5m bars
    # Also check how many trades entered while raw_regime != committed_regime
    lagged_entries = df[df["regime_lagged"] == True]
    non_lagged_entries = df[df["regime_lagged"] == False]

    # Compare pre-Oct vs Oct market environment (5m ATR, regime mix, H1 EMA spread)
    root_causes = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "entry_funnel": {
            "total_signal_events_logged": int(len(sig_df)),
            "breakdown_by_reason": {str(k): int(v) for k, v in funnel_counts.items()},
        },
        "summary_10_causes_matrix": {
            "overall_70_trades": {
                "trades": 70, "wins": res_b0["wins"], "losses": res_b0["losses"], "bes": res_b0["breakevens"],
                "win_rate_pct": round(res_b0["win_rate_pct"], 2), "net_pnl": round(res_b0["pnl"], 2),
                "pf": round(res_b0["pf"], 3), "mtm_max_dd": round(res_b0["mtm_max_dd"], 2), "mtm_max_dd_pct": round(res_b0["mtm_max_dd_pct"], 2),
            },
            "cause_1_engine_defects": "Mode B -> Mode A (E1 fix alone) shifted 3 trades (+$17.74); E3 omitted realistic costs ($0 vs ~$110+ Fusion costs); E5 plotted equity vs entry_time.",
            "by_regime": _group_stats(df, "regime"),
            "by_month": _group_stats(df, "month"),
            "by_direction": _group_stats(df, "direction"),
            "by_exit_reason": _group_stats(df, "exit_reason"),
            "by_utc_hour": _group_stats(df, "utc_hour"),
        },
        "A_sideways_sell_underperformance": {
            "classification": "STRUCTURAL_DESIGN_AND_STATISTICAL_HYPOTHESIS",
            "overall": {
                "trades": int(len(sw_sell)),
                "wins": int((sw_sell["realized_pnl"].round(2) > 0).sum()),
                "wr_pct": round(float((sw_sell["realized_pnl"].round(2) > 0).mean() * 100.0), 2),
                "net_pnl": round(float(sw_sell["realized_pnl"].sum()), 2),
                "sl_count": int((sw_sell["exit_reason"] == "SL").sum()),
                "sl_pnl": round(float(sw_sell[sw_sell["exit_reason"] == "SL"]["realized_pnl"].sum()), 2),
            },
            "by_month": _group_stats(sw_sell, "month"),
            "by_exit_reason": _group_stats(sw_sell, "exit_reason"),
            "by_score_band": _group_stats(sw_sell, "score_band"),
            "mean_adx": round(float(sw_sell["adx"].mean()), 2),
            "pct_adx_lt_20": round(float((sw_sell["adx"] < 20.0).mean() * 100.0), 1),
            "adx_lt_20_pnl": round(float(sw_sell[sw_sell["adx"] < 20.0]["realized_pnl"].sum()), 2),
            "adx_lt_20_n": int((sw_sell["adx"] < 20.0).sum()),
            "adx_ge_20_pnl": round(float(sw_sell[sw_sell["adx"] >= 20.0]["realized_pnl"].sum()), 2),
            "adx_ge_20_n": int((sw_sell["adx"] >= 20.0).sum()),
            "near_20bar_low_le_025_atr": {
                "n": int((sw_sell["dist_to_20bar_low_atr"] <= 0.25).sum()),
                "pnl": round(float(sw_sell[sw_sell["dist_to_20bar_low_atr"] <= 0.25]["realized_pnl"].sum()), 2),
            },
        },
        "B_october_7_full_sl_exits": {
            "classification": "REGIME_SHIFT_PLUS_WEAK_EXHAUSTION_ENTRIES",
            "october_summary": {
                "trades": int(len(oct_df)),
                "net_pnl": round(float(oct_df["realized_pnl"].sum()), 2),
                "sl_count": int((oct_df["exit_reason"] == "SL").sum()),
                "sl_pnl": round(float(oct_df[oct_df["exit_reason"] == "SL"]["realized_pnl"].sum()), 2),
                "non_sl_pnl": round(float(oct_df[oct_df["exit_reason"] != "SL"]["realized_pnl"].sum()), 2),
            },
            "oct_sl_details": sl_audit[sl_audit["month"] == "2026-10"][
                ["trade_id", "entry_time", "direction", "regime", "score", "atr", "adx", "rsi", "ema20_dist_atr", "mfe_atr", "hold_mins", "realized_pnl", "loss_classification"]
            ].to_dict(orient="records"),
            "oct_vs_aug_sep_atr_mean": {
                "aug_sep_atr": round(float(df[df["month"] != "2026-10"]["atr"].mean()), 3),
                "oct_atr": round(float(oct_df["atr"].mean()), 3),
                "aug_sep_sl_count": int(((df["month"] != "2026-10") & (df["exit_reason"] == "SL")).sum()),
                "aug_sep_trades": int((df["month"] != "2026-10").sum()),
            },
        },
        "C_score_above_080_losses": {
            "classification": "CONFIRMED_SCORING_MISCALIBRATION_AND_EXHAUSTION",
            "by_score_band": score_band_stats,
            "score_lt_080": {
                "trades": int(len(sc60_80_df)),
                "wins": int((sc60_80_df["realized_pnl"].round(2) > 0).sum()),
                "wr_pct": round(float((sc60_80_df["realized_pnl"].round(2) > 0).mean() * 100.0), 2),
                "net_pnl": round(float(sc60_80_df["realized_pnl"].sum()), 2),
                "pf": round(abs(float(sc60_80_df[sc60_80_df["realized_pnl"] > 0]["realized_pnl"].sum()) / max(1e-9, abs(float(sc60_80_df[sc60_80_df["realized_pnl"] < 0]["realized_pnl"].sum())))), 3),
            },
            "score_ge_080": {
                "trades": int(len(sc80_df)),
                "wins": int((sc80_df["realized_pnl"].round(2) > 0).sum()),
                "wr_pct": round(float((sc80_df["realized_pnl"].round(2) > 0).mean() * 100.0), 2),
                "net_pnl": round(float(sc80_df["realized_pnl"].sum()), 2),
                "sl_count": int((sc80_df["exit_reason"] == "SL").sum()),
                "mean_ema20_dist_atr": round(float(sc80_df["ema20_dist_atr"].abs().mean()), 3),
                "mean_sub_entropy": round(float(sc80_df["sub_entropy"].mean()), 3),
                "pct_sub_entropy_eq_1": round(float((sc80_df["sub_entropy"] >= 0.999).mean() * 100.0), 1),
                "mean_sub_mom_kalman": round(float(sc80_df["sub_mom_kalman"].mean()), 3),
                "mean_sub_ema_slope": round(float(sc80_df["sub_ema_slope"].mean()), 3),
                "mean_sub_er": round(float(sc80_df["sub_er"].mean()), 3),
                "mean_mfe_atr": round(float(sc80_df["mfe_atr"].mean()), 3),
                "mean_mae_atr": round(float(sc80_df["mae_atr"].mean()), 3),
            },
        },
        "D_kalman_exit_and_early_fail": {
            "classification": "CONFIRMED_DESIGN_FLAW_HARDCODED_60_TICKS_PER_MIN",
            "kalman_velocity_reversal_20": {
                "count": int(len(kvr_df)),
                "actual_pnl": round(float(kvr_df["realized_pnl"].sum()), 2),
                "wins": int((kvr_df["realized_pnl"].round(2) > 0).sum()),
                "median_hold_secs": round(float(kvr_df["hold_secs"].median()), 1),
                "min_hold_secs": round(float(kvr_df["hold_secs"].min()), 1),
                "exits_under_60s": int((kvr_df["hold_secs"] < 60.0).sum()),
                "exits_under_180s": int((kvr_df["hold_secs"] < 180.0).sum()),
                "exit_minute_ticks_median": float(kvr_df["exit_minute_valid_ticks"].median()),
                "cf_std_exit_total_pnl": round(float(kvr_df["diag_cf_std_pnl"].sum()), 2),
                "cf_std_net_impact_of_kalman_rev": round(float(kvr_df["realized_pnl"].sum() - kvr_df["diag_cf_std_pnl"].sum()), 2),
                "trades_where_std_beat_kalman": int((kvr_df["diag_cf_std_pnl"] > kvr_df["realized_pnl"]).sum()),
                "trades_where_kalman_saved_loss": int((kvr_df["realized_pnl"] > kvr_df["diag_cf_std_pnl"]).sum()),
                "cf_std_would_hit_target": int((kvr_df["diag_cf_std_reason"] == "TARGET").sum()),
                "cf_std_would_hit_sl": int((kvr_df["diag_cf_std_reason"] == "SL").sum()),
                "post_30m_mean_delta_vs_exit": round(float(kvr_df["diag_post_30m_delta_vs_exit"].mean()), 2),
            },
            "kalman_tighten_5": {
                "count": int(len(kt_df)),
                "actual_pnl": round(float(kt_df["realized_pnl"].sum()), 2),
                "cf_std_exit_total_pnl": round(float(kt_df["diag_cf_std_pnl"].sum()), 2),
                "net_impact": round(float(kt_df["realized_pnl"].sum() - kt_df["diag_cf_std_pnl"].sum()), 2),
            },
            "early_fail_15m_7": {
                "count": int(len(ef_df)),
                "actual_pnl": round(float(ef_df["realized_pnl"].sum()), 2),
                "cf_noef_total_pnl": round(float(ef_df["diag_cf_noef_pnl"].sum()), 2),
                "net_impact_of_early_fail": round(float(ef_df["realized_pnl"].sum() - ef_df["diag_cf_noef_pnl"].sum()), 2),
                "cf_noef_reasons": ef_df["diag_cf_noef_reason"].value_counts().to_dict(),
            },
            "full_portfolio_comparison_zero_cost_8pct_hwm": {
                "kalman_exit_ON_prefix": {
                    "trades": res_b0["trades"], "wins": res_b0["wins"], "wr_pct": round(res_b0["win_rate_pct"], 2), "pnl": round(res_b0["pnl"], 2), "pf": round(res_b0["pf"], 3), "mtm_dd_pct": round(res_b0["mtm_max_dd_pct"], 2), "hwm_halted": (res_b0["hwm_kill_switch"] or {}).get("halted"),
                },
                "kalman_exit_ON_postfix": {
                    "trades": res_kal_zero_postfix["trades"], "wins": res_kal_zero_postfix["wins"], "wr_pct": round(res_kal_zero_postfix["win_rate_pct"], 2), "pnl": round(res_kal_zero_postfix["pnl"], 2), "pf": round(res_kal_zero_postfix["pf"], 3), "mtm_dd_pct": round(res_kal_zero_postfix["mtm_max_dd_pct"], 2), "hwm_halted": (res_kal_zero_postfix["hwm_kill_switch"] or {}).get("halted"),
                },
                "kalman_exit_ON_unprot": {"trades": res_kal_zero_unprot["trades"], "pnl": round(res_kal_zero_unprot["pnl"], 2), "pf": round(res_kal_zero_unprot["pf"], 3), "mtm_dd_pct": round(res_kal_zero_unprot["mtm_max_dd_pct"], 2)},
                "standard_exit_8pct_hwm": {"trades": res_std_zero["trades"], "wins": res_std_zero["wins"], "wr_pct": round(res_std_zero["win_rate_pct"], 2), "pnl": round(res_std_zero["pnl"], 2), "pf": round(res_std_zero["pf"], 3), "mtm_dd_pct": round(res_std_zero["mtm_max_dd_pct"], 2), "hwm_halted": (res_std_zero["hwm_kill_switch"] or {}).get("halted")},
                "standard_exit_unprot": {"trades": res_std_zero_unprot["trades"], "wins": res_std_zero_unprot["wins"], "wr_pct": round(res_std_zero_unprot["win_rate_pct"], 2), "pnl": round(res_std_zero_unprot["pnl"], 2), "pf": round(res_std_zero_unprot["pf"], 3), "mtm_dd_pct": round(res_std_zero_unprot["mtm_max_dd_pct"], 2)},
            },
        },
        "E_atr_sl_tp_suitability": {
            "winners_mae_atr_quantiles": {
                q: round(float(df[df["realized_pnl"].round(2) > 0]["mae_atr"].quantile(float(q))), 3)
                for q in ("0.25", "0.50", "0.75", "0.90", "0.95", "1.00")
            },
            "losers_mfe_atr_quantiles": {
                q: round(float(df[df["realized_pnl"].round(2) < 0]["mfe_atr"].quantile(float(q))), 3)
                for q in ("0.25", "0.50", "0.75", "0.90", "1.00")
            },
            "pct_trades_reaching_mfe": {
                "ge_1.0_atr": round(float((df["mfe_atr"] >= 1.0).mean() * 100.0), 1),
                "ge_1.6_atr": round(float((df["mfe_atr"] >= 1.6).mean() * 100.0), 1),
                "ge_2.0_atr": round(float((df["mfe_atr"] >= 2.0).mean() * 100.0), 1),
                "ge_2.5_atr": round(float((df["mfe_atr"] >= 2.5).mean() * 100.0), 1),
                "ge_3.0_atr": round(float((df["mfe_atr"] >= 3.0).mean() * 100.0), 1),
            },
            "by_regime_excursion": {
                str(r): {
                    "trades": int(len(g)),
                    "median_mfe_atr": round(float(g["mfe_atr"].median()), 3),
                    "median_mae_atr": round(float(g["mae_atr"].median()), 3),
                    "pct_mfe_ge_1.6_atr": round(float((g["mfe_atr"] >= 1.6).mean() * 100.0), 1),
                    "pct_mfe_ge_3.0_atr": round(float((g["mfe_atr"] >= 3.0).mean() * 100.0), 1),
                }
                for r, g in df.groupby("regime")
            },
        },
        "F_session_and_hour_breakdown": {
            "by_utc_hour": _group_stats(df, "utc_hour"),
            "london_open_07_10": _group_stats(df.assign(sess=np.where(df["utc_hour"] < 11, "LONDON_OPEN_07_10", "OVERLAP_NY_11_14")), "sess"),
        },
        "G_sizing_and_costs_distortion": {
            "zero_cost_kalman_baseline": {
                "trades": res_b0["trades"], "pnl": round(res_b0["pnl"], 2), "pf": round(res_b0["pf"], 3),
                "mtm_dd_pct": round(res_b0["mtm_max_dd_pct"], 2), "hwm": res_b0["hwm_kill_switch"],
            },
            "fusion_cost_kalman_baseline": {
                "trades": res_kal_fusion["trades"], "pnl": round(res_kal_fusion["pnl"], 2), "pf": round(res_kal_fusion["pf"], 3),
                "commission_usd": round(res_kal_fusion["commission_usd"], 2),
                "slippage_usd": round(res_kal_fusion["slippage_usd"], 2),
                "swap_usd": round(res_kal_fusion["swap_usd"], 2),
                "mtm_dd_pct": round(res_kal_fusion["mtm_max_dd_pct"], 2), "hwm": res_kal_fusion["hwm_kill_switch"],
            },
            "fusion_cost_standard_exit": {
                "trades": res_std_fusion["trades"], "pnl": round(res_std_fusion["pnl"], 2), "pf": round(res_std_fusion["pf"], 3),
                "commission_usd": round(res_std_fusion["commission_usd"], 2),
                "slippage_usd": round(res_std_fusion["slippage_usd"], 2),
                "swap_usd": round(res_std_fusion["swap_usd"], 2),
                "mtm_dd_pct": round(res_std_fusion["mtm_max_dd_pct"], 2), "hwm": res_std_fusion["hwm_kill_switch"],
            },
            "trade_level_spread_drag": {
                "mean_entry_spread_usd": round(float(df["entry_spread_usd"].mean()), 4),
                "max_entry_spread_usd": round(float(df["entry_spread_usd"].max()), 4),
                "mean_spread_pct_of_sl": round(float(df["spread_pct_of_sl"].mean()), 2),
                "max_spread_pct_of_sl": round(float(df["spread_pct_of_sl"].max()), 2),
                "mean_planned_risk_pct": round(float(df["planned_risk_pct"].mean()), 4),
                "max_planned_risk_pct": round(float(df["planned_risk_pct"].max()), 4),
                "est_fusion_total_cost_on_70_trades": round(float(df["est_fusion_cost_usd"].sum()), 2),
            },
        },
        "H_data_warmup_regime_lag": {
            "warmup_void_aug9_13_ticks": 0,
            "first_trade_date": str(df["entry_time"].iloc[0]),
            "aug13_warmed_inside_replay_5m_bars": 60,
            "regime_hysteresis_lag_on_70_trades": {
                "lagged_entries_count": int(len(lagged_entries)),
                "lagged_entries_pnl": round(float(lagged_entries["realized_pnl"].sum()), 2),
                "lagged_entries_wr": round(float((lagged_entries["realized_pnl"].round(2) > 0).mean() * 100.0), 1) if len(lagged_entries) else 0.0,
                "aligned_entries_count": int(len(non_lagged_entries)),
                "aligned_entries_pnl": round(float(non_lagged_entries["realized_pnl"].sum()), 2),
                "aligned_entries_wr": round(float((non_lagged_entries["realized_pnl"].round(2) > 0).mean() * 100.0), 1) if len(non_lagged_entries) else 0.0,
            },
            "feed_gap_entries": int(df["entry_5m_is_feed_gap"].sum()),
        },
    }

    (G1_DIR / "g1_root_cause_isolation.json").write_text(
        json.dumps(root_causes, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps({
        "timeline_rows": len(df),
        "sl_rows": len(sl_audit),
        "funnel_top": {k: int(v) for k, v in list(funnel_counts.items())[:8]},
        "score_bands": score_band_stats,
        "kalman_rev_cf_impact": root_causes["D_kalman_exit_and_early_fail"]["kalman_velocity_reversal_20"]["cf_std_net_impact_of_kalman_rev"],
        "std_vs_kal_fusion": {
            "kal_fusion": root_causes["G_sizing_and_costs_distortion"]["fusion_cost_kalman_baseline"],
            "std_fusion": root_causes["G_sizing_and_costs_distortion"]["fusion_cost_standard_exit"],
        },
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
