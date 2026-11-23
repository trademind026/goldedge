"""
Task G2 — Phases D & E: Predeclared G2 Scorer Hypothesis Evaluation,
Unprotected & Cost Stress Tests, Concentration, and Dependence-Aware Bootstrap + BH-FDR.

STRICT RULE: ZERO imports from `src/goldedge/`.
Writes Deliverable 6: `backtest_results/audit/task_g2_scorer/g2_research_comparison.json`.
"""

from __future__ import annotations

from datetime import datetime, timezone
import gc
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Optional

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

SPLITS: list[dict[str, Any]] = [
    {
        "name": "DEV",
        "start": datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc),
        "end": datetime(2026, 9, 25, 23, 59, 59, tzinfo=timezone.utc),
        "research_exposed": False,
    },
    {
        "name": "VAL",
        "start": datetime(2026, 9, 26, 0, 0, 0, tzinfo=timezone.utc),
        "end": datetime(2026, 10, 25, 23, 59, 59, tzinfo=timezone.utc),
        "research_exposed": False,
    },
    {
        "name": "HO_EXPOSED",
        "start": datetime(2026, 10, 26, 0, 0, 0, tzinfo=timezone.utc),
        "end": datetime(2026, 11, 19, 23, 59, 59, tzinfo=timezone.utc),
        "research_exposed": True,
    },
    {
        "name": "FULL",
        "start": datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc),
        "end": datetime(2026, 11, 19, 23, 59, 59, tzinfo=timezone.utc),
        "research_exposed": True,
    },
]

# Predeclared candidate configs (2 frozen baselines + 9 G2 hypotheses)
CANDIDATES: list[dict[str, Any]] = [
    {"id": "H0", "mode": "legacy", "min_score": 0.60, "max_score": None, "is_g2": False},
    {"id": "H3b", "mode": "legacy", "min_score": 0.60, "max_score": 0.80, "is_g2": False},
    {"id": "G2_A_ENTROPY_FIX", "mode": "g2_a_entropy_fix", "min_score": 0.60, "max_score": None, "is_g2": True},
    {"id": "G2_B_SLOPE_5M_FIX", "mode": "g2_b_slope_5m_fix", "min_score": 0.60, "max_score": None, "is_g2": True},
    {"id": "G2_C_KALMAN_NORM_FIX", "mode": "g2_c_kalman_norm_fix", "min_score": 0.60, "max_score": None, "is_g2": True},
    {"id": "G2_D_EXHAUSTION_REPLACE_TWP", "mode": "g2_d_exhaustion_replace_twp", "min_score": 0.60, "max_score": None, "is_g2": True},
    {"id": "G2_D2_TWP_BREAKOUT_FIX", "mode": "g2_d2_twp_breakout_fix", "min_score": 0.60, "max_score": None, "is_g2": True},
    {"id": "G2_H_MINIMAL_DEFECT_ONLY", "mode": "g2_h_minimal_defect_only", "min_score": 0.60, "max_score": None, "is_g2": True},
    {"id": "G2_E_CALIBRATED_CORE", "mode": "g2_e_calibrated_core", "min_score": 0.60, "max_score": None, "is_g2": True},
    {"id": "G2_E_DECORRELATED", "mode": "g2_e_decorrelated", "min_score": 0.60, "max_score": None, "is_g2": True},
    {"id": "G2_F_REGIME_CONDITIONED", "mode": "g2_f_regime_conditioned", "min_score": 0.60, "max_score": None, "is_g2": True},
]


def _pf_from_pnls(pnls: list[float]) -> float:
    gw = sum(p for p in pnls if round(p, 2) > 0.0)
    gl = sum(p for p in pnls if round(p, 2) < 0.0)
    if gl == 0.0:
        return 999.0 if gw > 0.0 else 0.0
    return round(abs(gw / gl), 4)


