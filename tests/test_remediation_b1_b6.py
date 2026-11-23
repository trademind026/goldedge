import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd

from goldedge.adapters.secondary.db.tick_repository import TickRepository
from goldedge.application.use_cases.run_backtest import RunBacktestUseCase, _OpenPosition
from goldedge.config.risk_profiles import MEDIUM_RISK_BACKTEST
from goldedge.domain.models.signal import Signal, SignalDirection
from goldedge.domain.services.sizing_service import PositionSizingService


def _make_tick_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


def _build_m1_trend_ticks(start_dt: datetime, minutes: int, base: float = 3840.0, step: float = 0.25):
    rows = []
    px = base
    for m in range(minutes):
        t = start_dt + timedelta(minutes=m)
        # 2 ticks per minute, steady climb
        b0 = round(px, 2)
        b1 = round(px + step, 2)
        rows.append({"time": t + timedelta(seconds=1), "symbol": "XAUUSD", "bid": b0, "ask": round(b0 + 0.08, 2), "price": b0, "volume": 1})
        rows.append({"time": t + timedelta(seconds=30), "symbol": "XAUUSD", "bid": b1, "ask": round(b1 + 0.08, 2), "price": b1, "volume": 1})
        px = b1
    return rows


class TestRemediationB1ToB6(unittest.TestCase):
    def _make_uc(self, df_by_call: list[pd.DataFrame]):
        tick_repo = MagicMock()
        tick_repo.get_ticks_as_df.side_effect = df_by_call
        uc = RunBacktestUseCase(
            tick_repo=tick_repo,
            virtual_broker=MagicMock(),
            manage_position=None,
            sizing_service=PositionSizingService(contract_size=100.0),
            profile=MEDIUM_RISK_BACKTEST,
        )
        return uc

    def test_b2_b3_entry_minute_exit_scan_and_gapped_sl_fill(self):
        start = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 11, 16, 10, 10, tzinfo=timezone.utc)

        # Warmup: 400 minutes of steady uptrend (~80 5m bars) before 10:00Z
        warmup_start = start - timedelta(minutes=400)
        warmup_df = _make_tick_df(_build_m1_trend_ticks(warmup_start, 400, base=3800.0, step=0.10))

        # Replay: 10:00Z has entry tick 0 (ask=3850.08) followed by intra-minute gap crash (bid=3844.20)
        replay_rows = []
        # 10:00..10:04 build 15m? Wait: Signal fires on closed_5m at 10:00Z!
        t0 = start
        replay_rows.append({"time": t0 + timedelta(seconds=1), "symbol": "XAUUSD", "bid": 3850.00, "ask": 3850.08, "price": 3850.00, "volume": 1})
        replay_rows.append({"time": t0 + timedelta(seconds=20), "symbol": "XAUUSD", "bid": 3844.20, "ask": 3844.28, "price": 3844.20, "volume": 1})
        replay_df = _make_tick_df(replay_rows)

        uc = self._make_uc([warmup_df, replay_df])
        fake_sig = Signal(
            strategy="bullish_momentum",
            direction=SignalDirection.BUY,
            symbol="XAUUSD",
            entry_price=3850.00,
            technical_strength=1.0,
        )
        with (
            patch("goldedge.application.use_cases.run_backtest.compute_htf_ema_trend", return_value=1),
            patch("goldedge.application.use_cases.run_backtest.SignalGeneratorService.generate_signals", return_value=[fake_sig]),
            patch.object(uc.trade_scorer, "filter_and_rank", side_effect=lambda sigs, **kw: [MagicMock(strategy="bullish_momentum", direction=SignalDirection.BUY, final_score=0.90)]),
        ):
            res = uc.execute("XAUUSD", start, end, initial_capital=10000.0, use_kalman_exit=False, export_artifacts=False)

        self.assertEqual(res["trades"], 1)
        t = res["trade_list"][0]
        # Entry executed at tick 0 ask=3850.08; exited inside SAME entry minute 10:00 at gapped bid=3844.20 (NOT theoretical SL!)
        self.assertAlmostEqual(t["entry_price"], 3850.08, places=2)
        self.assertAlmostEqual(t["exit_price"], 3844.20, places=2)
        self.assertLess(t["theoretical_stop_price"], 3850.08)
        self.assertGreater(t["theoretical_stop_price"], 3844.20)
        self.assertEqual(t["exit_reason"], "SL")
        self.assertLess(t["sl_gap_usd"], 0.0)
        self.assertLess(t["pnl"], 0.0)

    def test_b1_causal_kalman_does_not_leak_future_entry_minute_ticks(self):
        start = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 11, 16, 10, 5, tzinfo=timezone.utc)
        warmup_df = _make_tick_df(_build_m1_trend_ticks(start - timedelta(minutes=400), 400, base=3800.0, step=0.10))

        # In minute 10:00Z: tick 0 continues trend cleanly, while ticks 1..5 crash -$25
        replay_rows = [
            {"time": start + timedelta(seconds=1), "symbol": "XAUUSD", "bid": 3840.10, "ask": 3840.18, "price": 3840.10, "volume": 1},
            {"time": start + timedelta(seconds=15), "symbol": "XAUUSD", "bid": 3830.00, "ask": 3830.08, "price": 3830.00, "volume": 1},
            {"time": start + timedelta(seconds=30), "symbol": "XAUUSD", "bid": 3820.00, "ask": 3820.08, "price": 3820.00, "volume": 1},
            {"time": start + timedelta(seconds=45), "symbol": "XAUUSD", "bid": 3815.00, "ask": 3815.08, "price": 3815.00, "volume": 1},
        ]
        replay_df = _make_tick_df(replay_rows)

        captured_features = []
        uc = self._make_uc([warmup_df, replay_df])
        fake_sig = Signal(
            strategy="bullish_momentum",
            direction=SignalDirection.BUY,
            symbol="XAUUSD",
            entry_price=3840.0,
            technical_strength=1.0,
        )

        def _capture_filter(sigs, **kw):
            captured_features.append(dict(kw.get("features", {})))
            return []

        with (
            patch("goldedge.application.use_cases.run_backtest.compute_htf_ema_trend", return_value=1),
            patch("goldedge.application.use_cases.run_backtest.SignalGeneratorService.generate_signals", return_value=[fake_sig]),
            patch.object(uc.trade_scorer, "filter_and_rank", side_effect=_capture_filter),
        ):
            uc.execute("XAUUSD", start, end, initial_capital=10000.0, use_kalman_exit=False, export_artifacts=False)

        self.assertGreaterEqual(len(captured_features), 1)
        # Positive causal uptrend velocity (+~0.05), NEVER poisoned by future -$25 intra-minute crash
        self.assertGreater(captured_features[0]["kalman_vel"], 0.0)
        # B6: closed_5m bar before 10:00Z rollover had 09:55 timestamp -> utc_hour == 9 (not 10)
        self.assertEqual(captured_features[0]["utc_hour"], 9)

    def test_b4_inverted_quotes_skipped_and_repo_sql_uses_half_open_window(self):
        # 1. Verify TickRepository SQL uses half-open < end and preserves NULL real_volume (Task 7)
        mock_engine = MagicMock()
        repo = TickRepository(mock_engine)
        with patch("pandas.read_sql", return_value=pd.DataFrame()) as mock_sql:
            t1 = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)
            t2 = datetime(2026, 11, 17, 0, 0, tzinfo=timezone.utc)
            repo.get_ticks_as_df("XAUUSD", t1, t2)
            sql_str = mock_sql.call_args[0][0]
            self.assertIn("volume AS real_volume", sql_str)
            self.assertNotIn("COALESCE(volume, 1)", sql_str)
            self.assertIn("time < %(end)s", sql_str)
            self.assertNotIn("time <=", sql_str)

        # 2. Verify inverted quotes (bid >= ask) are skipped and never trigger phantom SL/TP
        pos = _OpenPosition(
            ticket=1, symbol="XAUUSD", direction="BUY", strategy="bullish_momentum",
            entry_price=3850.0, sl=3845.0, tp=3860.0, atr=3.0, volume=0.10,
            entry_time=datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc),
            entry_bar_idx=1, final_score=0.80, regime="TRENDING_BULL",
        )
        pos.bars_held = 1
        # Tick 0 is inverted (bid=3840 <= sl, ask=3839 < bid!), Tick 1 is valid and within bounds
        import numpy as np
        kf = MagicMock()
        kf.update_tick.return_value = (3850.0, 0.01)
        exited, reason, ex_px, _ = pos.check_exit_ticks(
            bids=np.array([3840.0, 3851.0]),
            asks=np.array([3839.0, 3851.1]),
            vols=np.array([1.0, 1.0]),
            bar_idx=2, max_hold_bars=60, kalman=kf, use_kalman_exit=False,
        )
        self.assertFalse(exited)

    def test_b5_b6_commission_slippage_swap_and_end_of_backtest_close(self):
        start = datetime(2026, 11, 11, 20, 50, tzinfo=timezone.utc)  # Wednesday
        end = datetime(2026, 11, 11, 21, 5, tzinfo=timezone.utc)
        warmup_df = _make_tick_df(_build_m1_trend_ticks(start - timedelta(minutes=400), 400, base=3800.0, step=0.25))

        # 20:50 enters BUY; holds calmly across 21:00Z Wednesday rollover -> 3x swap applied; closes at END_OF_BACKTEST
        replay_rows = []
        for m in range(15):
            t = start + timedelta(minutes=m)
            b = round(3920.0 + m * 0.02, 2)
            replay_rows.append({"time": t + timedelta(seconds=1), "symbol": "XAUUSD", "bid": b, "ask": round(b + 0.10, 2), "price": b, "volume": 1})
        replay_df = _make_tick_df(replay_rows)

        uc = self._make_uc([warmup_df, replay_df])
        fake_sig = Signal(
            strategy="bullish_momentum",
            direction=SignalDirection.BUY,
            symbol="XAUUSD",
            entry_price=3825.0,
            technical_strength=1.0,
        )
        with (
            patch("goldedge.application.use_cases.run_backtest.compute_htf_ema_trend", return_value=1),
            patch("goldedge.application.use_cases.run_backtest.SignalGeneratorService.generate_signals", return_value=[fake_sig]),
            patch.object(uc.trade_scorer, "filter_and_rank", side_effect=lambda sigs, **kw: [MagicMock(strategy="bullish_momentum", direction=SignalDirection.BUY, final_score=0.90)]),
        ):
            res = uc.execute(
                "XAUUSD", start, end, initial_capital=10000.0, use_kalman_exit=False,
                commission_per_lot=7.0, slippage_pips=0.5, swap_long_per_lot=5.0, rollover_utc_hour=21,
                export_artifacts=False,
            )

        self.assertEqual(res["trades"], 1)
        t = res["trade_list"][0]
        self.assertEqual(t["exit_reason"], "END_OF_BACKTEST")
        self.assertAlmostEqual(t["commission_usd"], 7.0 * t["qty"], places=4)
        self.assertAlmostEqual(t["swap_usd"], 5.0 * t["qty"] * 3.0, places=4)  # 3x Wednesday rollover fee
        self.assertGreater(t["slippage_usd"], 0.0)
        self.assertAlmostEqual(
            t["pnl"],
            t["gross_pnl"] - t["commission_usd"] - t["swap_usd"],
            places=6,
        )


if __name__ == "__main__":
    unittest.main()
