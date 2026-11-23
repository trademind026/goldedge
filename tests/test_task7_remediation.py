"""
Task 7 — Financial Cost, Rollover/Swap & Volume Semantics Unit & Parity Test Suite.
"""

from __future__ import annotations

import ast
import csv
from datetime import date, datetime, timezone
import math
from pathlib import Path
import sys
import unittest

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if not _SRC.exists() and Path("/app/src").exists():
    _ROOT = Path("/app")
    _SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from goldedge.domain.models.bar import Bar
from goldedge.domain.models.regime import MarketRegime
from goldedge.domain.models.signal import Signal, SignalDirection
from goldedge.domain.services.broker_cost_service import (
    BrokerCostService,
    FUSION_CTRADER_DEMO_48439552,
    FUSION_XAUUSD_SWAP_SPEC,
    ZERO_COMMISSION_SCHEDULE,
    resolve_commission_schedule,
)
from goldedge.domain.services.denoise_service import VolumeAdaptiveKalmanFilter
from goldedge.domain.services.indicator_service import IndicatorService
from goldedge.domain.services.mtf_trend import RollingBarAggregator, aggregate_ohlcv_bars
from goldedge.domain.services.signal_generator import SignalGeneratorService
from goldedge.domain.services.trade_scorer import TradeScorer
from goldedge.application.use_cases.run_backtest import _OpenPosition


def _make_1m_bars(n: int = 80, real_volume=None) -> list[Bar]:
    base_ts = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    bars: list[Bar] = []
    px = 4300.0
    for i in range(n):
        ts = datetime.fromtimestamp(base_ts.timestamp() + i * 60, tz=timezone.utc)
        delta = 0.45 * math.sin(i / 4.0) + 0.12
        op = px
        cl = px + delta
        hi = max(op, cl) + 0.8
        lo = min(op, cl) - 0.6
        tc = 120 + (i % 7) * 25
        bars.append(
            Bar(
                symbol="XAUUSD",
                time=ts,
                open=round(op, 2),
                high=round(hi, 2),
                low=round(lo, 2),
                close=round(cl, 2),
                tick_volume=tc,
                real_volume=real_volume,
            )
        )
        px = cl
    return bars


