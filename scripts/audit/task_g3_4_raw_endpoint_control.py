"""
TASK G3.4 Corrective Control (Issue 1):
Raw 0.10% Endpoint-Direction Macro Target (`raw_class_010`) vs Cost-Aware (`net_class_010`).

Strict discipline:
  - Uses exact verified G3.3 6.2-month Macro Parquet (SHA-256 verified).
  - Exact same 4 purged expanding walk-forward folds (Train -> Inner Val -> Cal [untouched] -> OOT Test).
  - Exact same model families & hyperparameter grids selected strictly on INNER VAL (zero OOT retuning).
  - Compares `raw_class_010` side-by-side against `net_class_010`, including conditional UP-vs-DOWN AUC
    and H3b economic relevance quintiles.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")

from src.goldedge.adapters.secondary.ml.leakage_proof_pipeline import evaluate_classification_fold
from src.goldedge.adapters.secondary.ml.model_trainer import (
    fit_baseline_models,
    fit_select_constrained_lightgbm,
    fit_select_logistic_regression,
)
from src.goldedge.domain.services.ml_training_contract import (
    CAUSAL_MACRO_FEATURE_COLUMNS,
    MACRO_EMBARGO_BASELINE_SEC,
    WalkForwardFoldSpec,
    assert_zero_leakage_columns,
    assign_purged_4way_walkforward_fold,
)

G33_EXP_DIR = Path("backtest_results/audit/task_g3_3_leakage_proof_pipeline/expanded_6m")
OUT_DIR = Path("backtest_results/audit/task_g3_4_ml_model_research")
EXPECTED_MACRO_SHA256 = "2E9A9D9B7215A50B75B6464FD1E3A17AA887BF415B5A683586E0220B045189E3"

EXPANDED_FOLDS: List[WalkForwardFoldSpec] = [
    WalkForwardFoldSpec(1, "2026-05-13T00:00:00Z", "2026-07-10T00:00:00Z", "2026-07-10T00:00:00Z", "2026-07-24T00:00:00Z", "2026-07-24T00:00:00Z", "2026-08-07T00:00:00Z", "2026-08-07T00:00:00Z", "2026-08-28T00:00:00Z"),
    WalkForwardFoldSpec(2, "2026-05-13T00:00:00Z", "2026-08-07T00:00:00Z", "2026-08-07T00:00:00Z", "2026-08-21T00:00:00Z", "2026-08-21T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-25T00:00:00Z"),
    WalkForwardFoldSpec(3, "2026-05-13T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-18T00:00:00Z", "2026-09-18T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-23T00:00:00Z"),
    WalkForwardFoldSpec(4, "2026-05-13T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-16T00:00:00Z", "2026-10-16T00:00:00Z", "2026-10-30T00:00:00Z", "2026-10-30T00:00:00Z", "2026-11-19T12:00:00Z"),
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _ts(iso_z: str) -> float:
    return datetime.fromisoformat(iso_z.replace("Z", "+00:00")).timestamp()


def _git_rev() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def _per_sample_logloss(y_true: np.ndarray, probs: np.ndarray, classes: List[str]) -> np.ndarray:
    c2i = {c: i for i, c in enumerate(classes)}
    idx = np.array([c2i[str(v)] for v in y_true], dtype=np.int64)
    p = np.clip(probs, 1e-15, 1.0)
    p = p / p.sum(axis=1, keepdims=True)
    return -np.log(p[np.arange(len(y_true)), idx])


def _day_block_diff_ci(loss_a: np.ndarray, loss_b: np.ndarray, day_ids: np.ndarray) -> Dict[str, Any]:
    diff = np.asarray(loss_a, dtype=np.float64) - np.asarray(loss_b, dtype=np.float64)
    uniq, inv = np.unique(day_ids, return_inverse=True)
    g = len(uniq)
    cnt = np.bincount(inv).astype(np.float64)
    d_means = np.bincount(inv, weights=diff) / np.maximum(1.0, cnt)
    mu = float(np.mean(d_means))
    se = float(np.std(d_means, ddof=1) / math.sqrt(g))
    lo, hi = mu - 1.96 * se, mu + 1.96 * se
    best_idx = int(np.argmin(d_means))
    mu_ex_best = float(np.mean(np.delete(d_means, best_idx)))
    return {
        "n_days": int(g),
        "mean_diff_per_day": round(mu, 6),
        "se_day_block": round(se, 6),
        "ci95_low": round(lo, 6),
        "ci95_high": round(hi, 6),
        "significant_improvement_95pct": bool(hi < 0.0),
        "mean_diff_excluding_best_day": round(mu_ex_best, 6),
        "single_day_fragile": bool((mu < 0.0) and (mu_ex_best >= 0.0)),
    }


def _conditional_up_vs_down_auc(y_true: pd.Series, p_up: np.ndarray, p_down: np.ndarray) -> Dict[str, Any]:
    mask = y_true.isin(["UP", "DOWN"]).to_numpy()
    y_bin = (y_true.to_numpy()[mask] == "UP").astype(int)
    pu = np.asarray(p_up, dtype=np.float64)[mask]
    pdn = np.asarray(p_down, dtype=np.float64)[mask]
    p_cond = pu / np.clip(pu + pdn, 1e-12, None)
    return {
        "n_moving": int(mask.sum()),
        "up_rate_conditional": round(float(y_bin.mean()), 5),
        "roc_auc_up_vs_down": round(float(roc_auc_score(y_bin, p_cond)), 5),
    }


def main() -> None:
    t0 = time.time()
    git_head = _git_rev()
    macro_path = G33_EXP_DIR / "g3_3_expanded_6m_macro_dataset.parquet"
    m_sha = _sha256(macro_path)
    if m_sha != EXPECTED_MACRO_SHA256:
        raise RuntimeError(f"Macro SHA-256 mismatch: {m_sha} != {EXPECTED_MACRO_SHA256}")

    df_m = pd.read_parquet(macro_path)
    macro_cols = assert_zero_leakage_columns(CAUSAL_MACRO_FEATURE_COLUMNS)
    classes = ["UP", "DOWN", "FLAT"]

    # 1. Target definition & distribution comparison
    raw_counts = {c: int((df_m["raw_class_010"] == c).sum()) for c in classes}
    net_counts = {c: int((df_m["net_class_010"] == c).sum()) for c in classes}
    crosstab = pd.crosstab(df_m["raw_class_010"], df_m["net_class_010"]).to_dict()
    agree_pct = round(float((df_m["raw_class_010"] == df_m["net_class_010"]).mean() * 100.0), 3)
    print(f"[RAW CONTROL] Macro rows={len(df_m):,}, agreement raw_class_010 vs net_class_010 = {agree_pct}%", flush=True)

    m_starts = df_m["label_obs_start_utc"].values.astype(np.float64)
    m_ends = df_m["label_obs_end_utc"].values.astype(np.float64)

    fold_reports: List[Dict[str, Any]] = []
    oof_parts: List[pd.DataFrame] = []

    for fspec in EXPANDED_FOLDS:
        tr_s, val_s, cal_s, tst_s, tst_e = (
            _ts(fspec.train_start_utc), _ts(fspec.val_start_utc), _ts(fspec.cal_start_utc),
            _ts(fspec.test_start_utc), _ts(fspec.test_end_utc),
        )
        sp = assign_purged_4way_walkforward_fold(
            m_starts, m_ends, tr_s, val_s, cal_s, tst_s, tst_e, MACRO_EMBARGO_BASELINE_SEC
        )
        d_tr = df_m.iloc[sp["train_indices"]]
        d_va = df_m.iloc[sp["val_indices"]]
        d_ts = df_m.iloc[sp["test_indices"]]
        # CALIBRATION strictly untouched!

        y_tr = d_tr["raw_class_010"].values
        y_va = d_va["raw_class_010"].values
        y_ts = d_ts["raw_class_010"].values
        days_ts = d_ts["day_utc"].values

        tw = (fspec.train_start_utc, fspec.train_end_utc)
        baselines = fit_baseline_models(
            d_tr, y_tr, classes, macro_cols, "prior_ret_15m",
            "macro_raw_010", m_sha, fspec.fold_id, tw, git_head,
        )
        b3_art, _ = fit_select_logistic_regression(
            d_tr, y_tr, d_va, y_va, classes, macro_cols, val_s,
            "macro_raw_010", m_sha, fspec.fold_id, tw, git_head,
        )
        c1_art, _ = fit_select_constrained_lightgbm(
            d_tr, y_tr, d_va, y_va, classes, macro_cols, val_s,
            "macro_raw_010", m_sha, fspec.fold_id, tw, git_head,
            min_child_grid=(150, 300), depth_grid=(2, 3),
        )

        models_map = {
            "B1_TRAIN_PREVALENCE": baselines["B1_TRAIN_PREVALENCE"],
            "B2_CAUSAL_MOMENTUM": baselines["B2_CAUSAL_MOMENTUM"],
            "B3_LOGISTIC_L2": b3_art,
            "C1_LIGHTGBM_CONSTRAINED": c1_art,
        }

        fold_models: Dict[str, Any] = {}
        oof_df = d_ts[[
            "feature_cutoff_utc", "signal_decision_utc", "day_utc", "session_utc",
            "regime", "is_nonoverlap_15m", "raw_class_010", "net_class_010",
            "buy_ret_net", "sell_ret_net", "buy_barrier_010", "sell_barrier_010",
        ]].copy()
        oof_df["fold_id"] = fspec.fold_id

        for mname, art in models_map.items():
            probs = art.predict_proba_df(d_ts)
            for ci, cname in enumerate(classes):
                oof_df[f"{mname}_p_{cname}"] = probs[:, ci]
            metrics = evaluate_classification_fold(
                y_true=y_ts, y_prob=probs, classes=classes, day_ids=days_ts
            )
            flat_pct = round(float((np.argmax(probs, axis=1) == 2).mean() * 100.0), 2)
            fold_models[mname] = {
                "log_loss": metrics["log_loss"],
                "multiclass_brier": metrics["multiclass_brier"],
                "accuracy": metrics["accuracy"],
                "balanced_accuracy": metrics["balanced_accuracy"],
                "macro_f1": metrics["macro_f1"],
                "roc_auc_ovr": metrics["roc_auc_ovr"],
                "pred_flat_collapse_pct": flat_pct,
                "conditional_up_vs_down": _conditional_up_vs_down_auc(
                    pd.Series(y_ts), probs[:, 0], probs[:, 1]
                ),
            }

        fold_reports.append({
            "fold_id": fspec.fold_id,
            "test_window": [fspec.test_start_utc, fspec.test_end_utc],
            "n_train": len(d_tr),
            "n_val": len(d_va),
            "n_test": len(d_ts),
            "selected_hyperparams": {
                "B3_LOGISTIC_L2": b3_art.metadata.hyperparameters,
                "C1_LIGHTGBM_CONSTRAINED": c1_art.metadata.hyperparameters,
            },
            "models": fold_models,
        })
        oof_parts.append(oof_df)
        print(
            f"  Fold {fspec.fold_id}: B1={fold_models['B1_TRAIN_PREVALENCE']['log_loss']:.5f}, "
            f"B3={fold_models['B3_LOGISTIC_L2']['log_loss']:.5f}, "
            f"C1={fold_models['C1_LIGHTGBM_CONSTRAINED']['log_loss']:.5f}, "
            f"C1_cond_AUC={fold_models['C1_LIGHTGBM_CONSTRAINED']['conditional_up_vs_down']['roc_auc_up_vs_down']:.5f}",
            flush=True,
        )

    oof_all = pd.concat(oof_parts, ignore_index=True)
    oof_path = OUT_DIR / "g3_4_macro_raw_endpoint_oof.parquet"
    oof_all.to_parquet(oof_path, index=False)

    y_pool = oof_all["raw_class_010"].values
    days_pool = oof_all["day_utc"].values
    no_mask = oof_all["is_nonoverlap_15m"].values.astype(bool)

    b1_probs = oof_all[[f"B1_TRAIN_PREVALENCE_p_{c}" for c in classes]].to_numpy()
    b1_ll_vec = _per_sample_logloss(y_pool, b1_probs, classes)

    pooled_raw: Dict[str, Any] = {}
    for mname in ["B1_TRAIN_PREVALENCE", "B2_CAUSAL_MOMENTUM", "B3_LOGISTIC_L2", "C1_LIGHTGBM_CONSTRAINED"]:
        probs = oof_all[[f"{mname}_p_{c}" for c in classes]].to_numpy()
        m5 = evaluate_classification_fold(y_true=y_pool, y_prob=probs, classes=classes, day_ids=days_pool)
        m15 = evaluate_classification_fold(y_true=y_pool[no_mask], y_prob=probs[no_mask], classes=classes, day_ids=days_pool[no_mask])
        cond = _conditional_up_vs_down_auc(oof_all["raw_class_010"], probs[:, 0], probs[:, 1])
        flat_pct = round(float((np.argmax(probs, axis=1) == 2).mean() * 100.0), 2)
        ll_diff = None if mname == "B1_TRAIN_PREVALENCE" else _day_block_diff_ci(
            _per_sample_logloss(y_pool, probs, classes), b1_ll_vec, days_pool
        )
        pooled_raw[mname] = {
            "pooled_5m": {
                "n_samples": m5["n_samples"],
                "unique_days": m5["unique_days"],
                "log_loss": m5["log_loss"],
                "multiclass_brier": m5["multiclass_brier"],
                "accuracy": m5["accuracy"],
                "naive_majority_accuracy": m5["naive_majority_accuracy"],
                "balanced_accuracy": m5["balanced_accuracy"],
                "macro_f1": m5["macro_f1"],
                "roc_auc_ovr": m5["roc_auc_ovr"],
                "pr_auc_ovr": m5["pr_auc_ovr"],
                "pred_flat_collapse_pct": flat_pct,
                "conditional_up_vs_down": cond,
            },
            "pooled_nonoverlap_15m": {
                "n_samples": m15["n_samples"],
                "log_loss": m15["log_loss"],
                "multiclass_brier": m15["multiclass_brier"],
                "accuracy": m15["accuracy"],
                "balanced_accuracy": m15["balanced_accuracy"],
                "roc_auc_ovr": m15["roc_auc_ovr"],
            },
            "day_block_log_loss_diff_vs_B1": ll_diff,
        }

    def _quintiles(prob_col: str, ret_col: str, barrier_col: str) -> List[Dict[str, Any]]:
        q_idx = pd.qcut(oof_all[prob_col], q=5, labels=["Q1_LOW", "Q2", "Q3", "Q4", "Q5_HIGH"], duplicates="drop")
        rows = []
        for q_lbl, g in oof_all.groupby(q_idx, observed=True):
            b = g[barrier_col]
            valid_b = b.isin(["TP_HIT", "SL_HIT", "TIMEOUT"])
            r_vals = np.where(b[valid_b] == "TP_HIT", 2.0, np.where(b[valid_b] == "SL_HIT", -1.0, 0.0))
            rows.append({
                "quintile": str(q_lbl),
                "n": int(len(g)),
                "mean_pred_prob": round(float(g[prob_col].mean()), 4),
                "mean_15m_ret_net_bps": round(float(g[ret_col].mean() * 1e4), 3),
                "h3b_sim_n": int(valid_b.sum()),
                "mean_h3b_sim_r": round(float(r_vals.mean()), 4) if len(r_vals) else None,
            })
        return rows

    net_base = json.loads((OUT_DIR / "g3_4_baseline_comparison.json").read_text(encoding="utf-8"))["macro_net_010_pooled"]
    net_oof = pd.read_parquet(OUT_DIR / "g3_4_macro_oof_predictions.parquet")
    net_cond_b3 = _conditional_up_vs_down_auc(
        net_oof["net_class_010"], net_oof["B3_LOGISTIC_L2_p_UP"].to_numpy(), net_oof["B3_LOGISTIC_L2_p_DOWN"].to_numpy()
    )
    net_cond_c1 = _conditional_up_vs_down_auc(
        net_oof["net_class_010"], net_oof["C1_LIGHTGBM_CONSTRAINED_p_UP"].to_numpy(), net_oof["C1_LIGHTGBM_CONSTRAINED_p_DOWN"].to_numpy()
    )

    dups = int(oof_all["feature_cutoff_utc"].duplicated().sum())
    max_simplex_err = 0.0
    for mname in ["B1_TRAIN_PREVALENCE", "B2_CAUSAL_MOMENTUM", "B3_LOGISTIC_L2", "C1_LIGHTGBM_CONSTRAINED"]:
        p = oof_all[[f"{mname}_p_{c}" for c in classes]].to_numpy()
        max_simplex_err = max(max_simplex_err, float(np.max(np.abs(p.sum(axis=1) - 1.0))))

    report = {
        "task": "G3.4_ISSUE1_RAW_ENDPOINT_CONTROL",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "macro_dataset_sha256": m_sha,
        "raw_oof_sha256": _sha256(oof_path),
        "target_verification": {
            "raw_class_010_counts": raw_counts,
            "net_class_010_counts": net_counts,
            "row_agreement_pct": agree_pct,
            "rows_differing": int((df_m["raw_class_010"] != df_m["net_class_010"]).sum()),
            "crosstab_raw_rows_vs_net_cols": crosstab,
        },
        "pooled_raw_class_010": pooled_raw,
        "folds_raw_class_010": fold_reports,
        "side_by_side_raw_vs_net": {
            "B1_log_loss": {"raw_class_010": pooled_raw["B1_TRAIN_PREVALENCE"]["pooled_5m"]["log_loss"], "net_class_010": net_base["B1_TRAIN_PREVALENCE"]["pooled_5m"]["log_loss"]},
            "B3_log_loss": {"raw_class_010": pooled_raw["B3_LOGISTIC_L2"]["pooled_5m"]["log_loss"], "net_class_010": net_base["B3_LOGISTIC_L2"]["pooled_5m"]["log_loss"]},
            "C1_log_loss": {"raw_class_010": pooled_raw["C1_LIGHTGBM_CONSTRAINED"]["pooled_5m"]["log_loss"], "net_class_010": net_base["C1_LIGHTGBM_CONSTRAINED"]["pooled_5m"]["log_loss"]},
            "B3_day_block_ll_diff_vs_B1": {"raw_class_010": pooled_raw["B3_LOGISTIC_L2"]["day_block_log_loss_diff_vs_B1"]["mean_diff_per_day"], "net_class_010": net_base["B3_LOGISTIC_L2"]["day_block_log_loss_diff_vs_B1"]["mean_diff_per_day"]},
            "C1_day_block_ll_diff_vs_B1": {"raw_class_010": pooled_raw["C1_LIGHTGBM_CONSTRAINED"]["day_block_log_loss_diff_vs_B1"]["mean_diff_per_day"], "net_class_010": net_base["C1_LIGHTGBM_CONSTRAINED"]["day_block_log_loss_diff_vs_B1"]["mean_diff_per_day"]},
            "B3_conditional_UP_vs_DOWN_AUC": {"raw_class_010": pooled_raw["B3_LOGISTIC_L2"]["pooled_5m"]["conditional_up_vs_down"]["roc_auc_up_vs_down"], "net_class_010": net_cond_b3["roc_auc_up_vs_down"]},
            "C1_conditional_UP_vs_DOWN_AUC": {"raw_class_010": pooled_raw["C1_LIGHTGBM_CONSTRAINED"]["pooled_5m"]["conditional_up_vs_down"]["roc_auc_up_vs_down"], "net_class_010": net_cond_c1["roc_auc_up_vs_down"]},
            "C1_pred_flat_collapse_pct": {"raw_class_010": pooled_raw["C1_LIGHTGBM_CONSTRAINED"]["pooled_5m"]["pred_flat_collapse_pct"], "net_class_010": 97.67},
        },
        "economic_relevance_quintiles_C1_raw": {
            "p_UP_vs_BUY": _quintiles("C1_LIGHTGBM_CONSTRAINED_p_UP", "buy_ret_net", "buy_barrier_010"),
            "p_DOWN_vs_SELL": _quintiles("C1_LIGHTGBM_CONSTRAINED_p_DOWN", "sell_ret_net", "sell_barrier_010"),
        },
        "integrity_checks": {
            "duplicate_timestamps": dups,
            "max_simplex_err": round(max_simplex_err, 9),
            "calibration_folds_untouched": True,
            "zero_oot_retuning": True,
            "status": "PASS" if (dups == 0 and max_simplex_err <= 1e-6) else "FAIL",
        },
        "elapsed_sec": round(time.time() - t0, 2),
    }

    out_json = OUT_DIR / "g3_4_macro_raw_endpoint_control.json"
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[RAW CONTROL DONE in {report['elapsed_sec']}s] status={report['integrity_checks']['status']}")
    print("Side-by-side summary:")
    print(json.dumps(report["side_by_side_raw_vs_net"], indent=2))
    print("Quintiles C1 raw p_UP vs BUY:")
    print(json.dumps(report["economic_relevance_quintiles_C1_raw"]["p_UP_vs_BUY"], indent=2))
    print("Quintiles C1 raw p_DOWN vs SELL:")
    print(json.dumps(report["economic_relevance_quintiles_C1_raw"]["p_DOWN_vs_SELL"], indent=2))


if __name__ == "__main__":
    main()