def _subset_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "wins": 0, "win_rate_pct": 0.0, "pnl": 0.0, "pf": 0.0, "expectancy_r": 0.0}
    pnls = [float(t["pnl"]) for t in trades]
    wins = sum(1 for p in pnls if round(p, 2) > 0.0)
    return {
        "trades": n,
        "wins": wins,
        "win_rate_pct": round(wins / n * 100.0, 2),
        "pnl": round(sum(pnls), 4),
        "pf": _pf_from_pnls(pnls),
        "expectancy_r": round(sum(float(t["r_multiple"]) for t in trades) / n, 4),
    }


def _summarize_run(res: dict[str, Any], market_days: list[Any], keep_daily: bool = False) -> dict[str, Any]:
    trades = res["trade_list"]
    n = len(trades)
    scores = np.array([float(t["score"]) for t in trades], dtype=float)
    r_vals = np.array([float(t["r_multiple"]) for t in trades], dtype=float)
    pnls = np.array([float(t["pnl"]) for t in trades], dtype=float)

    # Executed score calibration
    if n >= 5 and np.std(scores) > 1e-12 and np.std(r_vals) > 1e-12:
        rho, sp_p = stats.spearmanr(scores, r_vals)
        rho_v = round(float(rho), 4) if not math.isnan(float(rho)) else None
        p_v = round(float(sp_p), 5) if not math.isnan(float(sp_p)) else None
    else:
        rho_v, p_v = None, None

    band_stats: dict[str, Any] = {}
    for b_lbl, lo, hi in [("[0.60,0.70)", 0.60, 0.70), ("[0.70,0.80)", 0.70, 0.80), ("[0.80,1.00]", 0.80, 1.01)]:
        sub = [t for t in trades if lo <= float(t["score"]) < hi]
        band_stats[b_lbl] = _subset_metrics(sub)

    tertile_spread = None
    if n >= 9:
        order = np.argsort(scores)
        k = max(3, n // 3)
        tertile_spread = round(float(np.mean(r_vals[order[-k:]]) - np.mean(r_vals[order[:k]])), 4)

    sl_trades = [t for t in trades if t["exit_reason"] == "SL"]

    # Direction & regime breakdowns
    by_direction = {d: _subset_metrics([t for t in trades if t["direction"] == d]) for d in ("BUY", "SELL")}
    by_regime = {
        rg: _subset_metrics([t for t in trades if t["regime"] == rg])
        for rg in ("SIDEWAYS", "TRENDING_BEAR", "TRENDING_BULL")
    }

    # Concentration ex top-2 trades and ex top-2 days
    sorted_idx = np.argsort(pnls)[::-1] if n > 0 else np.array([], dtype=int)
    ex_tr_pnls = [float(pnls[i]) for i in sorted_idx[2:]] if n > 2 else []
    day_pnl_map: dict[Any, float] = {d: 0.0 for d in market_days}
    for t in trades:
        ed = t["exit_time"].date()
        day_pnl_map[ed] = day_pnl_map.get(ed, 0.0) + float(t["pnl"])
    sorted_days = sorted(day_pnl_map.items(), key=lambda x: x[1], reverse=True)
    top2_days = {d for d, _ in sorted_days[:2]}
    ex_day_pnls = [float(t["pnl"]) for t in trades if t["exit_time"].date() not in top2_days]

    out = {
        "trades": int(res["trades"]),
        "wins": int(res["wins"]),
        "losses": int(res["losses"]),
        "win_rate_pct": round(float(res["win_rate_pct"]), 2),
        "pnl": round(float(res["pnl"]), 4),
        "gross_pnl": round(float(res["gross_pnl"]), 4),
        "commission_usd": round(float(res["commission_usd"]), 4),
        "slippage_usd": round(float(res["slippage_usd"]), 4),
        "swap_usd": round(float(res["swap_usd"]), 4),
        "pf": round(float(res["pf"]), 4),
        "expectancy": round(float(res["expectancy"]), 4),
        "expectancy_r": round(float(res["expectancy_r"]), 4),
        "closed_max_dd": round(float(res["closed_max_dd"]), 4),
        "closed_max_dd_pct": round(float(res["closed_max_dd_pct"]), 4),
        "mtm_max_dd": round(float(res["mtm_max_dd"]), 4),
        "mtm_max_dd_pct": round(float(res["mtm_max_dd_pct"]), 4),
        "hwm_kill_switch": res["hwm_kill_switch"],
        "calibration": {
            "spearman_score_vs_r": {"n": n, "rho": rho_v, "p_value": p_v},
            "top_minus_bottom_tertile_r": tertile_spread,
            "score_bands": band_stats,
        },
        "sl_exits": {"count": len(sl_trades), "pnl": round(sum(float(t["pnl"]) for t in sl_trades), 4)},
        "by_direction": by_direction,
        "by_regime": by_regime,
        "concentration": {
            "ex_top2_trades_pnl": round(sum(ex_tr_pnls), 4),
            "ex_top2_trades_pf": _pf_from_pnls(ex_tr_pnls),
            "ex_top2_days_pnl": round(sum(ex_day_pnls), 4),
            "ex_top2_days_pf": _pf_from_pnls(ex_day_pnls),
        },
    }
    if keep_daily:
        out["_daily_pnl_vec"] = np.array([day_pnl_map[d] for d in market_days], dtype=float)
    return out


def _daily_block_bootstrap(daily_vec: np.ndarray, n_boot: int = 3000, seed: int = 42) -> dict[str, Any]:
    d = len(daily_vec)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, d, size=(n_boot, d))
    sampled = daily_vec[idx]
    tot_pnls = sampled.sum(axis=1)
    mean_d = sampled.mean(axis=1)
    t_lo, t_hi = np.percentile(tot_pnls, [2.5, 97.5])
    m_lo, m_hi = np.percentile(mean_d, [2.5, 97.5])
    return {
        "n_market_days": d,
        "n_boot": n_boot,
        "total_pnl_ci95": [round(float(t_lo), 2), round(float(t_hi), 2)],
        "mean_daily_pnl_ci95": [round(float(m_lo), 4), round(float(m_hi), 4)],
        "prob_positive_pnl": round(float((tot_pnls > 0.0).mean()), 4),
    }


