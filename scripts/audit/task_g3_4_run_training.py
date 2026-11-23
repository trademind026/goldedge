"""
TASK G3.4 — Phases A–I: Regularized Macro & Micro ML Training, Feature Ablation,
Overfitting/Probability Diagnostics, Discrete-Tick Sensitivity & H3b Economic Relevance.

Uses verified G3.3 expanded 6.2-month Parquets (verifying exact SHA-256 hashes!)
and strictly enforces:
  - 4-way chronological purged + embargoed walk-forward folds (Train -> Inner Val -> Cal -> OOT Test)
  - Train-only preprocessing (`FoldIsolatedPreprocessor`)
  - Hyperparameter selection strictly on INNER VALIDATION (CALIBRATION reserved for G3.5)
  - Zero OOT test retuning; zero strategy/H3b changes; strict SHADOW_MODE.

Outputs 12 artifacts + versioned model bundles under:
  backtest_results/audit/task_g3_4_ml_model_research/
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
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.metrics import confusion_matrix, log_loss

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")

from src.goldedge.adapters.secondary.ml.leakage_proof_pipeline import evaluate_classification_fold
from src.goldedge.adapters.secondary.ml.model_trainer import (
    fit_baseline_models,
    fit_select_constrained_lightgbm,
    fit_select_logistic_regression,
)
from src.goldedge.domain.services.denoise_service import VolumeAdaptiveKalmanFilter
from src.goldedge.domain.services.ml_training_contract import (
    CAUSAL_MACRO_FEATURE_COLUMNS,
    CAUSAL_MICRO_FEATURE_COLUMNS,
    FEATURE_SCHEMA_VERSION,
    MACRO_EMBARGO_BASELINE_SEC,
    MICRO_EMBARGO_BASELINE_SEC,
    WalkForwardFoldSpec,
    assert_zero_leakage_columns,
    assign_purged_4way_walkforward_fold,
)

G33_EXP_DIR = Path("backtest_results/audit/task_g3_3_leakage_proof_pipeline/expanded_6m")
G32_DIR = Path("backtest_results/audit/task_g3_2_label_design")
OUT_DIR = Path("backtest_results/audit/task_g3_4_ml_model_research")
MODELS_DIR = OUT_DIR / "models"

EXPECTED_MACRO_SHA256 = "2E9A9D9B7215A50B75B6464FD1E3A17AA887BF415B5A683586E0220B045189E3"
EXPECTED_MICRO_SHA256 = "7B565DFAF77D2B23B987FF8EE64952B0875287D428EE2BE39B5DAF48C6778667"

EXPANDED_FOLDS: List[WalkForwardFoldSpec] = [
    WalkForwardFoldSpec(1, "2026-05-13T00:00:00Z", "2026-07-10T00:00:00Z", "2026-07-10T00:00:00Z", "2026-07-24T00:00:00Z", "2026-07-24T00:00:00Z", "2026-08-07T00:00:00Z", "2026-08-07T00:00:00Z", "2026-08-28T00:00:00Z"),
    WalkForwardFoldSpec(2, "2026-05-13T00:00:00Z", "2026-08-07T00:00:00Z", "2026-08-07T00:00:00Z", "2026-08-21T00:00:00Z", "2026-08-21T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-25T00:00:00Z"),
    WalkForwardFoldSpec(3, "2026-05-13T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-18T00:00:00Z", "2026-09-18T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-23T00:00:00Z"),
    WalkForwardFoldSpec(4, "2026-05-13T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-16T00:00:00Z", "2026-10-16T00:00:00Z", "2026-10-30T00:00:00Z", "2026-10-30T00:00:00Z", "2026-11-19T12:00:00Z"),
]

MACRO_GROUPS: Dict[str, List[str]] = {
    "trend_ema": ["ema20_ema50_spread_atr", "ema20_slope_atr", "h1_ema_trend_sign", "adx_14_5m", "kaufman_er_20"],
    "volatility_atr_bb": ["atr_14_pct", "atr_ratio_20", "bb_width_20", "bb_pos_20", "prior_realized_vol_12"],
    "momentum_rsi_ret": ["rsi_14_5m", "prior_ret_5m", "prior_ret_15m", "prior_ret_60m"],
    "structure_regime": ["dist_swing_low_atr", "dist_swing_high_atr", "regime_code"],
    "session_spread_activity": ["tick_rate_ratio", "entry_spread_bps", "hour_sin", "hour_cos"],
}

MICRO_GROUPS: Dict[str, List[str]] = {
    "spread": ["entry_spread_usd", "entry_spread_bps"],
    "prior_returns": ["prior_mid_ret_5t", "prior_mid_ret_10t", "prior_mid_ret_30t"],
    "volatility": ["short_term_vol_30t_usd"],
    "quote_timing_intensity": ["prior_interarrival_mean_10t", "prior_quote_intensity_30t"],
    "session_time": ["hour_sin", "hour_cos"],
}


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


def _day_block_diff_ci(loss_a: np.ndarray, loss_b: np.ndarray, day_ids: np.ndarray) -> Dict[str, Any]:
    """Paired day-block 95% CI on (loss_a - loss_b); negative means model A has lower log-loss than B."""
    diff = np.asarray(loss_a, dtype=np.float64) - np.asarray(loss_b, dtype=np.float64)
    uniq, inv = np.unique(day_ids, return_inverse=True)
    g = len(uniq)
    if g < 3:
        return {"mean_diff": round(float(np.mean(diff)), 6), "n_days": int(g), "ci95_low": None, "ci95_high": None, "significant_improvement": False}
    cnt = np.bincount(inv).astype(np.float64)
    d_means = np.bincount(inv, weights=diff) / np.maximum(1.0, cnt)
    mu = float(np.mean(d_means))
    se = float(np.std(d_means, ddof=1) / math.sqrt(g))
    lo, hi = mu - 1.96 * se, mu + 1.96 * se
    # Also check single-day concentration: would removing the single best day flip sign?
    if g > 3:
        best_day_idx = int(np.argmin(d_means))
        mu_ex_best = float(np.mean(np.delete(d_means, best_day_idx)))
    else:
        mu_ex_best = mu
    return {
        "n_days": int(g),
        "mean_diff_per_day": round(mu, 6),
        "se_day_block": round(se, 6),
        "ci95_low": round(lo, 6),
        "ci95_high": round(hi, 6),
        "significant_improvement_95pct": bool(hi < 0.0),
        "mean_diff_excluding_best_day": round(mu_ex_best, 6),
        "single_day_fragile": bool(mu < 0.0 and mu_ex_best >= 0.0),
    }


def _row_log_loss(y_true: np.ndarray, probs: np.ndarray, classes: Sequence[str]) -> np.ndarray:
    c2i = {c: i for i, c in enumerate(classes)}
    y_i = np.array([c2i[str(v)] for v in y_true], dtype=np.int64)
    p = np.clip(probs[np.arange(len(y_i)), y_i], 1e-12, 1.0 - 1e-12)
    return -np.log(p)


def _ece_and_class_reliability(y_true: np.ndarray, probs: np.ndarray, classes: Sequence[str]) -> Dict[str, Any]:
    c2i = {c: i for i, c in enumerate(classes)}
    y_i = np.array([c2i[str(v)] for v in y_true], dtype=np.int64)
    pred_i = np.argmax(probs, axis=1)
    conf = np.max(probs, axis=1)
    corr = (pred_i == y_i).astype(np.float64)

    edges = np.linspace(0.0, 1.0, 11)
    ece = 0.0
    n = max(1, len(y_i))
    bins = []
    for k in range(10):
        m = (conf >= edges[k]) & (conf < edges[k + 1] if k < 9 else conf <= edges[k + 1])
        cnt = int(np.sum(m))
        if cnt > 0:
            mp = float(np.mean(conf[m]))
            ma = float(np.mean(corr[m]))
            ece += (cnt / n) * abs(ma - mp)
            bins.append({"bin": f"[{edges[k]:.1f},{edges[k+1]:.1f}]", "count": cnt, "mean_prob": round(mp, 4), "emp_acc": round(ma, 4)})

    class_rel = {}
    for ci, cname in enumerate(classes):
        p_c = probs[:, ci]
        y_c = (y_i == ci).astype(np.float64)
        class_rel[cname] = {
            "mean_pred_prob": round(float(np.mean(p_c)), 5),
            "empirical_rate": round(float(np.mean(y_c)), 5),
            "prob_p10_p50_p90": [round(float(np.percentile(p_c, q)), 4) for q in (10, 50, 90)],
        }
    return {"ece_10bin": round(ece, 5), "reliability_bins": bins, "class_conditional": class_rel}


def _compute_causal_kalman_features(series_values: np.ndarray, dt_series: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Strictly causal 2D scalar Kalman velocity & acceleration (resets on gap >= 3600s)."""
    kf = VolumeAdaptiveKalmanFilter()
    n = len(series_values)
    vels = np.zeros(n, dtype=np.float64)
    accels = np.zeros(n, dtype=np.float64)
    prev_v = 0.0
    for i in range(n):
        if i > 0 and dt_series[i] >= 3600.0:
            kf = VolumeAdaptiveKalmanFilter()
            prev_v = 0.0
        _px, v = kf.update_tick(float(series_values[i]), volume=1.0)
        vels[i] = v
        accels[i] = v - prev_v
        prev_v = v
    return vels, accels


