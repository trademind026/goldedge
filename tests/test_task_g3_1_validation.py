"""
TASK G3.1 Validation & Clean-Room Isolation Test Suite.
Verifies:
  1. Zero src/goldedge imports in all clean-room G3.1 audit scripts (AST check).
  2. Mathematical consistency and completeness of G3.1 deliverables.
  3. Zero discrepancies in Phase H independent reconciliation.
  4. Safety locks and frozen baseline SHA-256 integrity.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import unittest
from pathlib import Path

from src.goldedge.adapters.secondary.features.microstructure_feature_repo import (
    compute_forex_micro_features,
)
from src.goldedge.config.settings import settings
from src.goldedge.domain.services.ml_feature_contract import (
    ALLOWED_MACRO_ML_FEATURES,
    ALLOWED_MICRO_ML_FEATURES,
    EXCLUDED_UNAVAILABLE_ML_FEATURES,
    validate_ml_feature_names,
)

ROOT = Path("/app") if Path("/app/scripts").exists() else Path(".")
AUDIT_DIR = ROOT / "backtest_results/audit/task_g3_1_ml_feasibility"
CLEAN_ROOM_SCRIPTS = [
    ROOT / "scripts/audit/task_g3_1_phase_a_db_audit.py",
    ROOT / "scripts/audit/task_g3_1_phase_bcd_feasibility.py",
    ROOT / "scripts/audit/task_g3_1_phase_efg_assessment.py",
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


class TestTaskG31Validation(unittest.TestCase):
    def test_clean_room_scripts_zero_goldedge_imports(self):
        for script in CLEAN_ROOM_SCRIPTS:
            self.assertTrue(script.exists(), f"Missing script: {script}")
            tree = ast.parse(script.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn("goldedge", alias.name, f"Forbidden import in {script}: {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    self.assertNotIn("goldedge", mod, f"Forbidden import-from in {script}: {mod}")

    def test_phase_a_audit_invariants(self):
        p = AUDIT_DIR / "g3_1_historical_data_audit.json"
        self.assertTrue(p.exists())
        data = json.loads(p.read_text(encoding="utf-8"))
        s = data["xauusd_summary"]
        v = data["quote_validity"]
        self.assertEqual(s["total_raw_ticks"], s["total_valid_ticks"] + s["total_invalid_ticks"])
        self.assertEqual(
            s["total_invalid_ticks"],
            v["null_bid_or_ask"] + v["nonpositive_price"] + v["inverted_bid_gt_ask"] + v["zero_spread_bid_eq_ask"],
        )
        self.assertEqual(data["stale_identical_quotes"]["out_of_order_timestamps"], 0)
        self.assertEqual(s["non_null_volume_rows"], 0)

        csv_p = AUDIT_DIR / "g3_1_daily_coverage.csv"
        self.assertTrue(csv_p.exists())
        with open(csv_p, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertGreaterEqual(len(rows), 95)

    def test_phase_b_macro_invariants(self):
        data = json.loads((AUDIT_DIR / "g3_1_macro_feasibility.json").read_text(encoding="utf-8"))
        for grid_key in (
            "threshold_sensitivity_1m_strict",
            "threshold_sensitivity_5m_decision_grid",
            "threshold_sensitivity_nonoverlap_15m",
        ):
            for thr_key, block in data[grid_key].items():
                ep = block["endpoint_executable"]
                self.assertEqual(
                    ep["UP_count"] + ep["DOWN_count"] + ep["FLAT_count"],
                    block["n_samples"],
                    f"Mismatch in {grid_key}/{thr_key}",
                )
                bt = block["intrahorizon_barrier_touch"]
                self.assertEqual(
                    bt["UP_only_count"] + bt["DOWN_only_count"] + bt["BOTH_touched_count"] + bt["NEITHER_count"],
                    block["n_samples"],
                    f"Barrier mismatch in {grid_key}/{thr_key}",
                )

    def test_phase_c_micro_invariants(self):
        data = json.loads((AUDIT_DIR / "g3_1_micro_feasibility.json").read_text(encoding="utf-8"))
        self.assertFalse(data["order_book_and_volume_availability"]["level2_order_book_available"])
        self.assertFalse(data["order_book_and_volume_availability"]["traded_volume_available"])
        n30 = data["sample_counts"]["contiguous_30_tick_windows_overlapping"]
        for key in ("0.0", "0.1", "0.25", "0.5"):
            b = data["executable_post_spread_30_tick"][key]
            self.assertEqual(b["UP"] + b["DOWN"] + b["FLAT"], n30)

    def test_phase_d_independence_invariants(self):
        data = json.loads((AUDIT_DIR / "g3_1_independence_leakage_audit.json").read_text(encoding="utf-8"))
        m = data["macro_effective_sample_size"]
        self.assertLess(m["newey_west_1m_mid_ret_lag60"]["n_eff"], m["raw_1m_overlapping_15m_windows"])
        # Day-clustered effective sample size must be strictly smaller than naive non-overlapping count
        dc_m = m["day_level_cluster_dependence"]
        self.assertLess(dc_m["conservative_day_clustered_n_eff"], m["nonoverlap_15m_windows"])
        dc_u = data["micro_effective_sample_size"]["day_level_cluster_dependence"]
        self.assertLess(dc_u["conservative_day_clustered_n_eff"], data["micro_effective_sample_size"]["nonoverlap_30_tick_windows"])
        self.assertGreaterEqual(data["recommended_chronological_validation_framework"]["macro_purge_length_minutes"], 15)
        self.assertGreaterEqual(data["recommended_chronological_validation_framework"]["macro_embargo_length_minutes"], 30)

    def test_phase_h_reconciliation_pass(self):
        data = json.loads((AUDIT_DIR / "g3_1_independent_reconciliation.json").read_text(encoding="utf-8"))
        self.assertEqual(data["overall_status"], "PASS")
        self.assertEqual(data["checks"]["tick_repository_reconciliation"]["mismatches"], 0)
        self.assertEqual(data["checks"]["candle_1m_quote_semantics_reconciliation"]["mismatches"], 0)
        self.assertEqual(data["checks"]["aggregation_5m_and_causality"]["ohlcv_mismatches"], 0)
        self.assertEqual(data["checks"]["order_flow_feature_exclusion_enforcement"]["status"], "PASS")
        self.assertEqual(data["checks"]["quarantine_and_843_bar_reconciliation"]["status"], "PASS")

    def test_excluded_order_flow_features_cannot_enter_ml_training(self):
        import pandas as pd

        # 1. Domain ML feature contract must reject every excluded order-flow/L2/volume feature
        for bad in EXCLUDED_UNAVAILABLE_ML_FEATURES:
            with self.assertRaises(ValueError):
                validate_ml_feature_names([bad])
            with self.assertRaises(ValueError):
                validate_ml_feature_names(["ema20_slope", bad], horizon="macro")
            with self.assertRaises(ValueError):
                validate_ml_feature_names(["spread_usd_and_zscore_30t", bad], horizon="micro")

        # 2. Allowed Macro & Micro feature lists must pass cleanly
        self.assertEqual(len(validate_ml_feature_names(ALLOWED_MACRO_ML_FEATURES, horizon="macro")), len(ALLOWED_MACRO_ML_FEATURES))
        self.assertEqual(len(validate_ml_feature_names(ALLOWED_MICRO_ML_FEATURES, horizon="micro")), len(ALLOWED_MICRO_ML_FEATURES))

        # 3. compute_forex_micro_features must NOT emit Lee-Ready order-flow columns and must raise if explicitly requested
        sample_df = pd.DataFrame(
            [
                {"time": "2026-10-14T13:00:00Z", "bid": 4100.00, "ask": 4100.07},
                {"time": "2026-10-14T13:00:01Z", "bid": 4100.05, "ask": 4100.12},
            ]
        )
        out = compute_forex_micro_features(sample_df, window_seconds=30)
        for col in ("aggressor", "buy_vol", "sell_vol", "net_order_flow", "tick_momentum"):
            self.assertNotIn(col, out.columns)
        with self.assertRaises(ValueError):
            compute_forex_micro_features(sample_df, window_seconds=30, include_disabled_order_flow=True)

    def test_quarantine_and_843_bar_reconciliation_in_audit_json(self):
        data = json.loads((AUDIT_DIR / "g3_1_historical_data_audit.json").read_text(encoding="utf-8"))
        q = data["quarantined_off_session_ticks"]
        self.assertEqual(q["quarantined_count"], 1)
        self.assertEqual(q["ml_eligible_valid_ticks"], data["xauusd_summary"]["total_valid_ticks"] - 1)
        b = data["bar_table_comparison"]
        self.assertEqual(b["diff_all_valid_vs_qs_v1_bars"], 843)
        self.assertEqual(b["unexplained_diff_bars"], 0)
        self.assertEqual(b["ml_eligible_1m_buckets_excl_quarantined"], 96862)

    def test_safety_locks_and_frozen_artifacts(self):
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