def _paired_daily_pvalue(cand_vec: np.ndarray, base_vec: np.ndarray, n_boot: int = 3000, seed: int = 42) -> float:
    diff = cand_vec - base_vec
    if np.allclose(diff, 0.0):
        return 1.0
    obs_mean = float(np.mean(diff))
    centered = diff - obs_mean
    d = len(diff)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, d, size=(n_boot, d))
    boot_means = centered[idx].mean(axis=1)
    hits = int(np.sum(np.abs(boot_means) >= abs(obs_mean) - 1e-12))
    return round(float((hits + 1) / (n_boot + 1)), 5)


def _bh_fdr(pvals: list[float]) -> list[float]:
    m = len(pvals)
    if m == 0:
        return []
    order = np.argsort(pvals)
    q = np.empty(m, dtype=float)
    prev = 1.0
    for rank_rev in range(m - 1, -1, -1):
        i = int(order[rank_rev])
        rank = rank_rev + 1
        val = min(prev, float(pvals[i]) * m / rank)
        prev = val
        q[i] = round(min(1.0, val), 5)
    return q.tolist()


def main() -> None:
    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    conn = psycopg2.connect(db_url)
    try:
        print("[G2 DE] Loading full XAUUSD tick span ONCE (2026-08-13 .. 2026-11-19)...", flush=True)
        raw_span = g1.load_prepared_span(conn, "XAUUSD", FULL_LOAD_START, FULL_LOAD_END, chunk_days=7)
    finally:
        conn.close()

    results: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "candidates": CANDIDATES,
        "splits": {},
        "full_no_hwm": {},
        "cost_stress": {},
        "dev_threshold_sensitivity": {},
        "bootstrap_and_fdr": {},
    }

    full_daily_hwm_on: dict[str, np.ndarray] = {}
    full_daily_hwm_off: dict[str, np.ndarray] = {}

    for sp_spec in SPLITS:
        sname = sp_spec["name"]
        sp = g1.slice_prepared_span(raw_span, sp_spec["start"], sp_spec["end"])
        market_days = sorted({ms.minute_ts.date() for ms in sp.replay_slices if ms.candle is not None})
        print(f"[G2 DE] Building cache for {sname} (warmup={len(sp.warmup_slices)}m, replay={len(sp.replay_slices)}m, days={len(market_days)})...", flush=True)
        cache = g2.precompute_g2_span_cache(sp)

        sp_out: dict[str, Any] = {
            "start_utc": sp_spec["start"].isoformat(),
            "end_utc": sp_spec["end"].isoformat(),
            "research_exposed": bool(sp_spec["research_exposed"]),
            "market_days": len(market_days),
            "runs": {},
        }
        is_full = (sname == "FULL")

        for c in CANDIDATES:
            cfg = g1.CleanSimConfig(
                use_kalman_exit=True,
                session_close_utc=15,
                early_fail_mins=15,
                early_fail_min_mfe_atr=0.4,
                min_score=float(c["min_score"]),
                max_score=c["max_score"],
                cost_mode="fusion_ctrader_demo",
                cost_multiplier=1.0,
                max_hwm_dd_pct=8.0,
            )
            res = g2.run_g2_backtest(sp, cache, cfg, scoring_mode=c["mode"])
            if is_full and c["id"] == "H0":
                assert res["trades"] == 68 and abs(res["pnl"] - 112.7398) < 0.01, f"H0 mismatch: {res['trades']} {res['pnl']}"
            if is_full and c["id"] == "H3b":
                assert res["trades"] == 90 and abs(res["pnl"] - 1584.4266) < 0.01, f"H3b mismatch: {res['trades']} {res['pnl']}"

            summ = _summarize_run(res, market_days, keep_daily=is_full)
            if is_full:
                full_daily_hwm_on[c["id"]] = summ.pop("_daily_pnl_vec")
            sp_out["runs"][c["id"]] = summ
        results["splits"][sname] = sp_out

        # DEV-only min_score threshold sensitivity (0.50, 0.55, 0.60)
        if sname == "DEV":
            dev_sens: dict[str, Any] = {}
            for c in CANDIDATES:
                if c["id"] == "H3b":
                    continue
                row_s: dict[str, Any] = {}
                for ms_th in (0.50, 0.55, 0.60):
                    cfg_s = g1.CleanSimConfig(
                        use_kalman_exit=True, session_close_utc=15, early_fail_mins=15,
                        early_fail_min_mfe_atr=0.4, min_score=ms_th, max_score=None,
                        cost_mode="fusion_ctrader_demo", cost_multiplier=1.0, max_hwm_dd_pct=8.0,
                    )
                    rs = g2.run_g2_backtest(sp, cache, cfg_s, scoring_mode=c["mode"])
                    row_s[f"min_{ms_th:.2f}"] = _summarize_run(rs, market_days, keep_daily=False)
                dev_sens[c["id"]] = row_s
            results["dev_threshold_sensitivity"] = dev_sens

        # Cost stress (1.5x and 2.0x) on FULL and HO_EXPOSED
        if sname in ("FULL", "HO_EXPOSED"):
            cs_split: dict[str, Any] = {}
            for cmult in (1.5, 2.0):
                cm_runs: dict[str, Any] = {}
                for c in CANDIDATES:
                    cfg_c = g1.CleanSimConfig(
                        use_kalman_exit=True, session_close_utc=15, early_fail_mins=15,
                        early_fail_min_mfe_atr=0.4, min_score=float(c["min_score"]), max_score=c["max_score"],
                        cost_mode="fusion_ctrader_demo", cost_multiplier=cmult, max_hwm_dd_pct=8.0,
                    )
                    rc = g2.run_g2_backtest(sp, cache, cfg_c, scoring_mode=c["mode"])
                    cm_runs[c["id"]] = _summarize_run(rc, market_days, keep_daily=False)
                cs_split[f"cost_{cmult:.1f}x"] = cm_runs
            results["cost_stress"][sname] = cs_split

        # Counterfactual unprotected FULL (max_hwm_dd_pct=None)
        if is_full:
            no_hwm_runs: dict[str, Any] = {}
            for c in CANDIDATES:
                cfg_u = g1.CleanSimConfig(
                    use_kalman_exit=True, session_close_utc=15, early_fail_mins=15,
                    early_fail_min_mfe_atr=0.4, min_score=float(c["min_score"]), max_score=c["max_score"],
                    cost_mode="fusion_ctrader_demo", cost_multiplier=1.0, max_hwm_dd_pct=None,
                )
                ru = g2.run_g2_backtest(sp, cache, cfg_u, scoring_mode=c["mode"])
                su = _summarize_run(ru, market_days, keep_daily=True)
                full_daily_hwm_off[c["id"]] = su.pop("_daily_pnl_vec")
                no_hwm_runs[c["id"]] = su
            results["full_no_hwm"] = no_hwm_runs

        del cache
        del sp
        gc.collect()

    # Dependence-aware daily-block bootstrap CIs + paired p-values + BH-FDR
    g2_ids = [c["id"] for c in CANDIDATES if c["is_g2"]]
    for tag, d_map in [("FULL_HWM_ON", full_daily_hwm_on), ("FULL_HWM_OFF", full_daily_hwm_off)]:
        p_h0_list: list[float] = []
        p_h3b_list: list[float] = []
        boot_rows: dict[str, Any] = {}
        for cid in [c["id"] for c in CANDIDATES]:
            ci = _daily_block_bootstrap(d_map[cid], n_boot=3000, seed=42)
            row_b: dict[str, Any] = {**ci}
            if cid != "H0":
                ph0 = _paired_daily_pvalue(d_map[cid], d_map["H0"], n_boot=3000, seed=42)
                row_b["p_vs_H0"] = ph0
                if cid in g2_ids:
                    p_h0_list.append(ph0)
            if cid != "H3b":
                ph3 = _paired_daily_pvalue(d_map[cid], d_map["H3b"], n_boot=3000, seed=43)
                row_b["p_vs_H3b"] = ph3
                if cid in g2_ids:
                    p_h3b_list.append(ph3)
            boot_rows[cid] = row_b

        q_h0 = _bh_fdr(p_h0_list)
        q_h3b = _bh_fdr(p_h3b_list)
        for i, gid in enumerate(g2_ids):
            boot_rows[gid]["bh_q_vs_H0"] = q_h0[i]
            boot_rows[gid]["bh_q_vs_H3b"] = q_h3b[i]
        results["bootstrap_and_fdr"][tag] = boot_rows

    out_path = OUT_DIR / "g2_research_comparison.json"
    out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"[G2 DE] Wrote {out_path}", flush=True)

    # Compact console summary
    print("\n=== FULL (8% HWM ON) vs FULL_NO_HWM SUMMARY ===")
    for c in CANDIDATES:
        cid = c["id"]
        f_on = results["splits"]["FULL"]["runs"][cid]
        f_off = results["full_no_hwm"][cid]
        ho = results["splits"]["HO_EXPOSED"]["runs"][cid]
        val = results["splits"]["VAL"]["runs"][cid]
        dev = results["splits"]["DEV"]["runs"][cid]
        b_on = results["bootstrap_and_fdr"]["FULL_HWM_ON"][cid]
        print(
            f"{cid:28s} | FULL: {f_on['trades']:3d} tr ${f_on['pnl']:+8.2f} PF={f_on['pf']:4.2f} DD={f_on['mtm_max_dd_pct']:4.2f}% rho={str(f_on['calibration']['spearman_score_vs_r']['rho']):>6s} qH0={b_on.get('bh_q_vs_H0','-')} | "
            f"NO_HWM: ${f_off['pnl']:+8.2f} DD={f_off['mtm_max_dd_pct']:5.2f}% | "
            f"DEV=${dev['pnl']:+7.2f} VAL=${val['pnl']:+7.2f} HO=${ho['pnl']:+7.2f}"
        )


if __name__ == "__main__":
    main()
