"""
Task G1 — Phase 4: Controlled Improvement Research & Statistical Validation (SpanCache-Accelerated).

ZERO imports from `src/goldedge/`.
Chronological split discipline using verified Fusion Markets cTrader demo costs
($12/M per side, 0.0533p slippage, NY 17:00 rollover swap) + active 8% HWM kill-switch:
  - DEV:  2026-08-17 .. 2026-09-25 (40d, warmed Aug 13..16)
  - VAL:  2026-09-26 .. 2026-10-25 (30d, warmed Sep 22..25)
  - HO:   2026-10-26 .. 2026-11-19 (25d untouched holdout, warmed Oct 22..25)
  - FULL: 2026-08-17 .. 2026-11-19 (95d continuous equity & HWM state, warmed Aug 13..16)
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import gc
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd
import psycopg2

ROOT = Path(__file__).resolve().parent.parent.parent
if not (ROOT / "src").exists() and Path("/app/src").exists():
    ROOT = Path("/app")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import g1_clean_room_engine as clean

G1_DIR = ROOT / "backtest_results" / "audit" / "task_g1_root_cause"
FULL_LOAD_START = datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc)
FULL_LOAD_END = datetime(2026, 11, 19, 23, 59, 59, tzinfo=timezone.utc)

SPLITS = {
    "DEV": (datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 9, 25, 23, 59, 59, tzinfo=timezone.utc)),
    "VAL": (datetime(2026, 9, 26, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 10, 25, 23, 59, 59, tzinfo=timezone.utc)),
    "HO": (datetime(2026, 10, 26, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 19, 23, 59, 59, tzinfo=timezone.utc)),
    "FULL": (datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 19, 23, 59, 59, tzinfo=timezone.utc)),
}


def _summarize_run(res: dict[str, Any]) -> dict[str, Any]:
    trades = res["trade_list"]
    pnls = np.array([float(t["pnl"]) for t in trades], dtype=np.float64)
    n = len(pnls)
    hwm_st = res.get("hwm_kill_switch") or {}

    pnl_ex_top2_trades = float(np.sum(np.sort(pnls)[:-2])) if n > 2 else 0.0
    if n > 0:
        df_t = pd.DataFrame([{"day": t["exit_time"].strftime("%Y-%m-%d"), "pnl": float(t["pnl"])} for t in trades])
        day_sums = df_t.groupby("day")["pnl"].sum().sort_values(ascending=False)
        pnl_ex_top2_days = float(day_sums.iloc[2:].sum()) if len(day_sums) > 2 else 0.0
        n_days = int(len(day_sums))
    else:
        pnl_ex_top2_days = 0.0
        n_days = 0

    return {
        "trades": int(res["trades"]),
        "wins": int(res["wins"]),
        "losses": int(res["losses"]),
        "wr_pct": round(float(res["win_rate_pct"]), 2),
        "net_pnl": round(float(res["pnl"]), 2),
        "gross_pnl": round(float(res["gross_pnl"]), 2),
        "costs_usd": round(float(res["commission_usd"] + res["swap_usd"]), 2),
        "slippage_usd": round(float(res["slippage_usd"]), 2),
        "pf": round(float(res["pf"]), 4),
        "expectancy": round(float(res["expectancy"]), 2),
        "closed_dd_pct": round(float(res["closed_max_dd_pct"]), 2),
        "mtm_dd_pct": round(float(res["mtm_max_dd_pct"]), 2),
        "buy_trades": int(res["buy_trades"]),
        "buy_pnl": round(float(res["buy_pnl"]), 2),
        "sell_trades": int(res["sell_trades"]),
        "sell_pnl": round(float(res["sell_pnl"]), 2),
        "hwm_halted": bool(hwm_st.get("halted", False)),
        "hwm_trigger_time": hwm_st.get("trigger_time"),
        "hwm_prevented": int(hwm_st.get("trades_prevented", 0)),
        "days_traded": n_days,
        "pnl_ex_top2_trades": round(pnl_ex_top2_trades, 2),
        "pnl_ex_top2_days": round(pnl_ex_top2_days, 2),
    }


def _bootstrap_mean_ci(pnls: np.ndarray, n_boot: int = 2000, seed: int = 42) -> tuple[float, float]:
    if len(pnls) < 2:
        val = float(pnls[0]) if len(pnls) == 1 else 0.0
        return round(val, 2), round(val, 2)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(pnls), size=(n_boot, len(pnls)))
    means = pnls[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return round(float(lo), 2), round(float(hi), 2)


def _paired_daily_test(cand_trades: list[dict], base_trades: list[dict], start_dt: datetime, end_dt: datetime) -> dict[str, Any]:
    all_days = pd.date_range(start_dt.date(), end_dt.date(), freq="D").strftime("%Y-%m-%d").tolist()
    c_map: dict[str, float] = {}
    b_map: dict[str, float] = {}
    for t in cand_trades:
        d = t["exit_time"].strftime("%Y-%m-%d")
        c_map[d] = c_map.get(d, 0.0) + float(t["pnl"])
    for t in base_trades:
        d = t["exit_time"].strftime("%Y-%m-%d")
        b_map[d] = b_map.get(d, 0.0) + float(t["pnl"])

    diffs = np.array([c_map.get(d, 0.0) - b_map.get(d, 0.0) for d in all_days if (d in c_map or d in b_map)], dtype=np.float64)
    nz = diffs[np.abs(diffs) > 1e-6]
    if len(nz) < 3:
        return {"active_days": int(len(diffs)), "nonzero_diff_days": int(len(nz)), "mean_daily_diff": round(float(diffs.mean()), 2) if len(diffs) else 0.0, "p_value": 1.0}

    mean_d = float(nz.mean())
    se_d = float(nz.std(ddof=1) / math.sqrt(len(nz))) if len(nz) > 1 else 1e-9
    z = mean_d / max(se_d, 1e-9)
    p_val = float(math.erfc(abs(z) / math.sqrt(2.0)))
    return {
        "active_days": int(len(diffs)),
        "nonzero_diff_days": int(len(nz)),
        "pos_diff_days": int((nz > 0).sum()),
        "neg_diff_days": int((nz < 0).sum()),
        "mean_daily_diff": round(mean_d, 2),
        "z_stat": round(z, 3),
        "p_value": round(p_val, 5),
    }


def _bh_fdr(p_vals: dict[str, float]) -> dict[str, float]:
    if not p_vals:
        return {}
    items = sorted(p_vals.items(), key=lambda x: x[1])
    m = len(items)
    adj_vals = [0.0] * m
    running_min = 1.0
    for rank_rev in range(m - 1, -1, -1):
        rank = rank_rev + 1
        raw_p = items[rank_rev][1]
        val = min(running_min, raw_p * m / rank)
        running_min = val
        adj_vals[rank_rev] = round(min(1.0, val), 5)
    return {items[i][0]: adj_vals[i] for i in range(m)}


def main() -> None:
    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    conn = psycopg2.connect(db_url)
    try:
        print("[PHASE 4] Loading full dataset span (2026-08-13 .. 2026-11-19)...", flush=True)
        full_raw_span = clean.load_prepared_span(conn, "XAUUSD", FULL_LOAD_START, FULL_LOAD_END, chunk_days=7)
    finally:
        conn.close()

    split_spans = {
        k: clean.slice_prepared_span(full_raw_span, sd, ed)
        for k, (sd, ed) in SPLITS.items()
    }
    print(
        "[PHASE 4] Split bar counts:",
        {k: {"warmup_1m": len(sp.warmup_slices), "replay_1m": len(sp.replay_slices)} for k, sp in split_spans.items()},
        flush=True,
    )

    def _base_cfg(**overrides: Any) -> clean.CleanSimConfig:
        kw = dict(
            use_kalman_exit=True,
            session_close_utc=15,
            early_fail_mins=15,
            early_fail_min_mfe_atr=0.4,
            cost_mode="fusion_ctrader_demo",
            cost_multiplier=1.0,
            max_hwm_dd_pct=8.0,
            compute_adx_features=True,
        )
        kw.update(overrides)
        return clean.CleanSimConfig(**kw)

    hypotheses: dict[str, tuple[str, clean.CleanSimConfig]] = {
        "B0_KALMAN": ("Current app candidate (15:00+E1, use_kalman_exit=True, Fusion costs)", _base_cfg()),
        "H1_STD_EXIT": ("Disable Kalman exit (use_kalman_exit=False — isolates Cause D)", _base_cfg(use_kalman_exit=False)),
        "H2a_KAL_NO_EF": ("Disable EARLY_FAIL_15M on B0_KALMAN", _base_cfg(use_kalman_exit=True, early_fail_mins=None)),
        "H2b_STD_NO_EF": ("Disable EARLY_FAIL_15M on H1_STD_EXIT", _base_cfg(use_kalman_exit=False, early_fail_mins=None)),
        "H3a_STD_CAP80": ("Score cap < 0.80 on H1_STD_EXIT (isolates Cause C)", _base_cfg(use_kalman_exit=False, max_score=0.80)),
        "H3b_KAL_CAP80": ("Score cap < 0.80 on B0_KALMAN", _base_cfg(use_kalman_exit=True, max_score=0.80)),
        "H4_STD_NO_SW_SELL": ("Block SIDEWAYS SELL on H1_STD_EXIT (isolates Cause A)", _base_cfg(use_kalman_exit=False, block_sell_sideways=True)),
        "H5_STD_ADX20": ("Require ADX >= 20 on H1_STD_EXIT (trend strength)", _base_cfg(use_kalman_exit=False, adx_min=20.0)),
        "H6_STD_EMAEXT20": ("Block entries > 2.0 ATR from EMA20 on H1_STD_EXIT", _base_cfg(use_kalman_exit=False, ema20_max_dist_atr=2.0)),
        "H7_KALMAN_RATE_NORM": ("Tick-rate-normalized Kalman exit (vel>=3.5, min_hold>=3m)", _base_cfg(use_kalman_exit=True, kalman_rate_norm=True, kalman_vel_thresh=3.5, kalman_min_hold_bars=3)),
        "H8_STD_SL20_TP30": ("Wider stop SL=2.0x ATR, TP=3.0x ATR on H1_STD_EXIT", _base_cfg(use_kalman_exit=False, sl_atr_mult=2.0, tp_atr_mult=3.0)),
        "H9_STD_SESS17": ("Extend session_close_utc to 17:00 UTC on H1_STD_EXIT", _base_cfg(use_kalman_exit=False, session_close_utc=17)),
    }

    sensitivity: dict[str, dict[str, dict[str, Any]]] = {
        "score_cap_sweep_std_exit": {f"cap_{c}" if c is not None else "no_cap": {} for c in (0.75, 0.80, 0.85, None)},
        "kalman_vel_thresh_sweep": {f"rate_norm_vt_{vt}": {} for vt in (2.5, 3.5, 4.5, 5.5)},
    }

    dev_val_raw_runs: dict[tuple[str, str], dict[str, Any]] = {}
    stage_a_results: dict[str, Any] = {hid: {"description": desc} for hid, (desc, _) in hypotheses.items()}

    # Process DEV & VAL first (strictly before inspecting HO)
    for sp_name in ("DEV", "VAL"):
        print(f"[PHASE 4] Precomputing SpanCache for {sp_name}...", flush=True)
        cache = clean.precompute_span_cache(split_spans[sp_name], compute_adx=True)
        for hid, (_desc, cfg) in hypotheses.items():
            res = clean.run_clean_backtest(split_spans[sp_name], cfg, cache=cache)
            dev_val_raw_runs[(hid, sp_name)] = res
            stage_a_results[hid][sp_name] = _summarize_run(res)
            print(
                f"  [{sp_name}] {hid:<22s} trades={res['trades']:>3d} pnl=${res['pnl']:>+8.2f} pf={res['pf']:.3f} dd={res['mtm_max_dd_pct']:.2f}%",
                flush=True,
            )
        # Sensitivity sweeps on DEV/VAL while cache is hot
        for c in (0.75, 0.80, 0.85, None):
            lbl = f"cap_{c}" if c is not None else "no_cap"
            r_s = clean.run_clean_backtest(split_spans[sp_name], _base_cfg(use_kalman_exit=False, max_score=c), cache=cache)
            sensitivity["score_cap_sweep_std_exit"][lbl][sp_name] = _summarize_run(r_s)
        for vt in (2.5, 3.5, 4.5, 5.5):
            r_k = clean.run_clean_backtest(split_spans[sp_name], _base_cfg(use_kalman_exit=True, kalman_vel_thresh=vt, kalman_rate_norm=True, kalman_min_hold_bars=3), cache=cache)
            sensitivity["kalman_vel_thresh_sweep"][f"rate_norm_vt_{vt}"][sp_name] = _summarize_run(r_k)
        del cache
        gc.collect()

    b0_dev_pnl = stage_a_results["B0_KALMAN"]["DEV"]["net_pnl"]
    b0_val_pnl = stage_a_results["B0_KALMAN"]["VAL"]["net_pnl"]
    h1_dev_pnl = stage_a_results["H1_STD_EXIT"]["DEV"]["net_pnl"]
    h1_val_pnl = stage_a_results["H1_STD_EXIT"]["VAL"]["net_pnl"]

    stage_a_qualifiers = []
    for hid, info in stage_a_results.items():
        info["DEV_plus_VAL_pnl"] = round(info["DEV"]["net_pnl"] + info["VAL"]["net_pnl"], 2)
        if hid == "B0_KALMAN":
            continue
        beats_b0_both = (
            info["DEV"]["net_pnl"] > b0_dev_pnl
            and info["VAL"]["net_pnl"] > b0_val_pnl
            and not info["DEV"]["hwm_halted"]
            and not info["VAL"]["hwm_halted"]
        )
        info["passes_dev_val_vs_b0"] = beats_b0_both
        if beats_b0_both and hid != "H1_STD_EXIT":
            stage_a_qualifiers.append(hid)

    print("[PHASE 4] Stage A qualifiers beating B0_KALMAN in BOTH DEV & VAL:", stage_a_qualifiers, flush=True)

    stage_b_candidates: dict[str, tuple[str, clean.CleanSimConfig]] = {
        "B0_KALMAN": hypotheses["B0_KALMAN"],
        "H1_STD_EXIT": hypotheses["H1_STD_EXIT"],
        "H3a_STD_CAP80": hypotheses["H3a_STD_CAP80"],
        "H4_STD_NO_SW_SELL": hypotheses["H4_STD_NO_SW_SELL"],
    }
    for qid in stage_a_qualifiers:
        if qid not in stage_b_candidates:
            stage_b_candidates[qid] = hypotheses[qid]

    h1_comb = h1_dev_pnl + h1_val_pnl
    indiv_over_h1 = [
        hid for hid in ("H3a_STD_CAP80", "H4_STD_NO_SW_SELL", "H5_STD_ADX20", "H6_STD_EMAEXT20", "H2b_STD_NO_EF")
        if hid in stage_a_results
        and stage_a_results[hid]["DEV"]["net_pnl"] >= h1_dev_pnl
        and stage_a_results[hid]["VAL"]["net_pnl"] >= h1_val_pnl
        and stage_a_results[hid]["DEV_plus_VAL_pnl"] > h1_comb
    ]
    combo_added: list[str] = []
    if "H3a_STD_CAP80" in indiv_over_h1 and "H4_STD_NO_SW_SELL" in indiv_over_h1:
        cid_c = "C1_STD_CAP80_NOSWSELL"
        stage_b_candidates[cid_c] = (
            "Combo of H3a (score<0.80) + H4 (no sideways SELL) on Standard exit",
            _base_cfg(use_kalman_exit=False, max_score=0.80, block_sell_sideways=True),
        )
        combo_added.append(cid_c)
    elif len(indiv_over_h1) >= 2:
        k1, k2 = indiv_over_h1[0], indiv_over_h1[1]
        c1, c2 = hypotheses[k1][1], hypotheses[k2][1]
        cid_c = f"C1_{k1}_{k2}"
        stage_b_candidates[cid_c] = (
            f"Combo of {k1} + {k2}",
            _base_cfg(
                use_kalman_exit=False,
                max_score=c1.max_score or c2.max_score,
                block_sell_sideways=c1.block_sell_sideways or c2.block_sell_sideways,
                adx_min=c1.adx_min or c2.adx_min,
                ema20_max_dist_atr=c1.ema20_max_dist_atr or c2.ema20_max_dist_atr,
                early_fail_mins=None if (c1.early_fail_mins is None or c2.early_fail_mins is None) else 15,
            ),
        )
        combo_added.append(cid_c)

    # Evaluate any newly added combos on DEV/VAL first
    if combo_added:
        for sp_name in ("DEV", "VAL"):
            cache = clean.precompute_span_cache(split_spans[sp_name], compute_adx=True)
            for cid_c in combo_added:
                dev_val_raw_runs[(cid_c, sp_name)] = clean.run_clean_backtest(split_spans[sp_name], stage_b_candidates[cid_c][1], cache=cache)
            del cache
            gc.collect()

    stage_b_results: dict[str, Any] = {
        cid: {
            "description": desc,
            "DEV": _summarize_run(dev_val_raw_runs[(cid, "DEV")]),
            "VAL": _summarize_run(dev_val_raw_runs[(cid, "VAL")]),
        }
        for cid, (desc, _) in stage_b_candidates.items()
    }

    # Evaluate Stage B finalists ONCE on HO & FULL (+ 1.5x stress + sensitivity)
    full_raw_runs: dict[str, dict[str, Any]] = {}
    for sp_name in ("HO", "FULL"):
        print(f"[PHASE 4] Precomputing SpanCache for {sp_name}...", flush=True)
        cache = clean.precompute_span_cache(split_spans[sp_name], compute_adx=True)
        for cid, (_desc, cfg) in stage_b_candidates.items():
            res = clean.run_clean_backtest(split_spans[sp_name], cfg, cache=cache)
            res_st = clean.run_clean_backtest(split_spans[sp_name], replace(cfg, cost_multiplier=1.5), cache=cache)
            if sp_name == "HO":
                stage_b_results[cid]["HO_untouched"] = _summarize_run(res)
                stage_b_results[cid]["STRESS_1_5x_HO"] = _summarize_run(res_st)
            else:
                res_unprot = clean.run_clean_backtest(split_spans[sp_name], replace(cfg, max_hwm_dd_pct=None), cache=cache)
                stage_b_results[cid]["FULL_8pct_hwm"] = _summarize_run(res)
                stage_b_results[cid]["FULL_unprotected"] = _summarize_run(res_unprot)
                stage_b_results[cid]["STRESS_1_5x_FULL"] = _summarize_run(res_st)
                full_raw_runs[cid] = res
            print(
                f"  [{sp_name}] {cid:<24s} trades={res['trades']:>3d} pnl=${res['pnl']:>+8.2f} pf={res['pf']:.3f} dd={res['mtm_max_dd_pct']:.2f}% halt={(res.get('hwm_kill_switch') or {}).get('halted')}",
                flush=True,
            )
        for c in (0.75, 0.80, 0.85, None):
            lbl = f"cap_{c}" if c is not None else "no_cap"
            r_s = clean.run_clean_backtest(split_spans[sp_name], _base_cfg(use_kalman_exit=False, max_score=c), cache=cache)
            sensitivity["score_cap_sweep_std_exit"][lbl][sp_name] = _summarize_run(r_s)
        for vt in (2.5, 3.5, 4.5, 5.5):
            r_k = clean.run_clean_backtest(split_spans[sp_name], _base_cfg(use_kalman_exit=True, kalman_vel_thresh=vt, kalman_rate_norm=True, kalman_min_hold_bars=3), cache=cache)
            sensitivity["kalman_vel_thresh_sweep"][f"rate_norm_vt_{vt}"][sp_name] = _summarize_run(r_k)
        del cache
        gc.collect()

    # Statistical significance + BH-FDR across hypotheses
    dv_pvals: dict[str, float] = {}
    b0_dv_trades = dev_val_raw_runs[("B0_KALMAN", "DEV")]["trade_list"] + dev_val_raw_runs[("B0_KALMAN", "VAL")]["trade_list"]
    for hid in hypotheses:
        if hid == "B0_KALMAN":
            continue
        htrades = dev_val_raw_runs[(hid, "DEV")]["trade_list"] + dev_val_raw_runs[(hid, "VAL")]["trade_list"]
        pt = _paired_daily_test(htrades, b0_dv_trades, SPLITS["DEV"][0], SPLITS["VAL"][1])
        dv_pvals[hid] = pt["p_value"]
        stage_a_results[hid]["paired_daily_dev_val_vs_b0"] = pt

    for hid, padj in _bh_fdr(dv_pvals).items():
        stage_a_results[hid]["bh_fdr_p_dev_val"] = padj

    raw_pvals_full: dict[str, float] = {}
    b0_full_trades = full_raw_runs["B0_KALMAN"]["trade_list"]
    for cid in stage_b_candidates:
        r_full = full_raw_runs[cid]
        pnls_full = np.array([float(t["pnl"]) for t in r_full["trade_list"]], dtype=np.float64)
        ci_lo, ci_hi = _bootstrap_mean_ci(pnls_full)
        dt = _paired_daily_test(r_full["trade_list"], b0_full_trades, *SPLITS["FULL"])
        stage_b_results[cid]["stats_full"] = {
            "expectancy_bootstrap_95ci": [ci_lo, ci_hi],
            "paired_daily_vs_b0": dt,
        }
        if cid != "B0_KALMAN":
            raw_pvals_full[cid] = dt["p_value"]

    for cid, padj in _bh_fdr(raw_pvals_full).items():
        stage_b_results[cid]["stats_full"]["bh_fdr_p_vs_b0"] = padj

    # Approval Gate
    approval_eval = {}
    b0_b = stage_b_results["B0_KALMAN"]
    h1_b = stage_b_results["H1_STD_EXIT"]
    for cid, info in stage_b_results.items():
        if cid == "B0_KALMAN":
            continue
        beats_b0_all = (
            info["DEV"]["net_pnl"] > b0_b["DEV"]["net_pnl"]
            and info["VAL"]["net_pnl"] > b0_b["VAL"]["net_pnl"]
            and info["HO_untouched"]["net_pnl"] > b0_b["HO_untouched"]["net_pnl"]
            and info["FULL_8pct_hwm"]["net_pnl"] > b0_b["FULL_8pct_hwm"]["net_pnl"]
        )
        survives_hwm_all = not info["FULL_8pct_hwm"]["hwm_halted"] and not info["HO_untouched"]["hwm_halted"]
        positive_ex_top2 = info["FULL_8pct_hwm"]["pnl_ex_top2_days"] > 0 and info["FULL_8pct_hwm"]["pnl_ex_top2_trades"] > 0
        positive_stress = info["STRESS_1_5x_FULL"]["net_pnl"] > b0_b["STRESS_1_5x_FULL"]["net_pnl"] and info["STRESS_1_5x_HO"]["net_pnl"] > 0
        beats_h1_all = (
            cid == "H1_STD_EXIT"
            or (
                info["DEV"]["net_pnl"] >= h1_b["DEV"]["net_pnl"]
                and info["VAL"]["net_pnl"] >= h1_b["VAL"]["net_pnl"]
                and info["HO_untouched"]["net_pnl"] > h1_b["HO_untouched"]["net_pnl"]
                and info["STRESS_1_5x_FULL"]["net_pnl"] > h1_b["STRESS_1_5x_FULL"]["net_pnl"]
            )
        )
        approval_eval[cid] = {
            "beats_b0_dev_val_ho_full": beats_b0_all,
            "survives_hwm_full_and_ho": survives_hwm_all,
            "positive_ex_top2_days_and_trades": positive_ex_top2,
            "passes_1_5x_cost_stress": positive_stress,
            "beats_or_is_h1_across_splits": beats_h1_all,
            "APPROVED": bool(beats_b0_all and survives_hwm_all and positive_stress and beats_h1_all),
        }

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "splits": {k: [s.isoformat(), e.isoformat()] for k, (s, e) in SPLITS.items()},
        "cost_model": "fusion_ctrader_demo ($12/M per side, 0.0533p slip, NY 17:00 swap), 8% HWM active",
        "stage_a_screen_dev_val": stage_a_results,
        "stage_a_qualifiers_over_b0": stage_a_qualifiers,
        "stage_b_out_of_sample_and_stress": stage_b_results,
        "parameter_sensitivity": sensitivity,
        "approval_gate_evaluation": approval_eval,
    }
    (G1_DIR / "g1_phase4_research_results.json").write_text(
        json.dumps(out, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps({
        "stage_a_qualifiers": stage_a_qualifiers,
        "approval_gate": approval_eval,
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
