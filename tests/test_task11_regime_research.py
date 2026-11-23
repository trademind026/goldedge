"""Task 11 — XAUUSD Market Regime Analysis & Strategy Robustness Verification Suite.

Verifies:
1. All 12 Task 11 deliverables exist under backtest_results/audit/.
2. Frozen Task 4, Task 6C V2, and Task 10 artifacts remain untouched and verified.
3. Zero src/goldedge imports in independent Task 11 research scripts.
4. Independent W1/W2/W3 reconciliation reports 0 mismatches and 0.00 USD max P&L diff.
5. Baseline W3 H0 (143 trades, +$211.48) and H4a (99 trades, +$1,257.70) match Task 10.
6. SHADOW_MODE = True and TRADING_MODE = 'demo' safety locks remain intact.
"""
from __future__ import annotations

import ast
import csv
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "backtest_results").exists() and Path("/app/backtest_results").exists():
    ROOT = Path("/app")

AUDIT_DIR = ROOT / "backtest_results" / "audit"
SCRIPTS_DIR = ROOT / "scripts" / "audit"

EXPECTED_DELIVERABLES = [
    "task11_baseline_verification.json",
    "task11_regime_classifier_audit.json",
    "task11_regime_features.csv",
    "task11_trade_regime_enrichment.csv",
    "task11_regime_performance.json",
    "task11_buy_sell_regime_analysis.csv",
    "task11_h4a_regime_comparison.json",
    "task11_regime_hypothesis_results.csv",
    "task11_parameter_sensitivity.csv",
    "task11_statistical_validation.json",
    "task11_independent_reconciliation.json",
    "task11_final_report.md",
]

TASK11_SCRIPTS = [
    "task11_regime_reconstruction_and_diagnosis.py",
    "task11_regime_hypotheses_and_validation.py",
]


class TestTask11RegimeResearch(unittest.TestCase):
    def test_01_all_12_deliverables_exist_and_non_empty(self):
        for name in EXPECTED_DELIVERABLES:
            path = AUDIT_DIR / name
            self.assertTrue(path.exists(), f"Missing deliverable: {path}")
            self.assertGreater(path.stat().st_size, 0, f"Empty deliverable: {path}")

    def test_02_independent_scripts_have_zero_goldedge_imports(self):
        for script_name in TASK11_SCRIPTS:
            path = SCRIPTS_DIR / script_name
            self.assertTrue(path.exists(), f"Missing script: {path}")
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(
                            alias.name.startswith("goldedge") or alias.name.startswith("src.goldedge"),
                            f"{script_name} imports {alias.name}",
                        )
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    self.assertFalse(
                        mod.startswith("goldedge") or mod.startswith("src.goldedge"),
                        f"{script_name} imports from {mod}",
                    )

    def test_03_baseline_verification_hashes_and_reproduction(self):
        data = json.loads((AUDIT_DIR / "task11_baseline_verification.json").read_text(encoding="utf-8"))
        hashes = data["frozen_hashes"]
        for k in ("task4_match", "v2_w1_match", "v2_w2_match", "v2_w3_match", "v2_w3_journeys_match"):
            self.assertTrue(hashes[k], f"Frozen hash mismatch for {k}")

        h0 = data["w3_reproduction"]["H0"]
        h4a = data["w3_reproduction"]["H4a"]
        self.assertEqual(h0["n"], 143)
        self.assertAlmostEqual(h0["net_pnl"], 211.48, places=2)
        self.assertAlmostEqual(h0["mtm_dd_pct"], 12.78, places=2)
        self.assertLessEqual(abs(h0["diff_vs_task10"]), 0.01)

        self.assertEqual(h4a["n"], 99)
        self.assertAlmostEqual(h4a["net_pnl"], 1257.70, places=2)
        self.assertAlmostEqual(h4a["mtm_dd_pct"], 6.18, places=2)
        self.assertLessEqual(abs(h4a["diff_vs_task10"]), 0.01)

    def test_04_independent_reconciliation_zero_mismatches_w1_w2_w3(self):
        rec = json.loads((AUDIT_DIR / "task11_independent_reconciliation.json").read_text(encoding="utf-8"))
        expected_counts = {"W1": 118, "W2": 131, "W3": 143}
        for win, expected_n in expected_counts.items():
            self.assertIn(win, rec)
            self.assertEqual(rec[win]["frozen_trades"], expected_n)
            self.assertEqual(rec[win]["sim_trades"], expected_n)
            self.assertEqual(rec[win]["mismatch_count"], 0)
            self.assertEqual(rec[win]["max_pnl_diff_usd"], 0.0)

    def test_05_hypothesis_results_preserve_h0_h4a_and_hwm_separation(self):
        rows = {}
        with (AUDIT_DIR / "task11_regime_hypothesis_results.csv").open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows[row["hypothesis"]] = row

        for required in (
            "H0_BASELINE_V2",
            "H8_H4a_ALL_REGIMES",
            "R1_ADX20_ON_H0",
            "R10_H4a_NO_SW_TO_BULL_BUY",
            "H0_HWM8",
            "H4a_HWM8",
            "H4a_COST_STRESS",
        ):
            self.assertIn(required, rows, f"Missing hypothesis row: {required}")

        self.assertEqual(int(rows["H0_BASELINE_V2"]["n"]), 143)
        self.assertEqual(int(rows["H8_H4a_ALL_REGIMES"]["n"]), 99)
        self.assertEqual(int(rows["H0_HWM8"]["n"]), 78)
        self.assertEqual(int(rows["H4a_HWM8"]["n"]), 99)
        self.assertAlmostEqual(
            float(rows["H4a_HWM8"]["net_pnl"]),
            float(rows["H8_H4a_ALL_REGIMES"]["net_pnl"]),
            places=2,
        )

    def test_06_safety_locks_shadow_mode_and_demo(self):
        settings_path = ROOT / "src" / "goldedge" / "config" / "settings.py"
        text = settings_path.read_text(encoding="utf-8")
        self.assertRegex(text, r"SHADOW_MODE:\s*bool\s*=\s*Field\(\s*True\b")
        self.assertRegex(text, r'TRADING_MODE:\s*str\s*=\s*Field\(\s*"demo"')


if __name__ == "__main__":
    unittest.main()
