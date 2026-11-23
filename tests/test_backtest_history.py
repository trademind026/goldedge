import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from goldedge.adapters.primary.rest_api.routes.backtest import _extract_run_metrics
from goldedge.adapters.secondary.db.backtest_repository import (
    BacktestRepository,
    compute_trade_breakdowns,
)


class _FakeConnCtx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        return False


class TestBacktestHistoryAndBreakdowns(unittest.TestCase):
    def setUp(self):
        self.sample_trades = [
            {
                "entry_time": "2026-10-15 08:15:00",
                "exit_time": "2026-10-15 08:45:00",
                "symbol": "XAUUSD",
                "direction": "BUY",
                "strategy": "MOMENTUM_SCALP",
                "entry_price": 3850.25,
                "exit_price": 3862.25,
                "sl": 3844.25,
                "target": 3862.25,
                "qty": 0.16,
                "pnl": 192.0,
                "planned_risk_pct": 0.96,
                "realized_loss_pct": 0.0,
                "exit_reason": "TARGET_HIT",
                "score": 0.78,
                "regime": "TRENDING_BULL",
            },
            {
                "entry_time": "2026-10-15 11:05:00",
                "exit_time": "2026-10-15 11:20:00",
                "symbol": "XAUUSD",
                "direction": "SELL",
                "strategy": "VWAP_PULLBACK",
                "entry_price": 3860.00,
                "exit_price": 3864.50,
                "sl": 3864.50,
                "target": 3851.00,
                "qty": 0.22,
                "pnl": -99.0,
                "planned_risk_pct": 0.99,
                "realized_loss_pct": -0.97,
                "exit_reason": "SL_HIT",
                "score": 0.66,
                "regime": "SIDEWAYS",
            },
            {
                "entry_time": "2026-10-16 09:30:00",
                "exit_time": "2026-10-16 09:45:00",
                "symbol": "XAUUSD",
                "direction": "BUY",
                "strategy": "MOMENTUM_SCALP",
                "entry_price": 3870.00,
                "exit_price": 3875.00,
                "sl": 3864.00,
                "target": 3882.00,
                "qty": 0.16,
                "pnl": 80.0,
                "planned_risk_pct": 0.95,
                "realized_loss_pct": 0.0,
                "exit_reason": "EARLY_TRAIL",
                "score": 0.72,
                "regime": "TRENDING_BULL",
            },
        ]

    def test_compute_trade_breakdowns_aggregates_strategy_direction_daily_and_risk(self):
        m = compute_trade_breakdowns(
            self.sample_trades,
            initial_capital=10000.0,
            total_pnl=173.0,
            max_dd=-99.0,
        )
        self.assertEqual(m["days_traded"], 2)
        self.assertAlmostEqual(m["final_equity"], 10173.0, places=2)
        self.assertAlmostEqual(m["max_planned_risk_pct"], 0.99, places=3)
        self.assertAlmostEqual(m["max_realized_loss_pct"], 0.97, places=3)

        self.assertEqual(m["direction_breakdown"]["BUY"]["trades"], 2)
        self.assertEqual(m["direction_breakdown"]["BUY"]["wins"], 2)
        self.assertAlmostEqual(m["direction_breakdown"]["BUY"]["pnl"], 272.0, places=2)
        self.assertEqual(m["direction_breakdown"]["SELL"]["trades"], 1)
        self.assertAlmostEqual(m["direction_breakdown"]["SELL"]["pnl"], -99.0, places=2)

        self.assertEqual(m["strategy_breakdown"]["MOMENTUM_SCALP"]["trades"], 2)
        self.assertEqual(m["daily_breakdown"]["2026-10-15"]["trades"], 2)
        self.assertAlmostEqual(m["daily_breakdown"]["2026-10-15"]["pnl"], 93.0, places=2)
        self.assertEqual(m["exit_reason_breakdown"]["TARGET_HIT"]["count"], 1)

    def test_extract_run_metrics_preserves_dual_mtm_and_strategy_mode(self):
        stats = {
            "trades": 3,
            "win_rate": 2 / 3,
            "pnl": 173.0,
            "initial_capital": 10000.0,
            "final_equity": 10173.0,
            "max_margin": 850.0,
            "required_capital": 949.0,
            "roi": 18.23,
            "max_dd": -99.0,
            "closed_max_dd": -99.0,
            "closed_max_dd_pct": 0.97,
            "mtm_max_dd": -125.5,
            "mtm_max_dd_pct": 1.23,
            "mtm_max_dd_initial_pct": 1.255,
            "mtm_peak_equity": 10192.0,
            "mtm_trough_equity": 10066.5,
            "max_planned_risk_pct": 0.99,
            "max_realized_loss_pct": 0.97,
            "sl_slippage_events": 0,
            "worst_sl_gap_usd": 0.0,
            "trade_list": self.sample_trades,
        }
        metrics = _extract_run_metrics(stats, strategy_mode="candidate_15_e1", commission_per_lot=2.5)
        self.assertEqual(metrics["strategy_mode"], "candidate_15_e1")
        self.assertEqual(metrics["commission_per_lot"], 2.5)
        self.assertAlmostEqual(metrics["closed_max_dd"], -99.0, places=2)
        self.assertAlmostEqual(metrics["mtm_max_dd"], -125.5, places=2)
        self.assertEqual(metrics["mtm_max_dd_pct"], 1.23)
        self.assertIn("BUY", metrics["direction_breakdown"])

    def test_repository_save_and_legacy_get_run_fallback(self):
        engine = MagicMock()
        conn = MagicMock()
        engine.begin.return_value = _FakeConnCtx(conn)
        engine.connect.return_value = _FakeConnCtx(conn)

        repo = BacktestRepository(engine)
        repo.save_run({
            "run_id": "bt_test_001",
            "symbol": "XAUUSD",
            "risk": "medium",
            "exit_agent": "Standard",
            "start_date": "2026-10-15",
            "end_date": "2026-10-16",
            "trades": 3,
            "win_rate": 0.667,
            "pnl": 173.0,
            "max_dd": -99.0,
            "pf": 2.75,
            "avg_win": 136.0,
            "avg_loss": -99.0,
            "rr": 1.37,
            "strategy_mode": "candidate_15_e1",
            "equity_curve": [{"time": "2026-10-15 08:45:00", "equity": 10192.0}],
            "trade_list": self.sample_trades,
        })
        self.assertTrue(conn.execute.called)
        params = conn.execute.call_args[0][1]
        saved_metrics = json.loads(params["metrics"])
        self.assertEqual(params["strategy_mode"], "candidate_15_e1")
        self.assertIn("strategy_breakdown", saved_metrics)
        self.assertIn("daily_breakdown", saved_metrics)

        # Simulate reading an older row whose metrics column was empty {}
        fake_row = SimpleNamespace(
            run_id="bt_legacy_001",
            symbol="XAUUSD",
            risk="medium",
            exit_agent="Kalman",
            start_date="2026-10-15",
            end_date="2026-10-16",
            trades=3,
            win_rate=0.667,
            pnl=173.0,
            max_dd=-99.0,
            pf=2.75,
            avg_win=136.0,
            avg_loss=-99.0,
            rr=1.37,
            status="done",
            strategy_mode="baseline",
            error=None,
            metrics={},
            equity_curve=[{"time": "2026-10-15 08:45:00", "equity": 10192.0}],
            trade_list=self.sample_trades,
            created_at=datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc),
        )
        conn.execute.return_value.fetchone.return_value = fake_row
        detail = repo.get_run("bt_legacy_001")
        self.assertIsNotNone(detail)
        self.assertIn("BUY", detail["metrics"]["direction_breakdown"])
        self.assertIn("2026-10-15", detail["metrics"]["daily_breakdown"])
        self.assertEqual(detail["metrics"]["mtm_max_dd"], -99.0)

    def test_cent_precision_classification_treats_float_epsilon_as_breakeven(self):
        # IEEE-754 +8.04e-12 residual (Trade #70 in Task 4 Phase 4) must be breakeven, not WIN or LOSS.
        trades = [
            {"direction": "SELL", "strategy": "s1", "exit_reason": "BREAKEVEN", "entry_time": "2026-10-20 12:30:00", "pnl": 8.039902077427996e-12, "qty": 0.13, "entry_price": 4117.61},
            {"direction": "BUY", "strategy": "s1", "exit_reason": "TARGET", "entry_time": "2026-10-20 13:00:00", "pnl": 50.0, "qty": 0.10, "entry_price": 4118.0},
            {"direction": "SELL", "strategy": "s1", "exit_reason": "SL", "entry_time": "2026-10-20 14:00:00", "pnl": -25.0, "qty": 0.10, "entry_price": 4119.0},
        ]
        m = compute_trade_breakdowns(trades, initial_capital=10000.0, total_pnl=25.0, max_dd=-25.0)
        bkt = m["strategy_breakdown"]["s1"]
        self.assertEqual(bkt["trades"], 3)
        self.assertEqual(bkt["wins"], 1)
        self.assertEqual(bkt["losses"], 1)
        self.assertAlmostEqual(bkt["win_rate"], 33.33, places=2)


if __name__ == "__main__":
    unittest.main()
