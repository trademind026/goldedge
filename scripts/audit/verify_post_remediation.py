"""
Post-Remediation Verification (Task 2 — B1 through B6).

Runs the remediated RunBacktestUseCase over TimescaleDB XAUUSD ticks
(2026-08-24..2026-11-16 with 2026-08-20..24 warmup) and compares against:
  1. Task 1 Pre-Remediation run (current_engine_trades.csv & lookahead_kalman_audit.csv).
  2. Independent Strict Causal Simulator (reconciling entry/exit/P&L on remediated signals).
  3. Candidate (15:00 UTC + E1) & realistic cost stress ($7/lot comm + 0.3p slip).

Saves outputs to backtest_results/audit/post_remediation_*.
Never modifies frozen_111_baseline_trades.csv or strategy parameters.
"""
import contextlib
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from goldedge.adapters.secondary.db.tick_repository import TickRepository
from goldedge.adapters.secondary.virtual_broker import VirtualBrokerAdapter
from goldedge.application.use_cases.run_backtest import RunBacktestUseCase
from goldedge.config.risk_profiles import MEDIUM_RISK_BACKTEST
from goldedge.domain.services.sizing_service import PositionSizingService
from phase5_6_7_reconcile import IndependentExecutionSimulator, _compute_metrics

RAW_DB_URL = os.environ.get("DB_URL", "postgresql+psycopg2://postgres:password@timescaledb:5432/goldedge")
if RAW_DB_URL.startswith("postgresql://"):
    RAW_DB_URL = RAW_DB_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

OUT_DIR = Path("backtest_results/audit")
FROZEN_PATH = Path("backtest_results/frozen_111_baseline_trades.csv")
EXPECTED_FROZEN_SHA256 = "73306e1529adc48284bce9ab6ac35122f33b9999d2464af441867eb3bb6cf168"


