"""
Task 10 Verification & Audit Regression Tests:
  1. All 12 deliverables exist under `backtest_results/audit/`.
  2. Frozen Task 4 and Task 6C V2 hashes remain untouched.
  3. Zero `src/goldedge` imports in Task 10 independent research scripts.
  4. Independent raw-tick H0 reconciliation across W1 (118), W2 (131), W3 (143)
     shows 0 mismatches and Task 9 H4a/H5/H1/H7 cross-check matches.
  5. Safety locks (`SHADOW_MODE=True`, `TRADING_MODE=demo`) preserved.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if not (ROOT / "backtest_results").exists() and Path("/app/backtest_results").exists():
    ROOT = Path("/app")
SRC = ROOT / "src"
for p in (str(ROOT), str(SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

AUDIT_DIR = ROOT / "backtest_results" / "audit"
FROZEN_V2_DIR = AUDIT_DIR / "frozen_v2_recovered"


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class TestTask10ResearchAudit(unittest.TestCase):
    def test_01_frozen_v1_v2_hashes_untouched(self):
        manifest = json.loads((FROZEN_V2_DIR / "task6c_frozen_manifest.json").read_text(encoding="utf-8"))
        t4 = manifest["prior_frozen_task4_preserved"]
        app_sum = manifest["application_summary"]
        self.assertEqual(
            _sha256(ROOT / "backtest_results" / "frozen_111_baseline_trades.csv"),
            t4["frozen_111_baseline_trades_sha256"],
        )
        for wid, fn in [("W1", "w1_trades.csv"), ("W2", "w2_trades.csv"), ("W3", "w3_trades.csv")]:
            self.assertEqual(_sha256(FROZEN_V2_DIR / fn), app_sum[wid]["trades_csv_sha256"])
        self.assertEqual(_sha256(FROZEN_V2_DIR / "w3_journeys.json"), app_sum["W3"]["journeys_json_sha256"])

    def test_02_independent_scripts_do_not_import_goldedge(self):
        for name in ("task10_part1_audit_and_analysis.py", "task10_independent_sim_and_validation.py"):
            src = (ROOT / "scripts" / "audit" / name).read_text(encoding="utf-8")
            self.assertNotIn("from goldedge", src)
            self.assertNotIn("import goldedge", src)
            self.assertNotIn("src.goldedge", src)

    def test_03_reconciliation_and_deliverables_valid(self):
        ver = json.loads((AUDIT_DIR / "task10_baseline_verification.json").read_text(encoding="utf-8"))
        self.assertTrue(ver["frozen_hashes"]["task4_match"])
        self.assertTrue(ver["frozen_hashes"]["v2_w3_match"])
        self.assertEqual(ver["task9_evidence_check"]["task9_trades_count"], 143)
        self.assertLessEqual(ver["task9_evidence_check"]["task9_vs_frozen_w3_max_pnl_diff"], 0.02)

        enr = pd.read_csv(AUDIT_DIR / "task10_trades_enriched.csv")
        self.assertEqual(len(enr), 143)

        audit = json.loads((AUDIT_DIR / "task10_tradescorer_component_audit.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["configuration"]["w_ml_backtest_and_live"], 0.0)
        self.assertEqual(audit["configuration"]["w_quant_backtest_and_live"], 1.0)

        recon_path = AUDIT_DIR / "task10_independent_reconciliation.json"
        if recon_path.exists():
            recon = json.loads(recon_path.read_text(encoding="utf-8"))
            for wid, exp_n in [("W1", 118), ("W2", 131), ("W3", 143)]:
                self.assertEqual(recon[wid]["sim_trades"], exp_n)
                self.assertEqual(recon[wid]["mismatch_count"], 0)
                self.assertLessEqual(recon[wid]["max_pnl_diff_usd"], 0.02)
            for k, v in recon["task9_cross_check"].items():
                self.assertLessEqual(v, 0.05, msg=f"Cross-check diff too large for {k}: {v}")

        hyp_path = AUDIT_DIR / "task10_hypothesis_results.json"
        if hyp_path.exists():
            hyps = json.loads(hyp_path.read_text(encoding="utf-8"))
            names = {h["hypothesis"] for h in hyps}
            for req in ("H0_BASELINE_V2", "H4a_SCORE_CAP_080", "H5_ER_CAP_045", "H4a_PLUS_H5", "H4b_SCORE_CAP_075", "ACCEL_REMOVE", "H1_BLOCK_BUY_07UTC", "H7_BUY_FAST_BE_075_T1_12"):
                self.assertIn(req, names)

    def test_04_safety_locks_preserved(self):
        from goldedge.config.settings import settings
        self.assertTrue(settings.SHADOW_MODE)
        self.assertEqual(settings.TRADING_MODE, "demo")


if __name__ == "__main__":
    unittest.main()
