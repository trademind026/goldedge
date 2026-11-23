"""
Task G1 Validation & Regression Test Suite.

Covers:
  - Defect E1: B4 invalid/inverted quote guard precedes Kalman update during exit scan
  - Defect E2: Hard SL/TARGET precede Kalman velocity reversal; MTM tracker updates on Kalman exit;
               EARLY_FAIL/TIMEOUT report last valid tick index
  - Defects E3, E4, E6, E8 & Phase 5 API contract on routes/backtest.py
  - Defect E5: equity_curve timestamps use realized exit_time (not entry_time)
  - Phase 5 isolated opt-in `max_score_threshold` gate (default-OFF H0 parity + 0.80 cap rejection)
  - Clean-room audit AST isolation & frozen baseline SHA-256 + Phase 1/5 reconciliation checks
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask
import numpy as np
import pandas as pd

from goldedge.adapters.primary.rest_api.routes import backtest as bt_routes
from goldedge.application.use_cases.run_backtest import (
    RunBacktestUseCase,
    _MtmDrawdownTracker,
    _OpenPosition,
)
from goldedge.config.risk_profiles import MEDIUM_RISK_BACKTEST
from goldedge.domain.models.signal import Signal, SignalDirection
from goldedge.domain.services.denoise_service import VolumeAdaptiveKalmanFilter
from goldedge.domain.services.sizing_service import PositionSizingService

ROOT = Path(__file__).resolve().parent.parent
G1_DIR = ROOT / "backtest_results" / "audit" / "task_g1_root_cause"


def _make_tick_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


def _build_m1_trend_ticks(start_dt: datetime, minutes: int, base: float = 3800.0, step: float = 0.10) -> list[dict]:
    rows = []
    px = base
    for m in range(minutes):
        t = start_dt + timedelta(minutes=m)
        b0 = round(px, 2)
        b1 = round(px + step, 2)
        rows.append({"time": t + timedelta(seconds=1), "symbol": "XAUUSD", "bid": b0, "ask": round(b0 + 0.08, 2), "price": b0, "volume": 1})
        rows.append({"time": t + timedelta(seconds=30), "symbol": "XAUUSD", "bid": b1, "ask": round(b1 + 0.08, 2), "price": b1, "volume": 1})
        px = b1
    return rows


class TestTaskG1Validation(unittest.TestCase):
    def test_e1_invalid_quotes_do_not_corrupt_kalman_during_exit_scan(self):
        k_pos = VolumeAdaptiveKalmanFilter()
        k_ref = VolumeAdaptiveKalmanFilter()
        for px in (3850.0, 3850.1, 3850.2, 3850.3, 3850.4):
            k_pos.update_tick(px, None)
            k_ref.update_tick(px, None)

        t0 = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)
        pos = _OpenPosition(
            ticket=1,
            symbol="XAUUSD",
            direction="BUY",
            strategy="test_strat",
            entry_price=3850.50,
            sl=3845.00,
            tp=3862.00,
            atr=3.0,
            volume=0.10,
            entry_time=t0,
            entry_bar_idx=100,
            final_score=0.72,
            regime="TRENDING_BULL",
        )
        # Corrupted quotes include massive negative/inverted/zero-spread spikes that previously poisoned Kalman state
        bids = np.array([3850.60, 100.00, -50.00, 0.00, 3850.65, 3850.70], dtype=np.float64)
        asks = np.array([3850.80, 99.50, 100.00, 0.00, 3850.65, 3850.90], dtype=np.float64)
        vols = np.array([np.nan] * len(bids), dtype=np.float64)

        exited, reason, _, _ = pos.check_exit_ticks(
            bids, asks, vols, bar_idx=101, max_hold_bars=60, kalman=k_pos,
            entry_utc_hour=10, use_kalman_exit=True, minute_ts=t0,
        )
        self.assertFalse(exited)
        self.assertEqual(reason, "")

        k_ref.update_tick(3850.60, None)
        k_ref.update_tick(3850.70, None)
        self.assertAlmostEqual(float(k_pos.x.item(0)), float(k_ref.x.item(0)), places=12)
        self.assertAlmostEqual(float(k_pos.x.item(1)), float(k_ref.x.item(1)), places=12)
        self.assertAlmostEqual(float(k_pos.P[0, 0]), float(k_ref.P[0, 0]), places=12)
        self.assertAlmostEqual(float(k_pos.P[1, 1]), float(k_ref.P[1, 1]), places=12)

    def test_e2_hard_sl_tp_beat_kalman_reversal_and_mtm_updates(self):
        t0 = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)

        # Case A: Sharp drop crosses hard SL AND induces huge negative Kalman velocity -> hard SL must win
        k_sl = VolumeAdaptiveKalmanFilter()
        for px in (3850.0, 3850.1, 3850.2):
            k_sl.update_tick(px, None)
        pos_sl = _OpenPosition(
            ticket=1, symbol="XAUUSD", direction="BUY", strategy="test_strat",
            entry_price=3850.20, sl=3846.00, tp=3860.00, atr=1.20, volume=0.10,
            entry_time=t0, entry_bar_idx=100, final_score=0.70, regime="TRENDING_BULL",
        )
        exited, reason, ex_px, idx = pos_sl.check_exit_ticks(
            np.array([3844.50]), np.array([3844.70]), np.array([np.nan]),
            bar_idx=101, max_hold_bars=60, kalman=k_sl, entry_utc_hour=10,
            use_kalman_exit=True, minute_ts=t0,
        )
        self.assertTrue(exited)
        self.assertEqual(reason, "SL")
        self.assertAlmostEqual(ex_px, 3844.50, places=4)
        self.assertEqual(idx, 0)

        # Case B: Sharp rally on SELL hits TARGET cleanly even if seeded Kalman velocity is strongly positive
        k_tp = VolumeAdaptiveKalmanFilter()
        for px in (3850.0, 3850.1, 3850.2):
            k_tp.update_tick(px, None)
        pos_tp = _OpenPosition(
            ticket=2, symbol="XAUUSD", direction="BUY", strategy="test_strat",
            entry_price=3850.20, sl=3845.00, tp=3851.00, atr=1.20, volume=0.10,
            entry_time=t0, entry_bar_idx=100, final_score=0.70, regime="TRENDING_BULL",
        )
        k_tp.x[1] = -5.0  # force extreme negative velocity prior to upside jump hitting TP
        exited_tp, reason_tp, ex_px_tp, _ = pos_tp.check_exit_ticks(
            np.array([3851.20]), np.array([3851.40]), np.array([np.nan]),
            bar_idx=101, max_hold_bars=60, kalman=k_tp, entry_utc_hour=10,
            use_kalman_exit=True, minute_ts=t0,
        )
        self.assertTrue(exited_tp)
        self.assertEqual(reason_tp, "TARGET")
        self.assertAlmostEqual(ex_px_tp, 3851.00, places=4)

        # Case C: Pure KALMAN_VELOCITY_REVERSAL updates mtm_tracker accurately
        k_rev = VolumeAdaptiveKalmanFilter()
        for px in (3850.0, 3850.0, 3850.0):
            k_rev.update_tick(px, None)
        pos_rev = _OpenPosition(
            ticket=3, symbol="XAUUSD", direction="BUY", strategy="test_strat",
            entry_price=3850.00, sl=3840.00, tp=3865.00, atr=0.60, volume=0.10,
            entry_time=t0, entry_bar_idx=100, final_score=0.70, regime="TRENDING_BULL",
        )
        mtm = _MtmDrawdownTracker(10000.0, t0)
        tick_t = t0 + timedelta(seconds=15)
        exited_rev, reason_rev, ex_px_rev, _ = pos_rev.check_exit_ticks(
            np.array([3848.50]), np.array([3848.70]), np.array([np.nan]),
            bar_idx=101, max_hold_bars=60, kalman=k_rev, entry_utc_hour=10,
            use_kalman_exit=True, closed_equity=10000.0, mtm_tracker=mtm,
            minute_ts=t0, tick_times=[tick_t],
        )
        self.assertTrue(exited_rev)
        self.assertEqual(reason_rev, "KALMAN_VELOCITY_REVERSAL")
        self.assertAlmostEqual(ex_px_rev, 3848.50, places=4)
        self.assertEqual(mtm.trough_time, tick_t)
        self.assertAlmostEqual(mtm.max_dd, -15.0, places=2)

    def test_e5_equity_curve_uses_exit_time_not_entry_time(self):
        uc = RunBacktestUseCase(
            tick_repo=MagicMock(),
            virtual_broker=MagicMock(),
            manage_position=None,
            sizing_service=PositionSizingService(contract_size=100.0),
            profile=MEDIUM_RISK_BACKTEST,
        )
        t_in = datetime(2026, 10, 15, 9, 0, 0)
        t_out = datetime(2026, 10, 15, 9, 37, 15)
        closed_trades = [{
            "entry_time": t_in,
            "exit_time": t_out,
            "symbol": "XAUUSD",
            "direction": "BUY",
            "strategy": "test_strat",
            "entry_price": 3850.0,
            "exit_price": 3855.0,
            "sl": 3846.0,
            "theoretical_stop_price": 3846.0,
            "tp": 3858.0,
            "gross_pnl": 50.0,
            "pnl": 50.0,
            "commission_usd": 0.0,
            "slippage_usd": 0.0,
            "swap_usd": 0.0,
            "qty": 0.10,
            "exit_reason": "TARGET",
            "score": 0.72,
            "regime": "TRENDING_BULL",
            "planned_risk_usd": 40.0,
            "planned_risk_pct": 0.40,
            "realized_loss_pct": 0.0,
            "sl_gap_usd": 0.0,
            "journey": [],
        }]
        res = uc._build_result(closed_trades, initial_capital=10000.0, final_equity=10050.0, export_artifacts=False)
        self.assertEqual(res["equity_curve"][0]["time"], "2026-10-15 09:37:15")
        self.assertNotEqual(res["equity_curve"][0]["time"], "2026-10-15 09:00:00")

    def test_e3_e4_e6_e8_and_phase5_api_routes_and_metrics(self):
        sample_stats = {
            "initial_capital": 10000.0,
            "pnl": 125.0,
            "max_dd": -80.0,
            "closed_max_dd": -80.0,
            "closed_max_dd_pct": 0.8,
            "mtm_max_dd": -95.0,
            "mtm_max_dd_pct": 0.94,
            "mtm_max_dd_initial_pct": 0.95,
            "mtm_peak_equity": 10125.0,
            "mtm_trough_equity": 10030.0,
            "raw_tick_mtm_max_dd": -95.0,
            "total_commission_usd": 12.40,
            "total_slippage_usd": 1.80,
            "total_swap_usd": 0.35,
            "total_sl_gap_usd": -2.10,
            "worst_sl_gap_usd": -2.10,
            "sl_slippage_events": 1,
            "hwm_kill_switch": {"halted": False, "trades_prevented": 0},
            "trade_list": [{
                "entry_time": "2026-10-15 08:15:00",
                "exit_time": "2026-10-15 08:45:00",
                "direction": "BUY",
                "strategy": "MOMENTUM_SCALP",
                "entry_price": 3850.0,
                "exit_price": 3862.5,
                "qty": 0.10,
                "pnl": 125.0,
                "planned_risk_pct": 0.5,
                "realized_loss_pct": 0.0,
                "exit_reason": "TARGET",
            }],
        }
        m = bt_routes._extract_run_metrics(
            sample_stats,
            strategy_mode="candidate_15_e1",
            commission_per_lot=0.0,
            commission_profile="fusion_ctrader_demo",
            slippage_pips=0.0533,
            apply_swap=True,
            max_score_threshold=0.80,
        )
        for key in (
            "hwm_kill_switch",
            "total_commission_usd",
            "total_slippage_usd",
            "total_swap_usd",
            "total_sl_gap_usd",
            "commission_profile",
            "slippage_pips",
            "apply_swap",
            "max_score_threshold",
        ):
            self.assertIn(key, m)
        self.assertEqual(m["commission_profile"], "fusion_ctrader_demo")
        self.assertEqual(m["max_score_threshold"], 0.80)

        app = Flask(__name__)
        captured_exec_kwargs: list[dict] = []
        saved_runs: list[dict] = []

        class _FakeThread:
            def __init__(self, target=None, daemon=None):
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()

        def _make_mocks():
            uc = MagicMock()
            uc.profile = MEDIUM_RISK_BACKTEST

            def _fake_exec(*args, **kwargs):
                captured_exec_kwargs.append(kwargs)
                return {
                    **sample_stats,
                    "trades": 1,
                    "win_rate": 1.0,
                    "pf": 999.0,
                    "avg_win": 125.0,
                    "avg_loss": 0.0,
                    "rr": 1.0,
                    "equity_curve": [{"time": "2026-10-15 08:45:00", "equity": 10125.0}],
                }

            uc.execute.side_effect = _fake_exec
            repo = MagicMock()
            repo.save_run.side_effect = lambda d: saved_runs.append(d)
            return uc, repo

        orig_running = bt_routes._backtest_status.get("running", False)
        orig_cache = dict(bt_routes._backtest_results_cache)
        try:
            bt_routes._backtest_status["running"] = False

            # Test E3 default fusion_ctrader_demo + E8 high risk + Phase 5 max_score_threshold=0.80
            uc1, repo1 = _make_mocks()
            with (
                app.test_request_context(
                    "/api/backtest/run",
                    method="POST",
                    json={
                        "start_date": "2026-10-01",
                        "end_date": "2026-10-15",
                        "risk": "high",
                        "exit_agent": "Kalman",
                        "max_score_threshold": 0.80,
                    },
                ),
                patch.object(bt_routes.threading, "Thread", _FakeThread),
                patch.object(bt_routes, "get_profile", wraps=bt_routes.get_profile) as gp_spy,
            ):
                resp, status = self._call_unwrapped(bt_routes.run_backtest, run_backtest_uc=uc1, backtest_repo=repo1)
                self.assertEqual(status, 200)
                gp_spy.assert_any_call("HIGH_BACKTEST")
            self.assertEqual(captured_exec_kwargs[-1]["commission_profile"], "fusion_ctrader_demo")
            self.assertEqual(captured_exec_kwargs[-1]["account_id"], 48439552)
            self.assertAlmostEqual(captured_exec_kwargs[-1]["slippage_pips"], 0.0533, places=6)
            self.assertTrue(captured_exec_kwargs[-1]["apply_swap"])
            self.assertEqual(captured_exec_kwargs[-1]["max_score_threshold"], 0.80)
            self.assertEqual(bt_routes._backtest_results_cache["high"]["max_score_threshold"], 0.80)

            # Test explicit commission_profile="zero"
            uc2, repo2 = _make_mocks()
            with (
                app.test_request_context(
                    "/api/backtest/run",
                    method="POST",
                    json={
                        "start_date": "2026-10-01",
                        "end_date": "2026-10-15",
                        "commission_profile": "zero",
                    },
                ),
                patch.object(bt_routes.threading, "Thread", _FakeThread),
            ):
                _, status2 = self._call_unwrapped(bt_routes.run_backtest, run_backtest_uc=uc2, backtest_repo=repo2)
                self.assertEqual(status2, 200)
            self.assertEqual(captured_exec_kwargs[-1]["commission_profile"], "zero")
            self.assertEqual(captured_exec_kwargs[-1]["slippage_pips"], 0.0)
            self.assertFalse(captured_exec_kwargs[-1]["apply_swap"])

            # Test HWM > 8.0 and invalid max_score_threshold return 400
            for bad_payload in (
                {"start_date": "2026-10-01", "end_date": "2026-10-15", "max_hwm_drawdown_pct": 12.0},
                {"start_date": "2026-10-01", "end_date": "2026-10-15", "max_hwm_drawdown_pct": None},
                {"start_date": "2026-10-01", "end_date": "2026-10-15", "max_score_threshold": 0.0},
                {"start_date": "2026-10-01", "end_date": "2026-10-15", "max_score_threshold": 1.2},
            ):
                with app.test_request_context("/api/backtest/run", method="POST", json=bad_payload):
                    _, bad_status = self._call_unwrapped(bt_routes.run_backtest, run_backtest_uc=uc1, backtest_repo=repo1)
                    self.assertEqual(bad_status, 400)

            # Test E4 concurrency lock returns 409 when running=True
            bt_routes._backtest_status["running"] = True
            with app.test_request_context(
                "/api/backtest/run",
                method="POST",
                json={"start_date": "2026-10-01", "end_date": "2026-10-15"},
            ):
                _, lock_status = self._call_unwrapped(bt_routes.run_backtest, run_backtest_uc=uc1, backtest_repo=repo1)
                self.assertEqual(lock_status, 409)
        finally:
            bt_routes._backtest_status["running"] = orig_running
            bt_routes._backtest_results_cache.clear()
            bt_routes._backtest_results_cache.update(orig_cache)

    @staticmethod
    def _call_unwrapped(view_fn, **kwargs):
        fn = view_fn
        while hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__
        res = fn(**kwargs)
        if isinstance(res, tuple):
            return res[0], res[1]
        return res, 200

    def test_phase5_max_score_threshold_default_off_and_cap_80(self):
        start = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 11, 16, 10, 10, tzinfo=timezone.utc)
        warmup_df = _make_tick_df(_build_m1_trend_ticks(start - timedelta(minutes=400), 400, base=3800.0, step=0.10))
        replay_rows = [
            {"time": start + timedelta(seconds=1), "symbol": "XAUUSD", "bid": 3850.00, "ask": 3850.08, "price": 3850.00, "volume": 1},
            {"time": start + timedelta(seconds=20), "symbol": "XAUUSD", "bid": 3850.50, "ask": 3850.58, "price": 3850.50, "volume": 1},
        ]
        replay_df = _make_tick_df(replay_rows)
        fake_sig = Signal(
            strategy="bullish_momentum",
            direction=SignalDirection.BUY,
            symbol="XAUUSD",
            entry_price=3850.00,
            technical_strength=1.0,
        )

        def _run_with_score(score_val: float, max_cap: float | None):
            tick_repo = MagicMock()
            tick_repo.get_ticks_as_df.side_effect = [warmup_df.copy(), replay_df.copy()]
            uc = RunBacktestUseCase(
                tick_repo=tick_repo,
                virtual_broker=MagicMock(),
                manage_position=None,
                sizing_service=PositionSizingService(contract_size=100.0),
                profile=MEDIUM_RISK_BACKTEST,
            )
            with (
                patch("goldedge.application.use_cases.run_backtest.compute_htf_ema_trend", return_value=1),
                patch("goldedge.application.use_cases.run_backtest.SignalGeneratorService.generate_signals", return_value=[fake_sig]),
                patch.object(
                    uc.trade_scorer,
                    "filter_and_rank",
                    side_effect=lambda sigs, **kw: [MagicMock(strategy="bullish_momentum", direction=SignalDirection.BUY, final_score=score_val)],
                ),
            ):
                return uc.execute(
                    "XAUUSD",
                    start,
                    end,
                    initial_capital=10000.0,
                    use_kalman_exit=False,
                    export_artifacts=False,
                    max_score_threshold=max_cap,
                )

        # Score 0.85: allowed under H0 (None), blocked under max_score_threshold=0.80
        res_h0 = _run_with_score(0.85, None)
        res_cap_block = _run_with_score(0.85, 0.80)
        res_cap_allow = _run_with_score(0.75, 0.80)

        self.assertEqual(res_h0["trades"], 1)
        self.assertIsNone(res_h0["max_score_threshold"])
        self.assertEqual(res_cap_block["trades"], 0)
        self.assertEqual(res_cap_block["max_score_threshold"], 0.80)
        self.assertEqual(res_cap_allow["trades"], 1)
        self.assertEqual(res_cap_allow["max_score_threshold"], 0.80)

    def test_clean_room_isolation_and_frozen_integrity(self):
        audit_dir = Path("/app/scripts/audit") if Path("/app/scripts/audit").exists() else (ROOT / "scripts" / "audit")
        for fname in ("g1_clean_room_engine.py", "task_g1_phase2_3_diagnose.py", "task_g1_phase4_research.py"):
            fpath = audit_dir / fname
            if not fpath.exists():
                continue
            tree = ast.parse(fpath.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn("goldedge", alias.name, f"{fname} imported {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    self.assertNotIn("goldedge", mod, f"{fname} imported from {mod}")

        base_root = Path("/app") if Path("/app/backtest_results").exists() else ROOT
        frozen_111 = base_root / "backtest_results" / "frozen_111_baseline_trades.csv"
        g1_70 = base_root / "backtest_results" / "audit" / "task_g1_root_cause" / "g1_original_70_baseline_trades.csv"
        self.assertEqual(
            hashlib.sha256(frozen_111.read_bytes()).hexdigest().upper(),
            "73306E1529ADC48284BCE9AB6AC35122F33B9999D2464AF441867EB3BB6CF168",
        )
        self.assertEqual(
            hashlib.sha256(g1_70.read_bytes()).hexdigest().upper(),
            "27200EEA2C7FF4842FC586DD1131EE450247D0BCCBCF3E2F77ECA6952D2CCDC4",
        )

        g1_dir = base_root / "backtest_results" / "audit" / "task_g1_root_cause"
        p1_post = json.loads((g1_dir / "g1_phase1_reconciliation_postfix.json").read_text(encoding="utf-8"))
        self.assertEqual(p1_post["postfix_zero_cost"]["reconciliation_vs_clean_mode_a"]["mismatch_count"], 0)
        self.assertEqual(p1_post["postfix_fusion_demo_costs"]["reconciliation_vs_clean_mode_a_fusion"]["mismatch_count"], 0)
        p5_path = g1_dir / "g1_phase5_verification.json"
        if p5_path.exists():
            p5 = json.loads(p5_path.read_text(encoding="utf-8"))
            self.assertTrue(p5["all_scenarios_zero_mismatch"])


if __name__ == "__main__":
    unittest.main()
