"""
TASK G3.2 Label Design, Cost Accuracy & Clean-Room Verification Test Suite.
"""

from __future__ import annotations

import ast
import hashlib
import json
import unittest
from pathlib import Path

from src.goldedge.config.settings import settings
from src.goldedge.domain.services.label_spec_service import (
    DEFAULT_COST_SPEC,
    classify_macro_endpoint,
    classify_micro_30tick,
    classify_micro_entry_timing,
    evaluate_tick_barrier_path,
    verify_broker_cost_schedule_parity,
)

ROOT = Path("/app") if Path("/app/scripts").exists() else Path(".")
AUDIT_DIR = ROOT / "backtest_results/audit/task_g3_2_label_design"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


class TestTaskG32Validation(unittest.TestCase):
    def test_clean_room_validator_zero_goldedge_imports(self):
        script = ROOT / "scripts/audit/task_g3_2_phase_de_independent_validator.py"
        self.assertTrue(script.exists())
        tree = ast.parse(script.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("goldedge", alias.name)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn("goldedge", node.module or "")

    def test_domain_services_strict_hexagonal_pure_python(self):
        forbidden = {"pandas", "numpy", "sqlalchemy", "psycopg2", "requests"}
        for rel in (
            "src/goldedge/domain/services/label_spec_service.py",
            "src/goldedge/domain/services/ml_feature_contract.py",
        ):
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".")[0], forbidden)
                elif isinstance(node, ast.ImportFrom):
                    mod = (node.module or "").split(".")[0]
                    self.assertNotIn(mod, forbidden)

    def test_cost_parity_and_raw_vs_net_separation(self):
        svc_tot, spec_tot, diff = verify_broker_cost_schedule_parity(1.0, 4179.94, 4179.94, DEFAULT_COST_SPEC)
        self.assertAlmostEqual(svc_tot, 10.04, places=2)
        self.assertLessEqual(diff, 0.01)

        # Marginal +0.101% raw move drops below +0.10% net after $12/$1M/side comm + 0.0533p slippage
        entry_ask = 4000.00
        entry_bid = 3999.93
        exit_bid = entry_ask * 1.00101  # +0.101% raw
        exit_ask = exit_bid + 0.07
        res = classify_macro_endpoint(entry_bid, entry_ask, exit_bid, exit_ask, threshold_pct=0.0010)
        self.assertEqual(res["raw_class"], "UP")
        self.assertEqual(res["net_class"], "FLAT")

    def test_tick_chronological_barrier_ordering_same_minute(self):
        # Within the same 60s minute, price hits SL first then TP -> MUST resolve SL_FIRST
        res1 = evaluate_tick_barrier_path(
            direction="BUY",
            entry_exec_px=4000.0,
            entry_ts_s=1000.0,
            tick_times_s=[1005.0, 1010.0, 1015.0],
            tick_bids=[3995.5, 4004.5, 4001.0],
            tick_asks=[3995.6, 4004.6, 4001.1],
            tp_dist_usd=4.0,
            sl_dist_usd=4.0,
        )
        self.assertEqual(res1["first_outcome"], "SL_FIRST")
        self.assertTrue(res1["both_reached"])
        self.assertAlmostEqual(res1["time_to_sl_s"], 5.0)
        self.assertAlmostEqual(res1["time_to_tp_s"], 10.0)

        # Reverse order: TP hit at +5s, SL hit at +10s -> MUST resolve TP_FIRST
        res2 = evaluate_tick_barrier_path(
            direction="BUY",
            entry_exec_px=4000.0,
            entry_ts_s=1000.0,
            tick_times_s=[1005.0, 1010.0],
            tick_bids=[4004.5, 3995.5],
            tick_asks=[4004.6, 3995.6],
            tp_dist_usd=4.0,
            sl_dist_usd=4.0,
        )
        self.assertEqual(res2["first_outcome"], "TP_FIRST")
        self.assertTrue(res2["both_reached"])

    def test_invalid_quotes_rejected(self):
        with self.assertRaises(ValueError):
            classify_macro_endpoint(4000.10, 4000.00, 4005.00, 4005.07)
        with self.assertRaises(ValueError):
            classify_micro_30tick(4000.00, 4000.00, 4001.00, 4001.07)

    def test_micro_entry_timing_vs_standalone_rt_scalp(self):
        # A +$0.14 upward quote move is relevant to H3b BUY entry timing (ENTER_NOW, beats $0.07 spread)
        # even though it is FLAT as a standalone 8s round-trip scalp (since $0.14 < $0.181 RT friction).
        rt = classify_micro_30tick(4180.00, 4180.07, 4180.14, 4180.21)
        self.assertEqual(rt["net_class"], "FLAT")

        et = classify_micro_entry_timing(4180.00, 4180.07, 4180.14, 4180.21)
        self.assertEqual(et["quote_move_class_1x_spread"], "UP")
        self.assertEqual(et["quote_move_class_010"], "UP")
        self.assertEqual(et["buy_timing_class"], "ENTER_NOW")
        self.assertEqual(et["sell_timing_class"], "WAIT_ADVERSE")

        # Verify exact proportional commission signs:
        # BUY pays ask*(1 + c_side) => Cost(wait) - Cost(now) = (ask_fut - ask_now)*(1 + c_side)
        # SELL receives bid*(1 - c_side) => Proceeds(now) - Proceeds(wait) = (bid_now - bid_fut)*(1 - c_side)
        c_side = DEFAULT_COST_SPEC.commission_usd_per_million_per_side / 1_000_000.0
        self.assertAlmostEqual(et["buy_timing_adv_usd"], (4180.21 - 4180.07) * (1.0 + c_side), places=12)
        self.assertAlmostEqual(et["sell_timing_adv_usd"], (4180.00 - 4180.14) * (1.0 - c_side), places=12)

    def test_phase_a_d_e_deliverables_pass(self):
        a1 = json.loads((AUDIT_DIR / "g3_2_candle_source_reconciliation.json").read_text(encoding="utf-8"))
        self.assertEqual(a1["status"], "PASS")
        self.assertEqual(a1["candle_1m_reconciliation"]["market_bars_1m_quote_semantics_v1_bars"], 96862)
        self.assertEqual(a1["candle_1m_reconciliation"]["ohlc_tickcount_spread_mismatches"], 0)

        a2 = json.loads((AUDIT_DIR / "g3_2_cost_parity_verification.json").read_text(encoding="utf-8"))
        self.assertEqual(a2["status"], "PASS")
        self.assertEqual(a2["configured_application_settings"]["commission_rate_per_million_per_side_usd"], 12.0)

        d_aud = json.loads((AUDIT_DIR / "g3_2_causality_audit.json").read_text(encoding="utf-8"))
        self.assertEqual(d_aud["status"], "PASS")
        self.assertEqual(d_aud["total_violations"], 0)

        e_rec = json.loads((AUDIT_DIR / "g3_2_independent_reconciliation.json").read_text(encoding="utf-8"))
        self.assertEqual(e_rec["overall_status"], "PASS")
        self.assertEqual(e_rec["macro_independent_check"]["coverage_pct"], 100.0)
        self.assertEqual(e_rec["macro_independent_check"]["rows_checked"], 19004)
        self.assertEqual(e_rec["macro_independent_check"]["mismatches"], 0)
        self.assertGreaterEqual(e_rec["micro_independent_check"]["rows_checked"], 15000)
        self.assertEqual(e_rec["micro_independent_check"]["mismatches"], 0)
        self.assertTrue(e_rec["macro_5m_bucket_accounting"]["exact_reconciliation_pass"])
        self.assertEqual(e_rec["macro_5m_bucket_accounting"]["unaccounted_rows"], 0)

        self.assertTrue((AUDIT_DIR / "g3_2_macro_labels.parquet").exists())
        self.assertTrue((AUDIT_DIR / "g3_2_micro_labels.parquet").exists())

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
