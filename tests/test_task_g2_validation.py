"""
Task G2 Validation & Regression Test Suite.

Covers:
  - Opt-in `scorer_mode="g2_calibrated"` pure-Python hexagonal isolation & default `"legacy"` parity
  - Resolution of confirmed scoring defects (5m entropy non-saturation, uniform Kalman scaling,
    5m slope climax decay, TWP breakout alignment, zero tick-accel noise, EMA20 extension dampener)
  - Causal no-lookahead invariance under `scorer_mode="g2_calibrated"` (`export_artifacts=False`)
  - 7-day chunk boundary continuity in `RunBacktestUseCase(..., scorer_mode="g2_calibrated")`
  - REST API `scorer_mode` parameter propagation, PostgreSQL metric schema & 400 validation
  - Clean-room G2 script AST isolation, frozen SHA-256 baseline preservation, and Phase F reconciliation
  - Live safety lock verification (`SHADOW_MODE = True`, `TRADING_MODE = "demo"`, `8.0%` HWM)
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for p in (str(ROOT), str(SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from goldedge.adapters.primary.rest_api.routes import backtest as bt_routes
from goldedge.adapters.secondary.virtual_broker import VirtualBrokerAdapter
from goldedge.application.use_cases.run_backtest import RunBacktestUseCase
from goldedge.config.risk_profiles import MEDIUM_RISK_BACKTEST
from goldedge.config.settings import settings
from goldedge.domain.models.signal import Signal, SignalDirection
from goldedge.domain.services.denoise_service import DenoiseService
from goldedge.domain.services.sizing_service import PositionSizingService
from goldedge.domain.services.trade_scorer import TradeScorer, VALID_SCORER_MODES


class _MockChunkTickRepo:
    """In-memory ITickRepository supporting exact half-open [start, end) chunk queries."""

    def __init__(self, df: pd.DataFrame):
        self._df = df

    def get_ticks_as_df(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        m = (self._df["time"] >= pd.Timestamp(start)) & (self._df["time"] < pd.Timestamp(end))
        return self._df.loc[m].copy()


def _synthetic_g2_ticks(
    start_dt: datetime,
    minutes: int,
    perturb_after_min: int | None = None,
) -> pd.DataFrame:
    rows = []
    px = 4000.0
    for m in range(minutes):
        t0 = start_dt + timedelta(minutes=m)
        if perturb_after_min is not None and m >= perturb_after_min:
            delta = 4.00
        elif 3600 <= m < 3680:
            delta = -0.35
        elif m < 3600:
            delta = -0.025 if (m % 4 != 0) else 0.01
        else:
            delta = -0.06
        for k in range(5):
            px += delta
            ts = t0 + timedelta(seconds=k * 10)
            rows.append({"time": ts, "bid": round(px, 2), "ask": round(px + 0.25, 2), "real_volume": np.nan})
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


class TestTaskG2Validation(unittest.TestCase):
    def test_hexagonal_pure_python_and_legacy_mode_unchanged(self):
        # Verify trade_scorer.py imports zero external infrastructure / vectorized libs (GEMINI.md)
        ts_path = (Path("/app/src") if Path("/app/src").exists() else SRC) / "goldedge" / "domain" / "services" / "trade_scorer.py"
        tree = ast.parse(ts_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for banned in ("pandas", "numpy", "scipy", "sqlalchemy", "requests"):
                        self.assertNotIn(banned, alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for banned in ("pandas", "numpy", "scipy", "sqlalchemy", "requests"):
                    self.assertNotIn(banned, mod)

        scorer = TradeScorer(w_ml=0.0, w_quant=1.0)
        sig = Signal(
            strategy="bearish_momentum",
            direction=SignalDirection.SELL,
            symbol="XAUUSD",
            entry_price=4000.0,
            technical_strength=1.0,
        )
        feat = {
            "kalman_vel": -0.030,
            "kalman_accel": -0.005,
            "shannon_entropy": 2.75,
            "kaufman_er": 0.45,
            "ema20_slope": -0.0007,
            "tick_weighted_price_dist": -0.0020,
            "tick_rate_ratio": 1.05,
            "utc_hour": 10,
        }
        s_def = scorer.score_signal(sig, regime="TRENDING_BEAR", features=feat)
        s_leg = scorer.score_signal(sig, regime="TRENDING_BEAR", features=feat, scorer_mode="legacy")
        self.assertEqual(s_def.final_score, s_leg.final_score)
        self.assertEqual(s_leg.details["scorer_mode"], "legacy")
        # Legacy formula manual check: 0.35*1 + 0.20*1 + 0.15*1 + 0.15*1 + 0.10*1 + 0.05*0.7 + (+0.01 accel) = 0.995
        self.assertAlmostEqual(s_leg.final_score, 0.995, places=4)

        with self.assertRaises(ValueError):
            scorer.compute_quant_score(sig, regime="SIDEWAYS", features=feat, scorer_mode="unknown_mode")
        self.assertIn("g2_calibrated", VALID_SCORER_MODES)

    def test_g2_calibrated_defect_fixes_unit(self):
        scorer = TradeScorer(w_ml=0.0, w_quant=1.0)
        sig_buy = Signal(
            strategy="bullish_momentum",
            direction=SignalDirection.BUY,
            symbol="XAUUSD",
            entry_price=4000.0,
            technical_strength=1.0,
        )

        # 1. 5m Entropy fix: structured trend returns produce strictly smaller ent_5m_norm (< 0.9) than noisy spread
        structured_rets = pd.Series([0.0007, 0.0008, 0.0006, 0.00075] * 8)
        noisy_rets = pd.Series([0.0014, -0.0013, 0.0005, -0.0004, 0.00018, -0.00015, 0.0003, -0.0003, 0.00005, -0.00006] * 3)
        e_struct = DenoiseService.compute_rolling_entropy_5m_norm(structured_rets)
        e_noise = DenoiseService.compute_rolling_entropy_5m_norm(noisy_rets)
        self.assertLess(e_struct, e_noise)
        self.assertLess(e_struct, 0.5)
        self.assertGreater(e_noise, 0.8)

        base_feat = {
            "kalman_vel": 0.025,
            "kalman_vel_1m_atr": 0.25,
            "kalman_accel": 0.04,
            "shannon_entropy": 2.8,
            "shannon_entropy_5m_norm": 0.72,
            "kaufman_er": 0.40,
            "ema20_slope": 0.00050,
            "tick_weighted_price_dist": 0.0070,
            "tick_rate_ratio": 1.15,
            "utc_hour": 10,
            "atr": 2.0,
            "ema20_dist_atr": 1.10,
            "entry_5m_tick_rate": 100.0,
        }
        st_tr = scorer.score_signal(sig_buy, regime="TRENDING_BULL", features=base_feat, scorer_mode="g2_calibrated")
        st_sw = scorer.score_signal(sig_buy, regime="SIDEWAYS", features=base_feat, scorer_mode="g2_calibrated")
        # 2. SIDEWAYS Kalman momentum subcomponent no longer halves thresholds
        self.assertEqual(st_tr.details["sub_mom_kalman"], st_sw.details["sub_mom_kalman"])
        # 3. Single-tick Kalman accel noise removed
        self.assertEqual(st_tr.details["accel_adj"], 0.0)
        st_neg_acc = scorer.score_signal(sig_buy, regime="TRENDING_BULL", features={**base_feat, "kalman_accel": -0.04}, scorer_mode="g2_calibrated")
        self.assertEqual(st_tr.final_score, st_neg_acc.final_score)

        # 4. Climax 5m slope decays instead of saturating monotonically at 1.0
        st_climax = scorer.score_signal(sig_buy, regime="TRENDING_BULL", features={**base_feat, "ema20_slope": 0.0014}, scorer_mode="g2_calibrated")
        self.assertLess(st_climax.details["sub_ema_slope"], st_tr.details["sub_ema_slope"])

        # 5. Severe EMA20 overextension (>1.8 ATR) smoothly dampens score via ext_mult -> 0.55 at >=3.0 ATR
        st_ext = scorer.score_signal(sig_buy, regime="TRENDING_BULL", features={**base_feat, "ema20_dist_atr": 3.2}, scorer_mode="g2_calibrated")
        self.assertAlmostEqual(st_ext.details["ext_mult"], 0.55, places=4)
        self.assertLess(st_ext.final_score, st_tr.final_score)

        # 6. TWP breakout aligned: confirmed breakout (+0.012) outscores flat/negative TWP (+0.001)
        st_twp_lo = scorer.score_signal(sig_buy, regime="TRENDING_BULL", features={**base_feat, "tick_weighted_price_dist": 0.0010}, scorer_mode="g2_calibrated")
        st_twp_hi = scorer.score_signal(sig_buy, regime="TRENDING_BULL", features={**base_feat, "tick_weighted_price_dist": 0.0120}, scorer_mode="g2_calibrated")
        self.assertGreater(st_twp_hi.details["sub_twp_breakout"], st_twp_lo.details["sub_twp_breakout"])

    def test_g2_calibrated_no_lookahead_and_chunk_stability(self):
        base_start = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        df_orig = _synthetic_g2_ticks(base_start, minutes=4000)
        df_pert = _synthetic_g2_ticks(base_start, minutes=4000, perturb_after_min=3680)
        replay_start = base_start + timedelta(hours=55)
        cutoff_dt = base_start + timedelta(minutes=3680)

        def _run_sim(df: pd.DataFrame, replay_end: datetime) -> dict:
            uc = RunBacktestUseCase(
                tick_repo=_MockChunkTickRepo(df),
                virtual_broker=VirtualBrokerAdapter(),
                manage_position=None,
                sizing_service=PositionSizingService(contract_size=100.0),
                profile=MEDIUM_RISK_BACKTEST,
            )
            return uc.execute(
                "XAUUSD",
                replay_start,
                replay_end,
                initial_capital=10000.0,
                use_kalman_exit=True,
                session_close_utc=15,
                early_fail_mins=15,
                early_fail_min_mfe_atr=0.4,
                commission_profile="fusion_ctrader_demo",
                trading_environment="demo",
                account_id=48439552,
                slippage_pips=0.0533,
                apply_swap=True,
                max_hwm_drawdown_pct=8.0,
                export_artifacts=False,
                scorer_mode="g2_calibrated",
            )

        end_dt = base_start + timedelta(minutes=3999)
        r1 = _run_sim(df_orig, end_dt)
        r2 = _run_sim(df_pert, end_dt)
        self.assertEqual(r1["scorer_mode"], "g2_calibrated")
        cut_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
        pre1 = [t for t in r1["trade_list"] if t["entry_time"] < cut_str]
        pre2 = [t for t in r2["trade_list"] if t["entry_time"] < cut_str]
        self.assertEqual(len(pre1), len(pre2))
        for a, b in zip(pre1, pre2):
            self.assertEqual(a["entry_time"], b["entry_time"])
            self.assertEqual(a["score"], b["score"])
            self.assertEqual(a["entry_price"], b["entry_price"])
            self.assertEqual(a["qty"], b["qty"])
            self.assertEqual(a["sl"], b["sl"])

        # Chunk boundary stability: spanning > 7 days triggers multiple chunk queries inside RunBacktestUseCase
        long_start = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        df_long = _synthetic_g2_ticks(long_start, minutes=12500)  # ~8.7 days
        rep_start_long = long_start + timedelta(days=4)
        rep_end_long = long_start + timedelta(minutes=12490)
        r_long_a = _run_sim_range(df_long, rep_start_long, rep_end_long)
        r_long_b = _run_sim_range(df_long, rep_start_long, rep_end_long)
        self.assertEqual(r_long_a["trades"], r_long_b["trades"])
        self.assertEqual(r_long_a["trade_list"], r_long_b["trade_list"])

    def test_api_scorer_mode_propagation_and_validation(self):
        app = Flask(__name__)
        captured_kwargs: list[dict] = []

        class _FakeThread:
            def __init__(self, target=None, daemon=None):
                self._target = target

            def start(self):
                if self._target:
                    self._target()

        uc = MagicMock()
        uc.profile = MEDIUM_RISK_BACKTEST

        def _fake_exec(*args, **kwargs):
            captured_kwargs.append(kwargs)
            return {
                "initial_capital": 10000.0, "pnl": 0.0, "max_dd": 0.0,
                "trades": 0, "win_rate": 0.0, "pf": 0.0, "avg_win": 0.0,
                "avg_loss": 0.0, "rr": 0.0, "equity_curve": [], "trade_list": [],
                "scorer_mode": kwargs.get("scorer_mode", "legacy"),
            }

        uc.execute.side_effect = _fake_exec
        repo = MagicMock()
        orig_running = bt_routes._backtest_status.get("running", False)
        orig_cache = dict(bt_routes._backtest_results_cache)
        try:
            bt_routes._backtest_status["running"] = False

            def _call(payload: dict):
                with (
                    app.test_request_context("/api/backtest/run", method="POST", json=payload),
                    patch.object(bt_routes.threading, "Thread", _FakeThread),
                ):
                    fn = bt_routes.run_backtest
                    while hasattr(fn, "__wrapped__"):
                        fn = fn.__wrapped__
                    res = fn(run_backtest_uc=uc, backtest_repo=repo)
                    return res if isinstance(res, tuple) else (res, 200)

            # 1. Default omitted scorer_mode -> "legacy"
            _, st1 = _call({"start_date": "2026-10-01", "end_date": "2026-10-15"})
            self.assertEqual(st1, 200)
            self.assertEqual(captured_kwargs[-1]["scorer_mode"], "legacy")

            # 2. Explicit opt-in "g2_calibrated"
            _, st2 = _call({"start_date": "2026-10-01", "end_date": "2026-10-15", "scorer_mode": "g2_calibrated"})
            self.assertEqual(st2, 200)
            self.assertEqual(captured_kwargs[-1]["scorer_mode"], "g2_calibrated")
            self.assertEqual(bt_routes._backtest_results_cache["medium"]["scorer_mode"], "g2_calibrated")

            # 3. Invalid scorer_mode -> HTTP 400
            _, st3 = _call({"start_date": "2026-10-01", "end_date": "2026-10-15", "scorer_mode": "bogus_ml_hack"})
            self.assertEqual(st3, 400)

            # 4. Disabled HWM still rejected with HTTP 400
            _, st4 = _call({"start_date": "2026-10-01", "end_date": "2026-10-15", "max_hwm_drawdown_pct": None})
            self.assertEqual(st4, 400)
        finally:
            bt_routes._backtest_status["running"] = orig_running
            bt_routes._backtest_results_cache.clear()
            bt_routes._backtest_results_cache.update(orig_cache)

    def test_clean_room_g2_ast_isolation_and_frozen_artifacts(self):
        base_root = Path("/app") if Path("/app/backtest_results").exists() else ROOT
        audit_dir = base_root / "scripts" / "audit"
        for fname in (
            "g2_clean_room_scorer.py",
            "g2_clean_room_export_refs.py",
            "task_g2_phase_abc.py",
            "task_g2_phase_de_research.py",
        ):
            fpath = audit_dir / fname
            self.assertTrue(fpath.exists(), f"Missing {fpath}")
            tree = ast.parse(fpath.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn("goldedge", alias.name, f"{fname} imported {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    self.assertNotIn("goldedge", mod, f"{fname} imported from {mod}")

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

        rec_path = base_root / "backtest_results" / "audit" / "task_g2_scorer" / "g2_application_reconciliation.json"
        if rec_path.exists():
            rec = json.loads(rec_path.read_text(encoding="utf-8"))
            self.assertTrue(rec["all_scenarios_zero_mismatch"])
            self.assertEqual(rec["total_mismatches"], 0)
            self.assertEqual(rec["scenarios"]["H0"]["app_summary"]["trades"], 68)
            self.assertAlmostEqual(rec["scenarios"]["H0"]["app_summary"]["pnl"], 112.7398, places=2)
            self.assertEqual(rec["scenarios"]["H3b"]["app_summary"]["trades"], 90)
            self.assertAlmostEqual(rec["scenarios"]["H3b"]["app_summary"]["pnl"], 1584.4266, places=2)

    def test_safety_locks_unchanged(self):
        self.assertIs(settings.SHADOW_MODE, True)
        self.assertEqual(settings.TRADING_MODE, "demo")
        self.assertEqual(float(settings.HWM_MAX_DRAWDOWN_PCT), 8.0)


def _run_sim_range(df: pd.DataFrame, replay_start: datetime, replay_end: datetime) -> dict:
    uc = RunBacktestUseCase(
        tick_repo=_MockChunkTickRepo(df),
        virtual_broker=VirtualBrokerAdapter(),
        manage_position=None,
        sizing_service=PositionSizingService(contract_size=100.0),
        profile=MEDIUM_RISK_BACKTEST,
    )
    return uc.execute(
        "XAUUSD",
        replay_start,
        replay_end,
        initial_capital=10000.0,
        use_kalman_exit=True,
        session_close_utc=15,
        early_fail_mins=15,
        early_fail_min_mfe_atr=0.4,
        commission_profile="fusion_ctrader_demo",
        trading_environment="demo",
        account_id=48439552,
        slippage_pips=0.0533,
        apply_swap=True,
        max_hwm_drawdown_pct=8.0,
        export_artifacts=False,
        scorer_mode="g2_calibrated",
    )


if __name__ == "__main__":
    unittest.main()
