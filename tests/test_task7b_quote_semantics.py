"""
Task 7B (Phase 6) — Automated Tests for Real Volume vs Quote-Arrival Frequency Semantics
(`QUOTE_ARRIVAL_SEMANTICS_V1`).

Covers all 10 required Task 7B acceptance checks:
  1. NULL real_volume remains NULL end-to-end.
  2. Synthetic volume is never interpreted as actual traded volume.
  3. Valid quote updates increment tick_count.
  4. Invalid quotes (inverted / zero-spread / <= 0) do not increment tick_count.
  5. Duplicate handling follows the defined ingestion policy (distinct same-ts prices preserved in live stream; exact duplicate skipped).
  6. Minute aggregation produces exact valid tick_count & tick_rate.
  7. Historical and live aggregation follow the exact same validity and counting rules.
  8. Missing feed intervals are distinguishable from zero activity (`is_feed_gap`).
  9. Tick-weighted price (`tick_weighted_price`) is never reported as genuine VWAP.
 10. Kalman filter does not consume fake traded volume.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if not _SRC.exists() and Path("/app/src").exists():
    _ROOT = Path("/app")
    _SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from goldedge.adapters.primary.scheduler.scan_scheduler import ScanScheduler
from goldedge.adapters.secondary.db.candle_repository import CandleRepository
from goldedge.adapters.secondary.db.tick_repository import TickRepository
from goldedge.application.use_cases.run_backtest import RunBacktestUseCase
from goldedge.config.risk_profiles import MEDIUM_RISK_BACKTEST
from goldedge.domain.events.market import DomainEvent
from goldedge.domain.models.bar import Bar
from goldedge.domain.services.denoise_service import VolumeAdaptiveKalmanFilter
from goldedge.domain.services.indicator_service import IndicatorService
from goldedge.domain.services.mtf_trend import RollingBarAggregator, aggregate_ohlcv_bars
from goldedge.domain.services.sizing_service import PositionSizingService


def _make_bars_no_real_vol(n: int = 75) -> list[Bar]:
    base = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    bars: list[Bar] = []
    px = 4310.0
    for i in range(n):
        ts = base + timedelta(minutes=i)
        op = px
        cl = op + 0.15 + 0.25 * np.sin(i / 3.0)
        bars.append(
            Bar(
                symbol="XAUUSD",
                time=ts,
                open=round(op, 2),
                high=round(max(op, cl) + 0.4, 2),
                low=round(min(op, cl) - 0.3, 2),
                close=round(cl, 2),
                tick_volume=100 + (i % 6) * 20,
                tick_count=100 + (i % 6) * 20,
                tick_rate=float(100 + (i % 6) * 20),
                real_volume=None,
            )
        )
        px = cl
    return bars


class TestTask7BQuoteSemantics(unittest.TestCase):
    def test_1_null_real_volume_remains_null_in_repo_and_ctrader_ingest(self):
        repo_src = (_SRC / "goldedge" / "adapters" / "secondary" / "db" / "tick_repository.py").read_text(encoding="utf-8")
        self.assertNotIn("COALESCE(volume, 1)", repo_src)
        self.assertIn("volume AS real_volume", repo_src)

        provider_src = (_SRC / "goldedge" / "adapters" / "secondary" / "market_data" / "ctrader_provider.py").read_text(encoding="utf-8")
        self.assertNotIn('df["volume"] = 1', provider_src)
        self.assertIn('df["volume"] = None', provider_src)

    def test_2_and_9_tick_weighted_price_never_reported_as_real_vwap_and_no_volume_ratio(self):
        bars = _make_bars_no_real_vol(75)
        df = IndicatorService.compute_indicators(bars)

        self.assertIn("tick_count", df.columns)
        self.assertIn("tick_rate", df.columns)
        self.assertIn("tick_rate_ratio", df.columns)
        self.assertIn("tick_weighted_price", df.columns)
        self.assertIn("tick_weighted_price_dist", df.columns)
        self.assertNotIn("volume", df.columns)
        self.assertNotIn("volume_ratio", df.columns)
        self.assertNotIn("volume_spike", df.columns)

        # Missing real_volume -> genuine VWAP must stay NaN, NOT equal tick_weighted_price
        self.assertTrue(df["real_volume"].isna().all())
        self.assertTrue(df["vwap"].isna().all())
        self.assertTrue(df["vwap_dist"].isna().all())
        self.assertTrue(df["tick_weighted_price"].notna().any())

        # When genuine provider real_volume exists (>0), real VWAP is computed cleanly
        bars_real = [
            Bar(
                symbol=b.symbol, time=b.time, open=b.open, high=b.high,
                low=b.low, close=b.close, tick_volume=b.tick_volume,
                tick_count=b.tick_count, tick_rate=b.tick_rate,
                real_volume=500 + idx * 10,
            )
            for idx, b in enumerate(bars)
        ]
        df_real = IndicatorService.compute_indicators(bars_real)
        self.assertTrue(df_real["vwap"].notna().any())

    def test_3_4_6_7_valid_vs_invalid_quotes_and_historical_live_parity(self):
        # Mix 3 valid quotes, 1 inverted (bid > ask), 1 zero-spread (bid == ask), 1 non-positive (bid <= 0)
        t0 = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
        mixed_ticks = [
            {"time": t0 + timedelta(seconds=1),  "symbol": "XAUUSD", "bid": 4300.10, "ask": 4300.20, "real_volume": None},
            {"time": t0 + timedelta(seconds=5),  "symbol": "XAUUSD", "bid": 4300.90, "ask": 4300.80, "real_volume": None},  # inverted -> MUST NOT count
            {"time": t0 + timedelta(seconds=10), "symbol": "XAUUSD", "bid": 4300.25, "ask": 4300.25, "real_volume": None},  # zero spread -> MUST NOT count
            {"time": t0 + timedelta(seconds=15), "symbol": "XAUUSD", "bid": 0.0,     "ask": 4300.20, "real_volume": None},  # non-pos -> MUST NOT count
            {"time": t0 + timedelta(seconds=20), "symbol": "XAUUSD", "bid": 4300.30, "ask": 4300.40, "real_volume": None},
            {"time": t0 + timedelta(seconds=30), "symbol": "XAUUSD", "bid": 4300.20, "ask": 4300.30, "real_volume": None},
        ]
        # Next minute closes bucket 12:00
        close_tick = {"time": t0 + timedelta(minutes=1, seconds=1), "symbol": "XAUUSD", "bid": 4300.50, "ask": 4300.60, "real_volume": None}

        # Live path: ScanScheduler._handle_tick
        saved_bars: list[Bar] = []
        candle_repo = MagicMock()
        candle_repo.save_bar.side_effect = lambda bar: saved_bars.append(bar)
        scan_uc = MagicMock()
        scan_uc.shadow_mode = False
        sched = ScanScheduler(scan_market_uc=scan_uc, event_bus=MagicMock(), candle_repo=candle_repo)

        for row in mixed_ticks + [close_tick]:
            ev = SimpleNamespace(
                payload={"tick": {"symbol": "XAUUSD", "bid": row["bid"], "ask": row["ask"], "time": row["time"].isoformat(), "volume": None}},
            )
            sched._handle_tick(ev)

        self.assertEqual(len(saved_bars), 1)
        live_bar = saved_bars[0]
        self.assertEqual(live_bar.tick_volume, 3)
        self.assertEqual(live_bar.tick_count, 3)
        self.assertEqual(live_bar.tick_rate, 3.0)
        self.assertIsNone(live_bar.real_volume)
        self.assertAlmostEqual(live_bar.open, 4300.10, places=2)
        self.assertAlmostEqual(live_bar.high, 4300.30, places=2)
        self.assertAlmostEqual(live_bar.low, 4300.10, places=2)
        self.assertAlmostEqual(live_bar.close, 4300.20, places=2)

        # Backtest candle SQL parity rule in CandleRepository.upsert_1m_bars_from_ticks
        engine = MagicMock()
        c_repo = CandleRepository(engine)
        c_repo.upsert_1m_bars_from_ticks("XAUUSD", t0, t0 + timedelta(minutes=1))
        sql_executed = str(engine.begin.return_value.__enter__.return_value.execute.call_args[0][0])
        self.assertIn("bid > 0", sql_executed)
        self.assertIn("ask > 0", sql_executed)
        self.assertIn("bid < ask", sql_executed)
        self.assertIn("NULL::bigint AS real_volume", sql_executed)

    def test_5_duplicate_handling_preserves_distinct_same_ts_and_skips_exact_dup(self):
        t0 = datetime(2026, 9, 2, 12, 0, 5, tzinfo=timezone.utc)
        t1 = datetime(2026, 9, 2, 12, 1, 1, tzinfo=timezone.utc)
        saved_bars: list[Bar] = []
        candle_repo = MagicMock()
        candle_repo.save_bar.side_effect = lambda b: saved_bars.append(b)
        sched = ScanScheduler(scan_market_uc=MagicMock(), event_bus=MagicMock(), candle_repo=candle_repo)

        ticks = [
            (t0, 4300.10, 4300.20),
            (t0, 4300.10, 4300.20),  # exact duplicate -> skipped
            (t0, 4300.15, 4300.25),  # distinct price quote sharing timestamp -> preserved
            (t1, 4300.30, 4300.40),
        ]
        for ts, b, a in ticks:
            sched._handle_tick(
                SimpleNamespace(
                    payload={"tick": {"symbol": "XAUUSD", "bid": b, "ask": a, "time": ts.isoformat(), "volume": None}},
                )
            )
        self.assertEqual(len(saved_bars), 1)
        self.assertEqual(saved_bars[0].resolved_tick_count, 2)

    def test_8_missing_feed_intervals_distinguished_from_zero_activity(self):
        t0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
        bars_with_gap = [
            {"time": t0, "open": 4300.0, "high": 4301.0, "low": 4299.5, "close": 4300.5, "tick_count": 120, "real_volume": None},
            # Minutes 10:01, 10:02 missing (feed gap!) -> only 3 of 5 minutes observed in 5m bucket
            {"time": t0 + timedelta(minutes=3), "open": 4300.5, "high": 4301.5, "low": 4300.0, "close": 4301.0, "tick_count": 100, "real_volume": None},
            {"time": t0 + timedelta(minutes=4), "open": 4301.0, "high": 4302.0, "low": 4300.8, "close": 4301.5, "tick_count": 110, "real_volume": None},
        ]
        agg = aggregate_ohlcv_bars(bars_with_gap, period_minutes=5)
        self.assertEqual(len(agg), 1)
        self.assertTrue(agg[0]["is_feed_gap"])
        self.assertEqual(agg[0]["observed_1m_bars"], 3)
        self.assertEqual(agg[0]["tick_count"], 330)
        self.assertAlmostEqual(agg[0]["tick_rate"], 66.0, places=4)

        full_5 = [
            {"time": t0 + timedelta(minutes=i), "open": 4300.0, "high": 4301.0, "low": 4299.5, "close": 4300.5, "tick_count": 100, "real_volume": None}
            for i in range(5)
        ]
        agg_full = aggregate_ohlcv_bars(full_5, period_minutes=5)
        self.assertFalse(agg_full[0]["is_feed_gap"])
        self.assertEqual(agg_full[0]["observed_1m_bars"], 5)

    def test_10_kalman_decoupled_from_fake_volume_and_quote_rate(self):
        kf_default = VolumeAdaptiveKalmanFilter()  # adapt_to_volume=False by default
        kf_quote = VolumeAdaptiveKalmanFilter()
        rng = np.random.default_rng(19)
        prices = 4300.0 + np.cumsum(rng.normal(0.0, 0.12, 150))
        diffs = []
        for idx, px in enumerate(prices):
            p1, v1 = kf_default.update_tick(float(px), None)
            # Even if a legacy caller accidentally passes fake volume=1.0 or arbitrary 250.0 quote counts,
            # default Kalman ignores non-genuine volume cleanly unless adapt_to_volume=True
            p2, v2 = kf_quote.update_tick(float(px), float(1 + (idx % 250)))
            diffs.append(max(abs(p1 - p2), abs(v1 - v2)))
        self.assertLess(max(diffs), 1e-12)


if __name__ == "__main__":
    unittest.main()
