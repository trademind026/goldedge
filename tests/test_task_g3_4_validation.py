"""
TASK G3.4 — Phase J Adversarial, Shuffled-Target Negative Control, Reload Parity & Reproducibility Tests.
Writes `g3_4_adversarial_test_results.json`.
"""

from __future__ import annotations

import ast
import hashlib
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import log_loss

from src.goldedge.adapters.secondary.ml.leakage_proof_pipeline import FoldIsolatedPreprocessor
from src.goldedge.adapters.secondary.ml.model_trainer import (
    VersionedMLArtifact,
    fit_select_constrained_lightgbm,
    fit_select_logistic_regression,
)
from src.goldedge.config.settings import settings
from src.goldedge.domain.services.ml_training_contract import (
    CAUSAL_MACRO_FEATURE_COLUMNS,
    assert_valid_timestamp_contract,
    assert_zero_leakage_columns,
    classify_micro_timing_exact_decimal,
)

ROOT = Path("/app") if Path("/app/scripts").exists() else Path(".")
AUDIT_DIR = ROOT / "backtest_results/audit/task_g3_4_ml_model_research"
G33_EXP_DIR = ROOT / "backtest_results/audit/task_g3_3_leakage_proof_pipeline/expanded_6m"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


class TestTaskG34Validation(unittest.TestCase):
    def test_clean_room_validator_zero_goldedge_imports(self):
        script = ROOT / "scripts/audit/task_g3_4_phase_j_independent.py"
        self.assertTrue(script.exists())
        tree = ast.parse(script.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("goldedge", alias.name)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn("goldedge", node.module or "")

    def test_phase_j_all_13_adversarial_and_negative_control_tests(self):
        adv: dict[str, str] = {}
        df_m = pd.read_parquet(G33_EXP_DIR / "g3_3_expanded_6m_macro_dataset.parquet")
        m_cols = list(CAUSAL_MACRO_FEATURE_COLUMNS)
        classes = ["UP", "DOWN", "FLAT"]
        c2i = {c: i for i, c in enumerate(classes)}

        # Slice deterministic small train/val/test window for fast negative control & reproducibility
        d_tr = df_m.iloc[:3000].copy()
        d_va = df_m.iloc[3100:3600].copy()
        d_ts = df_m.iloc[3700:4500].copy()
        val_s = float(d_va["label_obs_start_utc"].min())

        # 1. Shuffled-target negative control (LogReg & LightGBM must NOT beat prevalence baseline beyond chance!)
        rng = np.random.default_rng(123)
        y_tr_shuf = rng.permutation(d_tr["net_class_010"].values)
        y_va_shuf = rng.permutation(d_va["net_class_010"].values)
        y_ts_shuf = rng.permutation(d_ts["net_class_010"].values)
        y_ts_i = np.array([c2i[v] for v in y_ts_shuf])
        priors = np.array([float(np.mean(y_tr_shuf == c)) for c in classes])
        chance_ll = float(log_loss(y_ts_i, np.tile(priors, (len(y_ts_i), 1)), labels=[0, 1, 2]))

        art_lr_shuf, _ = fit_select_logistic_regression(d_tr, y_tr_shuf, d_va, y_va_shuf, classes, m_cols, val_s, "shuf", "shuf", 1, ("a", "b"), "rev", seed=42)
        art_lgb_shuf, _ = fit_select_constrained_lightgbm(d_tr, y_tr_shuf, d_va, y_va_shuf, classes, m_cols, val_s, "shuf", "shuf", 1, ("a", "b"), "rev", min_child_grid=(150,), depth_grid=(2,), seed=42)
        lr_shuf_ll = float(log_loss(y_ts_i, np.clip(art_lr_shuf.predict_proba_df(d_ts), 1e-12, 1.0 - 1e-12), labels=[0, 1, 2]))
        lgb_shuf_ll = float(log_loss(y_ts_i, np.clip(art_lgb_shuf.predict_proba_df(d_ts), 1e-12, 1.0 - 1e-12), labels=[0, 1, 2]))
        # Neither model should artificially crush chance_ll on shuffled labels
        self.assertGreater(lr_shuf_ll, chance_ll - 0.02)
        self.assertGreater(lgb_shuf_ll, chance_ll - 0.02)
        adv["01_shuffled_target_negative_control"] = f"PASS (chance_ll={chance_ll:.4f}, lr_shuf={lr_shuf_ll:.4f}, lgb_shuf={lgb_shuf_ll:.4f})"

        # 2. Train-only vs leaked global scaler detection
        prep = FoldIsolatedPreprocessor(m_cols)
        with self.assertRaises(ValueError):
            prep.fit(pd.concat([d_tr, d_va]), fold_id=1, eval_start_ts=val_s)
        adv["02_leaked_global_scaler_rejected"] = "PASS"

        # 3. Future feature timestamp rejection
        with self.assertRaises(ValueError):
            assert_valid_timestamp_contract(2000.0, 1000.0, 1001.0, 1001.0, 1900.0)
        adv["03_future_feature_rejected"] = "PASS"

        # 4. Wrong-fold future event_end in fit rejected
        bad_df = d_tr.copy()
        bad_df.iloc[-1, bad_df.columns.get_loc("label_obs_end_utc")] = val_s + 10.0
        with self.assertRaises(ValueError):
            FoldIsolatedPreprocessor(m_cols).fit(bad_df, fold_id=1, eval_start_ts=val_s)
        adv["04_wrong_fold_event_end_rejected"] = "PASS"

        # 5. Label column leakage rejected
        with self.assertRaises(ValueError):
            assert_zero_leakage_columns(m_cols + ["net_class_010"])
        adv["05_label_column_leakage_rejected"] = "PASS"

        # 6. Model artifact & feature schema mismatch rejected on reload
        saved_m = AUDIT_DIR / "models/macro_net_010_f4_C1_LIGHTGBM_CONSTRAINED.joblib"
        self.assertTrue(saved_m.exists())
        with self.assertRaises(ValueError):
            VersionedMLArtifact.load(saved_m, expected_feature_columns=m_cols[:-1])
        adv["06_model_schema_mismatch_rejected"] = "PASS"

        # 7. Duplicate prediction detection verified (0 duplicates in OOF)
        rec = json.loads((AUDIT_DIR / "g3_4_independent_reconciliation.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["duplicate_predictions"]["macro"], 0)
        self.assertEqual(rec["duplicate_predictions"]["micro"], 0)
        adv["07_duplicate_predictions_zero"] = "PASS"

        # 8. Class-probability ordering verified (classes == [UP, DOWN, FLAT])
        art_loaded = VersionedMLArtifact.load(saved_m, expected_feature_columns=m_cols)
        self.assertEqual(list(art_loaded.metadata.classes), ["UP", "DOWN", "FLAT"])
        adv["08_class_probability_ordering_verified"] = "PASS"

        # 9. Incomplete label-window handling (end <= start rejected)
        with self.assertRaises(ValueError):
            assert_valid_timestamp_contract(1000.0, 1000.0, 1000.5, 1000.5, 1000.5)
        adv["09_incomplete_label_window_rejected"] = "PASS"

        # 10. Exact price-tick threshold tie check
        r_ht = classify_micro_timing_exact_decimal("4180.00", "4180.07", "4180.07", "4180.14", policy="half_tick_buffer")
        self.assertEqual(r_ht["buy_class"], "NEUTRAL")
        adv["10_exact_price_tick_threshold_verified"] = "PASS"

        # 11. Session-gap handling (0 quarantined Sunday rows in OOF)
        oof_m = pd.read_parquet(AUDIT_DIR / "g3_4_macro_oof_predictions.parquet")
        self.assertEqual(int((oof_m["day_utc"] == "2026-11-22").sum()), 0)
        adv["11_session_gap_and_quarantine_verified"] = "PASS"

        # 12. Model reload prediction parity (< 1e-5 vs saved OOF parquet for Fold 4)
        df_f4_src = df_m[df_m["feature_cutoff_utc"].isin(oof_m[oof_m["fold_id"] == 4]["feature_cutoff_utc"])]
        p_reload = art_loaded.predict_proba_df(df_f4_src)
        p_saved = oof_m[oof_m["fold_id"] == 4][["C1_LIGHTGBM_CONSTRAINED_p_UP", "C1_LIGHTGBM_CONSTRAINED_p_DOWN", "C1_LIGHTGBM_CONSTRAINED_p_FLAT"]].values
        max_reload_diff = float(np.max(np.abs(p_reload - p_saved)))
        self.assertLess(max_reload_diff, 1e-5)
        adv["12_model_reload_prediction_parity"] = f"PASS (max_diff={max_reload_diff:.2e})"

        # 13. Reproducibility under fixed seeds
        a1, _ = fit_select_constrained_lightgbm(d_tr, d_tr["net_class_010"].values, d_va, d_va["net_class_010"].values, classes, m_cols, val_s, "rep", "rep", 1, ("a", "b"), "rev", min_child_grid=(150,), depth_grid=(2,), seed=42)
        a2, _ = fit_select_constrained_lightgbm(d_tr, d_tr["net_class_010"].values, d_va, d_va["net_class_010"].values, classes, m_cols, val_s, "rep", "rep", 1, ("a", "b"), "rev", min_child_grid=(150,), depth_grid=(2,), seed=42)
        rep_diff = float(np.max(np.abs(a1.predict_proba_df(d_ts) - a2.predict_proba_df(d_ts))))
        self.assertLess(rep_diff, 1e-12)
        adv["13_fixed_seed_reproducibility"] = f"PASS (max_diff={rep_diff:.2e})"

        (AUDIT_DIR / "g3_4_adversarial_test_results.json").write_text(
            json.dumps(
                {
                    "task": "G3.4_PHASE_J_ADVERSARIAL_TEST_RESULTS",
                    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                    "overall_status": "PASS",
                    "tests_passed": len(adv),
                    "tests_total": len(adv),
                    "results": adv,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def test_deliverables_reconciliation_and_safety_locks(self):
        rec = json.loads((AUDIT_DIR / "g3_4_independent_reconciliation.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["overall_status"], "PASS")
        self.assertEqual(rec["metric_mismatches"], 0)
        self.assertTrue(settings.SHADOW_MODE)
        self.assertEqual(settings.TRADING_MODE, "demo")
        self.assertAlmostEqual(float(settings.HWM_MAX_DRAWDOWN_PCT), 8.0, places=6)
        self.assertEqual(
            _sha256(ROOT / "backtest_results/frozen_111_baseline_trades.csv"),
            "73306E1529ADC48284BCE9AB6AC35122F33B9999D2464AF441867EB3BB6CF168",
        )
        self.assertEqual(
            _sha256(ROOT / "backtest_results/audit/task_g1_root_cause/g1_original_70_baseline_trades.csv"),
            "27200EEA2C7FF4842FC586DD1131EE450247D0BCCBCF3E2F77ECA6952D2CCDC4",
        )


if __name__ == "__main__":
    unittest.main()
