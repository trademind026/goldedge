"""
TASK G3.5 — Phases A & B: Artifact Integrity, Calibration Data Isolation
and Multiclass/Binary Probability Calibration.

Strictly enforces:
  - Verification of G3.3 dataset SHA-256s and all 16 G3.4 frozen model SHA-256s.
  - Exact 4-way purged + embargoed chronological folds (TRAIN -> VAL -> CAL -> TEST).
  - Calibrator fitting & selection STRICTLY on the reserved CALIBRATION partition.
  - ZERO OOT exposure during calibration fitting or method selection.
  - Outputs:
      backtest_results/audit/task_g3_5_calibration_utility/g3_5_calibration_integrity.json
      backtest_results/audit/task_g3_5_calibration_utility/g3_5_probability_calibration.json
      backtest_results/audit/task_g3_5_calibration_utility/calibrators/*.joblib (+ .meta.json)
      backtest_results/audit/task_g3_5_calibration_utility/g3_5_macro_calibrated_{cal,oof}.parquet
      backtest_results/audit/task_g3_5_calibration_utility/g3_5_micro_calibrated_{cal,oof}.parquet
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import joblib
import numpy as np
import pandas as pd

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")

from src.goldedge.adapters.secondary.ml.calibration_utility import (
    CalibratorMetadata,
    MulticlassProbabilityCalibrator,
    VersionedCalibrationArtifact,
    day_block_paired_diff_ci,
    evaluate_binary_event_calibration,
    evaluate_multiclass_calibration,
    fit_select_calibrator_strict,
    per_sample_binary_logloss,
    per_sample_multiclass_logloss,
)
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
G34_DIR = Path("backtest_results/audit/task_g3_4_ml_model_research")
OUT_DIR = Path("backtest_results/audit/task_g3_5_calibration_utility")
CAL_MODELS_DIR = OUT_DIR / "calibrators"

EXPECTED_MACRO_SHA256 = "2E9A9D9B7215A50B75B6464FD1E3A17AA887BF415B5A683586E0220B045189E3"
EXPECTED_MICRO_SHA256 = "7B565DFAF77D2B23B987FF8EE64952B0875287D428EE2BE39B5DAF48C6778667"

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


def _verify_adjacent_purge(starts: np.ndarray, ends: np.ndarray, sp: Dict[str, Any], embargo: float) -> Dict[str, Any]:
    tr, va, ca, te = sp["train_indices"], sp["val_indices"], sp["cal_indices"], sp["test_indices"]
    b1 = bool((ends[tr].max() < starts[va].min()) and (starts[tr].max() + embargo <= starts[va].min()))
    b2 = bool((ends[va].max() < starts[ca].min()) and (starts[va].max() + embargo <= starts[ca].min()))
    b3 = bool((ends[ca].max() < starts[te].min()) and (starts[ca].max() + embargo <= starts[te].min()))
    overlap = len(set(tr) & set(va)) + len(set(va) & set(ca)) + len(set(ca) & set(te)) + len(set(tr) & set(te))
    return {
        "train_to_val_purged": b1,
        "val_to_cal_purged": b2,
        "cal_to_test_purged": b3,
        "index_overlap_count": int(overlap),
        "all_clean": bool(b1 and b2 and b3 and overlap == 0),
    }


def main() -> None:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CAL_MODELS_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # PHASE A: Artifact Integrity & Calibration Data Isolation Audit
    # ------------------------------------------------------------------
    m_pq = G33_EXP_DIR / "g3_3_expanded_6m_macro_dataset.parquet"
    u_pq = G33_EXP_DIR / "g3_3_expanded_6m_micro_dataset.parquet"
    m_sha = _sha256(m_pq)
    u_sha = _sha256(u_pq)
    if m_sha != EXPECTED_MACRO_SHA256 or u_sha != EXPECTED_MICRO_SHA256:
        raise RuntimeError(f"G3.3 Dataset SHA-256 mismatch: macro={m_sha}, micro={u_sha}")

    g34_manifest = json.loads((G34_DIR / "g3_4_training_manifest.json").read_text(encoding="utf-8"))
    model_checks: List[Dict[str, Any]] = []
    loaded_models: Dict[str, Any] = {}
    all_models_ok = True

    for entry in g34_manifest["saved_model_artifacts"]:
        aid = entry["artifact_id"]
        apath = Path(entry["path"])
        actual_sha = _sha256(apath)
        ok = actual_sha == entry["sha256"]
        art = joblib.load(apath)
        expected_cols = list(CAUSAL_MACRO_FEATURE_COLUMNS) if aid.startswith("macro_") else list(CAUSAL_MICRO_FEATURE_COLUMNS)
        expected_cls = ("UP", "DOWN", "FLAT") if aid.startswith("macro_") else ("ENTER_NOW", "WAIT_ADVERSE", "NEUTRAL")
        schema_ok = list(art.metadata.feature_columns) == expected_cols
        cls_ok = tuple(art.metadata.classes) == expected_cls
        if not (ok and schema_ok and cls_ok):
            all_models_ok = False
        model_checks.append({
            "artifact_id": aid,
            "sha256_match": ok,
            "feature_schema_match": schema_ok,
            "class_order_match": cls_ok,
            "sha256": actual_sha,
        })
        loaded_models[aid] = (art, actual_sha)

    if not all_models_ok:
        raise RuntimeError("Phase A failed: one or more G3.4 model artifacts failed integrity checks.")

    df_m = pd.read_parquet(m_pq)
    df_u = pd.read_parquet(u_pq)
    assert_zero_leakage_columns(CAUSAL_MACRO_FEATURE_COLUMNS)
    assert_zero_leakage_columns(CAUSAL_MICRO_FEATURE_COLUMNS)

    m_starts = df_m["label_obs_start_utc"].values.astype(np.float64)
    m_ends = df_m["label_obs_end_utc"].values.astype(np.float64)
    u_starts = df_u["earliest_entry_utc"].values.astype(np.float64)
    u_ends = df_u["label_obs_end_utc"].values.astype(np.float64)

    fold_boundary_audits: List[Dict[str, Any]] = []
    macro_splits: Dict[int, Dict[str, Any]] = {}
    micro_splits: Dict[int, Dict[str, Any]] = {}

    for fspec in EXPANDED_FOLDS:
        tr_s, val_s, cal_s, tst_s, tst_e = (
            _ts(fspec.train_start_utc), _ts(fspec.val_start_utc), _ts(fspec.cal_start_utc),
            _ts(fspec.test_start_utc), _ts(fspec.test_end_utc),
        )
        sp_m = assign_purged_4way_walkforward_fold(m_starts, m_ends, tr_s, val_s, cal_s, tst_s, tst_e, MACRO_EMBARGO_BASELINE_SEC)
        sp_u = assign_purged_4way_walkforward_fold(u_starts, u_ends, tr_s, val_s, cal_s, tst_s, tst_e, MICRO_EMBARGO_BASELINE_SEC)
        macro_splits[fspec.fold_id] = sp_m
        micro_splits[fspec.fold_id] = sp_u
        m_chk = _verify_adjacent_purge(m_starts, m_ends, sp_m, MACRO_EMBARGO_BASELINE_SEC)
        u_chk = _verify_adjacent_purge(u_starts, u_ends, sp_u, MICRO_EMBARGO_BASELINE_SEC)
        if not (m_chk["all_clean"] and u_chk["all_clean"]):
            raise RuntimeError(f"Phase A purge check failed on fold {fspec.fold_id}")
        fold_boundary_audits.append({
            "fold_id": fspec.fold_id,
            "cal_window_utc": [fspec.cal_start_utc, fspec.cal_end_utc],
            "test_window_utc": [fspec.test_start_utc, fspec.test_end_utc],
            "macro_counts": {k: len(sp_m[f"{k}_indices"]) for k in ("train", "val", "cal", "test")},
            "micro_counts": {k: len(sp_u[f"{k}_indices"]) for k in ("train", "val", "cal", "test")},
            "macro_purge_audit": m_chk,
            "micro_purge_audit": u_chk,
        })

    print(f"[G3.5 PHASE A] Verified 2 G3.3 datasets, 16/16 G3.4 models, 4/4 purged folds (all 3 boundaries clean).", flush=True)

    # ------------------------------------------------------------------
    # PHASE B: Isolated Probability Calibration (Macro & Micro)
    # ------------------------------------------------------------------
    macro_classes = ("UP", "DOWN", "FLAT")
    micro_classes = ("ENTER_NOW", "WAIT_ADVERSE", "NEUTRAL")
    saved_calibrators: List[Dict[str, Any]] = []

    # --- B1: MACRO CALIBRATION ---
    macro_cal_chunks: List[pd.DataFrame] = []
    macro_oof_chunks: List[pd.DataFrame] = []
    macro_fold_selection: List[Dict[str, Any]] = []

    for fspec in EXPANDED_FOLDS:
        sp = macro_splits[fspec.fold_id]
        d_tr = df_m.iloc[sp["train_indices"]]
        d_ca = df_m.iloc[sp["cal_indices"]]
        d_ts = df_m.iloc[sp["test_indices"]]
        y_ca = d_ca["net_class_010"].values
        days_ca = d_ca["day_utc"].values

        # Train prevalence baseline B1
        tr_counts = np.array([(d_tr["net_class_010"].values == c).mean() for c in macro_classes], dtype=np.float64)
        b1_cal = np.tile(tr_counts, (len(d_ca), 1))
        b1_ts = np.tile(tr_counts, (len(d_ts), 1))

        cal_df = d_ca[[
            "feature_cutoff_utc", "signal_decision_utc", "day_utc", "session_utc",
            "regime", "is_nonoverlap_15m", "net_class_010", "raw_class_010",
            "buy_ret_net", "sell_ret_net", "atr_14_pct", "entry_spread_bps",
        ]].copy()
        cal_df["fold_id"] = fspec.fold_id

        oof_df = d_ts[[
            "feature_cutoff_utc", "signal_decision_utc", "day_utc", "session_utc",
            "regime", "is_nonoverlap_15m", "net_class_010", "raw_class_010",
            "buy_ret_net", "sell_ret_net", "atr_14_pct", "entry_spread_bps",
        ]].copy()
        oof_df["fold_id"] = fspec.fold_id

        for ci, c in enumerate(macro_classes):
            cal_df[f"B1_p_{c}"] = b1_cal[:, ci]
            oof_df[f"B1_p_{c}"] = b1_ts[:, ci]

        fold_sel_row: Dict[str, Any] = {"fold_id": fspec.fold_id, "models": {}}

        for mtag in ("B3_LOGISTIC_L2", "C1_LIGHTGBM_CONSTRAINED"):
            aid = f"macro_net_010_f{fspec.fold_id}_{mtag}"
            art, asha = loaded_models[aid]
            raw_ca = art.predict_proba_df(d_ca)
            raw_ts = art.predict_proba_df(d_ts)

            # Fit & select strictly on CALIBRATION partition
            best_cal, sel_info = fit_select_calibrator_strict(
                raw_ca, y_ca, days_ca, macro_classes, allow_isotonic=True, min_days_for_isotonic=10
            )
            cal_ca = best_cal.transform(raw_ca)
            cal_ts = best_cal.transform(raw_ts)

            # Also evaluate all candidate calibrators on OOT strictly for diagnostic reporting (NOT selection!)
            method_oof_probs = {"RAW": raw_ts, "SELECTED": cal_ts}
            for cand_m in ("TEMPERATURE", "SIGMOID_OVR", "ISOTONIC_OVR"):
                c_tmp = MulticlassProbabilityCalibrator(cand_m, macro_classes)
                c_tmp.fit(raw_ca, y_ca)
                method_oof_probs[cand_m] = c_tmp.transform(raw_ts)

            cal_id = f"cal_{aid}_{best_cal.method}"
            meta = CalibratorMetadata(
                calibrator_id=cal_id,
                base_artifact_id=aid,
                base_artifact_sha256=asha,
                method=best_cal.method,
                horizon="macro",
                target_name="net_class_010",
                fold_id=fspec.fold_id,
                classes=macro_classes,
                cal_start_utc=fspec.cal_start_utc,
                cal_end_utc=fspec.cal_end_utc,
                cal_n_samples=len(d_ca),
                cal_unique_days=int(len(np.unique(days_ca))),
                internal_val_logloss=float(sel_info["selected_internal_logloss"]),
                created_at_utc=datetime.now(timezone.utc).isoformat(),
            )
            vcal = VersionedCalibrationArtifact(metadata=meta, calibrator=best_cal)
            cpath, csha = vcal.save(CAL_MODELS_DIR)
            saved_calibrators.append({"calibrator_id": cal_id, "method": best_cal.method, "path": str(cpath), "sha256": csha})

            for ci, c in enumerate(macro_classes):
                cal_df[f"{mtag}_raw_p_{c}"] = raw_ca[:, ci]
                cal_df[f"{mtag}_cal_p_{c}"] = cal_ca[:, ci]
                oof_df[f"{mtag}_raw_p_{c}"] = raw_ts[:, ci]
                oof_df[f"{mtag}_cal_p_{c}"] = cal_ts[:, ci]
                for cand_m in ("TEMPERATURE", "SIGMOID_OVR", "ISOTONIC_OVR"):
                    oof_df[f"{mtag}_{cand_m}_p_{c}"] = method_oof_probs[cand_m][:, ci]

            fold_sel_row["models"][mtag] = sel_info

        macro_cal_chunks.append(cal_df)
        macro_oof_chunks.append(oof_df)
        macro_fold_selection.append(fold_sel_row)

    df_m_cal = pd.concat(macro_cal_chunks, ignore_index=True)
    df_m_oof = pd.concat(macro_oof_chunks, ignore_index=True)
    m_cal_path = OUT_DIR / "g3_5_macro_calibrated_cal.parquet"
    m_oof_path = OUT_DIR / "g3_5_macro_calibrated_oof.parquet"
    df_m_cal.to_parquet(m_cal_path, index=False)
    df_m_oof.to_parquet(m_oof_path, index=False)

    # Pooled Macro calibration evaluation (multiclass + binary P_MOVE on net_class_010 & raw_class_010)
    y_m_net = df_m_oof["net_class_010"].values
    y_m_raw = df_m_oof["raw_class_010"].values
    y_move_net = (y_m_net != "FLAT").astype(int)
    y_move_raw = (y_m_raw != "FLAT").astype(int)
    d_m_days = df_m_oof["day_utc"].values

    macro_cal_report: Dict[str, Any] = {
        "fold_selection_strict_cal_only": macro_fold_selection,
        "pooled_oot_multiclass": {},
        "pooled_oot_binary_P_MOVE_net": {},
        "pooled_oot_binary_P_MOVE_raw": {},
        "cal_vs_raw_paired_day_block": {},
    }

    p_b1_m = df_m_oof[[f"B1_p_{c}" for c in macro_classes]].to_numpy()
    macro_cal_report["pooled_oot_multiclass"]["B1_TRAIN_PREVALENCE"] = evaluate_multiclass_calibration(y_m_net, p_b1_m, macro_classes, d_m_days)
    macro_cal_report["pooled_oot_binary_P_MOVE_net"]["B1_TRAIN_PREVALENCE"] = evaluate_binary_event_calibration(y_move_net, 1.0 - p_b1_m[:, 2], d_m_days)
    macro_cal_report["pooled_oot_binary_P_MOVE_raw"]["B1_TRAIN_PREVALENCE"] = evaluate_binary_event_calibration(y_move_raw, 1.0 - p_b1_m[:, 2], d_m_days)

    for mtag in ("B3_LOGISTIC_L2", "C1_LIGHTGBM_CONSTRAINED"):
        for variant in ("raw", "cal", "TEMPERATURE", "SIGMOID_OVR", "ISOTONIC_OVR"):
            key = f"{mtag}_{variant}"
            p_var = df_m_oof[[f"{mtag}_{variant}_p_{c}" for c in macro_classes]].to_numpy()
            macro_cal_report["pooled_oot_multiclass"][key] = evaluate_multiclass_calibration(y_m_net, p_var, macro_classes, d_m_days)
            macro_cal_report["pooled_oot_binary_P_MOVE_net"][key] = evaluate_binary_event_calibration(y_move_net, 1.0 - p_var[:, 2], d_m_days)
            macro_cal_report["pooled_oot_binary_P_MOVE_raw"][key] = evaluate_binary_event_calibration(y_move_raw, 1.0 - p_var[:, 2], d_m_days)

        p_raw = df_m_oof[[f"{mtag}_raw_p_{c}" for c in macro_classes]].to_numpy()
        p_cal = df_m_oof[[f"{mtag}_cal_p_{c}" for c in macro_classes]].to_numpy()
        macro_cal_report["cal_vs_raw_paired_day_block"][mtag] = {
            "multiclass_ll_cal_minus_raw": day_block_paired_diff_ci(
                per_sample_multiclass_logloss(y_m_net, p_cal, macro_classes),
                per_sample_multiclass_logloss(y_m_net, p_raw, macro_classes),
                d_m_days, lower_is_better=True,
            ),
            "binary_P_MOVE_ll_cal_minus_raw": day_block_paired_diff_ci(
                per_sample_binary_logloss(y_move_net, 1.0 - p_cal[:, 2]),
                per_sample_binary_logloss(y_move_net, 1.0 - p_raw[:, 2]),
                d_m_days, lower_is_better=True,
            ),
        }

    print(
        f"[G3.5 PHASE B MACRO] C1 raw->cal MC LL: "
        f"{macro_cal_report['pooled_oot_multiclass']['C1_LIGHTGBM_CONSTRAINED_raw']['multiclass_log_loss']:.5f} -> "
        f"{macro_cal_report['pooled_oot_multiclass']['C1_LIGHTGBM_CONSTRAINED_cal']['multiclass_log_loss']:.5f}, "
        f"P_MOVE AUC={macro_cal_report['pooled_oot_binary_P_MOVE_net']['C1_LIGHTGBM_CONSTRAINED_cal']['roc_auc']:.5f}",
        flush=True,
    )

    # --- B2: MICRO CALIBRATION ---
    micro_cal_chunks: List[pd.DataFrame] = []
    micro_oof_chunks: List[pd.DataFrame] = []
    micro_fold_selection: List[Dict[str, Any]] = []

    for fspec in EXPANDED_FOLDS:
        sp = micro_splits[fspec.fold_id]
        d_tr = df_u.iloc[sp["train_indices"]]
        d_ca = df_u.iloc[sp["cal_indices"]]
        d_ts = df_u.iloc[sp["test_indices"]]
        y_ca = d_ca["buy_timing_class"].values
        days_ca = d_ca["day_utc"].values

        tr_counts = np.array([(d_tr["buy_timing_class"].values == c).mean() for c in micro_classes], dtype=np.float64)
        b1_cal = np.tile(tr_counts, (len(d_ca), 1))
        b1_ts = np.tile(tr_counts, (len(d_ts), 1))

        cal_df = d_ca[[
            "feature_cutoff_utc", "earliest_entry_utc", "day_utc",
            "entry_spread_usd", "buy_timing_adv_usd", "sell_timing_adv_usd",
            "buy_timing_class", "sell_timing_class", "buy_timing_class_half_tick",
        ]].copy()
        cal_df["fold_id"] = fspec.fold_id

        oof_df = d_ts[[
            "feature_cutoff_utc", "earliest_entry_utc", "day_utc",
            "entry_spread_usd", "buy_timing_adv_usd", "sell_timing_adv_usd",
            "buy_timing_class", "sell_timing_class", "buy_timing_class_half_tick",
        ]].copy()
        oof_df["fold_id"] = fspec.fold_id

        for ci, c in enumerate(micro_classes):
            cal_df[f"B1_p_{c}"] = b1_cal[:, ci]
            oof_df[f"B1_p_{c}"] = b1_ts[:, ci]

        fold_sel_row = {"fold_id": fspec.fold_id, "models": {}}

        for mtag in ("B3_LOGISTIC_L2", "C1_LIGHTGBM_CONSTRAINED"):
            aid = f"micro_buy_timing_class_f{fspec.fold_id}_{mtag}"
            art, asha = loaded_models[aid]
            raw_ca = art.predict_proba_df(d_ca)
            raw_ts = art.predict_proba_df(d_ts)

            best_cal, sel_info = fit_select_calibrator_strict(
                raw_ca, y_ca, days_ca, micro_classes, allow_isotonic=True, min_days_for_isotonic=10
            )
            cal_ca = best_cal.transform(raw_ca)
            cal_ts = best_cal.transform(raw_ts)

            cal_id = f"cal_{aid}_{best_cal.method}"
            meta = CalibratorMetadata(
                calibrator_id=cal_id,
                base_artifact_id=aid,
                base_artifact_sha256=asha,
                method=best_cal.method,
                horizon="micro",
                target_name="buy_timing_class",
                fold_id=fspec.fold_id,
                classes=micro_classes,
                cal_start_utc=fspec.cal_start_utc,
                cal_end_utc=fspec.cal_end_utc,
                cal_n_samples=len(d_ca),
                cal_unique_days=int(len(np.unique(days_ca))),
                internal_val_logloss=float(sel_info["selected_internal_logloss"]),
                created_at_utc=datetime.now(timezone.utc).isoformat(),
            )
            vcal = VersionedCalibrationArtifact(metadata=meta, calibrator=best_cal)
            cpath, csha = vcal.save(CAL_MODELS_DIR)
            saved_calibrators.append({"calibrator_id": cal_id, "method": best_cal.method, "path": str(cpath), "sha256": csha})

            for ci, c in enumerate(micro_classes):
                cal_df[f"{mtag}_raw_p_{c}"] = raw_ca[:, ci]
                cal_df[f"{mtag}_cal_p_{c}"] = cal_ca[:, ci]
                oof_df[f"{mtag}_raw_p_{c}"] = raw_ts[:, ci]
                oof_df[f"{mtag}_cal_p_{c}"] = cal_ts[:, ci]

            fold_sel_row["models"][mtag] = sel_info

        micro_cal_chunks.append(cal_df)
        micro_oof_chunks.append(oof_df)
        micro_fold_selection.append(fold_sel_row)

    df_u_cal = pd.concat(micro_cal_chunks, ignore_index=True)
    df_u_oof = pd.concat(micro_oof_chunks, ignore_index=True)
    u_cal_path = OUT_DIR / "g3_5_micro_calibrated_cal.parquet"
    u_oof_path = OUT_DIR / "g3_5_micro_calibrated_oof.parquet"
    df_u_cal.to_parquet(u_cal_path, index=False)
    df_u_oof.to_parquet(u_oof_path, index=False)

    y_u_buy = df_u_oof["buy_timing_class"].values
    y_u_ht = df_u_oof["buy_timing_class_half_tick"].values
    d_u_days = df_u_oof["day_utc"].values

    micro_cal_report: Dict[str, Any] = {
        "fold_selection_strict_cal_only": micro_fold_selection,
        "pooled_oot_buy_strict_gt": {},
        "pooled_oot_buy_half_tick": {},
        "pooled_oot_conditional_enter_vs_wait": {},
        "cal_vs_raw_paired_day_block": {},
    }

    p_b1_u = df_u_oof[[f"B1_p_{c}" for c in micro_classes]].to_numpy()
    micro_cal_report["pooled_oot_buy_strict_gt"]["B1_TRAIN_PREVALENCE"] = evaluate_multiclass_calibration(y_u_buy, p_b1_u, micro_classes, d_u_days)
    micro_cal_report["pooled_oot_buy_half_tick"]["B1_TRAIN_PREVALENCE"] = evaluate_multiclass_calibration(y_u_ht, p_b1_u, micro_classes, d_u_days)

    non_neut = np.isin(y_u_buy, ["ENTER_NOW", "WAIT_ADVERSE"])
    y_cond_en = (y_u_buy[non_neut] == "ENTER_NOW").astype(int)
    d_u_non = d_u_days[non_neut]

    for mtag in ("B3_LOGISTIC_L2", "C1_LIGHTGBM_CONSTRAINED"):
        for variant in ("raw", "cal"):
            key = f"{mtag}_{variant}"
            p_var = df_u_oof[[f"{mtag}_{variant}_p_{c}" for c in micro_classes]].to_numpy()
            micro_cal_report["pooled_oot_buy_strict_gt"][key] = evaluate_multiclass_calibration(y_u_buy, p_var, micro_classes, d_u_days)
            micro_cal_report["pooled_oot_buy_half_tick"][key] = evaluate_multiclass_calibration(y_u_ht, p_var, micro_classes, d_u_days)
            p_en_cond = p_var[non_neut, 0] / np.clip(p_var[non_neut, 0] + p_var[non_neut, 1], 1e-12, None)
            micro_cal_report["pooled_oot_conditional_enter_vs_wait"][key] = evaluate_binary_event_calibration(y_cond_en, p_en_cond, d_u_non)

        p_raw = df_u_oof[[f"{mtag}_raw_p_{c}" for c in micro_classes]].to_numpy()
        p_cal = df_u_oof[[f"{mtag}_cal_p_{c}" for c in micro_classes]].to_numpy()
        micro_cal_report["cal_vs_raw_paired_day_block"][mtag] = {
            "multiclass_ll_cal_minus_raw": day_block_paired_diff_ci(
                per_sample_multiclass_logloss(y_u_buy, p_cal, micro_classes),
                per_sample_multiclass_logloss(y_u_buy, p_raw, micro_classes),
                d_u_days, lower_is_better=True,
            ),
        }

    print(
        f"[G3.5 PHASE B MICRO] C1 raw->cal MC LL: "
        f"{micro_cal_report['pooled_oot_buy_strict_gt']['C1_LIGHTGBM_CONSTRAINED_raw']['multiclass_log_loss']:.5f} -> "
        f"{micro_cal_report['pooled_oot_buy_strict_gt']['C1_LIGHTGBM_CONSTRAINED_cal']['multiclass_log_loss']:.5f}, "
        f"cond ENTER_vs_WAIT AUC={micro_cal_report['pooled_oot_conditional_enter_vs_wait']['C1_LIGHTGBM_CONSTRAINED_cal']['roc_auc']:.5f}",
        flush=True,
    )

    # Save Deliverable 1: g3_5_calibration_integrity.json
    integrity_out = {
        "task": "G3.5_PHASE_A_CALIBRATION_INTEGRITY",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "governance_notice": "Historical OOT folds are research-exposed; calibrators fitted strictly on reserved CALIBRATION folds only.",
        "datasets_verified": {
            "macro_sha256": m_sha,
            "macro_rows": int(len(df_m)),
            "micro_sha256": u_sha,
            "micro_rows": int(len(df_u)),
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
        },
        "g3_4_models_verified_count": len(model_checks),
        "g3_4_models_all_passed": all_models_ok,
        "g3_4_model_checks": model_checks,
        "fold_boundary_audits": fold_boundary_audits,
        "duplicate_timestamps": {
            "macro_cal": int(df_m_cal["feature_cutoff_utc"].duplicated().sum()),
            "macro_oof": int(df_m_oof["feature_cutoff_utc"].duplicated().sum()),
            "micro_cal": int(df_u_cal["feature_cutoff_utc"].duplicated().sum()),
            "micro_oof": int(df_u_oof["feature_cutoff_utc"].duplicated().sum()),
        },
        "saved_calibrators_count": len(saved_calibrators),
        "saved_calibrators": saved_calibrators,
        "output_parquets": {
            "macro_cal_sha256": _sha256(m_cal_path),
            "macro_oof_sha256": _sha256(m_oof_path),
            "micro_cal_sha256": _sha256(u_cal_path),
            "micro_oof_sha256": _sha256(u_oof_path),
        },
        "overall_status": "PASS",
        "elapsed_sec": round(time.time() - t0, 2),
    }
    (OUT_DIR / "g3_5_calibration_integrity.json").write_text(json.dumps(integrity_out, indent=2), encoding="utf-8")

    # Save Deliverable 2: g3_5_probability_calibration.json
    cal_out = {
        "task": "G3.5_PHASE_B_PROBABILITY_CALIBRATION",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "selection_rule": "Chronological 60/40 internal split inside reserved CALIBRATION fold; lowest internal val log-loss with 1e-4 parsimony tie-break; refit winner on full CALIBRATION fold. Zero OOT exposure.",
        "macro_calibration": macro_cal_report,
        "micro_calibration": micro_cal_report,
        "elapsed_sec": round(time.time() - t0, 2),
    }
    (OUT_DIR / "g3_5_probability_calibration.json").write_text(json.dumps(cal_out, indent=2), encoding="utf-8")
    print(f"[G3.5 PHASES A & B DONE in {round(time.time() - t0, 2)}s] Saved 16 calibrators + 4 calibrated parquets.", flush=True)


if __name__ == "__main__":
    main()
