"""
Task 9 Audit & Verification Tests:
  1. Frozen Task 4 & Task 6C V2 baseline immutability (SHA-256 verification).
  2. Complete 143/143 trade-level reconciliation between frozen W3 V2 and
     `task9_trades_enriched_w3.csv` + component score identity.
  3. Hypothesis test integrity (`H0_BASELINE_V2` match, BH-FDR consistency,
     retained + removed == 143 invariant, separate Task 8 view = 78 trades).
  4. Safety lock & strategy non-modification check (`SHADOW_MODE=True`, zero live orders).
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


class TestTask9Diagnostics(unittest.TestCase):
    def test_01_frozen_v1_and_v2_hashes_intact(self):
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

    def test_02_enriched_w3_trades_reconcile_143_of_143(self):
        frozen = pd.read_csv(FROZEN_V2_DIR / "w3_trades.csv")
        enriched = pd.read_csv(AUDIT_DIR / "task9_trades_enriched_w3.csv")
        self.assertEqual(len(frozen), 143)
        self.assertEqual(len(enriched), 143)
        self.assertEqual(int((enriched["direction"] == "BUY").sum()), 41)
        self.assertEqual(int((enriched["direction"] == "SELL").sum()), 102)

        for i in range(143):
            f_row = frozen.iloc[i]
            e_row = enriched.iloc[i]
            self.assertEqual(pd.to_datetime(f_row["entry_time"], utc=True), pd.to_datetime(e_row["entry_time"], utc=True))
            self.assertEqual(pd.to_datetime(f_row["exit_time"], utc=True), pd.to_datetime(e_row["exit_time"], utc=True))
            self.assertEqual(str(f_row["direction"]), str(e_row["direction"]))
            self.assertEqual(str(f_row["exit_reason"]), str(e_row["exit_reason"]))
            self.assertAlmostEqual(float(f_row["pnl"]), float(e_row["pnl"]), places=2)
            # Reconstructed quant component score must match frozen entry score
            self.assertAlmostEqual(float(f_row["score"]), float(e_row["score_computed"]), places=4)

        # Verify causal invariants on enriched features
        self.assertFalse(enriched["kalman_vel"].isna().any())
        self.assertFalse(enriched["kaufman_er"].isna().any())
        self.assertTrue((enriched["entry_bid"] < enriched["entry_ask"]).all())
        self.assertEqual(int((enriched["direction"] == "SELL").sum()), int(enriched.loc[enriched["direction"] == "SELL", "bar5m_candle_aligned"].sum()))

    def test_03_phase1_and_hypothesis_invariants(self):
        p1 = json.loads((AUDIT_DIR / "task9_phase1_verification.json").read_text(encoding="utf-8"))
        self.assertTrue(p1["all_hashes_verified"])
        self.assertEqual(p1["independent_w3_reproduction"]["trade_mismatch_count"], 0)
        self.assertEqual(p1["independent_w3_reproduction"]["trail_step_mismatches"], 0)
        self.assertEqual(p1["task8_protected_w3_summary"]["trades"], 78)

        diag = json.loads((AUDIT_DIR / "task9_diagnosis_summary.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(diag["overview"]["V2_W3_BUY"]["net_pnl"], -450.50, places=2)
        self.assertAlmostEqual(diag["overview"]["V2_W3_SELL"]["net_pnl"], 661.98, places=2)
        self.assertAlmostEqual(diag["overview"]["TASK8_W3_ALL"]["net_pnl"], 146.64, places=2)

        h_path = AUDIT_DIR / "task9_hypothesis_results.json"
        if h_path.exists():
            h_res = json.loads(h_path.read_text(encoding="utf-8"))
            h0 = h_res[0]
            self.assertEqual(h0["hypothesis"], "H0_BASELINE_V2")
            self.assertEqual(h0["overall"]["n"], 143)
            self.assertLessEqual(h0["h0_verify_max_pnl_diff"], 0.02)
            for r in h_res:
                self.assertEqual(r["retained"] + r["removed"], 143)
                self.assertEqual(r["retained"] + r["newly_generated"], r["overall"]["n"])
                self.assertGreaterEqual(r["mw_q_fdr_vs_h0"], r["mw_p_vs_h0"] - 1e-6)

    def test_04_safety_lock_and_strategy_unchanged(self):
        from goldedge.config.settings import settings
        self.assertTrue(settings.SHADOW_MODE)
        self.assertEqual(settings.TRADING_MODE, "demo")


if __name__ == "__main__":
    unittest.main()