class TestTask7CommissionCorrection(unittest.TestCase):
    def test_reconcile_all_15_historical_demo_deals(self):
        csv_path = _ROOT / "backtest_results" / "audit" / "task5_commission_reconciliation.csv"
        self.assertTrue(csv_path.exists(), f"Missing {csv_path}")

        svc = BrokerCostService(
            commission_schedule=FUSION_CTRADER_DEMO_48439552,
            swap_schedule=FUSION_XAUUSD_SWAP_SPEC,
        )
        with csv_path.open("r", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 15)

        pos_totals: dict[str, dict[str, float]] = {}
        for row in rows:
            lots = float(row["lots"])
            exec_price = float(row["exec_price"])
            actual_side = abs(float(row["actual_side_comm_usd"]))
            calc_side = svc.calculate_leg_commission(lots, exec_price)
            self.assertAlmostEqual(
                calc_side,
                actual_side,
                places=2,
                msg=f"Deal {row['deal_id']} commission mismatch",
            )
            pid = row["position_id"]
            bucket = pos_totals.setdefault(pid, {"calc": 0.0, "actual_rt": 0.0})
            bucket["calc"] = round(bucket["calc"] + calc_side, 2)
            if row["actual_rt_comm_usd"]:
                bucket["actual_rt"] = abs(float(row["actual_rt_comm_usd"]))

        self.assertEqual(len(pos_totals), 7)
        for pid, sums in pos_totals.items():
            self.assertAlmostEqual(
                sums["calc"],
                sums["actual_rt"],
                places=2,
                msg=f"Position {pid} round-trip commission mismatch",
            )

    def test_partial_close_multi_exec_and_duplicate_commission_guard(self):
        svc = BrokerCostService(
            commission_schedule=FUSION_CTRADER_DEMO_48439552,
            swap_schedule=FUSION_XAUUSD_SWAP_SPEC,
        )
        entry_time = datetime(2026, 9, 2, 11, 0, tzinfo=timezone.utc)
        pos = _OpenPosition(
            ticket=1,
            symbol="XAUUSD",
            direction="BUY",
            strategy="kinematic_kalman_trend",
            entry_price=4307.59,
            sl=4297.59,
            tp=4327.59,
            atr=6.0,
            volume=2.0,
            entry_time=entry_time,
            entry_bar_idx=1,
            final_score=0.75,
            regime="TRENDING_BULL",
        )
        c_ent_1 = pos.record_entry_commission(svc)
        c_ent_2 = pos.record_entry_commission(svc)
        self.assertAlmostEqual(c_ent_1, 10.34, places=2)
        self.assertEqual(c_ent_2, 0.0)
        self.assertAlmostEqual(pos.commission_entry_usd, 10.34, places=2)

        # Partial close 0.70 lots at 4320.00
        part_pnl = pos.close_partial(0.70, 4320.00, cost_service=svc)
        expected_part_comm = svc.calculate_leg_commission(0.70, 4320.00)  # $3.63
        part_entry_comm = round(10.34 * (0.70 / 2.0), 6)
        self.assertAlmostEqual(pos.volume, 1.30, places=6)
        self.assertAlmostEqual(
            part_pnl,
            (4320.00 - 4307.59) * 0.70 * 100.0 - part_entry_comm - expected_part_comm,
            places=2,
        )

        # Second partial close 0.80 lots at 4325.00
        _ = pos.close_partial(0.80, 4325.00, cost_service=svc)
        part_comm_2 = svc.calculate_leg_commission(0.80, 4325.00)
        self.assertAlmostEqual(pos.volume, 0.50, places=6)

        # Final exit of remaining 0.50 lots at 4330.00 via compute_pnl (MTM estimate)
        final_net = pos.compute_pnl(4330.00, cost_service=svc)
        expected_rem_comm = svc.calculate_leg_commission(0.50, 4330.00)
        final_net_repeat = pos.compute_pnl(4330.00, cost_service=svc)
        self.assertAlmostEqual(final_net, final_net_repeat, places=6)
        # Explicitly settle remaining leg
        rem_fee = pos.record_exit_leg_commission(pos.volume, 4330.00, svc)
        self.assertAlmostEqual(rem_fee, expected_rem_comm, places=2)
        self.assertAlmostEqual(
            pos.commission_exit_usd,
            round(expected_part_comm + part_comm_2 + expected_rem_comm, 2),
            places=2,
        )
        # Idempotent guard blocks charging past total initial lots
        self.assertEqual(pos.record_exit_leg_commission(1.0, 4330.00, svc), 0.0)

    def test_live_safety_guard_blocks_unverified_demo_commission_profile(self):
        demo_sched = resolve_commission_schedule(
            "fusion_ctrader_demo",
            trading_environment="demo",
            account_id=48439552,
        )
        self.assertEqual(demo_sched.rate_value, 12.0)
        self.assertFalse(demo_sched.verified_for_live)

        with self.assertRaises(ValueError):
            resolve_commission_schedule(
                "fusion_ctrader_demo",
                trading_environment="live",
                account_id=99999999,
            )

        zero_sched = resolve_commission_schedule("zero", trading_environment="live")
        self.assertEqual(zero_sched, ZERO_COMMISSION_SCHEDULE)


