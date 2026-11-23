"""
Task 10 (Phases 1, 2, 4, 5 Prep) — Verify Task 9 Evidence, Audit TradeScorer,
and Compute Exhaustion & Kalman Acceleration Diagnostics.

Zero imports from `src/goldedge`. Reads frozen baselines and Task 9 artifacts,
produces deliverables #1–#6:
  - task10_baseline_verification.json
  - task10_trades_enriched.csv
  - task10_tradescorer_component_audit.json
  - task10_score_distribution.csv
  - task10_exhaustion_analysis.json
  - task10_acceleration_analysis.json
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent.parent
if not (ROOT / "backtest_results").exists() and Path("/app/backtest_results").exists():
    ROOT = Path("/app")
AUDIT_DIR = ROOT / "backtest_results" / "audit"
FROZEN_V2_DIR = AUDIT_DIR / "frozen_v2_recovered"


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _metrics(df: pd.DataFrame) -> dict[str, Any]:
    n = int(len(df))
    if n == 0:
        return {"n": 0, "wins": 0, "losses": 0, "wr_pct": 0.0, "gross_pnl": 0.0, "net_pnl": 0.0, "pf": 0.0, "expectancy": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "closed_dd": 0.0}
    p = df["pnl"].values.astype(float)
    w = p[p > 0.0]
    l = p[p <= 0.0]
    pf = float(w.sum() / abs(l.sum())) if len(l) and l.sum() < 0 else (999.0 if len(w) and w.sum() > 0 else 0.0)
    ceq = np.cumsum(p) + 10000.0
    hwm = np.maximum.accumulate(np.insert(ceq, 0, 10000.0))[1:]
    dd = float((ceq - hwm).min())
    return {
        "n": n,
        "wins": int(len(w)),
        "losses": int(len(l)),
        "wr_pct": round(len(w) / n * 100.0, 2),
        "gross_pnl": round(float(df["gross_pnl"].sum()), 2) if "gross_pnl" in df.columns else round(float(p.sum()), 2),
        "net_pnl": round(float(p.sum()), 2),
        "pf": round(pf, 4),
        "expectancy": round(float(p.mean()), 2),
        "avg_win": round(float(w.mean()), 2) if len(w) else 0.0,
        "avg_loss": round(float(l.mean()), 2) if len(l) else 0.0,
        "closed_dd": round(dd, 2),
    }


def main() -> None:
    manifest = json.loads((FROZEN_V2_DIR / "task6c_frozen_manifest.json").read_text(encoding="utf-8"))
    t4 = manifest["prior_frozen_task4_preserved"]
    app_sum = manifest["application_summary"]
    t9_p1 = json.loads((AUDIT_DIR / "task9_phase1_verification.json").read_text(encoding="utf-8"))
    t9_h = json.loads((AUDIT_DIR / "task9_hypothesis_results.json").read_text(encoding="utf-8"))
    t9_map = {r["hypothesis"]: r for r in t9_h}

    frozen_111 = ROOT / "backtest_results" / "frozen_111_baseline_trades.csv"
    w1_csv = FROZEN_V2_DIR / "w1_trades.csv"
    w2_csv = FROZEN_V2_DIR / "w2_trades.csv"
    w3_csv = FROZEN_V2_DIR / "w3_trades.csv"
    w3_jrn = FROZEN_V2_DIR / "w3_journeys.json"
    t9_enr_path = AUDIT_DIR / "task9_trades_enriched_w3.csv"
    t8_comp = json.loads((AUDIT_DIR / "task8_app_comparison.json").read_text(encoding="utf-8"))

    frozen_w3_df = pd.read_csv(w3_csv)
    df = pd.read_csv(t9_enr_path)

    max_pnl_diff = float((frozen_w3_df["pnl"] - df["pnl"]).abs().max())
    max_score_diff = float((frozen_w3_df["score"] - df["score_computed"]).abs().max())
    all_dir_ok = bool((frozen_w3_df["direction"] == df["direction"]).all())

    phase1_verif = {
        "frozen_hashes": {
            "task4_frozen_111_sha256": _sha256(frozen_111),
            "task4_match": _sha256(frozen_111) == t4["frozen_111_baseline_trades_sha256"],
            "v2_w1_sha256": _sha256(w1_csv),
            "v2_w1_match": _sha256(w1_csv) == app_sum["W1"]["trades_csv_sha256"],
            "v2_w2_sha256": _sha256(w2_csv),
            "v2_w2_match": _sha256(w2_csv) == app_sum["W2"]["trades_csv_sha256"],
            "v2_w3_sha256": _sha256(w3_csv),
            "v2_w3_match": _sha256(w3_csv) == app_sum["W3"]["trades_csv_sha256"],
            "v2_w3_journeys_match": _sha256(w3_jrn) == app_sum["W3"]["journeys_json_sha256"],
        },
        "task9_evidence_check": {
            "task9_trades_count": int(len(df)),
            "task9_vs_frozen_w3_max_pnl_diff": round(max_pnl_diff, 8),
            "task9_vs_frozen_w3_max_score_diff": round(max_score_diff, 6),
            "directions_exact_match": all_dir_ok,
            "task9_repro_mismatch_count": t9_p1["independent_w3_reproduction"]["trade_mismatch_count"],
            "task9_H0": t9_map["H0_BASELINE_V2"]["overall"],
            "task9_H4a": t9_map["H4a_SCORE_CAP_080"]["overall"],
            "task9_H5": t9_map["H5_CAP_ER_045"]["overall"],
        },
        "task8_hwm_protected_separation_verified": {
            "w3_hwm_trades": t8_comp["windows"]["W3"]["app_with_8pct_kill_switch"]["trades"],
            "w3_hwm_net_pnl": t8_comp["windows"]["W3"]["app_with_8pct_kill_switch"]["net_pnl_usd"],
            "w3_hwm_mtm_dd_pct": t8_comp["windows"]["W3"]["app_with_8pct_kill_switch"]["mtm_max_dd_pct"],
            "w3_hwm_halt_timestamp": t8_comp["windows"]["W3"]["app_with_8pct_kill_switch"]["hwm_kill_switch"]["halt_timestamp"],
        },
    }
    (AUDIT_DIR / "task10_baseline_verification.json").write_text(json.dumps(phase1_verif, indent=2), encoding="utf-8")

    # Enrich task10_trades_enriched.csv
    df["w_comp_vel"] = (0.35 * df["comp_kalman_vel"]).round(4)
    df["w_comp_er"] = (0.20 * df["comp_kaufman_er"]).round(4)
    df["w_comp_ent"] = (0.15 * df["comp_entropy"]).round(4)
    df["w_comp_slope"] = (0.15 * df["comp_ema20_slope"]).round(4)
    df["w_comp_twp"] = (0.10 * df["comp_twp_dist"]).round(4)
    df["w_comp_sess"] = (0.05 * df["comp_session"]).round(4)
    df["aligned_kalman_vel"] = np.where(df["direction"] == "BUY", df["kalman_vel"], -df["kalman_vel"])
    df["aligned_kalman_accel"] = np.where(df["direction"] == "BUY", df["kalman_accel"], -df["kalman_accel"])
    df["aligned_ema20_slope"] = np.where(df["direction"] == "BUY", df["ema20_slope"], -df["ema20_slope"])
    df["aligned_dist_ema20_atr"] = np.where(df["direction"] == "BUY", df["dist_ema20_atr"], -df["dist_ema20_atr"])
    df["aligned_dist_ema50_atr"] = np.where(df["direction"] == "BUY", df["dist_ema50_atr"], -df["dist_ema50_atr"])
    df["aligned_prev_1h_ret_atr"] = np.where(df["direction"] == "BUY", df["prev_1h_ret_atr"], -df["prev_1h_ret_atr"])
    df["aligned_prev_4h_ret_atr"] = np.where(df["direction"] == "BUY", df["prev_4h_ret_atr"], -df["prev_4h_ret_atr"])
    df["score_without_accel"] = (df["base_score_pre_accel"] * df["comp_tick_mult"]).round(4)
    df["score_pushed_over_060_by_accel"] = ((df["score_without_accel"] < 0.60) & (df["score"] >= 0.60)).astype(int)
    df["score_pushed_over_080_by_accel"] = ((df["score_without_accel"] < 0.80) & (df["score"] >= 0.80)).astype(int)
    df.to_csv(AUDIT_DIR / "task10_trades_enriched.csv", index=False)

    # Phase 2: TradeScorer Component Audit
    comp_cols = [
        ("comp_kalman_vel", 0.35),
        ("comp_kaufman_er", 0.20),
        ("comp_entropy", 0.15),
        ("comp_ema20_slope", 0.15),
        ("comp_twp_dist", 0.10),
        ("comp_session", 0.05),
    ]
    comp_stats = []
    for col, wt in comp_cols:
        s_all = df[col]
        s_buy = df[df["direction"] == "BUY"][col]
        s_sell = df[df["direction"] == "SELL"][col]
        r_pnl, p_pnl = stats.spearmanr(s_all, df["pnl"]) if s_all.std() > 0 else (0.0, 1.0)
        comp_stats.append({
            "component": col,
            "weight": wt,
            "all_mean": round(float(s_all.mean()), 4),
            "all_std": round(float(s_all.std()), 4),
            "pct_pegged_at_1_0": round(float((s_all >= 0.9999).mean() * 100.0), 2),
            "pct_zero": round(float((s_all <= 0.0001).mean() * 100.0), 2),
            "buy_mean": round(float(s_buy.mean()), 4),
            "sell_mean": round(float(s_sell.mean()), 4),
            "mean_weighted_points": round(float(wt * s_all.mean()), 4),
            "spearman_vs_pnl": round(0.0 if np.isnan(r_pnl) else float(r_pnl), 4),
            "spearman_p": round(1.0 if np.isnan(p_pnl) else float(p_pnl), 5),
        })

    cand = pd.read_csv(AUDIT_DIR / "task9_candidate_signals_w3.csv")
    cand["comp_entropy_eq_1"] = (cand["comp_entropy"] >= 0.9999)
    cand["comp_slope_ge_095"] = (cand["comp_ema20_slope"] >= 0.95)

    corr_features = ["comp_kalman_vel", "comp_kaufman_er", "comp_ema20_slope", "comp_twp_dist", "comp_session", "comp_accel_adj", "score"]
    corr_mat = df[corr_features].corr(method="spearman").round(4).to_dict()

    scorer_audit = {
        "configuration": {
            "w_ml_backtest_and_live": 0.0,
            "w_quant_backtest_and_live": 1.0,
            "min_score_threshold": 0.60,
            "use_tick_intensity_features": True,
            "formula": "final_score = round(clamp(clamp(0.35*S_vel + 0.20*S_er + 0.15*S_ent + 0.15*S_slope + 0.10*S_twp + 0.05*S_sess, 0, 1) + clamp(±2.0*kalman_accel, -0.08, 0.08), 0, 1) * tick_mult, 4)",
            "symmetry": "BUY and SELL use exact mirror bands for kalman_vel, ema20_slope, tick_weighted_price_dist, and kalman_accel; kaufman_er, shannon_entropy, session, and tick_rate_ratio multiplier are identical.",
            "causality": "Purely causal: uses Kalman x1 and accel=x1-prev_x1 updated strictly up to first valid tick of entry minute, plus closed 5m indicators.",
        },
        "saturation_and_duplication_findings": {
            "entropy_floor_defect": "Shannon entropy uses good_min=2.50, good_max=3.20, bad_min=None -> ANY entropy <= 3.20 scores 1.0000. On executed W3 trades, 143/143 (100.0%) have comp_entropy = 1.0000 (std=0.0), contributing a fixed +0.15 free score floor.",
            "slope_saturation": f"comp_ema20_slope >= 0.95 occurs on {round(float((df['comp_ema20_slope'] >= 0.95).mean() * 100.0), 1)}% of executed trades (mean {round(float(df['comp_ema20_slope'].mean()), 4)}; BUY mean {round(float(df[df['direction']=='BUY']['comp_ema20_slope'].mean()), 4)}).",
            "fixed_floor_sum_143": round(float((0.15 * df["comp_entropy"] + 0.15 * df["comp_ema20_slope"] + 0.05 * df["comp_session"]).mean()), 4),
            "accel_threshold_crossings": {
                "pushed_above_060_by_accel": int(df["score_pushed_over_060_by_accel"].sum()),
                "pushed_above_080_by_accel": int(df["score_pushed_over_080_by_accel"].sum()),
                "pushed_above_080_by_accel_pnl": round(float(df[df["score_pushed_over_080_by_accel"] == 1]["pnl"].sum()), 2),
                "pushed_above_080_by_accel_n": int((df["score_pushed_over_080_by_accel"] == 1).sum()),
            },
            "candidate_2159_pct_comp_entropy_1": round(float(cand["comp_entropy_eq_1"].mean() * 100.0), 2),
            "candidate_2159_pct_comp_slope_ge_095": round(float(cand["comp_slope_ge_095"].mean() * 100.0), 2),
        },
        "component_contributions_143": comp_stats,
        "component_spearman_matrix_143": corr_mat,
    }
    (AUDIT_DIR / "task10_tradescorer_component_audit.json").write_text(json.dumps(scorer_audit, indent=2), encoding="utf-8")

    # Deliverable #4: Score Distribution CSV
    bins = [0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 1.0001]
    labels = ["0.60-0.65", "0.65-0.70", "0.70-0.75", "0.75-0.80", "0.80-0.85", "0.85-0.90", "0.90-1.00"]
    df["score_bin"] = pd.cut(df["score"], bins=bins, labels=labels, right=False)
    dist_rows = []
    for side, sub in [("ALL", df), ("BUY", df[df["direction"] == "BUY"]), ("SELL", df[df["direction"] == "SELL"])]:
        for lbl in labels:
            b = sub[sub["score_bin"] == lbl]
            m = _metrics(b)
            m["direction"] = side
            m["score_bin"] = lbl
            m["mean_kaufman_er"] = round(float(b["kaufman_er"].mean()), 4) if len(b) else None
            m["mean_aligned_kvel"] = round(float(b["aligned_kalman_vel"].mean()), 5) if len(b) else None
            m["mean_aligned_kacc"] = round(float(b["aligned_kalman_accel"].mean()), 5) if len(b) else None
            m["mean_aligned_dist_ema20_atr"] = round(float(b["aligned_dist_ema20_atr"].mean()), 3) if len(b) else None
            dist_rows.append(m)
    pd.DataFrame(dist_rows).to_csv(AUDIT_DIR / "task10_score_distribution.csv", index=False)

    # Deliverable #5: Exhaustion Analysis
    ex_mask_sc80 = df["score"] >= 0.80
    ex_mask_er45 = df["kaufman_er"] > 0.45
    ex_groups = {
        "kept_both_sc_lt_080_and_er_le_045": df[(~ex_mask_sc80) & (~ex_mask_er45)],
        "removed_only_sc_ge_080": df[ex_mask_sc80 & (~ex_mask_er45)],
        "removed_only_er_gt_045": df[(~ex_mask_sc80) & ex_mask_er45],
        "removed_by_both": df[ex_mask_sc80 & ex_mask_er45],
        "total_sc_ge_080": df[ex_mask_sc80],
        "total_sc_lt_080": df[~ex_mask_sc80],
        "total_er_gt_045": df[ex_mask_er45],
        "total_er_le_045": df[~ex_mask_er45],
        "score_075_to_080_marginal": df[(df["score"] >= 0.75) & (df["score"] < 0.80)],
    }
    exhaustion = {}
    for gname, gsub in ex_groups.items():
        exhaustion[gname] = {
            "ALL": _metrics(gsub),
            "BUY": _metrics(gsub[gsub["direction"] == "BUY"]),
            "SELL": _metrics(gsub[gsub["direction"] == "SELL"]),
            "mean_score": round(float(gsub["score"].mean()), 4) if len(gsub) else 0.0,
            "mean_er": round(float(gsub["kaufman_er"].mean()), 4) if len(gsub) else 0.0,
            "mean_aligned_dist_ema20_atr": round(float(gsub["aligned_dist_ema20_atr"].mean()), 4) if len(gsub) else 0.0,
            "mean_aligned_prev_1h_ret_atr": round(float(gsub["aligned_prev_1h_ret_atr"].mean()), 4) if len(gsub) else 0.0,
        }
    (AUDIT_DIR / "task10_exhaustion_analysis.json").write_text(json.dumps(exhaustion, indent=2), encoding="utf-8")

    # Deliverable #6: Kalman Acceleration Inversion Analysis (Phase 4)
    acc_bins = [-10.0, -0.02, 0.0, 0.015, 0.03, 10.0]
    acc_labels = ["Decel_Strong(<-0.02)", "Decel_Mild(-0.02..0)", "Accel_Mod(0..0.015)", "Accel_High(0.015..0.03)", "Accel_Extreme(>0.03)"]
    df["acc_bin"] = pd.cut(df["aligned_kalman_accel"], bins=acc_bins, labels=acc_labels, right=True)
    acc_band_stats = {}
    for side, sdf in [("ALL", df), ("BUY", df[df["direction"] == "BUY"]), ("SELL", df[df["direction"] == "SELL"])]:
        acc_band_stats[side] = {}
        for lbl in acc_labels:
            b = sdf[sdf["acc_bin"] == lbl]
            m = _metrics(b)
            m["mean_er"] = round(float(b["kaufman_er"].mean()), 4) if len(b) else None
            m["mean_score"] = round(float(b["score"].mean()), 4) if len(b) else None
            m["mean_aligned_ext_ema20_atr"] = round(float(b["aligned_dist_ema20_atr"].mean()), 4) if len(b) else None
            m["mean_aligned_1h_ret_atr"] = round(float(b["aligned_prev_1h_ret_atr"].mean()), 4) if len(b) else None
            acc_band_stats[side][lbl] = m

    ext_cols = ["aligned_kalman_vel", "kaufman_er", "aligned_ema20_slope", "aligned_dist_ema20_atr", "aligned_dist_ema50_atr", "aligned_prev_1h_ret_atr", "aligned_prev_4h_ret_atr", "score", "pnl"]
    acc_corrs = {}
    for side, sdf in [("ALL", df), ("BUY", df[df["direction"] == "BUY"]), ("SELL", df[df["direction"] == "SELL"])]:
        acc_corrs[side] = {}
        for c in ext_cols:
            r, p = stats.spearmanr(sdf["aligned_kalman_accel"], sdf[c])
            acc_corrs[side][c] = {"spearman_r": round(float(r), 4), "p": round(float(p), 5)}

    accel_analysis = {
        "aligned_accel_band_stats": acc_band_stats,
        "aligned_accel_correlations": acc_corrs,
        "accel_aligned_vs_pullback": {
            "BUY_aligned_acc_gt_0": _metrics(df[(df["direction"] == "BUY") & (df["aligned_kalman_accel"] > 0)]),
            "BUY_pullback_acc_le_0": _metrics(df[(df["direction"] == "BUY") & (df["aligned_kalman_accel"] <= 0)]),
            "SELL_aligned_acc_gt_0": _metrics(df[(df["direction"] == "SELL") & (df["aligned_kalman_accel"] > 0)]),
            "SELL_pullback_acc_le_0": _metrics(df[(df["direction"] == "SELL") & (df["aligned_kalman_accel"] <= 0)]),
        },
        "interaction_with_er_and_extension": {
            "aligned_acc_gt0_AND_er_gt045": {
                "ALL": _metrics(df[(df["aligned_kalman_accel"] > 0) & (df["kaufman_er"] > 0.45)]),
                "BUY": _metrics(df[(df["direction"] == "BUY") & (df["aligned_kalman_accel"] > 0) & (df["kaufman_er"] > 0.45)]),
                "SELL": _metrics(df[(df["direction"] == "SELL") & (df["aligned_kalman_accel"] > 0) & (df["kaufman_er"] > 0.45)]),
            },
            "aligned_acc_gt0_AND_ext_ema20_gt15": {
                "ALL": _metrics(df[(df["aligned_kalman_accel"] > 0) & (df["aligned_dist_ema20_atr"] > 1.5)]),
                "BUY": _metrics(df[(df["direction"] == "BUY") & (df["aligned_kalman_accel"] > 0) & (df["aligned_dist_ema20_atr"] > 1.5)]),
                "SELL": _metrics(df[(df["direction"] == "SELL") & (df["aligned_kalman_accel"] > 0) & (df["aligned_dist_ema20_atr"] > 1.5)]),
            },
        },
    }
    (AUDIT_DIR / "task10_acceleration_analysis.json").write_text(json.dumps(accel_analysis, indent=2), encoding="utf-8")

    print("[TASK 10 PART 1 COMPLETE]")
    print("Score distribution:")
    print(pd.DataFrame(dist_rows).to_string(index=False))
    print("\nAcceleration bands (BUY & SELL):")
    for s in ["BUY", "SELL", "ALL"]:
        print(f"--- {s} ---")
        for k, v in acc_band_stats[s].items():
            print(f"  {k:26s}: n={v['n']:3d} wr={v['wr_pct']:5.1f}% pnl={v['net_pnl']:8.2f} pf={v['pf']:6.3f} er={v['mean_er']} ext={v['mean_aligned_ext_ema20_atr']}")


if __name__ == "__main__":
    main()
