"""
TASK G3.5R Validation & Phase E/F 14-Scenario Synthetic & Adversarial Test Suite.

Covers:
  - AST verification that scripts/audit/task_g3_5r_phase_f_independent.py has ZERO src/goldedge/ imports
  - All 14 mandatory Phase E synthetic & adversarial execution tests (writes g3_5r_adversarial_test_results.json)
  - 112/112 exact reconciliation & safety locks (SHADOW_MODE=True, TRADING_MODE="demo", HWM=8.0%)
"""

from __future__ import annotations

import ast
import hashlib
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from src.goldedge.adapters.secondary.ml.calibration_utility import simulate_h3b_trade_on_ticks
from src.goldedge.config.settings import settings

ROOT = Path("/app") if Path("/app/backtest_results").exists() else Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "backtest_results" / "audit" / "task_g3_5r_h3b_execution_reconciliation"
INDEP_SCRIPT = ROOT / "scripts" / "audit" / "task_g3_5r_phase_f_independent.py"


class TestTaskG35RValidation(unittest.TestCase):
    def test_clean_room_auditor_zero_goldedge_imports(self):
        tree = ast.parse(INDEP_SCRIPT.read_text(encoding="utf-8"))
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "goldedge" in alias.name:
                        bad.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if "goldedge" in mod:
                    bad.append(mod)
        self.assertEqual(bad, [], f"Clean-room script must not import goldedge: {bad}")

    def test_safety_locks_and_reconciled_baseline(self):
        self.assertTrue(settings.SHADOW_MODE)
        self.assertEqual(str(settings.TRADING_MODE).lower(), "demo")
        self.assertAlmostEqual(float(settings.HWM_MAX_DRAWDOWN_PCT), 8.0, places=3)
        f111 = hashlib.sha256((ROOT / "backtest_results" / "frozen_111_baseline_trades.csv").read_bytes()).hexdigest().upper()
        self.assertEqual(f111, "73306E1529ADC48284BCE9AB6AC35122F33B9999D2464AF441867EB3BB6CF168")
        rec = json.loads((OUT_DIR / "g3_5r_reconciled_baseline_results.json").read_text(encoding="utf-8"))
        self.assertTrue(rec["exact_reconciled"])
        self.assertEqual(rec["exit_reason_mismatches"], 0)
        self.assertAlmostEqual(rec["total_r_diff"], 0.0, places=5)

    def test_phase_e_all_14_synthetic_and_adversarial_scenarios(self):
        results = {}
        t0 = datetime(2026, 9, 10, 10, 0, 10, tzinfo=timezone.utc).timestamp()

        # 01. E1 triggers at exact end of 15th completed post-entry 1m bar (MFE < 0.4 ATR)
        ts = np.array([t0] + [t0 + 60.0 * k for k in range(1, 17)])
        bids = np.full(len(ts), 4400.0)
        asks = np.full(len(ts), 4400.1)
        r = simulate_h3b_trade_on_ticks("BUY", 0, ts, bids, asks, atr=3.0)
        self.assertEqual(r["exit_reason"], "EARLY_FAIL_15M")
        self.assertEqual(r["exit_ts"], ts[15])
        results["01_e1_exact_15th_bar_trigger"] = "PASS"

        # 02. E1 does NOT trigger before candle close if MFE >= 0.4 ATR achieved during bar 15
        bids2 = bids.copy()
        asks2 = asks.copy()
        bids2[15] = 4401.60  # +1.50 >= 0.4 * 3.0 = 1.20 ATR
        asks2[15] = 4401.70
        r2 = simulate_h3b_trade_on_ticks("BUY", 0, ts, bids2, asks2, atr=3.0)
        self.assertNotEqual(r2["exit_reason"], "EARLY_FAIL_15M")
        results["02_e1_suppressed_when_mfe_reached_in_bar15"] = "PASS"

        # 03. Hard SL touched BEFORE E1 takes precedence
        bids3 = bids.copy()
        asks3 = asks.copy()
        bids3[5] = 4395.0  # below SL (~4395.6)
        asks3[5] = 4395.1
        r3 = simulate_h3b_trade_on_ticks("BUY", 0, ts, bids3, asks3, atr=3.0)
        self.assertEqual(r3["exit_reason"], "SL")
        results["03_sl_precedes_e1"] = "PASS"

        # 04. TP touched BEFORE E1 takes precedence and fills at exact TP (0 exit slippage)
        bids4 = bids.copy()
        asks4 = asks.copy()
        bids4[4] = 4410.0  # above TP (~4409.1)
        asks4[4] = 4410.1
        r4 = simulate_h3b_trade_on_ticks("BUY", 0, ts, bids4, asks4, atr=3.0)
        self.assertEqual(r4["exit_reason"], "TARGET")
        self.assertAlmostEqual(r4["exec_exit"], r4["tp"], places=5)
        results["04_tp_precedes_e1_exact_limit_fill"] = "PASS"

        # 05. Same-timestamp SL vs TP conflict (conservative SL check precedes TP)
        bids5 = np.array([4400.0, 4395.0, 4412.0])
        asks5 = np.array([4400.1, 4395.1, 4412.1])
        r5 = simulate_h3b_trade_on_ticks("BUY", 0, np.array([t0, t0 + 60.0, t0 + 60.0]), bids5, asks5, atr=3.0)
        self.assertEqual(r5["exit_reason"], "SL")
        results["05_same_timestamp_sl_precedes_tp"] = "PASS"

        # 06. Fast-moving bid/ask quotes trigger BREAKEVEN then exit at BREAKEVEN (not SL!)
        ts6 = np.array([t0, t0 + 60.0, t0 + 120.0, t0 + 180.0])
        bids6 = np.array([4400.0, 4403.5, 4400.2, 4399.0])  # +3.4 > 1.0 ATR -> BE at 4400.305 -> hit on bar 2
        asks6 = bids6 + 0.1
        r6 = simulate_h3b_trade_on_ticks("BUY", 0, ts6, bids6, asks6, atr=3.0)
        self.assertEqual(r6["exit_reason"], "BREAKEVEN")
        results["06_fast_quotes_breakeven_exit_reason"] = "PASS"

        # 07. Missing, zero-spread (b==a), and inverted (b>a) quotes skipped cleanly
        ts7 = np.array([t0, t0 + 60.0, t0 + 61.0, t0 + 62.0, t0 + 120.0])
        bids7 = np.array([4400.0, 4300.0, 4300.0, 0.0, 4412.0])
        asks7 = np.array([4400.1, 4299.0, 4300.0, 0.0, 4412.1])  # inverted, zero-spread, zero
        r7 = simulate_h3b_trade_on_ticks("BUY", 0, ts7, bids7, asks7, atr=3.0)
        self.assertEqual(r7["exit_reason"], "TARGET")
        results["07_corrupted_zero_spread_inverted_quotes_skipped"] = "PASS"

        # 08. Session cutoff >= 15:00 UTC rejects entry
        t_late = datetime(2026, 9, 10, 15, 0, 1, tzinfo=timezone.utc).timestamp()
        r8 = simulate_h3b_trade_on_ticks("BUY", 0, np.array([t_late, t_late + 60.0]), np.array([4400.0, 4401.0]), np.array([4400.1, 4401.1]), atr=3.0)
        self.assertFalse(r8["filled"])
        self.assertEqual(r8["reason"], "PAST_SESSION_CUTOFF")
        results["08_session_cutoff_15utc_enforced"] = "PASS"

        # 09. Short (SELL) trade executes at bid entry / ask exit & triggers TARGET cleanly
        ts9 = np.array([t0, t0 + 60.0, t0 + 120.0])
        bids9 = np.array([4400.0, 4395.0, 4390.0])
        asks9 = bids9 + 0.1
        r9 = simulate_h3b_trade_on_ticks("SELL", 0, ts9, bids9, asks9, atr=3.0)
        self.assertEqual(r9["exit_reason"], "TARGET")
        self.assertGreater(r9["net_r"], 1.5)
        results["09_short_trade_bid_ask_and_target"] = "PASS"

        # 10. Wide spread at entry does not corrupt TP/SL distance (anchored to executable fill)
        ts10 = np.array([t0, t0 + 60.0])
        bids10 = np.array([4400.0, 4400.0])
        asks10 = np.array([4401.0, 4401.0])
        r10 = simulate_h3b_trade_on_ticks("BUY", 0, ts10, bids10, asks10, atr=3.0)
        self.assertAlmostEqual(r10["tp"] - r10["exec_entry"], 9.0, places=4)
        self.assertAlmostEqual(r10["exec_entry"] - r10["init_sl"], 4.5, places=4)
        results["10_spread_anchored_sl_tp_distances"] = "PASS"

        # 11. Broker-rounded 2-decimal per-leg commission & slippage match exact formula
        ec = round(r4["volume"] * 100.0 * r4["exec_entry"] * (12.0 / 1_000_000.0), 2)
        xc = round(r4["volume"] * 100.0 * r4["exec_exit"] * (12.0 / 1_000_000.0), 2)
        self.assertAlmostEqual(r4["comm_usd"], round(ec + xc, 2), places=4)
        results["11_broker_rounded_commission_exact"] = "PASS"

        # 12. TIME_SQUEEZE triggers after 75% of 60m hold window (bar 45+)
        ts12 = np.array([t0] + [t0 + 60.0 * k for k in range(1, 62)])
        bids12 = np.full(len(ts12), 4402.0)  # avoids E1; by bar 54 TIME_SQUEEZE ratchets SL up near 4402.0
        bids12[0] = 4400.0
        bids12[55] = 4400.5  # pullback hits time-squeezed SL
        asks12 = bids12 + 0.1
        r12 = simulate_h3b_trade_on_ticks("BUY", 0, ts12, bids12, asks12, atr=3.0)
        self.assertEqual(r12["exit_reason"], "TIME_SQUEEZE")
        results["12_time_squeeze_canonical_trigger"] = "PASS"

        # 13. Position sizing & net-R conversion strictly bounded by 0.5% planned risk
        self.assertLessEqual(r4["planned_risk_usd"], 50.0001)
        self.assertAlmostEqual(r4["net_r"], r4["net_pnl_usd"] / r4["planned_risk_usd"], places=4)
        results["13_sizing_and_net_r_conversion_exact"] = "PASS"

        # 14. Deterministic replay produces bit-identical output
        r14a = simulate_h3b_trade_on_ticks("BUY", 0, ts6, bids6, asks6, atr=3.0)
        r14b = simulate_h3b_trade_on_ticks("BUY", 0, ts6, bids6, asks6, atr=3.0)
        self.assertEqual(r14a, r14b)
        results["14_deterministic_replay_bit_identical"] = "PASS"

        out = {
            "task": "G3.5R_PHASE_E_ADVERSARIAL_TEST_RESULTS",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "overall_status": "PASS",
            "tests_passed": len(results),
            "tests_total": 14,
            "results": results,
        }
        (OUT_DIR / "g3_5r_adversarial_test_results.json").write_text(json.dumps(out, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
