import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

from goldedge.adapters.secondary.persistence.jsonl_shadow_journal import JsonlShadowJournal
from goldedge.application.use_cases.run_backtest import RunBacktestUseCase, _MtmDrawdownTracker
from goldedge.application.use_cases.scan_market import ScanMarketUseCase
from goldedge.config.risk_profiles import MEDIUM_RISK_BACKTEST
from goldedge.domain.models.bar import Bar
from goldedge.domain.models.signal import SignalDirection
from goldedge.domain.services.denoise_service import VolumeAdaptiveKalmanFilter
from goldedge.domain.services.mtf_trend import compute_htf_ema_state, compute_htf_ema_trend
from goldedge.domain.services.shadow_tracker import ShadowValidationTracker
from goldedge.domain.services.sizing_service import PositionSizingService


def _build_synthetic_m1_bars(count: int = 4000, end_utc: datetime | None = None, trend: str = "up") -> list[Bar]:
    end_utc = end_utc or datetime(2026, 11, 16, 14, 0, tzinfo=timezone.utc)
    start_utc = end_utc - timedelta(minutes=count)
    bars: list[Bar] = []
    price = 3800.0 if trend == "up" else 3900.0
    step = 0.04 if trend == "up" else -0.04
    for i in range(count):
        t = start_utc + timedelta(minutes=i)
        osc = 0.35 if (i % 7) < 4 else -0.25
        o = round(price, 2)
        c = round(price + step + osc * 0.1, 2)
        h = round(max(o, c) + 0.45, 2)
        l = round(min(o, c) - 0.40, 2)
        bars.append(
            Bar(
                symbol="XAUUSD",
                time=t,
                open=o,
                high=h,
                low=l,
                close=c,
                tick_volume=150 + (i % 50),
                real_volume=0,
                spread=5,
            )
        )
        price = c
    return bars


