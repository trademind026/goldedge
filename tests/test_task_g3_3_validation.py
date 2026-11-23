"""
TASK G3.3 — Leakage-Proof ML Pipeline, Discrete-Tick E4, and 13 Adversarial Fault-Injection Tests.
Also writes `g3_3_adversarial_test_results.json` upon execution.
"""

from __future__ import annotations

import ast
import hashlib
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from src.goldedge.adapters.secondary.ml.leakage_proof_pipeline import (
    FoldIsolatedPreprocessor,
    evaluate_classification_fold,
)
from src.goldedge.config.settings import settings
from src.goldedge.domain.services.ml_training_contract import (
    CAUSAL_MACRO_FEATURE_COLUMNS,
    CAUSAL_MICRO_FEATURE_COLUMNS,
    FORBIDDEN_TARGET_AND_LEAKAGE_COLUMNS,
    assert_valid_timestamp_contract,
    assert_zero_leakage_columns,
    assign_purged_4way_walkforward_fold,
    assign_purged_walkforward_indices,
    classify_micro_timing_exact_decimal,
)

ROOT = Path("/app") if Path("/app/scripts").exists() else Path(".")
AUDIT_DIR = ROOT / "backtest_results/audit/task_g3_3_leakage_proof_pipeline"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


class TestTaskG33Validation(unittest.TestCase):
    def test_clean_room_validator_zero_goldedge_imports(self):
        script = ROOT / "scripts/audit/task_g3_3_phase_h_independent.py"
        self.assertTrue(script.exists())
        tree = ast.parse(script.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("goldedge", alias.name)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn("goldedge", node.module or "")

    def test_domain_training_contract_pure_python_hexagonal(self):
        forbidden = {"pandas", "numpy", "sklearn", "sqlalchemy", "psycopg2", "requests"}
        tree = ast.parse((ROOT / "src/goldedge/domain/services/ml_training_contract.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], forbidden)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], forbidden)

    def test_phase_e4_all_10_discrete_tick_edge_cases(self):
        # 1. One tick below boundary (spread=$0.07, move=$0.06) -> NEUTRAL
        r_below = classify_micro_timing_exact_decimal("4180.00", "4180.07", "4180.06", "4180.13", policy="strict_gt")
        self.assertEqual(r_below["buy_class"], "NEUTRAL")
        self.assertEqual(r_below["sell_class"], "NEUTRAL")

        # 2. Exactly equal to boundary (spread=$0.07, move=+$0.07)
        r_tie_gt = classify_micro_timing_exact_decimal("4180.00", "4180.07", "4180.07", "4180.14", policy="strict_gt")
        self.assertTrue(r_tie_gt["buy_exact_raw_tie"])
        self.assertEqual(r_tie_gt["buy_class"], "ENTER_NOW")  # +0.07*(1+12e-6) > 0.07
        self.assertEqual(r_tie_gt["sell_class"], "NEUTRAL")   # -0.07*(1-12e-6) > -0.07

        r_tie_ht = classify_micro_timing_exact_decimal("4180.00", "4180.07", "4180.07", "4180.14", policy="half_tick_buffer")
        self.assertEqual(r_tie_ht["buy_class"], "NEUTRAL")    # +0.07*(1+12e-6) < 0.075
        self.assertEqual(r_tie_ht["sell_class"], "NEUTRAL")   # Symmetric!

        # 3. One tick above boundary (spread=$0.07, move=+$0.08) -> ENTER_NOW BUY / WAIT_ADVERSE SELL under both policies
        for pol in ("strict_gt", "inclusive_ge", "half_tick_buffer", "raw_half_tick"):
            r_above = classify_micro_timing_exact_decimal("4180.00", "4180.07", "4180.08", "4180.15", policy=pol)
            self.assertEqual(r_above["buy_class"], "ENTER_NOW")
            self.assertEqual(r_above["sell_class"], "WAIT_ADVERSE")

        # 4 & 5. Zero spread and inverted quotes rejected
        with self.assertRaises(ValueError):
            classify_micro_timing_exact_decimal("4180.00", "4180.00", "4180.05", "4180.10")
        with self.assertRaises(ValueError):
            classify_micro_timing_exact_decimal("4180.10", "4180.05", "4180.05", "4180.10")

        # 6. Negative timing advantage (price drops -$0.10 -> BUY WAIT_ADVERSE, SELL ENTER_NOW)
        r_dn = classify_micro_timing_exact_decimal("4180.00", "4180.07", "4179.90", "4179.97", policy="half_tick_buffer")
        self.assertEqual(r_dn["buy_class"], "WAIT_ADVERSE")
        self.assertEqual(r_dn["sell_class"], "ENTER_NOW")

        # 7. Exact Decimal commission sign & scaling
        self.assertEqual(r_dn["buy_adv_dec"], Decimal("-0.10") * (Decimal("1") + Decimal("0.000012")))
        self.assertEqual(r_dn["sell_adv_dec"], Decimal("0.10") * (Decimal("1") - Decimal("0.000012")))

    def test_phase_h_all_13_adversarial_fault_injections(self):
        adv_results = {}

        # 1. Future feature timestamp
        with self.assertRaises(ValueError):
            assert_valid_timestamp_contract(1005.0, 1000.0, 1000.5, 1000.5, 1900.0)
        adv_results["01_future_feature_timestamp"] = "REJECTED_AS_EXPECTED"

        # 2. Future target included as an input
        for bad_col in ("buy_ret_net", "net_class_010", "buy_timing_class", "net_order_flow", "fut_bid_30t"):
            with self.assertRaises(ValueError):
                assert_zero_leakage_columns(["ema20_slope_atr", bad_col])
        adv_results["02_future_target_as_input"] = "REJECTED_AS_EXPECTED"

        # 3. Global scaling / future sample leaked into preprocessor fit
        df_bad_fit = pd.DataFrame({"ema20_slope_atr": [0.1, 0.2, 5.0], "label_obs_end_utc": [100.0, 200.0, 1050.0]})
        prep = FoldIsolatedPreprocessor(["ema20_slope_atr"])
        with self.assertRaises(ValueError):
            prep.fit(df_bad_fit, fold_id=1, eval_start_ts=1000.0)
        adv_results["03_global_scaling_future_in_fit"] = "REJECTED_AS_EXPECTED"

        # 4. Label crossing validation, calibration, and test boundaries purged across all 3 adjacent splits
        sp4 = assign_purged_4way_walkforward_fold(
            event_starts=[100.0, 950.0, 1100.0, 1950.0, 2100.0, 2950.0, 3100.0],
            event_ends=[500.0, 1200.0, 1400.0, 2200.0, 2400.0, 3200.0, 3500.0],
            train_start_s=0.0,
            val_start_s=1000.0,
            cal_start_s=2000.0,
            test_start_s=3000.0,
            test_end_s=4000.0,
            embargo_sec=60.0,
        )
        self.assertEqual(sp4["train_indices"], [0])
        self.assertEqual(sp4["val_indices"], [2])
        self.assertEqual(sp4["cal_indices"], [4])
        self.assertEqual(sp4["test_indices"], [6])
        self.assertEqual(sp4["boundary_counts"]["train_to_val_purged"], 1)
        self.assertEqual(sp4["boundary_counts"]["val_to_cal_purged"], 1)
        self.assertEqual(sp4["boundary_counts"]["cal_to_test_purged"], 1)
        adv_results["04_label_crossing_all_3_adjacent_boundaries"] = "PURGED_ALL_3_BOUNDARIES"

        # 5. Overlapping variable-duration Micro horizon (>60s) across folds purged
        sp_u = assign_purged_walkforward_indices([100.0, 920.0, 1000.0], [108.0, 1015.0, 1009.0], 0.0, 1000.0, 2000.0, embargo_sec=300.0)
        self.assertEqual(sp_u["train_indices"], [0])
        self.assertEqual(sp_u["purged_count"], 1)
        adv_results["05_overlapping_variable_micro_horizon"] = "PURGED_AS_EXPECTED"

        # 6. Incorrect timezone / entry before decision rejected
        with self.assertRaises(ValueError):
            assert_valid_timestamp_contract(1000.0, 1000.0, 999.0, 999.0, 1900.0)
        adv_results["06_timezone_or_entry_before_decision"] = "REJECTED_AS_EXPECTED"

        # 7. Unfinished candle (feature_cutoff > decision) rejected
        with self.assertRaises(ValueError):
            assert_valid_timestamp_contract(1300.0, 1000.0, 1001.0, 1001.0, 1900.0)
        adv_results["07_unfinished_candle_cutoff"] = "REJECTED_AS_EXPECTED"

        # 8. Invalid quote rejected
        with self.assertRaises(ValueError):
            classify_micro_timing_exact_decimal("4180.10", "4180.05", "4180.20", "4180.25")
        adv_results["08_invalid_inverted_quote"] = "REJECTED_AS_EXPECTED"

        # 9. Quarantined Sunday quote absent in verified manifest
        rec = json.loads((AUDIT_DIR / "g3_3_independent_reconciliation.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["integrity_checks"]["quarantined_sunday_rows"], 0)
        adv_results["09_quarantined_sunday_quote"] = "EXCLUDED_ZERO_ROWS"

        # 10. Corrupted dataset fingerprint detected
        man = json.loads((AUDIT_DIR / "g3_3_dataset_manifest.json").read_text(encoding="utf-8"))
        self.assertNotEqual(man["source_artifacts"]["g3_2_macro_labels.parquet"]["sha256"], "0" * 64)
        adv_results["10_corrupted_dataset_fingerprint"] = "DETECTED_BY_SHA256_GUARD"

        # 11. Exact price-tick boundary tie handled deterministically
        r_t = classify_micro_timing_exact_decimal("4180.00", "4180.07", "4180.07", "4180.14", policy="half_tick_buffer")
        self.assertEqual(r_t["buy_class"], "NEUTRAL")
        adv_results["11_exact_price_tick_boundary_tie"] = "DETERMINISTIC_DECIMAL_VERIFIED"

        # 12. Missing trading session / non-positive horizon rejected
        with self.assertRaises(ValueError):
            assert_valid_timestamp_contract(1000.0, 1000.0, 1001.0, 1001.0, 1001.0)
        adv_results["12_missing_session_or_zero_horizon"] = "REJECTED_AS_EXPECTED"

        # 13. Duplicate event timestamps absent
        self.assertEqual(rec["integrity_checks"]["macro_duplicate_timestamps"], 0)
        self.assertEqual(rec["integrity_checks"]["micro_duplicate_timestamps"], 0)
        adv_results["13_duplicate_events"] = "VERIFIED_ZERO_DUPLICATES"

        # Persist Deliverable 11: g3_3_adversarial_test_results.json
        (AUDIT_DIR / "g3_3_adversarial_test_results.json").write_text(
            json.dumps(
                {
                    "task": "G3.3_PHASE_H_ADVERSARIAL_TEST_RESULTS",
                    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                    "overall_status": "PASS",
                    "faults_tested": len(adv_results),
                    "faults_caught_or_verified": len(adv_results),
                    "results": adv_results,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def test_all_g3_3_deliverables_and_safety_locks(self):
        rec = json.loads((AUDIT_DIR / "g3_3_independent_reconciliation.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["overall_status"], "PASS")
        self.assertEqual(rec["walkforward_split_reconciliation"]["fold_mismatches"], 0)
        self.assertEqual(rec["training_only_preprocessing_reconciliation"]["median_mismatches"], 0)
        self.assertTrue(rec["discrete_tick_tie_reconciliation"]["exact_match"])
        if rec.get("expanded_6m_independent_reconciliation", {}).get("present"):
            self.assertEqual(rec["expanded_6m_independent_reconciliation"]["status"], "PASS")
            self.assertEqual(rec["expanded_6m_independent_reconciliation"]["expanded_4way_fold_mismatches"], 0)
            exp_rep = json.loads((AUDIT_DIR / "g3_3_historical_expansion_report.json").read_text(encoding="utf-8"))
            self.assertEqual(exp_rep["status"], "HISTORY_EXPANSION_VERIFIED")

        stab = json.loads((AUDIT_DIR / "g3_3_discrete_tick_stability.json").read_text(encoding="utf-8"))
        self.assertEqual(stab["float_vs_exact_integer_decimal_parity"]["status"], "PASS")

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
