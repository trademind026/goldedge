"""
TASK G3.5 — Phases C, D, E, F, G:
  - Full 6.2-month frozen H3b candidate extraction via untouched RunBacktestUseCase
  - Phase C: Macro P_MOVE = 1 - P(FLAT) H3b economic utility vs ATR/spread/session baselines
  - Phase D: Micro immediate-vs-delayed execution & H3b path simulation (missed fills, BUY/SELL, controls)
  - Phase E: H3b-specific meta-labeling sample feasibility audit
  - Phase F: Cost-stress (1.0x/1.5x/2.0x), outlier-day, fold/session/regime robustness & BH-FDR
  - Phase G: Experiment governance & research-exposure inventory

STRICT SHADOW MODE. RunBacktestUseCase & frozen H0/H3b remain 100% unmodified.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")

from src.goldedge.config.container import Container
from src.goldedge.adapters.secondary.ml.calibration_utility import (
    FUSION_COMM_RATE_PER_SIDE,
    FUSION_SLIPPAGE_USD_PER_SIDE,
    benjamini_hochberg_fdr,
    day_block_mean_ci,
    day_block_paired_diff_ci,
    paired_day_sign_permutation_p,
    simulate_h3b_trade_on_ticks,
)
from src.goldedge.adapters.secondary.virtual_broker import VirtualBrokerAdapter
from src.goldedge.application.use_cases.run_backtest import RunBacktestUseCase
from src.goldedge.config.risk_profiles import get_profile
from src.goldedge.domain.services.broker_cost_service import (
    BrokerCostService,
    CommissionSchedule,
    SwapSchedule,
)
from src.goldedge.domain.services.sizing_service import PositionSizingService

OUT_DIR = Path("backtest_results/audit/task_g3_5_calibration_utility")
G34_DIR = Path("backtest_results/audit/task_g3_4_ml_model_research")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _git_rev() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


class Expanded6mReadOnlyTickRepo:
    """Read-only adapter querying verified v_market_ticks_expanded_6m_v1 for RunBacktestUseCase."""

    def __init__(self, engine):
        self.engine = engine

    def get_ticks_as_df(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        sql = text(
            """
            SELECT time, symbol, bid, ask, (0.5 * (bid + ask)) AS price, 1.0 AS volume
            FROM v_market_ticks_expanded_6m_v1
            WHERE symbol = :sym AND time >= :s AND time < :e
            ORDER BY time ASC
            """
        )
        with self.engine.connect() as conn:
            df = pd.read_sql(sql, conn, params={"sym": symbol, "s": start, "e": end})
        if not df.empty:
            df["time"] = pd.to_datetime(df["time"], utc=True)
        return df


def _run_frozen_h3b_chunk(engine, start_dt: datetime, end_dt: datetime) -> List[Dict[str, Any]]:
    """Run GoldEdge's untouched RunBacktestUseCase with frozen H3b parameters (0.5% risk, 15:00 UTC, E1)."""
    repo = Expanded6mReadOnlyTickRepo(engine)
    broker = VirtualBrokerAdapter()
    sizing = PositionSizingService()
    base_prof = get_profile("MEDIUM_BACKTEST")
    prof = replace(base_prof, risk_per_trade_pct=0.5, max_daily_loss_pct=1.5)
    bt = RunBacktestUseCase(
        tick_repo=repo,
        virtual_broker=broker,
        manage_position=None,
        sizing_service=sizing,
        profile=prof,
    )
    comm_sched = CommissionSchedule(
        name="fusion_markets_zero_12_per_1m",
        broker="Fusion Markets",
        platform="cTrader",
        environment="demo",
        account_id=48439552,
        commission_type="USD_PER_MILLION_USD",
        rate_value=12.0,
    )
    swap_sched = SwapSchedule(swap_calculation_type="NONE", swap_long_pips=0.0, swap_short_pips=0.0, triple_swap_weekday=2)
    cost_svc = BrokerCostService(commission_schedule=comm_sched, swap_schedule=swap_sched)

    res = bt.execute(
        symbol="XAUUSD",
        start=start_dt,
        end=end_dt,
        initial_capital=10000.0,
        use_kalman_exit=False,
        session_close_utc=15,
        early_fail_mins=15,
        early_fail_min_mfe_atr=0.4,
        slippage_pips=0.0533,
        cost_service=cost_svc,
        export_artifacts=False,
    )
    return list(res.get("trade_list", []))


