"""
Task G2 — Phases A, B & C: Source Audit, Complete Candidate Population Reconstruction,
Calibration, Feature Saturation, Correlated Feature Inflation & Exhaustion Diagnosis.

STRICT RULE: ZERO imports from `src/goldedge/`.
Writes Deliverables 1-5 to `backtest_results/audit/task_g2_scorer/`:
  - g2_scoring_formula_audit.json
  - g2_candidate_decisions.csv
  - g2_score_calibration.csv
  - g2_feature_saturation_report.json
  - g2_exhaustion_diagnosis.csv
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd
import psycopg2
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent.parent
if not (ROOT / "src").exists() and Path("/app/src").exists():
    ROOT = Path("/app")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import g1_clean_room_engine as g1
import g2_clean_room_scorer as g2

OUT_DIR = ROOT / "backtest_results" / "audit" / "task_g2_scorer"
OUT_DIR.mkdir(parents=True, exist_ok=True)

FULL_LOAD_START = datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc)
FULL_LOAD_END = datetime(2026, 11, 19, 23, 59, 59, tzinfo=timezone.utc)
FULL_START = datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc)
DEV_END = datetime(2026, 9, 25, 23, 59, 59, tzinfo=timezone.utc)
VAL_END = datetime(2026, 10, 25, 23, 59, 59, tzinfo=timezone.utc)


def _split_label(dt: datetime) -> str:
    if dt <= DEV_END:
        return "DEV"
    if dt <= VAL_END:
        return "VAL"
    return "HO"


def _bootstrap_mean_ci(vals: np.ndarray, n_boot: int = 2000, seed: int = 42) -> tuple[float, float]:
    vals = vals[~np.isnan(vals)]
    if len(vals) == 0:
        return 0.0, 0.0
    if len(vals) == 1:
        return round(float(vals[0]), 4), round(float(vals[0]), 4)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(vals), size=(n_boot, len(vals)))
    means = vals[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return round(float(lo), 4), round(float(hi), 4)


def _spearman_safe(x: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    mask = (~np.isnan(x)) & (~np.isnan(y))
    n = int(mask.sum())
    if n < 5 or np.std(x[mask]) < 1e-12 or np.std(y[mask]) < 1e-12:
        return {"n": n, "rho": None, "p_value": None}
    rho, p = stats.spearmanr(x[mask], y[mask])
    return {
        "n": n,
        "rho": round(float(rho), 4) if not math.isnan(float(rho)) else None,
        "p_value": round(float(p), 5) if not math.isnan(float(p)) else None,
    }


def main() -> None:
    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    conn = psycopg2.connect(db_url)
    try:
        print("[G2 ABC] Loading full XAUUSD tick span (2026-08-13 .. 2026-11-19)...", flush=True)
        raw_span = g1.load_prepared_span(conn, "XAUUSD", FULL_LOAD_START, FULL_LOAD_END, chunk_days=7)
    finally:
        conn.close()

    full_span = g1.slice_prepared_span(raw_span, FULL_START, FULL_LOAD_END)
    print(f"[G2 ABC] Precomputing G2SpanCache (warmup={len(full_span.warmup_slices)}m, replay={len(full_span.replay_slices)}m)...", flush=True)
    cache = g2.precompute_g2_span_cache(full_span)
    print(f"[G2 ABC] Candidate signal minutes captured: {len(cache.candidate_minutes)} (active HTF minutes: {len(cache.signal_minutes)})", flush=True)

    # Verify baseline parity BEFORE proceeding
    cfg_h0 = g1.CleanSimConfig(
        use_kalman_exit=True, session_close_utc=15, early_fail_mins=15,
        early_fail_min_mfe_atr=0.4, cost_mode="fusion_ctrader_demo", max_hwm_dd_pct=8.0,
    )
    cfg_h3b = g1.CleanSimConfig(
        use_kalman_exit=True, session_close_utc=15, early_fail_mins=15,
        early_fail_min_mfe_atr=0.4, max_score=0.80, cost_mode="fusion_ctrader_demo", max_hwm_dd_pct=8.0,
    )
    res_h0 = g2.run_g2_backtest(full_span, cache, cfg_h0, scoring_mode="legacy")
    res_h3b = g2.run_g2_backtest(full_span, cache, cfg_h3b, scoring_mode="legacy")
    print(f"[G2 ABC] Parity H0: trades={res_h0['trades']} pnl=${res_h0['pnl']:+.4f} pf={res_h0['pf']:.4f} dd={res_h0['mtm_max_dd_pct']:.4f}%", flush=True)
    print(f"[G2 ABC] Parity H3b: trades={res_h3b['trades']} pnl=${res_h3b['pnl']:+.4f} pf={res_h3b['pf']:.4f} dd={res_h3b['mtm_max_dd_pct']:.4f}%", flush=True)
    assert res_h0["trades"] == 68 and abs(res_h0["pnl"] - 112.7398) < 0.01, f"H0 mismatch: {res_h0['trades']} {res_h0['pnl']}"
    assert res_h3b["trades"] == 90 and abs(res_h3b["pnl"] - 1584.4266) < 0.01, f"H3b mismatch: {res_h3b['trades']} {res_h3b['pnl']}"

    # Map executed H0 trade entry_ns -> trade details so candidate funnel links cleanly
    h0_exec_by_ns = {int(t["entry_ns"]): t for t in res_h0["trade_list"]}

    # Run H0 decision trace across replay to capture exact funnel reason even when position is open / post-halt
    open_intervals: list[tuple[int, int]] = [(int(t["entry_ns"]), int(t["exit_ns"])) for t in res_h0["trade_list"]]
    hwm_halt_ns = g1.dt_to_ns(datetime.fromisoformat(res_h0["hwm_kill_switch"]["trigger_time"])) if res_h0["hwm_kill_switch"]["trigger_time"] else None

    # Also build exact chronological H0 daily/cooldown tracker for reason assignment on ALL candidate_minutes
    diag_cfg = g1.CleanSimConfig(
        use_kalman_exit=True, session_close_utc=15, early_fail_mins=15,
        early_fail_min_mfe_atr=0.4, cost_mode="fusion_ctrader_demo", max_hwm_dd_pct=None,
    )
    diag_cache: dict[tuple[int, str], dict[str, Any]] = {}

    rows: list[dict[str, Any]] = []
    cid = 0
    cluster_last_ns: dict[str, int] = {}
    cluster_id_map: dict[str, int] = {}
    cluster_seq = 0

    # Walk closed trades to reconstruct exact daily trade/loss/consecutive-loss & cooldown bar indices under H0
    trade_exit_events = sorted([(int(t["exit_ns"]), float(t["pnl"])) for t in res_h0["trade_list"]], key=lambda x: x[0])
    cd_bar_by_idx: dict[tuple[str, str], int] = {}

    # Simpler & 100% exact: replay minute counter `bar_idx` in lockstep with executed trades
    bar_idx = -1
    cur_day = None
    day_eq = 10000.0
    cur_eq = 10000.0
    daily_pnl = 0.0
    daily_trades = 0
    consec_losses = 0
    t_ptr = 0
    open_until_ns = -1

    sorted_exec_ns = sorted(h0_exec_by_ns.keys())
    exec_ptr = 0

    print("[G2 ABC] Reconstructing full candidate decision population + separated diagnostic outcomes...", flush=True)
    for idx_m, ms in enumerate(full_span.replay_slices):
        bar_idx += 1
        m_py = ms.minute_ts
        m_day = m_py.date()
        if cur_day is None or m_day != cur_day:
            cur_day = m_day
            day_eq = max(cur_eq, 1.0)
            daily_pnl = 0.0
            daily_trades = 0
            consec_losses = 0

        s_i, e_i = ms.s, ms.e
        min_start_ns = int(full_span.r_ts_ns[s_i]) if s_i < e_i else int(ms.minute_epoch_s * g1._NS_PER_SEC)
        min_end_ns = int(full_span.r_ts_ns[e_i - 1]) if s_i < e_i else min_start_ns

        # Process any trade exits that finished BEFORE this minute's rollover check
        while t_ptr < len(trade_exit_events) and trade_exit_events[t_ptr][0] <= min_start_ns:
            _, tpnl = trade_exit_events[t_ptr]
            cur_eq += tpnl
            daily_pnl += tpnl
            consec_losses = 0 if tpnl > 0 else consec_losses + 1
            t_ptr += 1

        if idx_m not in cache.candidate_minutes:
            # Advance open_until_ns if an executed trade closed earlier
            continue

        pkt = cache.candidate_minutes[idx_m]
        entry_ns = int(pkt["entry_ns"])
        feat = pkt["feat"]
        reg = pkt["regime"]
        htf = int(pkt["htf_trend"])
        sigs = pkt["sigs"]

        # Update exit pointer strictly up to entry_ns
        while t_ptr < len(trade_exit_events) and trade_exit_events[t_ptr][0] <= entry_ns:
            _, tpnl = trade_exit_events[t_ptr]
            cur_eq += tpnl
            daily_pnl += tpnl
            consec_losses = 0 if tpnl > 0 else consec_losses + 1
            t_ptr += 1

        pos_is_open = any(st_ns < entry_ns < ex_ns for (st_ns, ex_ns) in open_intervals)
        daily_loss_pct = abs(min(0.0, daily_pnl)) / max(day_eq, 1.0) * 100.0
        daily_limit_hit = (daily_loss_pct >= 3.0 or daily_trades >= 8 or consec_losses >= 3)
        post_hwm_halt = (hwm_halt_ns is not None and entry_ns >= hwm_halt_ns)
        in_session_15 = (m_py.hour < 15)

        # Evaluate legacy scores for all generated strategies at this 5m bar
        leg_list = []
        for st_name, dir_val in sigs:
            sc_leg, comp_leg = g2.score_candidate(dir_val, reg, feat, mode="legacy")
            leg_list.append((sc_leg, st_name, dir_val, comp_leg))
        leg_list.sort(key=lambda x: x[0], reverse=True)
        top_leg_score = leg_list[0][0]
        top_leg_strat = leg_list[0][1]

        exec_trade_row = h0_exec_by_ns.get(entry_ns)

        for rank_i, (sc_leg, st_name, dir_val, comp_leg) in enumerate(leg_list):
            cid += 1
            d_sign = 1.0 if dir_val == "BUY" else -1.0
            dir_aligned_h1 = (dir_val == "BUY" and htf == 1) or (dir_val == "SELL" and htf == -1)
            passes_reg_gate = not ((reg == "HIGH_VOLATILITY") or (dir_val == "BUY" and reg == "SIDEWAYS"))
            is_eligible_setup = bool(in_session_15 and htf != 0 and dir_aligned_h1 and passes_reg_gate)
            cd_blocked = (bar_idx - cd_bar_by_idx.get((st_name, dir_val), -9999) < 15)

            # Determine exact H0 decision status cleanly
            if not in_session_15:
                h0_dec = "AFTER_15UTC"
            elif htf == 0:
                h0_dec = "H1_TREND_NEUTRAL"
            elif pos_is_open:
                h0_dec = "POSITION_OPEN"
            elif daily_limit_hit:
                h0_dec = "DAILY_LIMIT"
            elif sc_leg < 0.60:
                h0_dec = "SCORE_BELOW_MIN"
            elif rank_i > 0:
                h0_dec = "OUTRANKED_AT_BAR"
            elif not dir_aligned_h1:
                h0_dec = "H1_TREND_GATE"
            elif not passes_reg_gate:
                h0_dec = "REGIME_GATE"
            elif cd_blocked:
                h0_dec = "COOLDOWN_GATE"
            elif post_hwm_halt:
                h0_dec = "HWM_KILL_SWITCH_8PCT"
            elif exec_trade_row is not None and exec_trade_row["strategy"] == st_name:
                h0_dec = "EXECUTED"
            else:
                h0_dec = "OTHER_REJECTED"

            # Cluster repeated eligible setups within 30 minutes of cluster start (per direction)
            is_indep_elig = False
            cl_id = -1
            if is_eligible_setup and rank_i == 0:
                prev_ns = cluster_last_ns.get(dir_val)
                if prev_ns is None or (entry_ns - prev_ns) >= 30 * 60 * g1._NS_PER_SEC:
                    cluster_seq += 1
                    cluster_id_map[dir_val] = cluster_seq
                    cluster_last_ns[dir_val] = entry_ns
                    is_indep_elig = True
                cl_id = cluster_id_map[dir_val]

            # Compute alternate G2 scores
            sc_a, _ = g2.score_candidate(dir_val, reg, feat, mode="g2_a_entropy_fix")
            sc_b, _ = g2.score_candidate(dir_val, reg, feat, mode="g2_b_slope_5m_fix")
            sc_c, _ = g2.score_candidate(dir_val, reg, feat, mode="g2_c_kalman_norm_fix")
            sc_d, comp_d = g2.score_candidate(dir_val, reg, feat, mode="g2_d_exhaustion_replace_twp")
            sc_d2, comp_d2 = g2.score_candidate(dir_val, reg, feat, mode="g2_d2_twp_breakout_fix")
            sc_h, _ = g2.score_candidate(dir_val, reg, feat, mode="g2_h_minimal_defect_only")
            sc_e, comp_e = g2.score_candidate(dir_val, reg, feat, mode="g2_e_calibrated_core")
            sc_ed, _ = g2.score_candidate(dir_val, reg, feat, mode="g2_e_decorrelated")
            sc_f, _ = g2.score_candidate(dir_val, reg, feat, mode="g2_f_regime_conditioned")

            # Compute or look up research-only separated forward outcome
            d_key = (idx_m, dir_val)
            if d_key not in diag_cache:
                diag_cache[d_key] = g2.compute_diagnostic_outcome(
                    full_span, cache, idx_m, pkt["en_i"], dir_val, pkt["atr"], diag_cfg
                )
            diag = diag_cache[d_key]

            row = {
                "candidate_id": cid,
                "timestamp_utc": m_py.isoformat(),
                "closed_5m_utc": pkt["closed_5m_ts"].isoformat(),
                "month": m_py.strftime("%Y-%m"),
                "utc_hour": int(m_py.hour),
                "split": _split_label(m_py),
                "strategy": st_name,
                "direction": dir_val,
                "htf_trend": htf,
                "htf_spread_pct": round(float(pkt["htf_spread_pct"]), 4),
                "regime": reg,
                "raw_regime": pkt["raw_regime"],
                "regime_lagged": bool(pkt["regime_lagged"]),
                "bars_since_commit": int(pkt["bars_since_commit"]),
                "close": round(float(feat["close"]), 4),
                "entry_bid": round(float(pkt["entry_bid"]), 4),
                "entry_ask": round(float(pkt["entry_ask"]), 4),
                "entry_spread_usd": float(pkt["entry_spread_usd"]),
                "atr": round(float(feat["atr"]), 4),
                "atr_pct": round(float(feat["atr_pct"]), 5),
                "atr_ratio_20": round(float(feat["atr_ratio_20"]), 4),
                "rsi": round(float(feat["rsi"]), 3),
                "dir_rsi_ext": round((float(feat["rsi"]) - 50.0) * d_sign, 3),
                "adx": round(float(feat["adx"]), 3) if not math.isnan(float(feat["adx"])) else np.nan,
                "adx_delta_3": round(float(feat["adx_delta_3"]), 3) if not math.isnan(float(feat["adx_delta_3"])) else np.nan,
                "ema20": round(float(feat["ema20"]), 4),
                "ema50": round(float(feat["ema50"]), 4),
                "ema_spread_pct": round(float(feat["ema_spread_pct"]), 5),
                "ema20_slope": round(float(feat["ema20_slope"]), 7),
                "dir_ema20_slope": round(float(feat["ema20_slope"]) * d_sign, 7),
                "ema20_dist_atr": round(float(feat["ema20_dist_atr"]), 4),
                "dir_ema20_dist_atr": round(float(feat["ema20_dist_atr"]) * d_sign, 4),
                "bb_pos": round(float(feat["bb_pos"]), 4) if not math.isnan(float(feat["bb_pos"])) else np.nan,
                "bb_width": round(float(feat["bb_width"]), 6) if not math.isnan(float(feat["bb_width"])) else np.nan,
                "dist_20bar_high_atr": round(float(feat["dist_20bar_high_atr"]), 4),
                "dist_20bar_low_atr": round(float(feat["dist_20bar_low_atr"]), 4),
                "kaufman_er": round(float(feat["kaufman_er"]), 4),
                "shannon_entropy": round(float(feat["shannon_entropy"]), 4),
                "shannon_entropy_raw_norm": round(float(feat["shannon_entropy_raw_norm"]), 4),
                "shannon_entropy_5m_bits": round(float(feat["shannon_entropy_5m_bits"]), 4),
                "shannon_entropy_5m_norm": round(float(feat["shannon_entropy_5m_norm"]), 4),
                "kalman_pos": round(float(feat["kalman_pos"]), 4),
                "kalman_vel": round(float(feat["kalman_vel"]), 6),
                "dir_kalman_vel": round(float(feat["kalman_vel"]) * d_sign, 6),
                "kalman_vel_1m_atr": round(float(feat["kalman_vel_1m_atr"]), 4),
                "dir_kalman_vel_1m_atr": round(float(feat["kalman_vel_1m_atr"]) * d_sign, 4),
                "kalman_vel_per_tick_atr": round(float(feat["kalman_vel_per_tick_atr"]), 4),
                "kalman_accel": round(float(feat["kalman_accel"]), 6),
                "dir_kalman_accel": round(float(feat["kalman_accel"]) * d_sign, 6),
                "tick_weighted_price_dist": round(float(feat["tick_weighted_price_dist"]), 6),
                "dir_twp_dist": round(float(feat["tick_weighted_price_dist"]) * d_sign, 6),
                "tick_rate_ratio": round(float(feat["tick_rate_ratio"]), 4),
                "entry_5m_tick_rate": round(float(feat["entry_5m_tick_rate"]), 2),
                "n_valid_ticks": int(feat["n_valid_ticks"]),
                # Legacy subcomponents
                "sub_mom_kalman": comp_leg["sub_mom_kalman"],
                "sub_er": comp_leg["sub_er"],
                "sub_entropy": comp_leg["sub_entropy"],
                "sub_ema_slope": comp_leg["sub_ema_slope"],
                "sub_twp_dist": comp_leg.get("sub_twp_dist", comp_leg.get("sub_twp_or_ext")),
                "sub_session": comp_leg["sub_session"],
                "base_score_pre_accel": comp_leg["base_score_pre_accel"],
                "accel_adj": comp_leg["accel_adj"],
                "trr_mult": comp_leg["trr_mult"],
                "legacy_score": sc_leg,
                # G2 calibrated subcomponents (from G2_D / G2_D2 / G2_E_CORE)
                "g2_sub_mom_kalman": comp_e["sub_mom_kalman"],
                "g2_sub_er": comp_e["sub_er"],
                "g2_sub_entropy": comp_e["sub_entropy"],
                "g2_sub_ema_slope": comp_e["sub_ema_slope"],
                "g2_sub_ext_health": comp_d["sub_twp_or_ext"],
                "g2_sub_twp_breakout": comp_d2["sub_twp_or_ext"],
                "g2_ext_mult": comp_e["ext_mult"],
                # Candidate scores
                "score_g2_a_entropy": sc_a,
                "score_g2_b_slope": sc_b,
                "score_g2_c_kalman": sc_c,
                "score_g2_d_ext": sc_d,
                "score_g2_d2_twp": sc_d2,
                "score_g2_h_minimal": sc_h,
                "score_g2_e_core": sc_e,
                "score_g2_e_decorr": sc_ed,
                "score_g2_f_regime": sc_f,
                # Gate tracking
                "in_session_15utc": in_session_15,
                "is_eligible_setup": is_eligible_setup,
                "is_top_ranked_at_bar": (rank_i == 0),
                "h0_funnel_decision": h0_dec,
                "h0_executed": (h0_dec == "EXECUTED"),
                "h0_executed_trade_id": int(exec_trade_row["trade_id"]) if (h0_dec == "EXECUTED" and exec_trade_row) else -1,
                "h0_executed_pnl": round(float(exec_trade_row["pnl"]), 4) if (h0_dec == "EXECUTED" and exec_trade_row) else np.nan,
                "h0_executed_r": round(float(exec_trade_row["r_multiple"]), 4) if (h0_dec == "EXECUTED" and exec_trade_row) else np.nan,
                "h0_executed_reason": str(exec_trade_row["exit_reason"]) if (h0_dec == "EXECUTED" and exec_trade_row) else "",
                "setup_cluster_id": cl_id,
                "is_independent_eligible": is_indep_elig,
                **diag,
            }
            rows.append(row)

        if exec_trade_row is not None:
            daily_trades += 1
            cd_bar_by_idx[(exec_trade_row["strategy"], exec_trade_row["direction"])] = bar_idx

    df = pd.DataFrame(rows)
    assert int(df["h0_executed"].sum()) == 68, f"Expected 68 H0 executed rows, got {int(df['h0_executed'].sum())}"
    df.to_csv(OUT_DIR / "g2_candidate_decisions.csv", index=False)
    print(f"[G2 ABC] Saved {len(df)} total candidate rows to g2_candidate_decisions.csv", flush=True)

    # Funnel count reconciliation check vs G1 (flat pre-halt <15 UTC active HTF signals)
    df_pre_halt_flat = df[df["h0_funnel_decision"].isin(["SCORE_BELOW_MIN", "H1_TREND_GATE", "REGIME_GATE", "COOLDOWN_GATE", "HWM_KILL_SWITCH_8PCT", "EXECUTED"])]
    print("[G2 ABC] Flat pre-limit funnel breakdown:", df_pre_halt_flat["h0_funnel_decision"].value_counts().to_dict(), flush=True)

    # ---- Deliverable 1: g2_scoring_formula_audit.json ----
    df_elig = df[(df["is_eligible_setup"]) & (df["is_top_ranked_at_bar"])].copy()
    df_indep = df[df["is_independent_eligible"]].copy()

    raw_feats_audit = [
        ("kalman_vel", "dir_kalman_vel", "sub_mom_kalman", 0.35),
        ("kalman_vel_1m_atr", "dir_kalman_vel_1m_atr", "g2_sub_mom_kalman", 0.35),
        ("kaufman_er", "kaufman_er", "sub_er", 0.20),
        ("shannon_entropy_1m_bins", "shannon_entropy", "sub_entropy", 0.15),
        ("shannon_entropy_5m_norm", "shannon_entropy_5m_norm", "g2_sub_entropy", 0.15),
        ("ema20_slope", "dir_ema20_slope", "sub_ema_slope", 0.15),
        ("tick_weighted_price_dist", "dir_twp_dist", "sub_twp_dist", 0.10),
        ("ema20_dist_atr", "dir_ema20_dist_atr", "g2_sub_ext_health", 0.10),
        ("utc_hour", "utc_hour", "sub_session", 0.05),
        ("kalman_accel", "dir_kalman_accel", "accel_adj", 0.08),
        ("tick_rate_ratio", "tick_rate_ratio", "trr_mult", 0.0),
    ]

    emp_stats: dict[str, Any] = {}
    for label, col, sub_col, w in raw_feats_audit:
        s_all = pd.to_numeric(df_elig[col], errors="coerce").dropna()
        s_dev = pd.to_numeric(df_elig[df_elig["split"] == "DEV"][col], errors="coerce").dropna()
        s_ind_dev = df_indep[df_indep["split"] == "DEV"]
        s_ind_val = df_indep[df_indep["split"] == "VAL"]
        s_ind_ho = df_indep[df_indep["split"] == "HO"]
        emp_stats[label] = {
            "column": col,
            "subcomponent": sub_col,
            "nominal_weight": w,
            "eligible_n": int(len(s_all)),
            "eligible_quantiles": {
                "min": round(float(s_all.min()), 6),
                "p5": round(float(np.percentile(s_all, 5)), 6),
                "p25": round(float(np.percentile(s_all, 25)), 6),
                "p50": round(float(np.percentile(s_all, 50)), 6),
                "p75": round(float(np.percentile(s_all, 75)), 6),
                "p95": round(float(np.percentile(s_all, 95)), 6),
                "max": round(float(s_all.max()), 6),
            } if len(s_all) else {},
            "dev_quantiles": {
                "p5": round(float(np.percentile(s_dev, 5)), 6),
                "p50": round(float(np.percentile(s_dev, 50)), 6),
                "p95": round(float(np.percentile(s_dev, 95)), 6),
            } if len(s_dev) else {},
            "univariate_ic_vs_hyp_kal_r_indep": {
                "DEV": _spearman_safe(s_ind_dev[col].to_numpy(dtype=float), s_ind_dev["diag_hyp_kal_r"].to_numpy(dtype=float)),
                "VAL": _spearman_safe(s_ind_val[col].to_numpy(dtype=float), s_ind_val["diag_hyp_kal_r"].to_numpy(dtype=float)),
                "HO_exposed": _spearman_safe(s_ind_ho[col].to_numpy(dtype=float), s_ind_ho["diag_hyp_kal_r"].to_numpy(dtype=float)),
                "FULL_indep": _spearman_safe(df_indep[col].to_numpy(dtype=float), df_indep["diag_hyp_kal_r"].to_numpy(dtype=float)),
            },
        }

    formula_audit = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "candidate_population_summary": {
            "total_in_session_signal_rows": int(len(df)),
            "eligible_before_score_and_risk": int(len(df_elig)),
            "independent_eligible_clusters_30m": int(len(df_indep)),
            "flat_pre_limit_funnel_counts": {str(k): int(v) for k, v in df_pre_halt_flat["h0_funnel_decision"].value_counts().to_dict().items()},
            "full_funnel_counts": {str(k): int(v) for k, v in df["h0_funnel_decision"].value_counts().to_dict().items()},
        },
        "source_audit_components": {
            "shannon_entropy": {
                "formula": "scipy.stats.entropy(probs_over_10_bins, base=2) on trailing 30 5m bar pct returns",
                "raw_input": "5m bar close pct_change() (window=30); missing <= 30 -> 2.0 in RunBacktestUseCase, 3.5 in DenoiseService",
                "normalization_band": "_score_band(H, good_min=2.50, good_max=3.20, bad_min=None, bad_max=3.60)",
                "weight": 0.15,
                "causal_at_decision_time": True,
                "confirmed_defects": [
                    "theoretical_max_bits = log2(10) = 3.321928 < bad_max (3.60) -> sub_entropy mathematically bounded in [0.695, 1.0]",
                    "bad_min=None clips every structured/trending move to 1.0",
                    "good_max=3.20 is 96.3% of theoretical max, and 1m bin widths on 5m returns shift mass to tails -> ~100% saturation at 1.0",
                ],
            },
            "kalman_vel": {
                "formula": "2D constant-velocity scalar Kalman _x1 updated per valid bid tick (q_p=1e-4, q_v=3e-5, r_base=1e-2, adapt_to_volume=False)",
                "raw_input": "spot BID quotes in $/tick (missing -> 0.0)",
                "normalization_band": "SIDEWAYS: _score_band(|v|, 0.008, 0.040, 0.002, 0.080); TRENDING: _score_band(|v|, 0.015, 0.060, 0.005, 0.120)",
                "weight": 0.35,
                "causal_at_decision_time": True,
                "confirmed_defects": [
                    "Units are $/tick without normalizing for tick rate or ATR",
                    "SIDEWAYS halves velocity thresholds, inflating noisy sideways fluctuations to sub_mom_kalman=1.0",
                ],
            },
            "kaufman_er": {
                "formula": "abs(close[-1] - close[-11]) / sum(abs(diff(close[-10:]))) on 5m bars",
                "raw_input": "5m bar close series (10 bars = 50 minutes, not 10m as docstring states; missing -> 0.3)",
                "normalization_band": "_score_band(er, 0.20, 0.60, 0.08, 0.90)",
                "weight": 0.20,
                "causal_at_decision_time": True,
                "confirmed_defects": ["Wide 1.0 plateau up to 0.60 overlaps with ema20_slope & kalman_vel"],
            },
            "ema20_slope": {
                "formula": "(ema20[-1] - ema20[-6]) / ema20[-6] on 5m bars (25m fractional slope)",
                "raw_input": "5m ema20 diff(5)/shift(5) (missing -> 0.0)",
                "normalization_band": "_interp(dir * ema20_slope, 0.00005, 0.00030) (monotonic up to 1.0)",
                "weight": 0.15,
                "causal_at_decision_time": True,
                "confirmed_defects": [
                    "1m slope thresholds (5e-5..3e-4) applied to 25m 5m span; SignalGenerator already requires |slope| > 2e-4 unless trr > 2.0",
                    "Monotonic saturation at 1.0 with no upper exhaustion decay on climax extensions",
                ],
            },
            "tick_weighted_price_dist": {
                "formula": "(close - TWP) / TWP where TWP = cumsum(close*tick_count)/cumsum(tick_count) (Note: ScanMarketUseCase divides by close instead of TWP)",
                "raw_input": "tick-weighted session average distance (fractional)",
                "normalization_band": "BUY (-0.0005, 0.003, -0.003, 0.006); SELL (-0.003, 0.0005, -0.006, 0.003)",
                "weight": 0.10,
                "causal_at_decision_time": True,
                "confirmed_defects": ["Fixed fractional distance ~3 ATR rewards chasing extended price runs"],
            },
            "utc_hour_session": {
                "formula": "_session_score(closed_5m_ts.hour): 0.0 (0..6), 0.4 (7..8), 0.7 (9..11), 1.0 (12..16), 0.7 (17..19), 0.2 (20..23)",
                "weight": 0.05,
                "causal_at_decision_time": True,
                "confirmed_defects": ["Peak weight 1.0 in 12..16 UTC slightly amplifies US-open scores right where G1 observed high SL incidence"],
            },
            "kalman_accel": {
                "formula": "clip(dir * (x1 - prev_x1) * 2.0, -0.08, +0.08)",
                "weight": "additive +/-0.08",
                "causal_at_decision_time": True,
                "confirmed_defects": ["Single-tick difference amplifies microstructure noise"],
            },
            "tick_rate_ratio": {
                "formula": "tick_rate / sma20(tick_rate) on 5m bars (<0.70 -> *0.75; SIDEWAYS & <1.10 -> *0.85)",
                "weight": "multiplicative",
                "causal_at_decision_time": True,
                "confirmed_defects": ["Uses quote arrival rate only (zero real traded volume)"],
            },
        },
        "empirical_feature_distributions_and_ic": emp_stats,
    }
    (OUT_DIR / "g2_scoring_formula_audit.json").write_text(json.dumps(formula_audit, indent=2, default=str), encoding="utf-8")

    # ---- Deliverable 3: g2_score_calibration.csv ----
    bins = [(0.0, 0.40, "[0.00,0.40)"), (0.40, 0.50, "[0.40,0.50)"), (0.50, 0.60, "[0.50,0.60)"),
            (0.60, 0.70, "[0.60,0.70)"), (0.70, 0.80, "[0.70,0.80)"), (0.80, 0.90, "[0.80,0.90)"),
            (0.90, 1.01, "[0.90,1.00]")]

    cal_rows: list[dict[str, Any]] = []
    cohorts = [
        ("ELIGIBLE_TOP_ALL", "legacy_score", df_elig),
        ("INDEP_ELIGIBLE_ALL", "legacy_score", df_indep),
        ("INDEP_ELIGIBLE_BUY", "legacy_score", df_indep[df_indep["direction"] == "BUY"]),
        ("INDEP_ELIGIBLE_SELL", "legacy_score", df_indep[df_indep["direction"] == "SELL"]),
        ("INDEP_ELIGIBLE_DEV", "legacy_score", df_indep[df_indep["split"] == "DEV"]),
        ("INDEP_ELIGIBLE_VAL_HO", "legacy_score", df_indep[df_indep["split"] != "DEV"]),
        ("H0_EXECUTED_68", "legacy_score", df[df["h0_executed"]]),
        ("INDEP_G2_A_ENTROPY", "score_g2_a_entropy", df_indep),
        ("INDEP_G2_B_SLOPE", "score_g2_b_slope", df_indep),
        ("INDEP_G2_C_KALMAN", "score_g2_c_kalman", df_indep),
        ("INDEP_G2_D_EXT", "score_g2_d_ext", df_indep),
        ("INDEP_G2_D2_TWP", "score_g2_d2_twp", df_indep),
        ("INDEP_G2_H_MINIMAL", "score_g2_h_minimal", df_indep),
        ("INDEP_G2_E_CORE", "score_g2_e_core", df_indep),
        ("INDEP_G2_E_DECORR", "score_g2_e_decorr", df_indep),
        ("INDEP_G2_F_REGIME", "score_g2_f_regime", df_indep),
    ]

    for cname, sc_col, sub_df in cohorts:
        use_actual = (cname == "H0_EXECUTED_68")
        r_col = "h0_executed_r" if use_actual else "diag_hyp_kal_r"
        w_col = None if use_actual else "diag_hyp_kal_win"
        sl_col = None if use_actual else "diag_hyp_kal_sl"

        for lo, hi, blabel in bins:
            b = sub_df[(sub_df[sc_col] >= lo) & (sub_df[sc_col] < hi)]
            n = len(b)
            if n == 0:
                continue
            r_arr = b[r_col].to_numpy(dtype=float)
            ci_lo, ci_hi = _bootstrap_mean_ci(r_arr)
            wr = float((b["h0_executed_pnl"].round(2) > 0).mean() * 100.0) if use_actual else float(b[w_col].mean() * 100.0)
            sl_r = float((b["h0_executed_reason"] == "SL").mean() * 100.0) if use_actual else float(b[sl_col].mean() * 100.0)
            cal_rows.append({
                "cohort": cname,
                "score_column": sc_col,
                "score_band": blabel,
                "n": int(n),
                "mean_r": round(float(np.nanmean(r_arr)), 4),
                "ci95_lo_r": ci_lo,
                "ci95_hi_r": ci_hi,
                "win_rate_pct": round(wr, 2),
                "hit_1atr_60m_pct": round(float(b["diag_hit_1atr_60m"].mean() * 100.0), 2),
                "full_sl_rate_pct": round(sl_r, 2),
                "mean_mfe_60m_atr": round(float(b["diag_mfe_60m_atr"].mean()), 3),
                "mean_mae_60m_atr": round(float(b["diag_mae_60m_atr"].mean()), 3),
                "mean_hyp_kal_pnl": round(float(b["diag_hyp_kal_pnl"].mean()), 2),
                "low_sample_warning": bool(n < 20),
            })

        # Rank correlation summary row across >= 0.60 and overall
        sub60 = sub_df[sub_df[sc_col] >= 0.60]
        sp_all = _spearman_safe(sub_df[sc_col].to_numpy(dtype=float), sub_df[r_col].to_numpy(dtype=float))
        sp_60 = _spearman_safe(sub60[sc_col].to_numpy(dtype=float), sub60[r_col].to_numpy(dtype=float))
        cal_rows.append({
            "cohort": cname,
            "score_column": sc_col,
            "score_band": "RANK_CORR_ALL_AND_GE60",
            "n": int(len(sub_df)),
            "mean_r": sp_all["rho"] if sp_all["rho"] is not None else np.nan,
            "ci95_lo_r": sp_all["p_value"] if sp_all["p_value"] is not None else np.nan,
            "ci95_hi_r": sp_60["rho"] if sp_60["rho"] is not None else np.nan,
            "win_rate_pct": sp_60["p_value"] if sp_60["p_value"] is not None else np.nan,
            "hit_1atr_60m_pct": float(len(sub60)),
            "full_sl_rate_pct": np.nan,
            "mean_mfe_60m_atr": np.nan,
            "mean_mae_60m_atr": np.nan,
            "mean_hyp_kal_pnl": np.nan,
            "low_sample_warning": bool(len(sub60) < 20),
        })

    pd.DataFrame(cal_rows).to_csv(OUT_DIR / "g2_score_calibration.csv", index=False)

    # ---- Deliverable 4: g2_feature_saturation_report.json ----
    sat_cols = [
        "sub_mom_kalman", "sub_er", "sub_entropy", "sub_ema_slope", "sub_twp_dist", "sub_session",
        "g2_sub_mom_kalman", "g2_sub_er", "g2_sub_entropy", "g2_sub_ema_slope", "g2_sub_ext_health",
        "g2_sub_twp_breakout", "g2_ext_mult",
    ]
    sat_report: dict[str, Any] = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "saturation": {}}
    sat_cohorts = {
        "ELIGIBLE_ALL": df_elig,
        "ELIGIBLE_GE_060": df_elig[df_elig["legacy_score"] >= 0.60],
        "ELIGIBLE_GE_080": df_elig[df_elig["legacy_score"] >= 0.80],
        "H0_EXECUTED_68": df[df["h0_executed"]],
        "INDEP_ELIGIBLE": df_indep,
    }
    for c_lbl, c_df in sat_cohorts.items():
        m: dict[str, Any] = {"n": int(len(c_df))}
        for sc in sat_cols:
            s = pd.to_numeric(c_df[sc], errors="coerce").dropna()
            if len(s) == 0:
                continue
            m[sc] = {
                "mean": round(float(s.mean()), 4),
                "std": round(float(s.std()), 4),
                "iqr": round(float(np.percentile(s, 75) - np.percentile(s, 25)), 4),
                "pct_eq_1_00": round(float((s >= 0.9999).mean() * 100.0), 2),
                "pct_ge_0_95": round(float((s >= 0.95).mean() * 100.0), 2),
                "pct_eq_0_00": round(float((s <= 0.0001).mean() * 100.0), 2),
                "unique_values": int(s.nunique()),
            }
        sat_report["saturation"][c_lbl] = m

    corr_cols = [
        "dir_kalman_vel", "dir_kalman_vel_1m_atr", "dir_ema20_slope", "kaufman_er",
        "shannon_entropy", "shannon_entropy_5m_norm", "dir_ema20_dist_atr", "dir_twp_dist",
        "adx", "dir_rsi_ext", "sub_mom_kalman", "sub_er", "sub_entropy", "sub_ema_slope", "sub_twp_dist",
        "legacy_score", "score_g2_e_core", "diag_hyp_kal_r",
    ]
    df_corr = df_indep[corr_cols].apply(pd.to_numeric, errors="coerce").dropna()
    corr_spearman = df_corr.corr(method="spearman").round(4).to_dict()
    corr_pearson = df_corr.corr(method="pearson").round(4).to_dict()

    # Collinearity VIF across the 5 non-trivial legacy subcomponents & extended raw predictors
    vif_cols = ["dir_kalman_vel", "dir_ema20_slope", "kaufman_er", "dir_twp_dist", "dir_ema20_dist_atr", "adx", "dir_rsi_ext"]
    x_mat = df_corr[vif_cols].to_numpy(dtype=float)
    x_std = (x_mat - x_mat.mean(axis=0)) / np.maximum(x_mat.std(axis=0), 1e-12)
    corr_x = np.corrcoef(x_std, rowvar=False)
    inv_corr = np.linalg.pinv(corr_x)
    vifs = {vif_cols[i]: round(float(inv_corr[i, i]), 3) for i in range(len(vif_cols))}

    sat_report["collinearity_spearman_indep"] = corr_spearman
    sat_report["collinearity_pearson_indep"] = corr_pearson
    sat_report["raw_feature_vif_indep"] = vifs
    (OUT_DIR / "g2_feature_saturation_report.json").write_text(json.dumps(sat_report, indent=2, default=str), encoding="utf-8")

    # ---- Deliverable 5: g2_exhaustion_diagnosis.csv (C4 Exhaustion, C5 Regime, C6 UTC Hour) ----
    exh_rows: list[dict[str, Any]] = []
    # Restrict exhaustion diagnosis to independent eligible candidates (`<15 UTC`, H1 & regime aligned) so setup repeats don't distort counts
    # PLUS include H0 executed subset cleanly in section tag
    bands_3 = [
        ("LT_060", (df_indep["legacy_score"] < 0.60)),
        ("060_TO_080", (df_indep["legacy_score"] >= 0.60) & (df_indep["legacy_score"] < 0.80)),
        ("GE_080", (df_indep["legacy_score"] >= 0.80)),
    ]

    def _add_slice(cat: str, sname: str, band_lbl: str, sub: pd.DataFrame) -> None:
        n = len(sub)
        if n == 0:
            return
        r_arr = sub["diag_hyp_kal_r"].to_numpy(dtype=float)
        exh_rows.append({
            "category": cat,
            "slice": sname,
            "legacy_score_band": band_lbl,
            "n_indep": int(n),
            "win_rate_pct": round(float(sub["diag_hyp_kal_win"].mean() * 100.0), 2),
            "sl_rate_pct": round(float(sub["diag_hyp_kal_sl"].mean() * 100.0), 2),
            "mean_hyp_kal_r": round(float(np.mean(r_arr)), 4),
            "mean_hyp_kal_pnl": round(float(sub["diag_hyp_kal_pnl"].mean()), 2),
            "mean_hyp_std_r": round(float(sub["diag_hyp_std_r"].mean()), 4),
            "hit_1atr_pct": round(float(sub["diag_hit_1atr_60m"].mean() * 100.0), 2),
            "stalled_15m_pct": round(float((sub["diag_mfe_15m_atr"] < 0.4).mean() * 100.0), 2),
            "mean_mfe_60m_atr": round(float(sub["diag_mfe_60m_atr"].mean()), 3),
            "mean_mae_60m_atr": round(float(sub["diag_mae_60m_atr"].mean()), 3),
            "mean_ema20_ext_atr": round(float(sub["dir_ema20_dist_atr"].mean()), 3),
            "low_sample_warning": bool(n < 15),
        })

    c4_masks = [
        ("ALL_ELIGIBLE_INDEP", pd.Series(True, index=df_indep.index)),
        ("RSI_EXTREME_GT70_OR_LT30", ((df_indep["direction"] == "BUY") & (df_indep["rsi"] > 70.0)) | ((df_indep["direction"] == "SELL") & (df_indep["rsi"] < 30.0))),
        ("EMA20_EXT_GT_1_5_ATR", df_indep["dir_ema20_dist_atr"] > 1.5),
        ("EMA20_EXT_GT_2_0_ATR", df_indep["dir_ema20_dist_atr"] > 2.0),
        ("EMA20_HEALTHY_LE_1_2_ATR", df_indep["dir_ema20_dist_atr"] <= 1.2),
        ("KALMAN_DECELERATING", df_indep["dir_kalman_accel"] < 0.0),
        ("KALMAN_ACCELERATING", df_indep["dir_kalman_accel"] >= 0.0),
        ("ADX_LT_20", df_indep["adx"] < 20.0),
        ("ADX_GE_20", df_indep["adx"] >= 20.0),
        ("ADX_FALLING_3BAR", df_indep["adx_delta_3"] < 0.0),
        ("ATR_EXPANSION_GT_1_3x", df_indep["atr_ratio_20"] > 1.3),
        ("HIT_1ATR_THEN_LOST", df_indep["diag_hit_1atr_60m"] & (~df_indep["diag_hyp_kal_win"])),
        ("IMMEDIATE_REVERSAL_SL", (df_indep["diag_mfe_15m_atr"] < 0.25) & df_indep["diag_hyp_kal_sl"]),
    ]
    for sname, m in c4_masks:
        for blbl, bm in bands_3:
            _add_slice("C4_EXHAUSTION", sname, blbl, df_indep[m & bm])

    for reg_val in sorted(df_indep["regime"].unique()):
        for blbl, bm in bands_3:
            _add_slice("C5_REGIME", reg_val, blbl, df_indep[(df_indep["regime"] == reg_val) & bm])
    _add_slice("C5_REGIME_TRANSITION", "REGIME_LAGGED", "ALL", df_indep[df_indep["regime_lagged"]])
    _add_slice("C5_REGIME_TRANSITION", "REGIME_ALIGNED", "ALL", df_indep[~df_indep["regime_lagged"]])
    for blbl, bm in bands_3:
        _add_slice("C5_REGIME_TRANSITION", "REGIME_LAGGED", blbl, df_indep[df_indep["regime_lagged"] & bm])

    for hr in sorted(df_indep["utc_hour"].unique()):
        for blbl, bm in bands_3:
            _add_slice("C6_UTC_HOUR", f"H_{hr:02d}_UTC", blbl, df_indep[(df_indep["utc_hour"] == hr) & bm])

    pd.DataFrame(exh_rows).to_csv(OUT_DIR / "g2_exhaustion_diagnosis.csv", index=False)

    print(json.dumps({
        "parity": {"H0_trades": res_h0["trades"], "H0_pnl": round(res_h0["pnl"], 4), "H3b_trades": res_h3b["trades"], "H3b_pnl": round(res_h3b["pnl"], 4)},
        "candidate_counts": formula_audit["candidate_population_summary"],
        "vif": vifs,
    }, indent=2))


if __name__ == "__main__":
    main()