class TestShadowValidation(unittest.TestCase):
    def test_kalman_scalar_matches_reference_numpy(self):
        ref_F = np.array([[1.0, 1.0], [0.0, 1.0]])
        ref_H = np.array([[1.0, 0.0]])
        ref_x = np.zeros((2, 1))
        ref_P = np.eye(2)
        ref_last = None
        ref_prev_vel = 0.0

        kf = VolumeAdaptiveKalmanFilter(adapt_to_volume=True)
        rng = np.random.default_rng(42)
        prices = 3850.0 + np.cumsum(rng.normal(0.0, 0.08, size=5000))
        volumes = rng.integers(0, 10, size=5000).astype(float)
        volumes[17] = 0.0

        max_dp = 0.0
        max_dv = 0.0
        max_da = 0.0
        for i, (p, v) in enumerate(zip(prices, volumes)):
            p_f = float(p)
            v_f = float(v)
            if i == 0:
                ref_x[0, 0] = p_f
                ref_x[1, 0] = 0.0
                ref_last = p_f
                rp, rv, ra = p_f, 0.0, 0.0
            else:
                pdiff = abs(p_f - ref_last) if ref_last else 0.0
                ref_last = p_f
                R_i = np.clip(0.01 * (max(pdiff, 0.0001) / max(v_f, 1.0)), 0.01 / 3.0, 0.01 * 3.0)
                Q = np.array([[1e-4, 0.0], [0.0, 3e-5]])
                x_m = ref_F @ ref_x
                P_m = ref_F @ ref_P @ ref_F.T + Q
                S = ref_H @ P_m @ ref_H.T + np.array([[R_i]])
                K = P_m @ ref_H.T @ np.linalg.inv(S)
                y = np.array([[p_f]]) - (ref_H @ x_m)
                ref_prev_vel = float(ref_x[1, 0])
                ref_x = x_m + (K @ y)
                ref_P = (np.eye(2) - K @ ref_H) @ P_m
                rp, rv = float(ref_x[0, 0]), float(ref_x[1, 0])
                ra = rv - ref_prev_vel

            op, ov = kf.update_tick(p_f, v_f)
            oa = kf.get_acceleration()
            max_dp = max(max_dp, abs(rp - op))
            max_dv = max(max_dv, abs(rv - ov))
            max_da = max(max_da, abs(ra - oa))

        self.assertLess(max_dp, 1e-12)
        self.assertLess(max_dv, 1e-12)
        self.assertLess(max_da, 1e-12)
        self.assertLess(float(np.max(np.abs(ref_P - kf.P))), 1e-12)
        self.assertLess(float(np.max(np.abs(ref_x - kf.x))), 1e-12)

    def test_scan_market_shadow_excludes_incomplete_bars_and_blocks_orders(self):
        cutoff = datetime(2026, 11, 16, 14, 5, 12, tzinfo=timezone.utc)
        closed_bars = _build_synthetic_m1_bars(count=4000, end_utc=datetime(2026, 11, 16, 14, 5, tzinfo=timezone.utc))
        incomplete_bar = Bar(
            symbol="XAUUSD",
            time=datetime(2026, 11, 16, 14, 5, tzinfo=timezone.utc),
            open=closed_bars[-1].close,
            high=9999.0,
            low=1000.0,
            close=9999.0,
            tick_volume=99999,
            real_volume=0,
            spread=99,
        )

        with tempfile.TemporaryDirectory() as td:
            candle_repo = MagicMock()
            candle_repo.get_latest_bars.return_value = closed_bars + [incomplete_bar]
            execute_uc = MagicMock()
            journal = JsonlShadowJournal(str(Path(td) / "shadow.jsonl"))
            mock_ridge = MagicMock()
            mock_ridge.predict_expected_pnl.return_value = 42.5

            uc = ScanMarketUseCase(
                candle_repo=candle_repo,
                event_bus=MagicMock(),
                execute_trade_uc=execute_uc,
                expected_pnl_model=mock_ridge,
                min_expected_pnl=None,
                shadow_mode=True,
                shadow_journal=journal,
                shadow_tracker=ShadowValidationTracker(initial_equity=10000.0),
            )
            res_with_incomplete = uc.execute(
                "XAUUSD",
                kalman_features={"kalman_price": 3860.1, "kalman_vel": 0.021, "kalman_accel": 0.002},
                live_quote={"bid": 3860.00, "ask": 3860.08, "time": cutoff},
                as_of_time=cutoff,
            )

            candle_repo.get_latest_bars.return_value = closed_bars
            uc2 = ScanMarketUseCase(
                candle_repo=candle_repo,
                event_bus=MagicMock(),
                execute_trade_uc=execute_uc,
                expected_pnl_model=mock_ridge,
                min_expected_pnl=None,
                shadow_mode=True,
                shadow_journal=journal,
                shadow_tracker=ShadowValidationTracker(initial_equity=10000.0),
            )
            res_clean = uc2.execute(
                "XAUUSD",
                kalman_features={"kalman_price": 3860.1, "kalman_vel": 0.021, "kalman_accel": 0.002},
                live_quote={"bid": 3860.00, "ask": 3860.08, "time": cutoff},
                as_of_time=cutoff,
            )

            execute_uc.execute.assert_not_called()
            self.assertFalse(res_with_incomplete["order_placed"])
            self.assertTrue(res_with_incomplete["shadow_mode"])
            self.assertEqual(res_with_incomplete["closed_5m_ts"], "2026-11-16T14:00:00+00:00")
            self.assertEqual(res_with_incomplete["closed_1h_ts"], "2026-11-16T13:00:00+00:00")
            self.assertEqual(res_with_incomplete["rsi"], res_clean["rsi"])
            self.assertEqual(res_with_incomplete["vwap"], res_clean["vwap"])
            self.assertEqual(res_with_incomplete["h1_ema20"], res_clean["h1_ema20"])
            self.assertEqual(res_with_incomplete["h1_ema50"], res_clean["h1_ema50"])
            self.assertEqual(len(journal.read_recent(10)), 1)
            self.assertTrue(res_clean["duplicate_skipped"])

    def test_shadow_tracker_separates_buy_and_sell_stats(self):
        tracker = ShadowValidationTracker(initial_equity=10000.0, contract_size=100.0)
        t0 = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)

        # Open SELL first and verify concurrent BUY still opens independently
        tracker.record_candidate("XAUUSD", "SELL", approved=True)
        tracker.open_virtual_position(
            "XAUUSD", "SELL", 3860.0, 3865.0, 3850.0, 0.10, 3.0, 0.65, "TRENDING_BEAR", t0,
            ridge_pred_pnl=-5.0, kalman_velocity=-0.02, kalman_acceleration=-0.001,
            distance_to_swing_low_atr=0.85, distance_to_vwap_atr=-3.2,
        )

        tracker.record_candidate("XAUUSD", "BUY", approved=True)
        buy_open = tracker.open_virtual_position(
            "XAUUSD", "BUY", 3850.0, 3845.0, 3860.0, 0.10, 3.0, 0.72, "TRENDING_BULL",
            t0 + timedelta(minutes=5), ridge_pred_pnl=18.0,
            kalman_velocity=0.03, kalman_acceleration=0.002,
            distance_to_swing_low_atr=2.1, distance_to_vwap_atr=1.4,
        )
        self.assertIsNotNone(buy_open)

        # Step checkpoints at 5m, 10m, 15m before triggering exits at 16m
        tracker.on_tick_all("XAUUSD", bid=3851.5, ask=3860.3, ts=t0 + timedelta(minutes=6))
        tracker.on_tick_all("XAUUSD", bid=3852.0, ask=3859.9, ts=t0 + timedelta(minutes=11))
        events_1 = tracker.on_tick_all("XAUUSD", bid=3860.5, ask=3865.4, ts=t0 + timedelta(minutes=16))
        self.assertEqual(len(events_1), 2)
        buy_ev = next(e for e in events_1 if e["direction"] == "BUY")
        sell_ev = next(e for e in events_1 if e["direction"] == "SELL")
        self.assertEqual(buy_ev["exit_reason"], "TARGET")
        self.assertEqual(sell_ev["exit_reason"], "SL")
        self.assertTrue(sell_ev["e1_15m_would_exit"])

        for req_key in (
            "entry_timestamp", "direction", "regime", "kalman_velocity", "kalman_acceleration",
            "distance_to_swing_low_atr", "distance_to_vwap_atr",
            "mfe_5m_atr", "mfe_10m_atr", "mfe_15m_atr",
            "mae_5m_atr", "mae_10m_atr", "mae_15m_atr",
            "e1_15m_would_exit", "in_15utc_window", "exit_reason", "net_pnl",
        ):
            self.assertIn(req_key, sell_ev)
            self.assertIsNotNone(sell_ev[req_key])

        s = tracker.summary("XAUUSD")
        self.assertEqual(s["BUY"]["wins"], 1)
        self.assertEqual(s["BUY"]["losses"], 0)
        self.assertGreater(s["BUY"]["pnl"], 0)
        self.assertEqual(s["BUY"]["win_rate"], 100.0)
        self.assertEqual(s["SELL"]["wins"], 0)
        self.assertEqual(s["SELL"]["losses"], 1)
        self.assertLess(s["SELL"]["pnl"], 0)
        self.assertEqual(s["SELL"]["e1_15m_triggered"], 1)

    def test_shadow_journal_dedup_and_restart_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            j_path = str(Path(td) / "shadow.jsonl")
            journal1 = JsonlShadowJournal(j_path)
            t0 = datetime(2026, 11, 16, 11, 0, tzinfo=timezone.utc)
            rollover_ev = {
                "event": "SHADOW_ROLLOVER",
                "status": "APPROVED_SHADOW",
                "symbol": "XAUUSD",
                "rollover_utc": t0.isoformat(),
                "direction": "BUY",
                "order_placed": False,
            }
            tracker1 = ShadowValidationTracker(initial_equity=10000.0, contract_size=100.0)
            open_ev = tracker1.open_virtual_position(
                "XAUUSD", "BUY", 3850.0, 3845.0, 3860.0, 0.10, 3.0, 0.72, "TRENDING_BULL", t0,
                kalman_velocity=0.025, kalman_acceleration=0.001,
                distance_to_swing_low_atr=1.9, distance_to_vwap_atr=0.8,
            )
            self.assertTrue(journal1.record_event(rollover_ev))
            self.assertFalse(journal1.record_event(rollover_ev))  # Duplicate rollover blocked
            self.assertTrue(journal1.record_event(open_ev))
            self.assertFalse(journal1.record_event(open_ev))      # Duplicate open blocked

            # Simulate fresh backend restart: new journal + new tracker restored from persisted file
            journal2 = JsonlShadowJournal(j_path)
            self.assertTrue(journal2.has_rollover("XAUUSD", t0.isoformat()))
            tracker2 = ShadowValidationTracker(initial_equity=10000.0, contract_size=100.0)
            restored_open = tracker2.restore_from_events(journal2.read_all())
            self.assertEqual(restored_open, 1)
            self.assertIn(("XAUUSD", "BUY"), tracker2.open_positions)
            self.assertEqual(tracker2.summary("XAUUSD")["BUY"]["signals_approved"], 1)

            # Reconstructed position hits TP cleanly after restart
            tracker2.on_tick_all("XAUUSD", bid=3851.0, ask=3851.1, ts=t0 + timedelta(minutes=6))
            tracker2.on_tick_all("XAUUSD", bid=3852.0, ask=3852.1, ts=t0 + timedelta(minutes=11))
            exits = tracker2.on_tick_all("XAUUSD", bid=3860.5, ask=3860.6, ts=t0 + timedelta(minutes=16))
            self.assertEqual(len(exits), 1)
            self.assertEqual(exits[0]["exit_reason"], "TARGET")
            self.assertTrue(journal2.record_event(exits[0]))
            self.assertEqual(len(journal2.read_all()), 3)  # Exact: 1 rollover + 1 open + 1 exit

            # Simulate second restart after exit
            journal3 = JsonlShadowJournal(j_path)
            tracker3 = ShadowValidationTracker(initial_equity=10000.0, contract_size=100.0)
            self.assertEqual(tracker3.restore_from_events(journal3.read_all()), 0)
            self.assertEqual(tracker3.summary("XAUUSD")["BUY"]["wins"], 1)
            self.assertGreater(tracker3.summary("XAUUSD")["BUY"]["pnl"], 0)

    def test_htf_ema_state_consistent_with_trend(self):
        closes = [3800.0 + i * 0.5 for i in range(60)]
        trend, ema20, ema50 = compute_htf_ema_state(closes)
        self.assertEqual(trend, 1)
        self.assertEqual(trend, compute_htf_ema_trend(closes))
        self.assertIsNotNone(ema20)
        self.assertIsNotNone(ema50)
        self.assertGreater(ema20, ema50)

    def test_execution_price_sizing_never_exceeds_budget_and_rejects_min_lot_overrun(self):
        svc = PositionSizingService(contract_size=100.0, tick_size=0.01, tick_value=1.0, min_lot=0.01, commission_per_lot=7.0)
        bid, ask = 3850.00, 3850.45
        buy_entry = svc.resolve_executable_entry("BUY", bid, ask)
        sell_entry = svc.resolve_executable_entry("SELL", bid, ask)
        self.assertEqual(buy_entry, ask)
        self.assertEqual(sell_entry, bid)

        lots, sl, tp, reason = svc.calculate_size(
            account_balance=10000.0,
            risk_profile=MEDIUM_RISK_BACKTEST,
            entry_price=buy_entry,
            atr_value=3.0,
            direction=SignalDirection.BUY,
            symbol="XAUUSD",
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(lots)
        self.assertAlmostEqual(sl, buy_entry - 4.5, places=6)
        self.assertAlmostEqual(tp, buy_entry + 9.0, places=6)
        planned_risk = abs(buy_entry - sl) * lots * 100.0 + 7.0 * lots
        self.assertLessEqual(planned_risk, 100.0 + 1e-6)  # <= 1% of $10,000

        # Reject when min lot 0.01 risk exceeds budget ($5 budget vs $7.57 min-lot risk)
        lots_rej, _, _, rej_reason = svc.calculate_size(
            account_balance=500.0,
            risk_profile=MEDIUM_RISK_BACKTEST,
            entry_price=sell_entry,
            atr_value=5.0,
            direction=SignalDirection.SELL,
            symbol="XAUUSD",
        )
        self.assertIsNone(lots_rej)
        self.assertIn("exceeds risk budget", rej_reason)

    def test_mtm_tracker_and_backtest_report_both_closed_and_tick_mtm_dd(self):
        t0 = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)
        mtm = _MtmDrawdownTracker(initial_capital=10000.0, start_time=t0)
        # Open-profit spike -> HWM moves to 10200 -> intratrade dip to 9900 (-300 HWM MTM DD) -> closes at +50
        mtm.update(10200.0, 10200.0, t0 + timedelta(minutes=2))
        mtm.update(9900.0, 9900.0, t0 + timedelta(minutes=6))
        mtm.update(10050.0, 10050.0, t0 + timedelta(minutes=10))

        uc = RunBacktestUseCase(
            tick_repo=MagicMock(),
            virtual_broker=MagicMock(),
            manage_position=None,
            sizing_service=PositionSizingService(contract_size=100.0),
            profile=MEDIUM_RISK_BACKTEST,
        )
        res = uc._build_result(
            closed_trades=[{
                "entry_time": t0,
                "exit_time": t0 + timedelta(minutes=10),
                "symbol": "XAUUSD",
                "direction": "BUY",
                "strategy": "bullish_momentum",
                "entry_price": 3850.0,
                "exit_price": 3850.5,
                "sl": 3845.5,
                "tp": 3859.0,
                "qty": 1.0,
                "pnl": 50.0,
                "planned_risk_usd": 90.0,
                "planned_risk_pct": 0.90,
                "realized_loss_pct": 0.0,
                "sl_gap_usd": 0.0,
                "exit_reason": "TARGET",
                "score": 0.72,
                "regime": "TRENDING_BULL",
                "journey": [],
            }],
            initial_capital=10000.0,
            final_equity=10050.0,
            mtm_tracker=mtm,
        )
        self.assertEqual(res["max_dd"], 0.0)
        self.assertEqual(res["closed_max_dd"], 0.0)
        self.assertAlmostEqual(res["mtm_max_dd"], -300.0, places=2)
        self.assertAlmostEqual(res["mtm_max_dd_pct"], 2.9412, places=2)
        self.assertAlmostEqual(res["max_planned_risk_pct"], 0.90, places=2)

    def test_shadow_tracker_three_independent_portfolios_and_hwm_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            j_path = str(Path(td) / "shadow.jsonl")
            journal = JsonlShadowJournal(j_path)
            tracker = ShadowValidationTracker(initial_equity=10000.0, contract_size=100.0)
            t0 = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)

            # 10:00 BUY -> enters CANDIDATE and BUY_ONLY
            ev_buy = tracker.open_virtual_position(
                "XAUUSD", "BUY", 3850.0, 3845.5, 3859.0, 0.20, 3.0, 0.75, "TRENDING_BULL", t0,
            )
            self.assertTrue(journal.record_event(ev_buy))
            # 10:05 SELL -> skipped by CANDIDATE (1 pos limit), enters SELL_ONLY
            ev_sell = tracker.open_virtual_position(
                "XAUUSD", "SELL", 3850.2, 3854.7, 3841.2, 0.20, 3.0, 0.70, "TRENDING_BEAR", t0 + timedelta(minutes=5),
            )
            self.assertTrue(journal.record_event(ev_sell))

            # Price drops slightly while BUY stays stagnant (< 0.4 ATR = 1.2) -> triggers E1 on BUY_ONLY & CANDIDATE at 15m
            tracker.on_tick_all("XAUUSD", bid=3849.5, ask=3849.7, ts=t0 + timedelta(minutes=6))
            tracker.on_tick_all("XAUUSD", bid=3849.4, ask=3849.6, ts=t0 + timedelta(minutes=15, seconds=5))
            # Now hit SELL TP cleanly at 10:25
            exits = tracker.on_tick_all("XAUUSD", bid=3841.0, ask=3841.1, ts=t0 + timedelta(minutes=25))
            for ex in exits:
                journal.record_event(ex)
            p_exits = tracker.drain_portfolio_exits()
            for pe in p_exits:
                self.assertTrue(journal.record_event(pe))
                self.assertFalse(journal.record_event(pe))  # Idempotent dedup

            # 16:00 signal (outside 15:00 UTC window) must be ignored by all 3 Candidate portfolios
            tracker.open_virtual_position(
                "XAUUSD", "BUY", 3842.0, 3837.5, 3851.0, 0.20, 3.0, 0.75, "TRENDING_BULL",
                datetime(2026, 11, 16, 16, 0, tzinfo=timezone.utc),
            )
            self.assertNotIn(("XAUUSD", "CANDIDATE"), tracker.open_portfolio_positions)
            self.assertNotIn(("XAUUSD", "BUY_ONLY"), tracker.open_portfolio_positions)

            ps = tracker.portfolios_summary("XAUUSD")
            self.assertEqual(ps["CANDIDATE"]["e1_exits"], 1)
            self.assertEqual(ps["CANDIDATE"]["trades_closed"], 1)
            self.assertEqual(ps["BUY_ONLY"]["e1_exits"], 1)
            self.assertEqual(ps["SELL_ONLY"]["tp_hits"], 1)
            self.assertGreater(ps["SELL_ONLY"]["pnl"], 0.0)
            self.assertLess(ps["BUY_ONLY"]["pnl"], 0.0)

            # Restart recovery preserves all 3 independent portfolio curves identically
            tracker2 = ShadowValidationTracker(initial_equity=10000.0, contract_size=100.0)
            tracker2.restore_from_events(journal.read_all())
            ps2 = tracker2.portfolios_summary("XAUUSD")
            for p_name in ("CANDIDATE", "BUY_ONLY", "SELL_ONLY"):
                self.assertAlmostEqual(ps2[p_name]["equity"], ps[p_name]["equity"], places=4)
                self.assertEqual(ps2[p_name]["trades_closed"], ps[p_name]["trades_closed"])


if __name__ == "__main__":
    unittest.main()