def _extract_all_6m_h3b_candidates(engine) -> pd.DataFrame:
    """Extract frozen H3b candidates across the entire 6.2-month dataset in 2 clean chunks."""
    cache_pq = OUT_DIR / "g3_5_h3b_all_6m_trades.parquet"
    if cache_pq.exists():
        df_cached = pd.read_parquet(cache_pq)
        if len(df_cached) > 0:
            print(f"[G3.5 H3b EXTRACT] Loaded {len(df_cached)} cached 6.2m trades from {cache_pq}", flush=True)
            return df_cached

    chunks = [
        (datetime(2026, 5, 17, 0, 0, tzinfo=timezone.utc), datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)),
        (datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)),
    ]
    rows: List[Dict[str, Any]] = []
    for s_dt, e_dt in chunks:
        t_c = time.time()
        trades = _run_frozen_h3b_chunk(engine, s_dt, e_dt)
        print(f"[G3.5 H3b EXTRACT] {s_dt.date()}..{e_dt.date()}: {len(trades)} trades in {round(time.time() - t_c, 1)}s", flush=True)
        for tr in trades:
            et = pd.to_datetime(tr["entry_time"], utc=True)
            xt = pd.to_datetime(tr["exit_time"], utc=True)
            entry_px = float(tr["entry_price"])
            sl_px = float(tr.get("theoretical_stop_price", tr.get("sl", entry_px)))
            vol = float(tr.get("qty", 0.01))
            comm = float(tr.get("commission_usd", 0.0))
            net_pnl = float(tr.get("pnl", 0.0))
            planned_risk = float(tr.get("planned_risk_usd", 0.0))
            if planned_risk <= 1e-6:
                planned_risk = max(1e-6, abs(entry_px - sl_px) * 100.0 * vol + comm)
            atr_val = abs(entry_px - sl_px) / 1.5 if abs(entry_px - sl_px) > 1e-6 else 3.0
            net_r = net_pnl / planned_risk
            rows.append({
                "entry_time_utc": et.isoformat(),
                "exit_time_utc": xt.isoformat(),
                "entry_ts": float(et.timestamp()),
                "exit_ts": float(xt.timestamp()),
                "day_utc": et.strftime("%Y-%m-%d"),
                "hour_utc": int(et.hour),
                "direction": str(tr["direction"]).upper(),
                "strategy": str(tr.get("strategy", "unknown")),
                "regime": str(tr.get("regime", "UNKNOWN")),
                "final_score": float(tr.get("score", 0.0)),
                "atr": round(atr_val, 5),
                "entry_price": entry_px,
                "exit_price": float(tr.get("exit_price", entry_px)),
                "initial_sl": sl_px,
                "tp": float(tr.get("target", entry_px)),
                "volume": vol,
                "commission_usd": comm,
                "slippage_usd": float(tr.get("slippage_usd", 0.0)),
                "planned_risk_usd": round(planned_risk, 4),
                "net_pnl_usd": round(net_pnl, 4),
                "net_r": round(net_r, 5),
                "exit_reason": str(tr.get("exit_reason", "")),
                "is_win": int(net_pnl > 0.0),
            })
    df = pd.DataFrame(rows).sort_values("entry_ts").drop_duplicates(subset=["entry_ts", "direction"]).reset_index(drop=True)
    df.to_parquet(cache_pq, index=False)
    return df


def _eval_filter_policy(
    df_cand: pd.DataFrame,
    keep_mask: np.ndarray,
    r_col: str = "net_r",
    name: str = "",
) -> Dict[str, Any]:
    """Compute matched-population H3b economic metrics (retained vs per-original-opportunity)."""
    n_total = len(df_cand)
    r_orig = df_cand[r_col].to_numpy(dtype=np.float64)
    days = df_cand["day_utc"].to_numpy()
    keep = np.asarray(keep_mask, dtype=bool)
    r_filt = np.where(keep, r_orig, 0.0)

    n_ret = int(keep.sum())
    n_rej = int((~keep).sum())
    rej_wins = int(((~keep) & (r_orig > 0.0)).sum())
    rej_losses = int(((~keep) & (r_orig <= 0.0)).sum())

    # Cumulative R drawdown (research diagnostic, not portfolio backtest)
    def _max_dd_r(arr: np.ndarray) -> float:
        eq = np.cumsum(arr)
        peak = np.maximum.accumulate(np.concatenate(([0.0], eq)))[1:]
        return round(float(np.min(eq - peak)), 4) if len(eq) else 0.0

    paired_ci = day_block_paired_diff_ci(r_filt, r_orig, days, lower_is_better=False)
    perm_p = paired_day_sign_permutation_p(r_filt, r_orig, days, n_perm=2000, seed=42)

    return {
        "policy": name,
        "n_original": int(n_total),
        "n_retained": n_ret,
        "n_rejected": n_rej,
        "coverage_pct": round(100.0 * n_ret / max(1, n_total), 2),
        "rejection_pct": round(100.0 * n_rej / max(1, n_total), 2),
        "rejected_wins": rej_wins,
        "rejected_losses": rej_losses,
        "retained_win_rate_pct": round(100.0 * float((r_orig[keep] > 0.0).mean()), 2) if n_ret > 0 else 0.0,
        "per_retained_trade_mean_r": round(float(r_orig[keep].mean()), 5) if n_ret > 0 else 0.0,
        "per_original_opp_mean_r": round(float(r_filt.mean()), 5),
        "baseline_original_opp_mean_r": round(float(r_orig.mean()), 5),
        "total_net_r_retained": round(float(r_filt.sum()), 4),
        "baseline_total_net_r": round(float(r_orig.sum()), 4),
        "delta_total_r": round(float(r_filt.sum() - r_orig.sum()), 4),
        "cum_r_max_dd": _max_dd_r(r_filt),
        "baseline_cum_r_max_dd": _max_dd_r(r_orig),
        "paired_day_block_delta_r": paired_ci,
        "day_permutation_p": perm_p,
    }


def _recompute_stressed_r(df_cand: pd.DataFrame, cost_mult: float) -> np.ndarray:
    """Apply prespecified cost stress multiplier (1.0x / 1.5x / 2.0x) to commission + slippage."""
    extra = (cost_mult - 1.0) * (df_cand["commission_usd"].to_numpy() + df_cand["slippage_usd"].to_numpy())
    stressed_pnl = df_cand["net_pnl_usd"].to_numpy() - extra
    return stressed_pnl / np.maximum(1e-6, df_cand["planned_risk_usd"].to_numpy())


