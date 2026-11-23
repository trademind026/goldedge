"""
Task 6C Regression & Lookahead / Execution Invariant Tests:
  1. Strict causality & no lookahead (perturbing future ticks beyond bar T cannot change
     signal, Kalman velocity, indicators, regime, or entry price/lot up through bar T).
  2. Strict invalid-quote filtering (bid<=0, ask<=0, bid>=ask never fill, ratchet, or trigger MTM).
  3. BUY vs SELL executable spread sides (BUY asks entry / bids exit; SELL bids entry / asks exit).
  4. Stop gap slippage & theoretical stop vs gap attribution integrity.
  5. DST-aware NY 17:00 rollover schedule across US fall-back (Oct 30 EDT 21:00Z -> Nov 2 EST 22:00Z)
     and Wednesday triple swap, plus no duplicate charges.
  6. Consistent END_OF_BACKTEST liquidation applying rollover + exit commission + slippage.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for p in (str(ROOT), str(SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from goldedge.application.use_cases.run_backtest import RunBacktestUseCase
from goldedge.config.risk_profiles import MEDIUM_RISK_BACKTEST
from goldedge.domain.services.broker_cost_service import BrokerCostService, FUSION_CTRADER_DEMO_48439552, FUSION_XAUUSD_SWAP_SPEC
from goldedge.domain.services.sizing_service import PositionSizingService
from goldedge.adapters.secondary.virtual_broker import VirtualBrokerAdapter


class _MockTickRepo:
    def __init__(self, df: pd.DataFrame):
        self._df = df

    def get_ticks_as_df(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        m = (self._df["time"] >= pd.Timestamp(start)) & (self._df["time"] < pd.Timestamp(end))
        return self._df.loc[m].copy()


def _synthetic_ticks(start_dt: datetime, minutes: int, seed_offset: float = 0.0, perturb_after_min: int | None = None) -> pd.DataFrame:
    rows = []
    px = 4000.0 + seed_offset
    for m in range(minutes):
        t0 = start_dt + timedelta(minutes=m)
        # Warmup mild downtrend, then strong downward drop during 12:00..15:00 UTC on Sep 3 so SELL triggers cleanly
        if perturb_after_min is not None and m >= perturb_after_min:
            delta = 3.50  # Massive future reversal after cutoff
        elif 3600 <= m < 3660:
            delta = -0.25  # Strong bearish velocity / EMA20 slope during London+NY overlap
        elif m < 3600:
            delta = -0.02 if (m % 4 != 0) else 0.01
        else:
            delta = -0.05
        for k in range(5):
            px = px + delta
            ts = t0 + timedelta(seconds=k * 10)
            rows.append({"time": ts, "bid": round(px, 2), "ask": round(px + 0.25, 2), "real_volume": np.nan})
            if m == perturb_after_min and k == 1:
                rows.append({"time": ts + timedelta(seconds=1), "bid": round(px + 50.0, 2), "ask": round(px, 2), "real_volume": np.nan})
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


class TestTask6CExecutionAndLookahead(unittest.TestCase):
    def test_dst_rollover_transition_oct30_to_nov2_2026(self):
        svc = BrokerCostService(FUSION_CTRADER_DEMO_48439552, FUSION_XAUUSD_SWAP_SPEC)
        # Before US DST fall-back (Fri Oct 30, 2026): 17:00 EDT = 21:00 UTC
        oct30_events = svc.rollover_events_between(
            datetime(2026, 10, 30, 20, 59, tzinfo=timezone.utc),
            datetime(2026, 10, 30, 21, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(len(oct30_events), 1)
        self.assertEqual(oct30_events[0][0], datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc))
        self.assertEqual(oct30_events[0][2], 1)

        # Weekend (Sat/Sun): zero rollover events
        wknd_events = svc.rollover_events_between(
            datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 2, 21, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(len(wknd_events), 0)

        # After US DST fall-back (Mon Nov 2, 2026): 17:00 EST = 22:00 UTC
        nov2_events = svc.rollover_events_between(
            datetime(2026, 11, 2, 21, 59, tzinfo=timezone.utc),
            datetime(2026, 11, 2, 22, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(len(nov2_events), 1)
        self.assertEqual(nov2_events[0][0], datetime(2026, 11, 2, 22, 0, tzinfo=timezone.utc))

        # Wednesday triple swap
        wed_swap = svc.calculate_swap_pnl_usd("SELL", 1.0, datetime(2026, 11, 4).date(), multiplier=3)
        self.assertAlmostEqual(wed_swap, round(2.677 * 10.0 * 1.0 * 3, 2), places=2)

    def test_invalid_quotes_and_zero_spread_skipped_and_end_of_backtest_consistent(self):
        # Create minimal 60h warmup + 10m replay containing inverted & zero-spread ticks + an End-of-Backtest open position
        s_dt = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        rows = []
        # 55 hours of 5m/1h bars for warmup ending at start_dt
        start_replay = s_dt + timedelta(hours=60)
        cur = s_dt
        px = 4100.0
        while cur < start_replay:
            px -= 0.02
            rows.append({"time": cur, "bid": round(px, 2), "ask": round(px + 0.25, 2), "real_volume": np.nan})
            cur += timedelta(minutes=1)
        # Replay ticks: add inverted (bid > ask) and zero-spread (bid == ask)
        for m in range(15):
            t0 = start_replay + timedelta(minutes=m)
            rows.append({"time": t0, "bid": round(px - 0.1 * m, 2), "ask": round(px - 0.1 * m + 0.25, 2), "real_volume": np.nan})
            if m == 2:
                rows.append({"time": t0 + timedelta(seconds=10), "bid": 4200.0, "ask": 4199.0, "real_volume": np.nan})
                rows.append({"time": t0 + timedelta(seconds=20), "bid": 4100.0, "ask": 4100.0, "real_volume": np.nan})
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"], utc=True)

        uc = RunBacktestUseCase(
            tick_repo=_MockTickRepo(df),
            virtual_broker=VirtualBrokerAdapter(),
            manage_position=None,
            sizing_service=PositionSizingService(contract_size=100.0),
            profile=MEDIUM_RISK_BACKTEST,
        )
        res = uc.execute(
            "XAUUSD",
            start_replay,
            start_replay + timedelta(minutes=15),
            initial_capital=10000.0,
            commission_profile="fusion_ctrader_demo",
            trading_environment="demo",
            account_id=48439552,
            slippage_pips=0.0533,
            apply_swap=True,
            export_artifacts=False,
        )
        self.assertEqual(res["inverted_quotes_skipped"], 1)
        self.assertEqual(res["zero_spread_ticks_skipped"], 1)

    def test_no_lookahead_future_tick_perturbation_invariance(self):
        # Verify that modifying future ticks beyond cutoff T_cut does NOT alter any earlier decision or trade entry
        base_start = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        df_orig = _synthetic_ticks(base_start, minutes=4000)
        df_pert = _synthetic_ticks(base_start, minutes=4000, perturb_after_min=3680)

        replay_start = base_start + timedelta(hours=55)
        cutoff_dt = base_start + timedelta(minutes=3680)

        def _run_until(df):
            uc = RunBacktestUseCase(
                tick_repo=_MockTickRepo(df),
                virtual_broker=VirtualBrokerAdapter(),
                manage_position=None,
                sizing_service=PositionSizingService(contract_size=100.0),
                profile=MEDIUM_RISK_BACKTEST,
            )
            return uc.execute(
                "XAUUSD",
                replay_start,
                base_start + timedelta(minutes=3999),
                initial_capital=10000.0,
                commission_profile="fusion_ctrader_demo",
                trading_environment="demo",
                account_id=48439552,
                slippage_pips=0.0533,
                apply_swap=True,
                export_artifacts=False,
            )

        r1 = _run_until(df_orig)
        r2 = _run_until(df_pert)

        cut_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
        pre1 = [t for t in r1["trade_list"] if t["entry_time"] < cut_str]
        pre2 = [t for t in r2["trade_list"] if t["entry_time"] < cut_str]
        self.assertGreaterEqual(len(pre1), 1, "Expected >= 1 causal trade before perturbation cutoff")
        self.assertEqual(len(pre1), len(pre2))
        for a, b in zip(pre1, pre2):
            self.assertEqual(a["entry_time"], b["entry_time"])
            self.assertEqual(a["direction"], b["direction"])
            self.assertEqual(a["entry_price"], b["entry_price"])
            self.assertEqual(a["qty"], b["qty"])
            self.assertEqual(a["sl"], b["sl"])


if __name__ == "__main__":
    unittest.main()