def _summarize_res(res: dict) -> dict:
    return {
        "trades": res["trades"],
        "win_rate_pct": round(res["win_rate"] * 100.0, 2),
        "pnl": round(res["pnl"], 2),
        "closed_max_dd": round(res["closed_max_dd"], 2),
        "closed_max_dd_pct": round(res["closed_max_dd_pct"], 2),
        "mtm_max_dd": round(res["mtm_max_dd"], 2),
        "mtm_max_dd_pct": round(res["mtm_max_dd_pct"], 2),
        "max_planned_risk_pct": round(res["max_planned_risk_pct"], 3),
        "max_realized_loss_pct": round(res["max_realized_loss_pct"], 3),
        "pf": round(res["pf"], 4),
        "avg_win": round(res["avg_win"], 2),
        "avg_loss": round(res["avg_loss"], 2),
        "rr": round(res["rr"], 4),
        "sl_slippage_events": res["sl_slippage_events"],
        "total_sl_gap_usd": round(res["total_sl_gap_usd"], 2),
        "worst_sl_gap_usd": round(res["worst_sl_gap_usd"], 2),
        "inverted_quotes_skipped": res["inverted_quotes_skipped"],
        "zero_spread_ticks_skipped": res["zero_spread_ticks_skipped"],
        "total_commission_usd": round(res["total_commission_usd"], 2),
        "total_slippage_usd": round(res["total_slippage_usd"], 2),
        "total_swap_usd": round(res["total_swap_usd"], 2),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    frozen_sha = hashlib.sha256(FROZEN_PATH.read_bytes()).hexdigest()
    assert frozen_sha == EXPECTED_FROZEN_SHA256, f"Frozen artifact modified: {frozen_sha}"

    engine = create_engine(RAW_DB_URL)
    tick_repo = TickRepository(engine)
    start = datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc)

    def _run(session_close=None, early_mins=None, early_mfe=None, comm=0.0, slip=0.0):
        uc = RunBacktestUseCase(
            tick_repo=tick_repo,
            virtual_broker=VirtualBrokerAdapter(initial_equity=10000.0),
            manage_position=None,
            sizing_service=PositionSizingService(contract_size=100.0),
            profile=MEDIUM_RISK_BACKTEST,
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return uc.execute(
                "XAUUSD",
                start,
                end,
                initial_capital=10000.0,
                use_kalman_exit=False,
                session_close_utc=session_close,
                early_fail_mins=early_mins,
                early_fail_min_mfe_atr=early_mfe,
                commission_per_lot=comm,
                slippage_pips=slip,
            )

    print("[1/4] Running Remediated Baseline (17:00 UTC, $0 comm)...")
    res_base = _run()
    df_base = pd.DataFrame(res_base["trade_list"])
    df_base.to_csv(OUT_DIR / "post_remediation_baseline_trades.csv", index=False)

    print("[2/4] Running Remediated Candidate (15:00 UTC + E1, $0 comm)...")
    res_cand = _run(session_close=15, early_mins=15, early_mfe=0.4)
    df_cand = pd.DataFrame(res_cand["trade_list"])
    df_cand.to_csv(OUT_DIR / "post_remediation_candidate_trades.csv", index=False)

    print("[3/4] Running Remediated Baseline Cost Stress ($7/lot RT comm + 0.3p slip)...")
    res_base_cost = _run(comm=7.0, slip=0.3)

    print("[4/4] Reconciling Remediated Baseline vs Independent Strict Causal Simulator...")
    signals_for_sim = [
        {
            "entry_time": t["entry_time"],
            "direction": t["direction"],
            "strategy": t["strategy"],
            "entry_price": t["entry_price"],
            "sl": t["sl"],
        }
        for t in res_base["trade_list"]
    ]
    sim_strict = IndependentExecutionSimulator(engine, mode="strict_causal").run(start, end, signals_for_sim)

    max_entry_diff = 0.0
    max_exit_diff = 0.0
    max_pnl_diff = 0.0
    qty_mismatches = 0
    for o, s_t in zip(res_base["trade_list"], sim_strict):
        max_entry_diff = max(max_entry_diff, abs(float(o["entry_price"]) - float(s_t["entry_price"])))
        max_exit_diff = max(max_exit_diff, abs(float(o["exit_price"]) - float(s_t["exit_price"])))
        max_pnl_diff = max(max_pnl_diff, abs(float(o["pnl"]) - float(s_t["pnl"])))
        if abs(float(o["qty"]) - float(s_t["qty"])) > 1e-6:
            qty_mismatches += 1

    # Compare Task 1 pre-remediation trades vs Task 2 remediated baseline trades
    pre_path = OUT_DIR / "current_engine_trades.csv"
    df_pre = pd.read_csv(pre_path) if pre_path.exists() else pd.DataFrame()
    pre_keys = set(zip(df_pre["entry_time"].astype(str).str[:16], df_pre["direction"], df_pre["strategy"])) if not df_pre.empty else set()
    post_keys = set(zip(df_base["entry_time"].astype(str).str[:16], df_base["direction"], df_base["strategy"])) if not df_base.empty else set()

    # Build before/after trade diff report
    diff_rows = []
    pre_map = {
        (str(r["entry_time"])[:16], str(r["direction"]), str(r["strategy"])): r
        for _, r in df_pre.iterrows()
    } if not df_pre.empty else {}
    post_map = {
        (str(r["entry_time"])[:16], str(r["direction"]), str(r["strategy"])): r
        for _, r in df_base.iterrows()
    } if not df_base.empty else {}

    for k in sorted(pre_keys | post_keys):
        in_pre = k in pre_map
        in_post = k in post_map
        pr = pre_map.get(k)
        po = post_map.get(k)
        status = "KEPT" if (in_pre and in_post) else ("REMOVED_LOOKAHEAD_OR_DRIFT" if in_pre else "ADDED_CAUSAL")
        diff_rows.append({
            "minute_utc": k[0],
            "direction": k[1],
            "strategy": k[2],
            "status": status,
            "pre_pnl": round(float(pr["pnl"]), 2) if in_pre else None,
            "post_pnl": round(float(po["pnl"]), 2) if in_post else None,
            "pre_reason": pr["exit_reason"] if in_pre else None,
            "post_reason": po["exit_reason"] if in_post else None,
        })
    pd.DataFrame(diff_rows).to_csv(OUT_DIR / "post_remediation_signal_diff.csv", index=False)

    summary = {
        "frozen_baseline_sha256": frozen_sha,
        "frozen_baseline_intact": frozen_sha == EXPECTED_FROZEN_SHA256,
        "remediated_baseline_17utc_zero_cost": _summarize_res(res_base),
        "remediated_baseline_independent_metrics": _compute_metrics(res_base["trade_list"]),
        "independent_strict_causal_reconciliation": {
            "count_remediated": len(res_base["trade_list"]),
            "count_independent_strict": len(sim_strict),
            "max_entry_price_diff": round(max_entry_diff, 6),
            "max_exit_price_diff": round(max_exit_diff, 6),
            "max_pnl_diff_usd": round(max_pnl_diff, 6),
            "qty_mismatches": qty_mismatches,
        },
        "before_vs_after_trade_decisions": {
            "pre_remediation_trades": len(pre_keys),
            "post_remediation_trades": len(post_keys),
            "kept_trades": len(pre_keys & post_keys),
            "removed_trades": len(pre_keys - post_keys),
            "added_trades": len(post_keys - pre_keys),
        },
        "remediated_candidate_15utc_e1_zero_cost": _summarize_res(res_cand),
        "remediated_candidate_independent_metrics": _compute_metrics(res_cand["trade_list"]),
        "remediated_baseline_cost_stress_7usd_03p": _summarize_res(res_base_cost),
    }

    (OUT_DIR / "post_remediation_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