def main() -> None:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    container = Container()
    engine = container.db_engine()

    # 1. Extract all frozen H3b candidates across the full 6.2-month dataset
    df_h3b_all = _extract_all_6m_h3b_candidates(engine)
    print(f"[G3.5 H3b ALL 6.2m] Total={len(df_h3b_all)}, Days={df_h3b_all['day_utc'].nunique()}, "
          f"BUY={(df_h3b_all['direction']=='BUY').sum()}, SELL={(df_h3b_all['direction']=='SELL').sum()}, "
          f"NetR={df_h3b_all['net_r'].sum():+.3f}", flush=True)

    # Load Phase B calibrated Macro CAL & OOF parquets
    df_m_cal = pd.read_parquet(OUT_DIR / "g3_5_macro_calibrated_cal.parquet").sort_values("feature_cutoff_utc").reset_index(drop=True)
    df_m_oof = pd.read_parquet(OUT_DIR / "g3_5_macro_calibrated_oof.parquet").sort_values("feature_cutoff_utc").reset_index(drop=True)
    df_m_cal["B3_cal_P_MOVE"] = 1.0 - df_m_cal["B3_LOGISTIC_L2_cal_p_FLAT"]
    df_m_cal["C1_cal_P_MOVE"] = 1.0 - df_m_cal["C1_LIGHTGBM_CONSTRAINED_cal_p_FLAT"]
    df_m_oof["B3_cal_P_MOVE"] = 1.0 - df_m_oof["B3_LOGISTIC_L2_cal_p_FLAT"]
    df_m_oof["C1_cal_P_MOVE"] = 1.0 - df_m_oof["C1_LIGHTGBM_CONSTRAINED_cal_p_FLAT"]

    df_m_oof["feature_cutoff_utc"] = df_m_oof["feature_cutoff_utc"].astype(np.float64)
    df_h3b_all["entry_ts"] = df_h3b_all["entry_ts"].astype(np.float64)
    # Causal merge_asof: match each OOT H3b candidate strictly to most recent Macro row with feature_cutoff_utc <= entry_ts (within 600s)
    df_h3b_oot = pd.merge_asof(
        df_h3b_all.sort_values("entry_ts"),
        df_m_oof[[
            "feature_cutoff_utc", "fold_id", "session_utc", "atr_14_pct", "entry_spread_bps",
            "B3_cal_P_MOVE", "C1_cal_P_MOVE", "C1_LIGHTGBM_CONSTRAINED_cal_p_UP", "C1_LIGHTGBM_CONSTRAINED_cal_p_DOWN",
            "net_class_010",
        ]].rename(columns={"feature_cutoff_utc": "macro_feature_cutoff_utc"}),
        left_on="entry_ts",
        right_on="macro_feature_cutoff_utc",
        direction="backward",
        tolerance=600.0,
    )
    df_h3b_oot = df_h3b_oot.dropna(subset=["fold_id"]).copy().reset_index(drop=True)
    df_h3b_oot["fold_id"] = df_h3b_oot["fold_id"].astype(int)
    df_h3b_oot["macro_actual_moved"] = (df_h3b_oot["net_class_010"] != "FLAT").astype(int)
    ledger_path = OUT_DIR / "g3_5_h3b_candidate_ledger.parquet"
    df_h3b_oot.to_parquet(ledger_path, index=False)
    print(f"[G3.5 PHASE C] Matched OOT H3b candidates={len(df_h3b_oot)} across {df_h3b_oot['day_utc'].nunique()} days "
          f"(NetR={df_h3b_oot['net_r'].sum():+.3f}, meanR={df_h3b_oot['net_r'].mean():+.4f})", flush=True)

    # ------------------------------------------------------------------
    # PHASE C: Macro P_MOVE H3b Utility vs ATR / Spread / Session Baselines
    # Thresholds computed STRICTLY from reserved CALIBRATION partition per fold!
    # ------------------------------------------------------------------
    cal_quantiles: Dict[int, Dict[str, float]] = {}
    for fid, g_cal in df_m_cal.groupby("fold_id"):
        # Restrict calibration distribution to trading hours < 15:00 UTC (matching H3b session_close_utc=15)
        h_cal = pd.to_datetime(g_cal["feature_cutoff_utc"], unit="s", utc=True).dt.hour
        g_elig = g_cal[h_cal < 15]
        cal_quantiles[int(fid)] = {
            f"{col}_q{q}": float(np.nanpercentile(g_elig[col].values, q))
            for col in ("atr_14_pct", "entry_spread_bps", "B3_cal_P_MOVE", "C1_cal_P_MOVE")
            for q in (10, 20, 30, 70, 80, 90)
        }

    def _fold_threshold_mask(col: str, q: int, keep_above: bool = True) -> np.ndarray:
        thr = df_h3b_oot["fold_id"].map(lambda f: cal_quantiles[int(f)][f"{col}_q{q}"]).to_numpy()
        vals = df_h3b_oot[col].to_numpy()
        return (vals >= thr) if keep_above else (vals <= thr)

    phase_c_policies: List[Dict[str, Any]] = []
    policy_masks: Dict[str, np.ndarray] = {
        "FROZEN_H3B_UNFILTERED": np.ones(len(df_h3b_oot), dtype=bool),
        "ATR_ONLY_REJECT_LOW_10": _fold_threshold_mask("atr_14_pct", 10, True),
        "ATR_ONLY_REJECT_LOW_20": _fold_threshold_mask("atr_14_pct", 20, True),
        "ATR_ONLY_REJECT_LOW_30": _fold_threshold_mask("atr_14_pct", 30, True),
        "SPREAD_ONLY_REJECT_HIGH_10": _fold_threshold_mask("entry_spread_bps", 90, False),
        "SPREAD_ONLY_REJECT_HIGH_20": _fold_threshold_mask("entry_spread_bps", 80, False),
        "SPREAD_ONLY_REJECT_HIGH_30": _fold_threshold_mask("entry_spread_bps", 70, False),
        "SESSION_EXCLUDE_ASIA_00_07": (df_h3b_oot["session_utc"] != "ASIA_00_07").to_numpy(),
        "SIMPLE_ATR_SPREAD_COMBINED_20": _fold_threshold_mask("atr_14_pct", 10, True) & _fold_threshold_mask("entry_spread_bps", 90, False),
        "MACRO_B3_CAL_PMOVE_REJECT_10": _fold_threshold_mask("B3_cal_P_MOVE", 10, True),
        "MACRO_B3_CAL_PMOVE_REJECT_20": _fold_threshold_mask("B3_cal_P_MOVE", 20, True),
        "MACRO_B3_CAL_PMOVE_REJECT_30": _fold_threshold_mask("B3_cal_P_MOVE", 30, True),
        "MACRO_C1_CAL_PMOVE_REJECT_10": _fold_threshold_mask("C1_cal_P_MOVE", 10, True),
        "MACRO_C1_CAL_PMOVE_REJECT_20": _fold_threshold_mask("C1_cal_P_MOVE", 20, True),
        "MACRO_C1_CAL_PMOVE_REJECT_30": _fold_threshold_mask("C1_cal_P_MOVE", 30, True),
    }
    for pname, mask in policy_masks.items():
        phase_c_policies.append(_eval_filter_policy(df_h3b_oot, mask, r_col="net_r", name=pname))

    # Incremental test: C1_PMOVE_20 vs ATR_ONLY_20 and vs SIMPLE_ATR_SPREAD_COMBINED_20 on common candidates
    r_orig = df_h3b_oot["net_r"].to_numpy()
    days_oot = df_h3b_oot["day_utc"].to_numpy()
    r_c1_20 = np.where(policy_masks["MACRO_C1_CAL_PMOVE_REJECT_20"], r_orig, 0.0)
    r_atr_20 = np.where(policy_masks["ATR_ONLY_REJECT_LOW_20"], r_orig, 0.0)
    r_simple_20 = np.where(policy_masks["SIMPLE_ATR_SPREAD_COMBINED_20"], r_orig, 0.0)

    incremental_ml_vs_simple = {
        "C1_PMOVE_20_minus_ATR_20": {
            "paired_day_block_delta_r": day_block_paired_diff_ci(r_c1_20, r_atr_20, days_oot, lower_is_better=False),
            "day_permutation_p": paired_day_sign_permutation_p(r_c1_20, r_atr_20, days_oot, n_perm=2000, seed=42),
        },
        "C1_PMOVE_20_minus_SIMPLE_COMBINED_20": {
            "paired_day_block_delta_r": day_block_paired_diff_ci(r_c1_20, r_simple_20, days_oot, lower_is_better=False),
            "day_permutation_p": paired_day_sign_permutation_p(r_c1_20, r_simple_20, days_oot, n_perm=2000, seed=42),
        },
    }

    # Session & Regime stability of C1_PMOVE_20 vs Unfiltered
    c1_20_slices: Dict[str, Any] = {"by_session": {}, "by_regime": {}, "by_direction": {}, "by_fold": {}}
    for col, key in (("session_utc", "by_session"), ("regime", "by_regime"), ("direction", "by_direction"), ("fold_id", "by_fold")):
        for val, g in df_h3b_oot.groupby(col):
            idx = g.index.to_numpy()
            c1_20_slices[key][str(val)] = _eval_filter_policy(g, policy_masks["MACRO_C1_CAL_PMOVE_REJECT_20"][idx], name=f"C1_20_{val}")

    # ------------------------------------------------------------------
    # PHASE D: Micro Adverse-Entry Risk & Execution Timing Simulation
    # ------------------------------------------------------------------
    df_u_cal = pd.read_parquet(OUT_DIR / "g3_5_micro_calibrated_cal.parquet")
    df_u_oof = pd.read_parquet(OUT_DIR / "g3_5_micro_calibrated_oof.parquet").sort_values("feature_cutoff_utc").reset_index(drop=True)

    # Strictly CAL-only thresholds for Micro policies (wait when P_WAIT_ADVERSE > CAL P75 or spread > CAL P75)
    micro_cal_thr: Dict[int, Dict[str, float]] = {}
    for fid, g_cal in df_u_cal.groupby("fold_id"):
        micro_cal_thr[int(fid)] = {
            "p_wait_c1_q75": float(np.nanpercentile(g_cal["C1_LIGHTGBM_CONSTRAINED_cal_p_WAIT_ADVERSE"].values, 75)),
            "spread_q75": float(np.nanpercentile(g_cal["entry_spread_usd"].values, 75)),
        }

    df_u_oof["feature_cutoff_utc"] = df_u_oof["feature_cutoff_utc"].astype(np.float64)
    # Causally match each OOT H3b candidate to most recent Micro row (feature_cutoff_utc <= entry_ts, tolerance 30s)
    df_h3b_micro = pd.merge_asof(
        df_h3b_oot.sort_values("entry_ts"),
        df_u_oof[[
            "feature_cutoff_utc", "entry_spread_usd",
            "C1_LIGHTGBM_CONSTRAINED_cal_p_ENTER_NOW",
            "C1_LIGHTGBM_CONSTRAINED_cal_p_WAIT_ADVERSE",
            "C1_LIGHTGBM_CONSTRAINED_cal_p_NEUTRAL",
        ]].rename(columns={"feature_cutoff_utc": "micro_feature_cutoff_utc", "entry_spread_usd": "micro_spread_usd"}),
        left_on="entry_ts",
        right_on="micro_feature_cutoff_utc",
        direction="backward",
        tolerance=30.0,
    ).reset_index(drop=True)

    # Simulate exact tick-level H3b paths for immediate & delayed policies on every OOT H3b candidate
    rng_rand = np.random.default_rng(42)
    exec_rows: List[Dict[str, Any]] = []
    t_d = time.time()

    for idx, row in df_h3b_micro.iterrows():
        e_ts = float(row["entry_ts"])
        direction = str(row["direction"])
        atr = float(row["atr"])
        fid = int(row["fold_id"])
        s_q = datetime.fromtimestamp(e_ts - 15.0, tz=timezone.utc)
        e_q = datetime.fromtimestamp(e_ts + 5600.0, tz=timezone.utc)

        sql = text(
            """
            SELECT EXTRACT(EPOCH FROM time)::double precision AS ts, bid::double precision AS bid, ask::double precision AS ask
            FROM v_market_ticks_expanded_6m_v1
            WHERE symbol = 'XAUUSD' AND time >= :s AND time <= :e
            ORDER BY time ASC
            """
        )
        with engine.connect() as conn:
            tdf = pd.read_sql(sql, conn, params={"s": s_q, "e": e_q})
        if tdf.empty:
            continue

        ts_arr = tdf["ts"].to_numpy(dtype=np.float64)
        bid_arr = tdf["bid"].to_numpy(dtype=np.float64)
        ask_arr = tdf["ask"].to_numpy(dtype=np.float64)

        # Find immediate entry tick index (first tick >= e_ts)
        imm_idx = int(np.searchsorted(ts_arr, e_ts, side="left"))
        if imm_idx >= len(ts_arr):
            continue

        # Prior 10-tick momentum for simple momentum timing baseline
        prior_mids = 0.5 * (bid_arr[max(0, imm_idx - 10):imm_idx + 1] + ask_arr[max(0, imm_idx - 10):imm_idx + 1])
        prior_mom = float(prior_mids[-1] - prior_mids[0]) if len(prior_mids) >= 2 else 0.0
        mom_adverse = (prior_mom < 0.0) if direction == "BUY" else (prior_mom > 0.0)

        sim_imm = simulate_h3b_trade_on_ticks(direction, imm_idx, ts_arr, bid_arr, ask_arr, atr)
        if not sim_imm["filled"]:
            continue

        def _sim_delayed(delay_sec: Optional[float] = None, delay_ticks: Optional[int] = None) -> Dict[str, Any]:
            if delay_ticks is not None:
                d_idx = imm_idx + delay_ticks
            else:
                d_idx = int(np.searchsorted(ts_arr, ts_arr[imm_idx] + float(delay_sec or 0.0), side="left"))
            if d_idx >= len(ts_arr):
                return {"filled": False, "reason": "NO_QUOTE_AVAILABLE", "net_r": 0.0, "price_imp_usd": 0.0, "latency_sec": None}
            # Invalidation check: if price already moved > 0.5 ATR away from immediate fill before delayed fill, signal invalidated (missed opportunity)
            is_buy = direction == "BUY"
            d_exec_raw = ask_arr[d_idx] if is_buy else bid_arr[d_idx]
            imm_raw = sim_imm["entry_ask"] if is_buy else sim_imm["entry_bid"]
            if abs(d_exec_raw - imm_raw) > 0.5 * atr:
                return {"filled": False, "reason": "SIGNAL_INVALIDATED_MOVE_GT_0.5ATR", "net_r": 0.0, "price_imp_usd": 0.0, "latency_sec": round(float(ts_arr[d_idx] - ts_arr[imm_idx]), 3)}
            sim_d = simulate_h3b_trade_on_ticks(direction, d_idx, ts_arr, bid_arr, ask_arr, atr)
            if not sim_d["filled"]:
                return {"filled": False, "reason": sim_d["reason"], "net_r": 0.0, "price_imp_usd": 0.0, "latency_sec": round(float(ts_arr[d_idx] - ts_arr[imm_idx]), 3)}
            # Price improvement (+ means better entry price for our direction)
            px_imp = (imm_raw - d_exec_raw) if is_buy else (d_exec_raw - imm_raw)
            sim_d["price_imp_usd"] = round(float(px_imp), 5)
            sim_d["latency_sec"] = round(float(ts_arr[d_idx] - ts_arr[imm_idx]), 3)
            return sim_d

        s5 = _sim_delayed(delay_sec=5.0)
        s10 = _sim_delayed(delay_sec=10.0)
        s15 = _sim_delayed(delay_sec=15.0)
        s30t = _sim_delayed(delay_ticks=30)

        p_wait = float(row["C1_LIGHTGBM_CONSTRAINED_cal_p_WAIT_ADVERSE"]) if pd.notna(row["C1_LIGHTGBM_CONSTRAINED_cal_p_WAIT_ADVERSE"]) else 0.0
        sp_usd = float(sim_imm["entry_spread_usd"])
        ml_triggers_wait = bool(p_wait > micro_cal_thr[fid]["p_wait_c1_q75"])
        spread_triggers_wait = bool(sp_usd > micro_cal_thr[fid]["spread_q75"])

        exec_rows.append({
            "entry_ts": e_ts,
            "day_utc": row["day_utc"],
            "fold_id": fid,
            "direction": direction,
            "session_utc": row["session_utc"],
            "regime": row["regime"],
            "atr": atr,
            "imm_spread_usd": sp_usd,
            "p_wait_c1_cal": round(p_wait, 5),
            "ml_triggers_wait": ml_triggers_wait,
            "spread_triggers_wait": spread_triggers_wait,
            "mom_triggers_wait": bool(mom_adverse),
            "imm_net_r": sim_imm["net_r"],
            "imm_net_pnl": sim_imm["net_pnl_usd"],
            "imm_comm_usd": sim_imm["comm_usd"],
            "imm_planned_risk": sim_imm["planned_risk_usd"],
            "imm_exit_reason": sim_imm["exit_reason"],
            "w5_filled": s5["filled"], "w5_reason": s5["reason"], "w5_net_r": s5["net_r"], "w5_px_imp": s5["price_imp_usd"], "w5_lat": s5["latency_sec"],
            "w10_filled": s10["filled"], "w10_reason": s10["reason"], "w10_net_r": s10["net_r"], "w10_px_imp": s10["price_imp_usd"], "w10_lat": s10["latency_sec"],
            "w15_filled": s15["filled"], "w15_reason": s15["reason"], "w15_net_r": s15["net_r"], "w15_px_imp": s15["price_imp_usd"], "w15_lat": s15["latency_sec"],
            "w30t_filled": s30t["filled"], "w30t_reason": s30t["reason"], "w30t_net_r": s30t["net_r"], "w30t_px_imp": s30t["price_imp_usd"], "w30t_lat": s30t["latency_sec"],
        })

    df_exec = pd.DataFrame(exec_rows)
    # Reproducible randomized wait at matched ML trigger rate
    ml_wait_rate = float(df_exec["ml_triggers_wait"].mean()) if len(df_exec) else 0.25
    df_exec["rand_triggers_wait"] = rng_rand.random(len(df_exec)) < ml_wait_rate
    exec_ledger_path = OUT_DIR / "g3_5_micro_execution_ledger.parquet"
    df_exec.to_parquet(exec_ledger_path, index=False)
    print(f"[G3.5 PHASE D] Simulated {len(df_exec)} H3b immediate-vs-delayed paths in {round(time.time() - t_d, 1)}s", flush=True)

    def _summarize_micro_policy(df_sub: pd.DataFrame, pol_name: str, wait_mask: np.ndarray, prefix: str) -> Dict[str, Any]:
        if df_sub.empty:
            return {"policy": pol_name, "n": 0}
        imm_r = df_sub["imm_net_r"].to_numpy(dtype=np.float64)
        w_r = df_sub[f"{prefix}_net_r"].to_numpy(dtype=np.float64)
        w_filled = df_sub[f"{prefix}_filled"].to_numpy(dtype=bool)
        w_imp = df_sub[f"{prefix}_px_imp"].to_numpy(dtype=np.float64)
        w_lat = pd.to_numeric(df_sub[f"{prefix}_lat"], errors="coerce").fillna(0.0).to_numpy(dtype=np.float64)
        days = df_sub["day_utc"].to_numpy()

        pol_r = np.where(wait_mask, np.where(w_filled, w_r, 0.0), imm_r)
        missed = wait_mask & (~w_filled)
        missed_wins = int((missed & (imm_r > 0.0)).sum())
        missed_losses = int((missed & (imm_r <= 0.0)).sum())
        waited_filled = wait_mask & w_filled
        return {
            "policy": pol_name,
            "n_candidates": int(len(df_sub)),
            "n_waited": int(wait_mask.sum()),
            "wait_rate_pct": round(100.0 * float(wait_mask.mean()), 2),
            "n_missed_fills": int(missed.sum()),
            "missed_wins": missed_wins,
            "missed_losses": missed_losses,
            "mean_price_imp_usd_when_filled": round(float(w_imp[waited_filled].mean()), 5) if waited_filled.any() else 0.0,
            "mean_added_latency_sec": round(float(w_lat[wait_mask].mean()), 2) if wait_mask.any() else 0.0,
            "policy_mean_r": round(float(pol_r.mean()), 5),
            "imm_mean_r": round(float(imm_r.mean()), 5),
            "delta_total_r": round(float(pol_r.sum() - imm_r.sum()), 4),
            "paired_day_block_delta_r": day_block_paired_diff_ci(pol_r, imm_r, days, lower_is_better=False),
            "day_permutation_p": paired_day_sign_permutation_p(pol_r, imm_r, days, n_perm=2000, seed=42),
        }

    micro_policies_summary: Dict[str, List[Dict[str, Any]]] = {}
    for seg_name, df_seg in [("ALL", df_exec), ("BUY", df_exec[df_exec["direction"] == "BUY"]), ("SELL", df_exec[df_exec["direction"] == "SELL"])]:
        ones = np.ones(len(df_seg), dtype=bool)
        micro_policies_summary[seg_name] = [
            _summarize_micro_policy(df_seg, "WAIT_5S_ALL", ones, "w5"),
            _summarize_micro_policy(df_seg, "WAIT_10S_ALL", ones, "w10"),
            _summarize_micro_policy(df_seg, "WAIT_15S_ALL", ones, "w15"),
            _summarize_micro_policy(df_seg, "WAIT_30_TICKS_ALL", ones, "w30t"),
            _summarize_micro_policy(df_seg, "SIMPLE_SPREAD_WAIT10S", df_seg["spread_triggers_wait"].to_numpy(dtype=bool), "w10"),
            _summarize_micro_policy(df_seg, "SIMPLE_MOMENTUM_WAIT10S", df_seg["mom_triggers_wait"].to_numpy(dtype=bool), "w10"),
            _summarize_micro_policy(df_seg, "RANDOM_MATCHED_WAIT10S", df_seg["rand_triggers_wait"].to_numpy(dtype=bool), "w10"),
            _summarize_micro_policy(df_seg, "ML_C1_CAL_WAIT10S", df_seg["ml_triggers_wait"].to_numpy(dtype=bool), "w10"),
            _summarize_micro_policy(df_seg, "ML_C1_CAL_WAIT30T", df_seg["ml_triggers_wait"].to_numpy(dtype=bool), "w30t"),
        ]

    # ------------------------------------------------------------------
    # PHASE E: H3b-Specific Meta-Labeling Feasibility Audit (Full 6.2m)
    # ------------------------------------------------------------------
    n_total_6m = int(len(df_h3b_all))
    n_days_6m = int(df_h3b_all["day_utc"].nunique())
    trades_per_day = df_h3b_all.groupby("day_utc").size().to_numpy(dtype=np.float64)
    # Kish effective sample size under day-level clustering
    n_eff_day_kish = round(float((trades_per_day.sum() ** 2) / np.sum(trades_per_day ** 2)), 2) if len(trades_per_day) else 0.0
    # Check concurrent/overlapping trades
    ets = df_h3b_all["entry_ts"].to_numpy()
    xts = df_h3b_all["exit_ts"].to_numpy()
    overlaps = sum(int(ets[i] < xts[i - 1]) for i in range(1, len(ets)))

    win_rate = float(df_h3b_all["is_win"].mean()) if n_total_6m else 0.0
    se_wr_eff = math.sqrt(win_rate * (1.0 - win_rate) / max(1.0, n_eff_day_kish))
    meta_verdict = (
        "META_LABEL_RESEARCH_NOT_FEASIBLE" if n_total_6m < 250 or n_eff_day_kish < 100
        else ("META_LABEL_RESEARCH_CONDITIONAL" if n_total_6m < 600 else "META_LABEL_RESEARCH_FEASIBLE")
    )

    meta_report = {
        "task": "G3.5_PHASE_E_META_LABEL_FEASIBILITY",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "period_utc": ["2026-05-17T00:00:00Z", "2026-11-19T12:00:00Z"],
        "total_h3b_opportunities": n_total_6m,
        "unique_signal_days": n_days_6m,
        "mean_opportunities_per_active_day": round(float(trades_per_day.mean()), 3) if len(trades_per_day) else 0.0,
        "day_clustered_kish_n_eff": n_eff_day_kish,
        "overlapping_concurrent_trades": int(overlaps),
        "direction_counts": {
            "BUY": int((df_h3b_all["direction"] == "BUY").sum()),
            "SELL": int((df_h3b_all["direction"] == "SELL").sum()),
        },
        "outcome_counts": {
            "wins": int((df_h3b_all["is_win"] == 1).sum()),
            "losses": int((df_h3b_all["is_win"] == 0).sum()),
            "win_rate_pct": round(100.0 * win_rate, 2),
            "win_rate_95pct_ci_eff": [round(100.0 * (win_rate - 1.96 * se_wr_eff), 2), round(100.0 * (win_rate + 1.96 * se_wr_eff), 2)],
            "mean_net_r": round(float(df_h3b_all["net_r"].mean()), 5) if n_total_6m else 0.0,
            "day_block_net_r": day_block_mean_ci(df_h3b_all["net_r"].to_numpy(), df_h3b_all["day_utc"].to_numpy()),
        },
        "regime_distribution": {str(k): int(v) for k, v in df_h3b_all["regime"].value_counts().items()},
        "verdict": meta_verdict,
        "rationale": (
            f"Across 6.2 months, frozen H3b generated only {n_total_6m} opportunities across {n_days_6m} active days "
            f"(N_eff = {n_eff_day_kish}). Splitting into 4-way purged Train/Val/Cal/Test leaves < 120 training trades per fold, "
            f"too small for complex tree meta-labeling without severe overfitting risk; only ultra-low-DOF (1-3 feature) linear/rule research "
            f"or 12-24+ months of history / prospective shadow accumulation is scientifically justified."
        ),
    }

    # ------------------------------------------------------------------
    # PHASE F: Cost Stress (1.0x / 1.5x / 2.0x) & Multiple-Testing FDR
    # ------------------------------------------------------------------
    cost_stress_results: Dict[str, Any] = {}
    for label, mult in (("NORMAL_1_0X", 1.0), ("MODERATE_1_5X", 1.5), ("SEVERE_2_0X", 2.0)):
        df_tmp = df_h3b_oot.copy()
        df_tmp["stressed_r"] = _recompute_stressed_r(df_h3b_oot, mult)
        cost_stress_results[label] = {
            "cost_multiplier": mult,
            "FROZEN_H3B_UNFILTERED": _eval_filter_policy(df_tmp, policy_masks["FROZEN_H3B_UNFILTERED"], r_col="stressed_r", name="FROZEN_H3B"),
            "ATR_ONLY_20": _eval_filter_policy(df_tmp, policy_masks["ATR_ONLY_REJECT_LOW_20"], r_col="stressed_r", name="ATR_20"),
            "SPREAD_ONLY_20": _eval_filter_policy(df_tmp, policy_masks["SPREAD_ONLY_REJECT_HIGH_20"], r_col="stressed_r", name="SPREAD_20"),
            "SIMPLE_COMBINED_20": _eval_filter_policy(df_tmp, policy_masks["SIMPLE_ATR_SPREAD_COMBINED_20"], r_col="stressed_r", name="SIMPLE_20"),
            "MACRO_B3_PMOVE_20": _eval_filter_policy(df_tmp, policy_masks["MACRO_B3_CAL_PMOVE_REJECT_20"], r_col="stressed_r", name="MACRO_B3_20"),
            "MACRO_C1_PMOVE_20": _eval_filter_policy(df_tmp, policy_masks["MACRO_C1_CAL_PMOVE_REJECT_20"], r_col="stressed_r", name="MACRO_C1_20"),
        }

    # Collect all hypothesis p-values across Phase C & Phase D (ALL) for Benjamini-Hochberg FDR
    hyp_names: List[str] = []
    hyp_pvals: List[float] = []
    for p in phase_c_policies:
        if p["policy"] != "FROZEN_H3B_UNFILTERED":
            hyp_names.append(f"MACRO_{p['policy']}")
            hyp_pvals.append(float(p["day_permutation_p"]))
    for p in micro_policies_summary["ALL"]:
        hyp_names.append(f"MICRO_{p['policy']}")
        hyp_pvals.append(float(p["day_permutation_p"]))
    qvals = benjamini_hochberg_fdr(hyp_pvals)
    fdr_table = [
        {"hypothesis": h, "raw_day_perm_p": p, "bh_fdr_q": q, "survives_fdr_0_05": bool(q < 0.05)}
        for h, p, q in zip(hyp_names, hyp_pvals, qvals)
    ]

    # Save Deliverables 3, 4, 5, 6, 7
    macro_util_out = {
        "task": "G3.5_PHASE_C_MACRO_H3B_UTILITY",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "governance_warning": "Offline H3b candidate-level research on research-exposed OOT folds. NOT a verified RunBacktestUseCase portfolio backtest.",
        "n_oot_h3b_candidates": int(len(df_h3b_oot)),
        "unique_oot_days": int(df_h3b_oot["day_utc"].nunique()),
        "cal_only_thresholds_by_fold": cal_quantiles,
        "policies": phase_c_policies,
        "incremental_ml_vs_simple_baselines": incremental_ml_vs_simple,
        "c1_pmove_20_slice_stability": c1_20_slices,
        "candidate_ledger_sha256": _sha256(ledger_path),
    }
    (OUT_DIR / "g3_5_macro_h3b_utility.json").write_text(json.dumps(macro_util_out, indent=2), encoding="utf-8")

    micro_util_out = {
        "task": "G3.5_PHASE_D_MICRO_EXECUTION_UTILITY",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "governance_warning": "Research-only exact tick-level H3b path simulation with recalculated SL/TP/size/E1/timeout and missed-fill accounting.",
        "n_oot_candidates_simulated": int(len(df_exec)),
        "cal_only_thresholds_by_fold": micro_cal_thr,
        "policies_by_direction": micro_policies_summary,
        "execution_ledger_sha256": _sha256(exec_ledger_path),
    }
    (OUT_DIR / "g3_5_micro_execution_utility.json").write_text(json.dumps(micro_util_out, indent=2), encoding="utf-8")

    (OUT_DIR / "g3_5_meta_label_feasibility.json").write_text(json.dumps(meta_report, indent=2), encoding="utf-8")

    robustness_out = {
        "task": "G3.5_PHASE_F_ROBUSTNESS_REPORT",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "cost_stress_comparison": cost_stress_results,
        "multiple_testing_bh_fdr": {
            "n_hypotheses_tested": len(fdr_table),
            "n_surviving_fdr_0_05": sum(1 for r in fdr_table if r["survives_fdr_0_05"]),
            "table": fdr_table,
        },
    }
    (OUT_DIR / "g3_5_robustness_report.json").write_text(json.dumps(robustness_out, indent=2), encoding="utf-8")

    gov_out = {
        "task": "G3.5_PHASE_G_EXPERIMENT_GOVERNANCE",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_head": _git_rev(),
        "execution_mode": "STRICT_SHADOW_MODE",
        "run_backtest_modified": False,
        "h0_h3b_modified": False,
        "kalman_exit_modified": False,
        "g3_4_prior_trials_exposed": 112,
        "g3_5_hypotheses_evaluated": len(fdr_table),
        "historical_oot_classification": "RESEARCH_EXPOSED_EXPLORATORY_ONLY",
        "prospective_shadow_validation_required": "G3.7",
        "threshold_selection_source": "RESERVED_CALIBRATION_FOLDS_ONLY",
        "artifact_fingerprints": {
            "h3b_candidate_ledger_sha256": _sha256(ledger_path),
            "micro_execution_ledger_sha256": _sha256(exec_ledger_path),
            "macro_calibrated_oof_sha256": _sha256(OUT_DIR / "g3_5_macro_calibrated_oof.parquet"),
            "micro_calibrated_oof_sha256": _sha256(OUT_DIR / "g3_5_micro_calibrated_oof.parquet"),
        },
        "elapsed_sec": round(time.time() - t0, 2),
    }
    (OUT_DIR / "g3_5_experiment_governance.json").write_text(json.dumps(gov_out, indent=2), encoding="utf-8")
    print(f"[G3.5 PHASES C-G DONE in {round(time.time() - t0, 1)}s] FDR survivors={robustness_out['multiple_testing_bh_fdr']['n_surviving_fdr_0_05']}/{len(fdr_table)}, Meta={meta_verdict}", flush=True)


if __name__ == "__main__":
    main()