def run_g3_4() -> None:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    git_head = _git_rev()

    # 1. Verify exact G3.3 6.2-month Parquet SHA-256 hashes
    m_pq = G33_EXP_DIR / "g3_3_expanded_6m_macro_dataset.parquet"
    u_pq = G33_EXP_DIR / "g3_3_expanded_6m_micro_dataset.parquet"
    m_sha, u_sha = _sha256(m_pq), _sha256(u_pq)
    if m_sha != EXPECTED_MACRO_SHA256 or u_sha != EXPECTED_MICRO_SHA256:
        raise RuntimeError(f"DATASET_HASH_MISMATCH: macro={m_sha[:16]} micro={u_sha[:16]}")

    df_m = pd.read_parquet(m_pq)
    df_u = pd.read_parquet(u_pq)
    print(f"[G3.4] Verified 6.2m Parquets: Macro={len(df_m):,} ({m_sha[:12]}), Micro={len(df_u):,} ({u_sha[:12]})", flush=True)

    macro_cols = assert_zero_leakage_columns(CAUSAL_MACRO_FEATURE_COLUMNS)
    micro_cols = assert_zero_leakage_columns(CAUSAL_MICRO_FEATURE_COLUMNS)

    # Precompute strictly causal Kalman features for Phase E ablation ONLY (kept separate from frozen 21/10 schema)
    m_dt = np.concatenate(([0.0], np.diff(df_m["feature_cutoff_utc"].values.astype(np.float64))))
    # Reconstruct causal pseudo-price path from prior_ret_5m (cumulative product of 1 + prior_ret_5m) -> strictly causal!
    m_pseudo_px = 4000.0 * np.cumprod(1.0 + np.clip(df_m["prior_ret_5m"].fillna(0.0).values, -0.05, 0.05))
    df_m["_kalman_vel_causal"], df_m["_kalman_accel_causal"] = _compute_causal_kalman_features(m_pseudo_px, m_dt)

    u_dt = np.concatenate(([0.0], np.diff(df_u["feature_cutoff_utc"].values.astype(np.float64))))
    u_pseudo_px = 4000.0 * np.cumprod(1.0 + np.clip(df_u["prior_mid_ret_5t"].fillna(0.0).values, -0.02, 0.02))
    df_u["_kalman_vel_causal"], df_u["_kalman_accel_causal"] = _compute_causal_kalman_features(u_pseudo_px, u_dt)

    all_trials_log: List[Dict[str, Any]] = []
    saved_artifacts: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # MACRO WALK-FORWARD TRAINING (Primary Target: net_class_010 [UP, DOWN, FLAT])
    # ------------------------------------------------------------------
    print("[G3.4] Training Macro models across 4 expanding 4-way purged folds...", flush=True)
    macro_classes = ["UP", "DOWN", "FLAT"]
    m_starts = df_m["label_obs_start_utc"].values.astype(np.float64)
    m_ends = df_m["label_obs_end_utc"].values.astype(np.float64)

    macro_fold_results: List[Dict[str, Any]] = []
    macro_oof_chunks: List[pd.DataFrame] = []
    macro_lgb_importances: Dict[int, np.ndarray] = {}
    macro_overfit_rows: List[Dict[str, Any]] = []

    for fspec in EXPANDED_FOLDS:
        tr_s, val_s, cal_s, tst_s, tst_e = (
            _ts(fspec.train_start_utc), _ts(fspec.val_start_utc), _ts(fspec.cal_start_utc),
            _ts(fspec.test_start_utc), _ts(fspec.test_end_utc),
        )
        sp = assign_purged_4way_walkforward_fold(m_starts, m_ends, tr_s, val_s, cal_s, tst_s, tst_e, MACRO_EMBARGO_BASELINE_SEC)
        d_tr = df_m.iloc[sp["train_indices"]]
        d_va = df_m.iloc[sp["val_indices"]]
        d_ts = df_m.iloc[sp["test_indices"]]
        y_tr, y_va, y_ts = d_tr["net_class_010"].values, d_va["net_class_010"].values, d_ts["net_class_010"].values

        tw = (fspec.train_start_utc, fspec.train_end_utc)
        baselines = fit_baseline_models(d_tr, y_tr, macro_classes, macro_cols, "prior_ret_15m", "macro_net_010", m_sha, fspec.fold_id, tw, git_head)
        b3_art, b3_trials = fit_select_logistic_regression(d_tr, y_tr, d_va, y_va, macro_classes, macro_cols, val_s, "macro_net_010", m_sha, fspec.fold_id, tw, git_head)
        c1_art, c1_trials = fit_select_constrained_lightgbm(d_tr, y_tr, d_va, y_va, macro_classes, macro_cols, val_s, "macro_net_010", m_sha, fspec.fold_id, tw, git_head, min_child_grid=(150, 300), depth_grid=(2, 3))

        for t in b3_trials + c1_trials:
            all_trials_log.append({"horizon": "macro", "target": "net_class_010", "fold_id": fspec.fold_id, **t})

        models_map = {**baselines, "B3_LOGISTIC_L2": b3_art, "C1_LIGHTGBM_CONSTRAINED": c1_art}
        macro_lgb_importances[fspec.fold_id] = np.asarray(c1_art.estimator.feature_importances_, dtype=np.float64)

        fold_metrics: Dict[str, Any] = {"fold_id": fspec.fold_id, "test_window": [fspec.test_start_utc, fspec.test_end_utc], "n_train": len(d_tr), "n_val": len(d_va), "n_test": len(d_ts), "models": {}}
        oof_df = d_ts[["feature_cutoff_utc", "signal_decision_utc", "day_utc", "session_utc", "regime", "is_nonoverlap_15m", "net_class_010", "raw_class_010", "buy_ret_net", "sell_ret_net", "buy_barrier_010", "sell_barrier_010"]].copy()
        oof_df["fold_id"] = fspec.fold_id

        for mname, art in models_map.items():
            if mname in ("B3_LOGISTIC_L2", "C1_LIGHTGBM_CONSTRAINED"):
                p_path, p_sha = art.save(MODELS_DIR)
                saved_artifacts.append({"artifact_id": art.metadata.artifact_id, "path": str(p_path), "sha256": p_sha, "hyperparameters": art.metadata.hyperparameters})
            p_tr = art.predict_proba_df(d_tr)
            p_va = art.predict_proba_df(d_va)
            p_ts = art.predict_proba_df(d_ts)

            ev_ts = evaluate_classification_fold(y_ts, p_ts, macro_classes, d_ts["day_utc"].values, realized_returns=d_ts["buy_ret_net"].values)
            cm = confusion_matrix(y_ts, [macro_classes[i] for i in np.argmax(p_ts, axis=1)], labels=macro_classes).tolist()
            fold_metrics["models"][mname] = {**ev_ts, "confusion_matrix": cm}

            if mname in ("B1_TRAIN_PREVALENCE", "B2_CAUSAL_MOMENTUM", "B3_LOGISTIC_L2", "C1_LIGHTGBM_CONSTRAINED"):
                for ci, cname in enumerate(macro_classes):
                    oof_df[f"{mname}_p_{cname}"] = np.round(p_ts[:, ci], 6)

            if mname in ("B3_LOGISTIC_L2", "C1_LIGHTGBM_CONSTRAINED"):
                tr_ll = float(np.mean(_row_log_loss(y_tr, p_tr, macro_classes)))
                va_ll = float(np.mean(_row_log_loss(y_va, p_va, macro_classes)))
                ts_ll = float(np.mean(_row_log_loss(y_ts, p_ts, macro_classes)))
                pred_flat_pct = round(100.0 * float(np.mean(np.argmax(p_ts, axis=1) == 2)), 2)
                macro_overfit_rows.append(
                    {
                        "horizon": "macro",
                        "fold_id": fspec.fold_id,
                        "model": mname,
                        "train_log_loss": round(tr_ll, 5),
                        "val_log_loss": round(va_ll, 5),
                        "test_log_loss": round(ts_ll, 5),
                        "val_minus_train_gap": round(va_ll - tr_ll, 5),
                        "test_minus_train_gap": round(ts_ll - tr_ll, 5),
                        "pred_flat_collapse_pct": pred_flat_pct,
                    }
                )

        macro_fold_results.append(fold_metrics)
        macro_oof_chunks.append(oof_df)

    df_macro_oof = pd.concat(macro_oof_chunks, ignore_index=True)
    macro_oof_pq = OUT_DIR / "g3_4_macro_oof_predictions.parquet"
    df_macro_oof.to_parquet(macro_oof_pq, index=False, compression="snappy")

    # Pooled Macro OOT comparison & paired day-block CI vs B1_TRAIN_PREVALENCE
    macro_pooled: Dict[str, Any] = {}
    b1_m_ll_rows = _row_log_loss(df_macro_oof["net_class_010"].values, df_macro_oof[[f"B1_TRAIN_PREVALENCE_p_{c}" for c in macro_classes]].values, macro_classes)
    for mname in ("B1_TRAIN_PREVALENCE", "B2_CAUSAL_MOMENTUM", "B3_LOGISTIC_L2", "C1_LIGHTGBM_CONSTRAINED"):
        probs_m = df_macro_oof[[f"{mname}_p_{c}" for c in macro_classes]].values
        ev = evaluate_classification_fold(df_macro_oof["net_class_010"].values, probs_m, macro_classes, df_macro_oof["day_utc"].values)
        ll_rows = _row_log_loss(df_macro_oof["net_class_010"].values, probs_m, macro_classes)
        ci_vs_b1 = _day_block_diff_ci(ll_rows, b1_m_ll_rows, df_macro_oof["day_utc"].values) if mname != "B1_TRAIN_PREVALENCE" else None
        # Non-overlapping 15m subset evaluation too
        nonov_m = df_macro_oof["is_nonoverlap_15m"].values
        ev_nonov = evaluate_classification_fold(df_macro_oof.loc[nonov_m, "net_class_010"].values, probs_m[nonov_m], macro_classes, df_macro_oof.loc[nonov_m, "day_utc"].values)
        macro_pooled[mname] = {"pooled_5m": ev, "pooled_nonoverlap_15m": ev_nonov, "day_block_log_loss_diff_vs_B1": ci_vs_b1}

    # Macro regime & session stability for B3 & C1
    macro_regime_stab = {}
    for reg, g in df_macro_oof.groupby("regime"):
        b1_ll = float(np.mean(_row_log_loss(g["net_class_010"].values, g[[f"B1_TRAIN_PREVALENCE_p_{c}" for c in macro_classes]].values, macro_classes)))
        b3_ll = float(np.mean(_row_log_loss(g["net_class_010"].values, g[[f"B3_LOGISTIC_L2_p_{c}" for c in macro_classes]].values, macro_classes)))
        c1_ll = float(np.mean(_row_log_loss(g["net_class_010"].values, g[[f"C1_LIGHTGBM_CONSTRAINED_p_{c}" for c in macro_classes]].values, macro_classes)))
        macro_regime_stab[str(reg)] = {"n": int(len(g)), "B1_ll": round(b1_ll, 5), "B3_ll": round(b3_ll, 5), "C1_ll": round(c1_ll, 5), "C1_minus_B1": round(c1_ll - b1_ll, 5)}

    print(f"[G3.4 MACRO DONE] Pooled OOT LL: B1={macro_pooled['B1_TRAIN_PREVALENCE']['pooled_5m']['log_loss']:.5f}, B3={macro_pooled['B3_LOGISTIC_L2']['pooled_5m']['log_loss']:.5f}, C1={macro_pooled['C1_LIGHTGBM_CONSTRAINED']['pooled_5m']['log_loss']:.5f}", flush=True)

    # ------------------------------------------------------------------
    # MICRO WALK-FORWARD TRAINING (Targets: buy_timing_class, sell_timing_class, buy_timing_class_half_tick)
    # ------------------------------------------------------------------
    print("[G3.4] Training Micro models across 4 expanding 4-way purged folds...", flush=True)
    micro_classes = ["ENTER_NOW", "WAIT_ADVERSE", "NEUTRAL"]
    u_starts = df_u["earliest_entry_utc"].values.astype(np.float64)
    u_ends = df_u["label_obs_end_utc"].values.astype(np.float64)

    micro_targets = ["buy_timing_class", "sell_timing_class", "buy_timing_class_half_tick"]
    micro_metrics_by_target: Dict[str, Any] = {}
    micro_oof_buy_chunks: List[pd.DataFrame] = []
    micro_lgb_importances: Dict[int, np.ndarray] = {}
    micro_overfit_rows: List[Dict[str, Any]] = []

    for tgt in micro_targets:
        t_fold_res = []
        t_oof_list = []
        for fspec in EXPANDED_FOLDS:
            tr_s, val_s, cal_s, tst_s, tst_e = (
                _ts(fspec.train_start_utc), _ts(fspec.val_start_utc), _ts(fspec.cal_start_utc),
                _ts(fspec.test_start_utc), _ts(fspec.test_end_utc),
            )
            sp = assign_purged_4way_walkforward_fold(u_starts, u_ends, tr_s, val_s, cal_s, tst_s, tst_e, MICRO_EMBARGO_BASELINE_SEC)
            d_tr = df_u.iloc[sp["train_indices"]]
            d_va = df_u.iloc[sp["val_indices"]]
            d_ts = df_u.iloc[sp["test_indices"]]
            y_tr, y_va, y_ts = d_tr[tgt].values, d_va[tgt].values, d_ts[tgt].values

            tw = (fspec.train_start_utc, fspec.train_end_utc)
            baselines = fit_baseline_models(d_tr, y_tr, micro_classes, micro_cols, "prior_mid_ret_5t", f"micro_{tgt}", u_sha, fspec.fold_id, tw, git_head)
            b3_art, b3_trials = fit_select_logistic_regression(d_tr, y_tr, d_va, y_va, micro_classes, micro_cols, val_s, f"micro_{tgt}", u_sha, fspec.fold_id, tw, git_head, c_grid=(0.01, 0.1, 1.0))
            c1_art, c1_trials = fit_select_constrained_lightgbm(d_tr, y_tr, d_va, y_va, micro_classes, micro_cols, val_s, f"micro_{tgt}", u_sha, fspec.fold_id, tw, git_head, min_child_grid=(500, 1000), depth_grid=(2, 3))

            for t in b3_trials + c1_trials:
                all_trials_log.append({"horizon": "micro", "target": tgt, "fold_id": fspec.fold_id, **t})

            models_map = {**baselines, "B3_LOGISTIC_L2": b3_art, "C1_LIGHTGBM_CONSTRAINED": c1_art}
            if tgt == "buy_timing_class":
                micro_lgb_importances[fspec.fold_id] = np.asarray(c1_art.estimator.feature_importances_, dtype=np.float64)

            fm: Dict[str, Any] = {"fold_id": fspec.fold_id, "n_train": len(d_tr), "n_val": len(d_va), "n_test": len(d_ts), "models": {}}
            oof_u = d_ts[["feature_cutoff_utc", "earliest_entry_utc", "day_utc", "entry_spread_usd", "buy_timing_adv_usd", "sell_timing_adv_usd", "buy_timing_class", "sell_timing_class", "buy_timing_class_half_tick"]].copy()
            oof_u["fold_id"] = fspec.fold_id

            for mname, art in models_map.items():
                if tgt == "buy_timing_class" and mname in ("B3_LOGISTIC_L2", "C1_LIGHTGBM_CONSTRAINED"):
                    p_path, p_sha = art.save(MODELS_DIR)
                    saved_artifacts.append({"artifact_id": art.metadata.artifact_id, "path": str(p_path), "sha256": p_sha, "hyperparameters": art.metadata.hyperparameters})
                p_ts = art.predict_proba_df(d_ts)
                ev_ts = evaluate_classification_fold(y_ts, p_ts, micro_classes, d_ts["day_utc"].values)
                cm = confusion_matrix(y_ts, [micro_classes[i] for i in np.argmax(p_ts, axis=1)], labels=micro_classes).tolist()
                fm["models"][mname] = {**ev_ts, "confusion_matrix": cm}

                if mname in ("B1_TRAIN_PREVALENCE", "B2_CAUSAL_MOMENTUM", "B3_LOGISTIC_L2", "C1_LIGHTGBM_CONSTRAINED"):
                    for ci, cname in enumerate(micro_classes):
                        oof_u[f"{mname}_p_{cname}"] = np.round(p_ts[:, ci], 6)

                if tgt == "buy_timing_class" and mname in ("B3_LOGISTIC_L2", "C1_LIGHTGBM_CONSTRAINED"):
                    p_tr = art.predict_proba_df(d_tr)
                    p_va = art.predict_proba_df(d_va)
                    tr_ll = float(np.mean(_row_log_loss(y_tr, p_tr, micro_classes)))
                    va_ll = float(np.mean(_row_log_loss(y_va, p_va, micro_classes)))
                    ts_ll = float(np.mean(_row_log_loss(y_ts, p_ts, micro_classes)))
                    micro_overfit_rows.append(
                        {
                            "horizon": "micro_buy_timing",
                            "fold_id": fspec.fold_id,
                            "model": mname,
                            "train_log_loss": round(tr_ll, 5),
                            "val_log_loss": round(va_ll, 5),
                            "test_log_loss": round(ts_ll, 5),
                            "val_minus_train_gap": round(va_ll - tr_ll, 5),
                            "test_minus_train_gap": round(ts_ll - tr_ll, 5),
                        }
                    )

            t_fold_res.append(fm)
            t_oof_list.append(oof_u)

        df_t_oof = pd.concat(t_oof_list, ignore_index=True)
        if tgt == "buy_timing_class":
            micro_oof_buy_chunks = t_oof_list
            micro_oof_pq = OUT_DIR / "g3_4_micro_oof_predictions.parquet"
            df_t_oof.to_parquet(micro_oof_pq, index=False, compression="snappy")

        b1_u_ll = _row_log_loss(df_t_oof[tgt].values, df_t_oof[[f"B1_TRAIN_PREVALENCE_p_{c}" for c in micro_classes]].values, micro_classes)
        t_pooled = {}
        for mname in ("B1_TRAIN_PREVALENCE", "B2_CAUSAL_MOMENTUM", "B3_LOGISTIC_L2", "C1_LIGHTGBM_CONSTRAINED"):
            pr = df_t_oof[[f"{mname}_p_{c}" for c in micro_classes]].values
            ev = evaluate_classification_fold(df_t_oof[tgt].values, pr, micro_classes, df_t_oof["day_utc"].values)
            ll_r = _row_log_loss(df_t_oof[tgt].values, pr, micro_classes)
            ci_b1 = _day_block_diff_ci(ll_r, b1_u_ll, df_t_oof["day_utc"].values) if mname != "B1_TRAIN_PREVALENCE" else None
            t_pooled[mname] = {"pooled": ev, "day_block_log_loss_diff_vs_B1": ci_b1}
        micro_metrics_by_target[tgt] = {"folds": t_fold_res, "pooled": t_pooled}
        print(f"[G3.4 MICRO {tgt}] Pooled OOT LL: B1={t_pooled['B1_TRAIN_PREVALENCE']['pooled']['log_loss']:.5f}, B3={t_pooled['B3_LOGISTIC_L2']['pooled']['log_loss']:.5f}, C1={t_pooled['C1_LIGHTGBM_CONSTRAINED']['pooled']['log_loss']:.5f}", flush=True)

    # ------------------------------------------------------------------
    # PHASE E: Feature Group Ablation + Causal Kalman Addition Test (Paired on identical OOT rows)
    # ------------------------------------------------------------------
    print("[G3.4] Running Phase E controlled feature ablations & Kalman evaluation...", flush=True)

    def _run_ablation(df_src, starts, ends, emb_sec, base_cols, groups, target_col, classes, mcs_grid):
        # Compute paired pooled OOT log-loss for full_base, each drop_group, and +kalman_vel_accel
        def _eval_cols(cols_use: List[str]) -> Tuple[np.ndarray, np.ndarray]:
            ll_all, days_all = [], []
            for fspec in EXPANDED_FOLDS:
                sp = assign_purged_4way_walkforward_fold(
                    starts, ends, _ts(fspec.train_start_utc), _ts(fspec.val_start_utc), _ts(fspec.cal_start_utc),
                    _ts(fspec.test_start_utc), _ts(fspec.test_end_utc), emb_sec
                )
                d_tr, d_va, d_ts = df_src.iloc[sp["train_indices"]], df_src.iloc[sp["val_indices"]], df_src.iloc[sp["test_indices"]]
                art, _ = fit_select_constrained_lightgbm(
                    d_tr, d_tr[target_col].values, d_va, d_va[target_col].values, classes, cols_use,
                    _ts(fspec.val_start_utc), "ablation", "ablation", fspec.fold_id, (fspec.train_start_utc, fspec.train_end_utc),
                    git_head, min_child_grid=mcs_grid, depth_grid=(3,),
                )
                ll_all.append(_row_log_loss(d_ts[target_col].values, art.predict_proba_df(d_ts), classes))
                days_all.append(d_ts["day_utc"].values)
            return np.concatenate(ll_all), np.concatenate(days_all)

        base_ll, days_arr = _eval_cols(list(base_cols))
        out_ab: Dict[str, Any] = {"base_full_mean_ll": round(float(np.mean(base_ll)), 6), "drop_groups": {}, "kalman_addition": {}}
        for gname, gcols in groups.items():
            rem = [c for c in base_cols if c not in gcols]
            g_ll, _ = _eval_cols(rem)
            # Positive delta (g_ll - base_ll > 0) means removing the group HURT log-loss (group was useful!)
            out_ab["drop_groups"][gname] = {
                "dropped_cols": gcols,
                "ablated_mean_ll": round(float(np.mean(g_ll)), 6),
                "delta_ll_when_dropped": round(float(np.mean(g_ll) - np.mean(base_ll)), 6),
                "day_block_ci_dropped_minus_base": _day_block_diff_ci(g_ll, base_ll, days_arr),
            }
        # Kalman addition test
        kal_ll, _ = _eval_cols(list(base_cols) + ["_kalman_vel_causal", "_kalman_accel_causal"])
        out_ab["kalman_addition"] = {
            "added_cols": ["_kalman_vel_causal", "_kalman_accel_causal"],
            "with_kalman_mean_ll": round(float(np.mean(kal_ll)), 6),
            "delta_ll_with_kalman": round(float(np.mean(kal_ll) - np.mean(base_ll)), 6),
            "day_block_ci_kalman_minus_base": _day_block_diff_ci(kal_ll, base_ll, days_arr),
        }
        return out_ab

    ablation_macro = _run_ablation(df_m, m_starts, m_ends, MACRO_EMBARGO_BASELINE_SEC, macro_cols, MACRO_GROUPS, "net_class_010", macro_classes, (200,))
    ablation_micro = _run_ablation(df_u, u_starts, u_ends, MICRO_EMBARGO_BASELINE_SEC, micro_cols, MICRO_GROUPS, "buy_timing_class", micro_classes, (1000,))

    # ------------------------------------------------------------------
    # PHASE H: Economic Relevance to H3b (Strictly Out-of-Time Predictions Only!)
    # ------------------------------------------------------------------
    # Join isolated H3b simulated outcomes from G3.2 Macro Parquet on feature_cutoff_utc (Folds 2-4 overlap Aug-Nov)
    df_g32_m = pd.read_parquet(G32_DIR / "g3_2_macro_labels.parquet")[["feature_cutoff_utc", "h3b_sim_buy_r", "h3b_sim_sell_r"]]
    df_m_econ = df_macro_oof.merge(df_g32_m, on="feature_cutoff_utc", how="left")

    def _quintile_table(df_in: pd.DataFrame, prob_col: str, ret_col: str, h3b_r_col: str) -> List[Dict[str, Any]]:
        sub = df_in[[prob_col, ret_col, h3b_r_col]].dropna(subset=[prob_col, ret_col]).copy()
        sub["q"] = pd.qcut(sub[prob_col].rank(method="first"), 5, labels=["Q1_LOW", "Q2", "Q3", "Q4", "Q5_HIGH"])
        rows = []
        for q_lbl, g in sub.groupby("q", observed=False):
            rows.append(
                {
                    "quintile": str(q_lbl),
                    "n": int(len(g)),
                    "mean_pred_prob": round(float(g[prob_col].mean()), 4),
                    "mean_15m_ret_net_bps": round(float(g[ret_col].mean() * 1e4), 3),
                    "h3b_sim_n": int(g[h3b_r_col].notna().sum()),
                    "mean_h3b_sim_r": round(float(g[h3b_r_col].mean()), 4) if g[h3b_r_col].notna().any() else None,
                }
            )
        return rows

    df_u_oof_buy = pd.concat(micro_oof_buy_chunks, ignore_index=True)
    # Micro quintiles on immediate BUY timing advantage (entering NOW vs waiting 30 ticks)
    df_u_oof_buy["q_enter_now"] = pd.qcut(df_u_oof_buy["C1_LIGHTGBM_CONSTRAINED_p_ENTER_NOW"].rank(method="first"), 5, labels=["Q1_LOW", "Q2", "Q3", "Q4", "Q5_HIGH"])
    micro_econ_quintiles = [
        {
            "quintile": str(q_lbl),
            "n": int(len(g)),
            "mean_p_ENTER_NOW": round(float(g["C1_LIGHTGBM_CONSTRAINED_p_ENTER_NOW"].mean()), 4),
            "mean_p_WAIT_ADVERSE": round(float(g["C1_LIGHTGBM_CONSTRAINED_p_WAIT_ADVERSE"].mean()), 4),
            "mean_buy_timing_adv_usd": round(float(g["buy_timing_adv_usd"].mean()), 5),
            "empirical_enter_now_pct": round(100.0 * float((g["buy_timing_class"] == "ENTER_NOW").mean()), 2),
        }
        for q_lbl, g in df_u_oof_buy.groupby("q_enter_now", observed=False)
    ]

    # Feature importance rank stability across folds (Spearman correlation between Fold 1 and Fold 4)
    m_imp_rho = float(spearmanr(macro_lgb_importances[1], macro_lgb_importances[4]).statistic)
    u_imp_rho = float(spearmanr(micro_lgb_importances[1], micro_lgb_importances[4]).statistic)

    # ------------------------------------------------------------------
    # Save Deliverables 1-12
    # ------------------------------------------------------------------
    (OUT_DIR / "g3_4_training_manifest.json").write_text(
        json.dumps(
            {
                "task": "G3.4_TRAINING_MANIFEST",
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "git_head": git_head,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "macro_dataset_sha256": m_sha,
                "micro_dataset_sha256": u_sha,
                "macro_oof_sha256": _sha256(macro_oof_pq),
                "micro_oof_sha256": _sha256(OUT_DIR / "g3_4_micro_oof_predictions.parquet"),
                "saved_model_artifacts_count": len(saved_artifacts),
                "saved_model_artifacts": saved_artifacts,
                "total_hyperparameter_trials_logged": len(all_trials_log),
                "elapsed_sec": round(time.time() - t0, 2),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    (OUT_DIR / "g3_4_model_configurations.json").write_text(
        json.dumps({"task": "G3.4_MODEL_CONFIGURATIONS", "total_trials": len(all_trials_log), "trials": all_trials_log}, indent=2),
        encoding="utf-8",
    )

    (OUT_DIR / "g3_4_baseline_comparison.json").write_text(
        json.dumps(
            {
                "task": "G3.4_BASELINE_COMPARISON",
                "macro_net_010_pooled": macro_pooled,
                "micro_buy_timing_pooled": micro_metrics_by_target["buy_timing_class"]["pooled"],
                "micro_sell_timing_pooled": micro_metrics_by_target["sell_timing_class"]["pooled"],
                "micro_buy_timing_half_tick_pooled": micro_metrics_by_target["buy_timing_class_half_tick"]["pooled"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    (OUT_DIR / "g3_4_macro_walkforward_metrics.json").write_text(
        json.dumps({"task": "G3.4_MACRO_WALKFORWARD_METRICS", "folds": macro_fold_results, "pooled": macro_pooled, "regime_stability": macro_regime_stab}, indent=2),
        encoding="utf-8",
    )

    (OUT_DIR / "g3_4_micro_walkforward_metrics.json").write_text(
        json.dumps({"task": "G3.4_MICRO_WALKFORWARD_METRICS", "targets": micro_metrics_by_target}, indent=2),
        encoding="utf-8",
    )

    (OUT_DIR / "g3_4_feature_ablation.json").write_text(
        json.dumps({"task": "G3.4_PHASE_E_FEATURE_ABLATION", "macro_ablation": ablation_macro, "micro_ablation": ablation_micro}, indent=2),
        encoding="utf-8",
    )

    (OUT_DIR / "g3_4_overfitting_diagnostics.json").write_text(
        json.dumps(
            {
                "task": "G3.4_PHASE_F_OVERFITTING_DIAGNOSTICS",
                "total_trials_logged": len(all_trials_log),
                "macro_lgb_importance_spearman_f1_vs_f4": round(m_imp_rho, 4),
                "micro_lgb_importance_spearman_f1_vs_f4": round(u_imp_rho, 4),
                "macro_train_val_test_gaps": macro_overfit_rows,
                "micro_train_val_test_gaps": micro_overfit_rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    (OUT_DIR / "g3_4_probability_diagnostics.json").write_text(
        json.dumps(
            {
                "task": "G3.4_PHASE_I_PROBABILITY_DIAGNOSTICS",
                "note": "Raw uncalibrated probabilities only. CALIBRATION folds reserved for G3.5.",
                "macro_B3_LOGISTIC": _ece_and_class_reliability(df_macro_oof["net_class_010"].values, df_macro_oof[[f"B3_LOGISTIC_L2_p_{c}" for c in macro_classes]].values, macro_classes),
                "macro_C1_LIGHTGBM": _ece_and_class_reliability(df_macro_oof["net_class_010"].values, df_macro_oof[[f"C1_LIGHTGBM_CONSTRAINED_p_{c}" for c in macro_classes]].values, macro_classes),
                "micro_buy_B3_LOGISTIC": _ece_and_class_reliability(df_u_oof_buy["buy_timing_class"].values, df_u_oof_buy[[f"B3_LOGISTIC_L2_p_{c}" for c in micro_classes]].values, micro_classes),
                "micro_buy_C1_LIGHTGBM": _ece_and_class_reliability(df_u_oof_buy["buy_timing_class"].values, df_u_oof_buy[[f"C1_LIGHTGBM_CONSTRAINED_p_{c}" for c in micro_classes]].values, micro_classes),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    (OUT_DIR / "g3_4_discrete_tick_sensitivity.json").write_text(
        json.dumps(
            {
                "task": "G3.4_PHASE_G_DISCRETE_TICK_SENSITIVITY",
                "note": "Comparing frozen G3.2 strict_gt BUY timing target vs symmetric half_tick_buffer (+$0.005) across identical 4-way purged folds.",
                "frozen_strict_gt_pooled": micro_metrics_by_target["buy_timing_class"]["pooled"],
                "symmetric_half_tick_pooled": micro_metrics_by_target["buy_timing_class_half_tick"]["pooled"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    (OUT_DIR / "g3_4_h3b_economic_relevance.json").write_text(
        json.dumps(
            {
                "task": "G3.4_PHASE_H_ECONOMIC_RELEVANCE",
                "governance_warning": "Research-only OOT probability quintile diagnostics. NOT a verified RunBacktestUseCase portfolio backtest (deferred to G3.6).",
                "macro_C1_p_UP_quintiles_vs_BUY_outcomes": _quintile_table(df_m_econ, "C1_LIGHTGBM_CONSTRAINED_p_UP", "buy_ret_net", "h3b_sim_buy_r"),
                "macro_C1_p_DOWN_quintiles_vs_SELL_outcomes": _quintile_table(df_m_econ, "C1_LIGHTGBM_CONSTRAINED_p_DOWN", "sell_ret_net", "h3b_sim_sell_r"),
                "macro_B3_p_UP_quintiles_vs_BUY_outcomes": _quintile_table(df_m_econ, "B3_LOGISTIC_L2_p_UP", "buy_ret_net", "h3b_sim_buy_r"),
                "macro_B3_p_DOWN_quintiles_vs_SELL_outcomes": _quintile_table(df_m_econ, "B3_LOGISTIC_L2_p_DOWN", "sell_ret_net", "h3b_sim_sell_r"),
                "micro_C1_p_ENTER_NOW_quintiles_vs_BUY_timing_adv": micro_econ_quintiles,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"[G3.4 ALL 12 TRAINING ARTIFACTS DONE in {round(time.time() - t0, 2)}s]", flush=True)


if __name__ == "__main__":
    run_g3_4()
