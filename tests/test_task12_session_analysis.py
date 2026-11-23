"""Task 12 — XAUUSD Session & Time-of-Day Performance Analysis Verification Suite."""
from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "backtest_results").exists() and Path("/app/backtest_results").exists():
    ROOT = Path("/app")

OUT_DIR = ROOT / "backtest_results" / "audit" / "task12_session_analysis"
SCRIPTS_DIR = ROOT / "scripts" / "audit"

EXPECTED_DELIVERABLES = [
    "task12_baseline_verification.json",
    "task12_session_logic_audit.json",
    "task12_intraday_features.csv",
    "task12_trade_session_enrichment.csv",
    "task12_hourly_performance.csv",
    "task12_session_performance.json",
    "task12_buy_sell_session_analysis.csv",
    "task12_intraday_market_behavior.json",
    "task12_session_hypothesis_results.csv",
    "task12_parameter_sensitivity.csv",
    "task12_statistical_validation.json",
    "task12_hwm_comparison.json",
    "task12_independent_reconciliation.json",
    "task12_final_report.md",
]

TASK12_SCRIPTS = [
    "task12_session_analysis_and_diagnosis.py",
    "task12_hypotheses_hwm_and_reconciliation.py",
]


class TestTask12SessionAnalysis(unittest.TestCase):
    def test_01_all_14_deliverables_exist_and_non_empty(self):
        for name in EXPECTED_DELIVERABLES:
            p = OUT_DIR / name
            self.assertTrue(p.exists(), f"Missing deliverable: {p}")
            self.assertGreater(p.stat().st_size, 0, f"Empty deliverable: {p}")

    def test_02_zero_goldedge_imports_in_task12_scripts(self):
        for sname in TASK12_SCRIPTS:
            path = SCRIPTS_DIR / sname
            self.assertTrue(path.exists())
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(alias.name.startswith("goldedge") or alias.name.startswith("src.goldedge"))
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    self.assertFalse(mod.startswith("goldedge") or mod.startswith("src.goldedge"))

    def test_03_baseline_and_frozen_hashes_verified(self):
        bv = json.loads((OUT_DIR / "task12_baseline_verification.json").read_text(encoding="utf-8"))
        for k in ("task4_match", "v2_w1_match", "v2_w2_match", "v2_w3_match", "task8_hwm_8pct_config_verified", "task11a_all_checks_passed"):
            self.assertTrue(bv[k], f"Failed check: {k}")

    def test_04_independent_reconciliation_zero_mismatches(self):
        rec = json.loads((OUT_DIR / "task12_independent_reconciliation.json").read_text(encoding="utf-8"))
        self.assertTrue(rec["summary"]["all_reconciled"])
        self.assertEqual(rec["summary"]["unverified_or_mismatched_pairs"], 0)
        self.assertEqual(rec["summary"]["total_candidate_window_pairs"], 27)
        for wid in ("W1", "W2", "W3"):
            for cname, info in rec["windows"][wid].items():
                self.assertEqual(info["status"], "FULLY_RECONCILED", f"{wid}/{cname}")
                self.assertEqual(info["missing_trades"], 0)
                self.assertEqual(info["extra_trades"], 0)
                self.assertEqual(info["session_classification_mismatches"], 0)
                self.assertEqual(info["stop_update_mismatches"], 0)
                self.assertEqual(info["portfolio_constraint_violations"], 0)
                self.assertLessEqual(info["max_net_pnl_diff_usd"], 1e-6)

    def test_05_hwm_comparison_h0_halts_and_h4a_survives(self):
        hwm = json.loads((OUT_DIR / "task12_hwm_comparison.json").read_text(encoding="utf-8"))
        for wid in ("W1", "W2", "W3"):
            self.assertTrue(hwm[wid]["H0_HWM8"]["hwm_triggered"])
            self.assertFalse(hwm[wid]["H4a_HWM8"]["hwm_triggered"])

    def test_06_safety_locks_preserved(self):
        text = (ROOT / "src" / "goldedge" / "config" / "settings.py").read_text(encoding="utf-8")
        self.assertRegex(text, r"SHADOW_MODE:\s*bool\s*=\s*Field\(\s*True\b")
        self.assertRegex(text, r'TRADING_MODE:\s*str\s*=\s*Field\(\s*"demo"')

    def test_07_trade_count_accounting_identity_and_hwm_counts(self):
        rec = json.loads((OUT_DIR / "task12_trade_count_reconciliation.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["base_H4a_w3_trades"], 99)
        expected = {"S04_H4a_NO_UTC09": 89, "S10_H4a_BUY_NO_07_08": 91, "S15_H4a_NO_TRANS_15M": 72, "S17_H4a_NO_FIRST30M": 83}
        for name, n in expected.items():
            c = rec["candidates"][name]
            self.assertEqual(c["executed_n"], n)
            self.assertTrue(c["accounting_identity_verified"])
            self.assertEqual(99 - c["directly_filtered_n"] - c["cascade_prevented_n"] + c["cascade_created_n"], n)
        h = rec["hwm_w3_h0_clarification"]
        self.assertEqual(h["executed_trades_inc_trigger"], 78)
        self.assertEqual(h["prevented_counterfactual_trades"], 65)
        self.assertEqual(h["raw_post_halt_qualified_signals_blocked"], 149)
        self.assertEqual(h["unprotected_h0_trades"] - h["executed_trades_inc_trigger"], h["prevented_counterfactual_trades"])


if __name__ == "__main__":
    unittest.main()
