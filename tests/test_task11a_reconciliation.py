"""Task 11A — Independent Raw-Tick Reconciliation of Market Regime Candidates Verification Suite."""
from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "backtest_results").exists() and Path("/app/backtest_results").exists():
    ROOT = Path("/app")

AUDIT_DIR = ROOT / "backtest_results" / "audit"
SCRIPTS_DIR = ROOT / "scripts" / "audit"

EXPECTED_DELIVERABLES = [
    "task11a_candidate_reconciliation.json",
    "task11a_trade_level_differences.csv",
    "task11a_equity_path_comparison.json",
    "task11a_validation_tests.json",
    "task11a_final_report.md",
]

REQUIRED_CANDIDATES = [
    "H0_BASELINE_V2",
    "H8_H4a_ALL_REGIMES",
    "R1_ADX20_ON_H0",
    "R2_NO_RAW_HIVOL_ON_H0",
    "R3_SIDEWAYS_ER25_ON_H0",
    "R4_NO_POST_TRANS3_ON_H0",
    "R5_NO_SW_TO_BULL_BUY_H0",
    "R6_TREND_CAP080_SW_OPEN",
    "R7_H4a_PLUS_ADX20",
    "R8_H4a_PLUS_NO_RAW_HIVOL",
    "R9_H4a_PLUS_NO_POST_TRANS3",
    "R10_H4a_NO_SW_TO_BULL_BUY",
    "R11_H4a_PLUS_H7_REF",
]


class TestTask11AReconciliation(unittest.TestCase):
    def test_01_all_5_deliverables_exist_and_non_empty(self):
        for name in EXPECTED_DELIVERABLES:
            p = AUDIT_DIR / name
            self.assertTrue(p.exists(), f"Missing deliverable: {p}")
            self.assertGreater(p.stat().st_size, 0, f"Empty deliverable: {p}")

    def test_02_reconciler_script_has_zero_goldedge_imports(self):
        path = SCRIPTS_DIR / "task11a_independent_candidate_reconciler.py"
        self.assertTrue(path.exists())
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(
                        alias.name.startswith("goldedge") or alias.name.startswith("src.goldedge"),
                        f"Forbidden import: {alias.name}",
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                self.assertFalse(
                    mod.startswith("goldedge") or mod.startswith("src.goldedge"),
                    f"Forbidden import from: {mod}",
                )

    def test_03_all_39_candidate_window_pairs_fully_reconciled(self):
        rec = json.loads((AUDIT_DIR / "task11a_candidate_reconciliation.json").read_text(encoding="utf-8"))
        for wid in ("W1", "W2", "W3"):
            self.assertIn(wid, rec)
            for cname in REQUIRED_CANDIDATES:
                self.assertIn(cname, rec[wid], f"Missing {wid}/{cname}")
                info = rec[wid][cname]
                self.assertEqual(info["status"], "FULLY_RECONCILED", f"{wid}/{cname} not reconciled: {info}")
                self.assertEqual(info["missing_trades"], 0)
                self.assertEqual(info["extra_trades"], 0)
                self.assertEqual(info["stop_update_mismatches"], 0)
                self.assertEqual(info["portfolio_constraint_violations"], 0)
                self.assertLessEqual(info["max_gross_pnl_diff_usd"], 1e-6)
                self.assertLessEqual(info["max_net_pnl_diff_usd"], 1e-6)
                self.assertTrue(info["task11_published_match"])

    def test_04_equity_path_and_mtm_dd_reconcile(self):
        eq = json.loads((AUDIT_DIR / "task11a_equity_path_comparison.json").read_text(encoding="utf-8"))
        for wid in ("W1", "W2", "W3"):
            for cname in REQUIRED_CANDIDATES:
                info = eq[wid][cname]
                self.assertLessEqual(info["final_equity_diff_usd"], 1e-6)
                self.assertLessEqual(info["max_trade_equity_path_diff_usd"], 1e-6)
                self.assertLessEqual(info["closed_dd_diff_usd"], 1e-4)
                self.assertLessEqual(info["mtm_dd_pct_diff"], 1e-6)
                self.assertEqual(info["portfolio_constraint_violations"], 0)

    def test_05_validation_tests_json_and_hashes(self):
        vt = json.loads((AUDIT_DIR / "task11a_validation_tests.json").read_text(encoding="utf-8"))
        self.assertTrue(vt["all_checks_passed"])
        self.assertEqual(vt["total_candidate_window_pairs"], 39)
        self.assertEqual(vt["fully_reconciled_pairs"], 39)
        self.assertEqual(vt["unverified_or_mismatched_pairs"], 0)
        for k, v in vt["frozen_hashes"].items():
            self.assertTrue(v, f"Frozen hash failed: {k}")

    def test_06_safety_locks_preserved(self):
        text = (ROOT / "src" / "goldedge" / "config" / "settings.py").read_text(encoding="utf-8")
        self.assertRegex(text, r"SHADOW_MODE:\s*bool\s*=\s*Field\(\s*True\b")
        self.assertRegex(text, r'TRADING_MODE:\s*str\s*=\s*Field\(\s*"demo"')


if __name__ == "__main__":
    unittest.main()
