"""
TASK G3.6R — Phases C, D & F: Controlled Actual-Application Backtest Experiments,
Subperiod Robustness, Paired Daily Statistical Controls & Cost Stress (1.0x/1.5x/2.0x).

Executes ACTUAL `RunBacktestUseCase.execute(...)` continuously across the full
6.2-month verified dataset (`2026-05-17 00:00:00Z .. 2026-11-19 12:00:00Z`, 4d warmup)
for all 5 comparison groups:
  1. H0_ORIGINAL           (17:00 UTC, no E1, default)
  2. H3B_FROZEN            (15:00 UTC, E1 15m < 0.4 ATR, default)
  3. H3B_CHANGE_A_ER50     (H3b + opt-in max_kaufman_er=0.50)
  4. H3B_CHANGE_B_BE14     (H3b + opt-in be_trigger_atr=1.4)
  5. H3B_CHANGE_C_SCORE68  (H3b + opt-in min_score_threshold=0.68)

STRICT SHADOW MODE. Frozen H0 & H3b defaults remain 100% unchanged.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd
from sqlalchemy import text

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")

from src.goldedge.adapters.secondary.ml.calibration_utility import (
    benjamini_hochberg_fdr,
    day_block_paired_diff_ci,
    paired_day_sign_permutation_p,
)
from src.goldedge.adapters.secondary.virtual_broker import VirtualBrokerAdapter
from src.goldedge.application.use_cases.run_backtest import RunBacktestUseCase
from src.goldedge.config.container import Container
from src.goldedge.config.risk_profiles import get_profile
from src.goldedge.domain.services.broker_cost_service import (
    BrokerCostService,
    CommissionSchedule,
    SwapSchedule,
)
from src.goldedge.domain.services.sizing_service import PositionSizingService

OUT_DIR = Path("backtest_results/audit/task_g3_6r_h3b_profitability_improvement")
RUNS_DIR = OUT_DIR / "runs"

START_DT = datetime(2026, 5, 17, 0, 0, tzinfo=timezone.utc)
END_DT = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


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


CONFIGS: List[Dict[str, Any]] = [
    {
        "name": "H0_ORIGINAL",
        "session_close_utc": 17,
        "early_fail_mins": None,
        "early_fail_min_mfe_atr": None,
        "min_score_threshold": 0.60,
        "max_kaufman_er": None,
        "be_trigger_atr": 1.0,
    },
    {
        "name": "H3B_FROZEN",
        "session_close_utc": 15,
        "early_fail_mins": 15,
        "early_fail_min_mfe_atr": 0.4,
        "min_score_threshold": 0.60,
        "max_kaufman_er": None,
        "be_trigger_atr": 1.0,
    },
    {
        "name": "H3B_CHANGE_A_ER50",
        "session_close_utc": 15,
        "early_fail_mins": 15,
        "early_fail_min_mfe_atr": 0.4,
        "min_score_threshold": 0.60,
        "max_kaufman_er": 0.50,
        "be_trigger_atr": 1.0,
    },
    {
        "name": "H3B_CHANGE_B_BE14",
        "session_close_utc": 15,
        "early_fail_mins": 15,
        "early_fail_min_mfe_atr": 0.4,
        "min_score_threshold": 0.60,
        "max_kaufman_er": None,
        "be_trigger_atr": 1.4,
    },
    {
        "name": "H3B_CHANGE_C_SCORE68",
        "session_close_utc": 15,
        "early_fail_mins": 15,
        "early_fail_min_mfe_atr": 0.4,
        "min_score_threshold": 0.68,
        "max_kaufman_er": None,
        "be_trigger_atr": 1.0,
    },
]


def _run_one_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    t0 = time.time()
    name = cfg["name"]
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    pq_path = RUNS_DIR / f"{name}_trades.parquet"
    meta_path = RUNS_DIR / f"{name}_summary.json"

    container = Container()
    engine = container.db_engine()
    repo = Expanded6mReadOnlyTickRepo(engine)
    broker = VirtualBrokerAdapter()
    sizing = PositionSizingService()
    prof = replace(
        get_profile("MEDIUM_BACKTEST"),
        risk_per_trade_pct=0.5,
        max_daily_loss_pct=1.5,
        min_score_threshold=float(cfg["min_score_threshold"]),
    )
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

    buf = io.StringIO()
    with redirect_stdout(buf):
        res = bt.execute(
            symbol="XAUUSD",
            start=START_DT,
            end=END_DT,
            initial_capital=10000.0,
            use_kalman_exit=False,
            session_close_utc=cfg["session_close_utc"],
            early_fail_mins=cfg["early_fail_mins"],
            early_fail_min_mfe_atr=cfg["early_fail_min_mfe_atr"],
            slippage_pips=0.0533,
            cost_service=cost_svc,
            max_hwm_drawdown_pct=8.0,
            export_artifacts=False,
            max_kaufman_er=cfg["max_kaufman_er"],
            be_trigger_atr=cfg["be_trigger_atr"],
        )

    rows = []
    for tr in res.get("trade_list", []):
        et = pd.to_datetime(tr["entry_time"], utc=True)
        xt = pd.to_datetime(tr["exit_time"], utc=True)
        ep = float(tr["entry_price"])
        tp = float(tr["target"])
        true_atr = abs(tp - ep) / 3.0
        prisk = max(1e-6, float(tr.get("planned_risk_usd", 0.0)))
        pnl = float(tr["pnl"])
        rows.append({
            "entry_time_utc": et.isoformat(),
            "exit_time_utc": xt.isoformat(),
            "entry_ts": float(et.timestamp()),
            "exit_ts": float(xt.timestamp()),
            "day_utc": et.strftime("%Y-%m-%d"),
            "subperiod": "DEV_MAY_AUG" if et.strftime("%Y-%m-%d") < "2026-09-01" else "LATER_SEP_NOV",
            "direction": str(tr["direction"]).upper(),
            "strategy": str(tr.get("strategy", "")),
            "regime": str(tr.get("regime", "UNKNOWN")),
            "score": float(tr.get("score", 0.0)),
            "true_initial_atr": round(true_atr, 5),
            "entry_price": ep,
            "exit_price": float(tr["exit_price"]),
            "final_sl": float(tr["sl"]),
            "tp": tp,
            "volume": float(tr["qty"]),
            "commission_usd": float(tr.get("commission_usd", 0.0)),
            "slippage_usd": float(tr.get("slippage_usd", 0.0)),
            "planned_risk_usd": round(prisk, 4),
            "net_pnl_usd": round(pnl, 4),
            "net_r": round(pnl / prisk, 5),
            "exit_reason": str(tr.get("exit_reason", "")),
        })
    df_t = pd.DataFrame(rows)
    df_t.to_parquet(pq_path, index=False)

    summary = {
        "name": name,
        "config": cfg,
        "trades": int(res["trades"]),
        "unique_days": int(df_t["day_utc"].nunique()) if len(df_t) else 0,
        "win_rate_pct": round(float(res["win_rate"]), 2),
        "net_pnl_usd": round(float(res["pnl"]), 2),
        "final_equity_usd": round(float(res["final_equity"]), 2),
        "total_net_r": round(float(df_t["net_r"].sum()), 4) if len(df_t) else 0.0,
        "mean_net_r": round(float(df_t["net_r"].mean()), 5) if len(df_t) else 0.0,
        "pf": round(float(res["pf"]), 3),
        "closed_max_dd_usd": round(float(res["closed_max_dd"]), 2),
        "closed_max_dd_pct": round(float(res["closed_max_dd_pct"]), 2),
        "mtm_max_dd_usd": round(float(res["mtm_max_dd"]), 2),
        "mtm_max_dd_pct": round(float(res["mtm_max_dd_pct"]), 2),
        "hwm_kill_switch_halted": bool((res.get("hwm_kill_switch") or {}).get("halted", False)),
        "total_commission_usd": round(float(res["total_commission_usd"]), 2),
        "total_slippage_usd": round(float(res["total_slippage_usd"]), 2),
        "trades_parquet": str(pq_path),
        "trades_parquet_sha256": _sha256(pq_path),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    meta_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def _slice_stats(df: pd.DataFrame, pnl_col: str = "net_pnl_usd", r_col: str = "net_r") -> Dict[str, Any]:
    if df.empty:
        return {"n": 0, "net_pnl_usd": 0.0, "total_net_r": 0.0, "pf": 0.0, "win_rate_pct": 0.0}
    w = df[df[pnl_col] > 0.0]
    l = df[df[pnl_col] <= 0.0]
    gp = float(w[pnl_col].sum())
    gl = abs(float(l[pnl_col].sum()))
    pf = round(gp / gl, 3) if gl > 1e-9 else (999.0 if gp > 0 else 0.0)
    return {
        "n": int(len(df)),
        "unique_days": int(df["day_utc"].nunique()),
        "win_rate_pct": round(100.0 * len(w) / len(df), 2),
        "net_pnl_usd": round(float(df[pnl_col].sum()), 2),
        "total_net_r": round(float(df[r_col].sum()), 4),
        "mean_net_r": round(float(df[r_col].mean()), 5),
        "pf": pf,
    }


def main() -> None:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    # Run all 5 full 6.2-month actual RunBacktestUseCase configs in parallel (3 workers)
    summaries: Dict[str, Dict[str, Any]] = {}
    with ProcessPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(_run_one_config, cfg): cfg["name"] for cfg in CONFIGS}
        for fut in as_completed(futs):
            nm = futs[fut]
            s = fut.result()
            summaries[nm] = s
            print(
                f"[RUN DONE] {nm:22s} | trds={s['trades']:3d} | WR={s['win_rate_pct']:5.1f}% | "
                f"PnL=${s['net_pnl_usd']:+8.2f} | NetR={s['total_net_r']:+7.2f} | PF={s['pf']:5.2f} | "
                f"MTM_DD={s['mtm_max_dd_pct']:4.2f}% ({s['elapsed_sec']}s)",
                flush=True,
            )

    # Load trade tables & compute paired daily statistics vs H3B_FROZEN
    dfs = {nm: pd.read_parquet(RUNS_DIR / f"{nm}_trades.parquet") for nm in summaries}
    all_days = sorted(set().union(*(set(d["day_utc"].unique()) for d in dfs.values())))
    day_arr = np.array(all_days)

    def _daily_vec(df: pd.DataFrame, col: str) -> np.ndarray:
        s = df.groupby("day_utc")[col].sum()
        return np.array([float(s.get(d, 0.0)) for d in all_days], dtype=np.float64)

    h3b_daily_usd = _daily_vec(dfs["H3B_FROZEN"], "net_pnl_usd")
    h3b_daily_r = _daily_vec(dfs["H3B_FROZEN"], "net_r")

    paired_vs_h3b: Dict[str, Any] = {}
    hyp_names, hyp_pvals = [], []
    for nm in ("H3B_CHANGE_A_ER50", "H3B_CHANGE_B_BE14", "H3B_CHANGE_C_SCORE68"):
        c_usd = _daily_vec(dfs[nm], "net_pnl_usd")
        c_r = _daily_vec(dfs[nm], "net_r")
        ci_usd = day_block_paired_diff_ci(c_usd, h3b_daily_usd, day_arr, lower_is_better=False)
        ci_r = day_block_paired_diff_ci(c_r, h3b_daily_r, day_arr, lower_is_better=False)
        p_usd = paired_day_sign_permutation_p(c_usd, h3b_daily_usd, day_arr, n_perm=4000, seed=42)
        p_r = paired_day_sign_permutation_p(c_r, h3b_daily_r, day_arr, n_perm=4000, seed=42)
        hyp_names.append(nm)
        hyp_pvals.append(p_r)
        paired_vs_h3b[nm] = {
            "delta_net_pnl_usd": round(summaries[nm]["net_pnl_usd"] - summaries["H3B_FROZEN"]["net_pnl_usd"], 2),
            "delta_total_r": round(summaries[nm]["total_net_r"] - summaries["H3B_FROZEN"]["total_net_r"], 4),
            "delta_pf": round(summaries[nm]["pf"] - summaries["H3B_FROZEN"]["pf"], 3),
            "delta_mtm_dd_pct": round(summaries[nm]["mtm_max_dd_pct"] - summaries["H3B_FROZEN"]["mtm_max_dd_pct"], 2),
            "coverage_vs_h3b_pct": round(100.0 * summaries[nm]["trades"] / max(1, summaries["H3B_FROZEN"]["trades"]), 2),
            "paired_daily_usd": ci_usd,
            "paired_daily_r": ci_r,
            "day_perm_p_usd": p_usd,
            "day_perm_p_r": p_r,
        }

    qvals = benjamini_hochberg_fdr(hyp_pvals)
    for nm, q in zip(hyp_names, qvals):
        paired_vs_h3b[nm]["bh_fdr_q_r"] = q
        paired_vs_h3b[nm]["survives_fdr_0_05"] = bool(q < 0.05)

    # Subperiod & BUY/SELL stability
    subperiod_out: Dict[str, Any] = {}
    for nm in [c["name"] for c in CONFIGS]:
        d = dfs[nm]
        subperiod_out[nm] = {
            "DEV_MAY_AUG": _slice_stats(d[d["subperiod"] == "DEV_MAY_AUG"]),
            "LATER_SEP_NOV": _slice_stats(d[d["subperiod"] == "LATER_SEP_NOV"]),
            "BUY": _slice_stats(d[d["direction"] == "BUY"]),
            "SELL": _slice_stats(d[d["direction"] == "SELL"]),
        }

    # Cost stress (1.0x / 1.5x / 2.0x commission + slippage)
    cost_stress_out: Dict[str, Any] = {}
    for mult_lbl, mult in (("NORMAL_1_0X", 1.0), ("MODERATE_1_5X", 1.5), ("SEVERE_2_0X", 2.0)):
        cost_stress_out[mult_lbl] = {}
        for nm in [c["name"] for c in CONFIGS]:
            d = dfs[nm].copy()
            extra = (mult - 1.0) * (d["commission_usd"] + d["slippage_usd"])
            d["s_pnl"] = d["net_pnl_usd"] - extra
            d["s_r"] = d["s_pnl"] / np.maximum(1e-6, d["planned_risk_usd"])
            cost_stress_out[mult_lbl][nm] = _slice_stats(d, pnl_col="s_pnl", r_col="s_r")

    # Save Deliverables 6, 7, 8, 9
    exp_out = {
        "task": "G3.6R_PHASE_C_CONTROLLED_EXPERIMENTS",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "period_utc": [START_DT.isoformat(), END_DT.isoformat()],
        "governance_notice": "All May-Nov 2026 historical runs are RESEARCH_EXPOSED (not untouched OOS). Executed via actual RunBacktestUseCase.",
        "runs": {c["name"]: summaries[c["name"]] for c in CONFIGS},
        "paired_vs_frozen_h3b": paired_vs_h3b,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    (OUT_DIR / "g3_6r_controlled_experiments.json").write_text(json.dumps(exp_out, indent=2), encoding="utf-8")
    (OUT_DIR / "g3_6r_subperiod_robustness.json").write_text(json.dumps({"task": "G3.6R_SUBPERIOD_ROBUSTNESS", "generated_at_utc": datetime.now(timezone.utc).isoformat(), "results": subperiod_out}, indent=2), encoding="utf-8")
    (OUT_DIR / "g3_6r_cost_stress.json").write_text(json.dumps({"task": "G3.6R_COST_STRESS", "generated_at_utc": datetime.now(timezone.utc).isoformat(), "results": cost_stress_out}, indent=2), encoding="utf-8")

    impl_manifest = {
        "task": "G3.6R_APPLICATION_IMPLEMENTATION_MANIFEST",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_head": _git_rev(),
        "modified_application_file": "src/goldedge/application/use_cases/run_backtest.py",
        "run_backtest_sha256": _sha256(Path("src/goldedge/application/use_cases/run_backtest.py")),
        "opt_in_parameters_added": {
            "max_kaufman_er": "float | None = None (default OFF; preserves H0 & frozen H3b bit-identically)",
            "be_trigger_atr": "float = 1.0 (default 1.0; preserves H0 & frozen H3b bit-identically)",
        },
        "shadow_and_live_unchanged": True,
        "frozen_defaults_preserved": True,
    }
    (OUT_DIR / "g3_6r_application_implementation_manifest.json").write_text(json.dumps(impl_manifest, indent=2), encoding="utf-8")
    print(f"\n[G3.6R PHASES C-D-F DONE in {round(time.time() - t0, 1)}s]")


if __name__ == "__main__":
    main()