class TestTask7RolloverAndDST(unittest.TestCase):
    def setUp(self):
        self.svc = BrokerCostService(
            commission_schedule=FUSION_CTRADER_DEMO_48439552,
            swap_schedule=FUSION_XAUUSD_SWAP_SPEC,
        )

    def test_ny_1700_utc_conversion_summer_vs_winter_and_dst_transitions(self):
        self.assertEqual(
            self.svc.rollover_utc_for_local_date(date(2026, 9, 2)),
            datetime(2026, 9, 2, 21, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            self.svc.rollover_utc_for_local_date(date(2026, 11, 16)),
            datetime(2026, 11, 16, 22, 0, tzinfo=timezone.utc),
        )
        # Fall-back DST: Oct 30 EDT (21Z) vs Nov 2 EST (22Z)
        self.assertEqual(
            self.svc.rollover_utc_for_local_date(date(2026, 10, 30)),
            datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            self.svc.rollover_utc_for_local_date(date(2026, 11, 2)),
            datetime(2026, 11, 2, 22, 0, tzinfo=timezone.utc),
        )
        # Spring-forward DST: Mar 6 EST (22Z) vs Mar 9 EDT (21Z)
        self.assertEqual(
            self.svc.rollover_utc_for_local_date(date(2026, 3, 6)),
            datetime(2026, 3, 6, 22, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            self.svc.rollover_utc_for_local_date(date(2026, 3, 9)),
            datetime(2026, 3, 9, 21, 0, tzinfo=timezone.utc),
        )
        # Winter EST skips 21:00Z and fires strictly at 22:00Z
        self.assertEqual(
            self.svc.rollover_events_between(
                datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc),
                datetime(2026, 11, 16, 21, 30, tzinfo=timezone.utc),
            ),
            [],
        )
        events_2205 = self.svc.rollover_events_between(
            datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 16, 22, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(len(events_2205), 1)
        self.assertEqual(events_2205[0][0], datetime(2026, 11, 16, 22, 0, tzinfo=timezone.utc))

    def test_wednesday_triple_swap_long_short_signs_and_weekend_exclusion(self):
        self.assertAlmostEqual(self.svc.calculate_swap_pnl_usd("BUY", 1.0, date(2026, 9, 1)), -56.53, places=2)
        self.assertAlmostEqual(self.svc.calculate_swap_pnl_usd("SELL", 1.0, date(2026, 9, 1)), +26.77, places=2)
        self.assertAlmostEqual(self.svc.calculate_swap_pnl_usd("BUY", 1.0, date(2026, 9, 2)), -169.59, places=2)
        self.assertAlmostEqual(self.svc.calculate_swap_pnl_usd("SELL", 1.0, date(2026, 9, 2)), +80.31, places=2)

        weekend_events = self.svc.rollover_events_between(
            datetime(2026, 9, 4, 18, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(weekend_events), 1)
        self.assertEqual(weekend_events[0][1], date(2026, 9, 4))

    def test_open_position_intraday_vs_overnight_swap_pnl(self):
        pos_buy = _OpenPosition(
            ticket=2, symbol="XAUUSD", direction="BUY", strategy="test",
            entry_price=4300.0, sl=4290.0, tp=4320.0, atr=5.0, volume=0.10,
            entry_time=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
            entry_bar_idx=1, final_score=0.7, regime="SIDEWAYS",
        )
        self.assertEqual(
            pos_buy.apply_rollovers_until(datetime(2026, 9, 2, 19, 0, tzinfo=timezone.utc), self.svc),
            0.0,
        )
        delta_wed = pos_buy.apply_rollovers_until(
            datetime(2026, 9, 2, 21, 5, tzinfo=timezone.utc),
            self.svc,
        )
        self.assertAlmostEqual(delta_wed, -16.96, places=2)
        self.assertAlmostEqual(pos_buy.swap_pnl_usd, -16.96, places=2)
        self.assertEqual(
            pos_buy.apply_rollovers_until(datetime(2026, 9, 2, 21, 10, tzinfo=timezone.utc), self.svc),
            0.0,
        )
        net_pnl = pos_buy.compute_pnl(4300.0, cost_service=self.svc)
        self.assertLess(net_pnl, 0.0)


class TestTask7VolumeSemanticsAndParity(unittest.TestCase):
    def test_tick_repository_does_not_fabricate_volume_with_coalesce(self):
        repo_path = _SRC / "goldedge" / "adapters" / "secondary" / "db" / "tick_repository.py"
        txt = repo_path.read_text(encoding="utf-8")
        self.assertNotIn("COALESCE(volume, 1)", txt)
        self.assertIn("real_volume", txt)

    def test_kalman_none_matches_unit_baseline_exactly(self):
        kf_none = VolumeAdaptiveKalmanFilter()
        kf_one = VolumeAdaptiveKalmanFilter()
        rng = np.random.default_rng(7)
        prices = 4300.0 + np.cumsum(rng.normal(0.0, 0.2, 200))
        diffs = []
        for px in prices:
            e1, v1 = kf_none.update_tick(float(px), None)
            e2, v2 = kf_one.update_tick(float(px), 1.0)
            diffs.append(max(abs(e1 - e2), abs(v1 - v2)))
        self.assertLess(max(diffs), 1e-12)

    def test_live_and_backtest_1m_aggregation_and_indicator_parity(self):
        bars = _make_1m_bars(n=80, real_volume=None)
        agg_bt = aggregate_ohlcv_bars(bars, period_minutes=5)
        self.assertEqual(len(agg_bt), 16)
        for b in agg_bt:
            self.assertEqual(b["tick_count"], int(b["tick_volume"]))
            self.assertIsNone(b["real_volume"])

        rolling = RollingBarAggregator(period_minutes=5)
        for b in bars[:6]:
            rolling.update({
                "time": b.time,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "tick_count": b.tick_volume,
                "tick_volume": b.tick_volume,
                "real_volume": b.real_volume,
            })
        b5_roll = list(rolling.bars)[0]
        b5_batch = aggregate_ohlcv_bars(bars[:5], period_minutes=5)[0]
        self.assertEqual(b5_roll["tick_count"], b5_batch["tick_count"])
        self.assertEqual(b5_roll["tick_volume"], b5_batch["tick_volume"])
        self.assertIsNone(b5_roll["real_volume"])
        self.assertIsNone(b5_batch["real_volume"])

        df_ind = IndicatorService.compute_indicators(bars)
        self.assertIn("tick_count", df_ind.columns)
        self.assertIn("tick_rate", df_ind.columns)
        self.assertIn("tick_rate_ratio", df_ind.columns)
        self.assertIn("tick_weighted_price", df_ind.columns)
        self.assertIn("real_volume", df_ind.columns)
        self.assertTrue(df_ind["real_volume"].isna().all())
        self.assertTrue(df_ind["vwap"].isna().all())
        self.assertNotIn("volume_ratio", df_ind.columns)

    def test_signal_generator_and_scorer_tick_intensity_toggle(self):
        bars = _make_1m_bars(n=80, real_volume=None)
        df_ind = IndicatorService.compute_indicators(bars)
        sig = Signal(
            strategy="kinematic_kalman_trend",
            direction=SignalDirection.BUY,
            symbol="XAUUSD",
            entry_price=float(df_ind["close"].iloc[-1]),
            technical_strength=0.8,
        )
        features = {
            "kalman_vel": 0.03,
            "kaufman_er": 0.45,
            "shannon_entropy": 2.75,
            "ema20_slope": 0.0002,
            "tick_weighted_price_dist": 0.001,
            "tick_rate_ratio": 0.25,
            "utc_hour": 10,
        }
        scorer = TradeScorer()
        score_on = scorer.score_signal(
            sig, regime="TRENDING_BULL", features=features, use_tick_intensity_features=True
        )
        score_off = scorer.score_signal(
            sig, regime="TRENDING_BULL", features=features, use_tick_intensity_features=False
        )
        self.assertNotAlmostEqual(score_on.final_score, score_off.final_score, places=4)

        df_ind.loc[df_ind.index[-1], "time"] = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
        sigs_off = SignalGeneratorService.generate_signals(
            df_ind, "XAUUSD", MarketRegime.SESSION_ORB, use_tick_intensity_features=False
        )
        self.assertTrue(all(s.strategy != "vwap_momentum_breakout" for s in sigs_off))

    def test_new_task7_domain_service_is_pure_python_hexagonal(self):
        forbidden = {"pandas", "sqlalchemy", "requests", "psycopg2", "numpy"}
        bcs_path = _SRC / "goldedge" / "domain" / "services" / "broker_cost_service.py"
        tree = ast.parse(bcs_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], forbidden)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[0], forbidden)


if __name__ == "__main__":
    unittest.main()
