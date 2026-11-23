"""
TASK G3.5R — Phases A, B & C:
  - Phase A: Freeze & hash all original G3.5 evidence BEFORE any code modification
             (`g3_5r_original_evidence_manifest.json`)
  - Phase B: Deterministic 112-trade candidate universe audit & trade-by-trade mismatch classification
             (`g3_5r_candidate_universe_audit.json`, `g3_5r_trade_by_trade_reconciliation.parquet`)
  - Phase C: E1 / BE / TIME_SQUEEZE / SL / TP event timeline & root-cause breakdown
             (`g3_5r_e1_event_timeline.json`)
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

OUT_DIR = Path("backtest_results/audit/task_g3_5r_h3b_execution_reconciliation")
G35_DIR = Path("backtest_results/audit/task_g3_5_calibration_utility")


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


def main() -> None:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # PHASE A: Freeze Original Evidence Manifest (BEFORE any fixes)
    # ------------------------------------------------------------------
    files_to_hash = [
        "src/goldedge/application/use_cases/run_backtest.py",
        "src/goldedge/domain/services/sizing_service.py",
        "src/goldedge/domain/services/broker_cost_service.py",
        "src/goldedge/domain/services/signal_generator.py",
        "src/goldedge/domain/services/denoise_service.py",
        "src/goldedge/domain/services/hwm_kill_switch_service.py",
        "src/goldedge/config/risk_profiles.py",
        "src/goldedge/config/settings.py",
        "src/goldedge/adapters/secondary/ml/calibration_utility.py",
        "scripts/audit/task_g3_5_phase_cdefg_utility.py",
        "backtest_results/frozen_111_baseline_trades.csv",
        "backtest_results/audit/task_g1_root_cause/g1_original_70_baseline_trades.csv",
        "backtest_results/audit/task_g3_5_calibration_utility/g3_5_h3b_all_6m_trades.parquet",
        "backtest_results/audit/task_g3_5_calibration_utility/g3_5_h3b_candidate_ledger.parquet",
        "backtest_results/audit/task_g3_5_calibration_utility/g3_5_micro_execution_ledger.parquet",
        "backtest_results/audit/task_g3_5_calibration_utility/g3_5_macro_h3b_utility.json",
        "backtest_results/audit/task_g3_5_calibration_utility/g3_5_micro_execution_utility.json",
    ]
    hashes = {f: _sha256(Path(f)) for f in files_to_hash if Path(f).exists()}

    manifest = {
        "task": "G3.5R_PHASE_A_ORIGINAL_EVIDENCE_MANIFEST",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_head": _git_rev(),
        "safety_locks": {
            "SHADOW_MODE": True,
            "TRADING_MODE": "demo",
            "market_orders": 0,
            "market_trades": 0,
            "HWM_MAX_DRAWDOWN_PCT": 8.0,
        },
        "implementation_paths": {
            "application_backtest": "src/goldedge/application/use_cases/run_backtest.py",
            "signal_generator": "src/goldedge/domain/services/signal_generator.py",
            "position_sizing": "src/goldedge/domain/services/sizing_service.py",
            "broker_costs": "src/goldedge/domain/services/broker_cost_service.py",
            "kalman_filter": "src/goldedge/domain/services/denoise_service.py",
            "hwm_kill_switch": "src/goldedge/domain/services/hwm_kill_switch_service.py",
            "g3_5_research_simulator": "src/goldedge/adapters/secondary/ml/calibration_utility.py",
            "g3_5_extraction_driver": "scripts/audit/task_g3_5_phase_cdefg_utility.py",
        },
        "original_file_sha256": hashes,
    }
    (OUT_DIR / "g3_5r_original_evidence_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # PHASE B: Match Exact 112 Opportunities & Classify Every Mismatch
    # ------------------------------------------------------------------
    c = pd.read_parquet(G35_DIR / "g3_5_h3b_candidate_ledger.parquet").sort_values("entry_ts").reset_index(drop=True)
    e = pd.read_parquet(G35_DIR / "g3_5_micro_execution_ledger.parquet").sort_values("entry_ts").reset_index(drop=True)

    same_count = len(c) == len(e) == 112
    same_ts = bool((c["entry_ts"].to_numpy() == e["entry_ts"].to_numpy()).all())
    same_dir = bool((c["direction"].to_numpy() == e["direction"].to_numpy()).all())

    recon_rows: List[Dict[str, Any]] = []
    class_counts: Dict[str, int] = {}
    atr_corrupted_count = 0
    atr_corrupted_diff_r = 0.0

    for i in range(len(c)):
        cr = c.iloc[i]
        er = e.iloc[i]
        opp_id = f"H3B_{i+1:03d}_{cr['day_utc']}_{cr['direction']}"
        true_initial_atr = abs(float(cr["tp"]) - float(cr["entry_price"])) / 3.0
        used_atr = float(cr["atr"])
        atr_err = abs(used_atr - true_initial_atr)
        atr_corrupted = atr_err > 1e-3

        app_r = float(cr["net_r"])
        sim_r = float(er["imm_net_r"])
        diff_r = sim_r - app_r
        app_exit = str(cr["exit_reason"])
        sim_exit = str(er["imm_exit_reason"])

        if atr_corrupted:
            atr_corrupted_count += 1
            atr_corrupted_diff_r += diff_r
            mismatch_cls = "SIZING_MISMATCH"  # Corrupted ATR -> wrong SL/TP/size/commission & wrong exit
        elif app_exit == "EARLY_FAIL_15M" and sim_exit == "EARLY_FAIL" and abs(diff_r) > 1e-3:
            mismatch_cls = "E1_MISMATCH"
        elif app_exit == "TARGET" and sim_exit == "TP_HIT" and abs(diff_r) > 1e-3:
            mismatch_cls = "COST_MISMATCH"  # Simulator charged exit slippage on limit TP
        elif app_exit != sim_exit and not (app_exit == "SL" and sim_exit == "SL_HIT") and not (app_exit == "EARLY_FAIL_15M" and sim_exit == "EARLY_FAIL") and not (app_exit == "TARGET" and sim_exit == "TP_HIT"):
            mismatch_cls = "EXIT_MISMATCH"
        elif abs(diff_r) > 1e-3:
            mismatch_cls = "R_ACCOUNTING_MISMATCH"
        else:
            mismatch_cls = "EXACT_MATCH"

        class_counts[mismatch_cls] = class_counts.get(mismatch_cls, 0) + 1
        recon_rows.append({
            "opp_id": opp_id,
            "entry_time_utc": str(cr["entry_time_utc"]),
            "exit_time_utc_app": str(cr["exit_time_utc"]),
            "entry_ts": float(cr["entry_ts"]),
            "day_utc": str(cr["day_utc"]),
            "direction": str(cr["direction"]),
            "session_utc": str(cr["session_utc"]),
            "regime": str(cr["regime"]),
            "true_initial_atr": round(true_initial_atr, 5),
            "g35_corrupted_atr": round(used_atr, 5),
            "atr_corrupted": bool(atr_corrupted),
            "app_entry_price": float(cr["entry_price"]),
            "app_exit_price": float(cr["exit_price"]),
            "app_tp": float(cr["tp"]),
            "app_final_sl": float(cr["initial_sl"]),
            "app_volume": float(cr["volume"]),
            "app_comm_usd": float(cr["commission_usd"]),
            "sim_comm_usd": float(er["imm_comm_usd"]),
            "app_planned_risk": float(cr["planned_risk_usd"]),
            "sim_planned_risk": float(er["imm_planned_risk"]),
            "app_net_pnl": float(cr["net_pnl_usd"]),
            "sim_net_pnl": float(er["imm_net_pnl"]),
            "app_net_r": round(app_r, 6),
            "sim_net_r": round(sim_r, 6),
            "diff_r_sim_minus_app": round(diff_r, 6),
            "app_exit_reason": app_exit,
            "sim_exit_reason": sim_exit,
            "mismatch_class": mismatch_cls,
        })

    df_recon = pd.DataFrame(recon_rows)
    recon_pq = OUT_DIR / "g3_5r_trade_by_trade_reconciliation.parquet"
    df_recon.to_parquet(recon_pq, index=False)

    universe_audit = {
        "task": "G3.5R_PHASE_B_CANDIDATE_UNIVERSE_AUDIT",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "original_counts": {"app_candidates": int(len(c)), "sim_candidates": int(len(e))},
        "exact_112_universe_identical": bool(same_count and same_ts and same_dir),
        "exact_timestamp_match": same_ts,
        "exact_direction_match": same_dir,
        "unique_days": int(c["day_utc"].nunique()),
        "unrounded_totals": {
            "app_total_r": round(float(c["net_r"].sum()), 6),
            "app_mean_r": round(float(c["net_r"].mean()), 6),
            "sim_total_r": round(float(e["imm_net_r"].sum()), 6),
            "sim_mean_r": round(float(e["imm_net_r"].mean()), 6),
            "discrepancy_sim_minus_app_r": round(float((e["imm_net_r"] - c["net_r"]).sum()), 6),
        },
        "mismatch_classification_counts": class_counts,
        "atr_corruption_summary": {
            "n_trades_with_corrupted_atr": int(atr_corrupted_count),
            "pct_of_112_trades": round(100.0 * atr_corrupted_count / len(c), 2),
            "total_diff_r_from_atr_corrupted_trades": round(float(atr_corrupted_diff_r), 6),
            "total_diff_r_from_non_corrupted_trades": round(float((e["imm_net_r"] - c["net_r"]).sum() - atr_corrupted_diff_r), 6),
        },
        "reconciliation_parquet_sha256": _sha256(recon_pq),
    }
    (OUT_DIR / "g3_5r_candidate_universe_audit.json").write_text(json.dumps(universe_audit, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # PHASE C: E1 / Exit Semantics & Event Timeline Breakdown
    # ------------------------------------------------------------------
    e1_rows = df_recon[(df_recon["app_exit_reason"] == "EARLY_FAIL_15M") | (df_recon["sim_exit_reason"] == "EARLY_FAIL")]
    e1_timeline = {
        "task": "G3.5R_PHASE_C_E1_EVENT_TIMELINE",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "canonical_app_e1_semantics": {
            "trigger_event": "Completed 1-minute bar close when bars_held == 15 (post-entry completed minute bars)",
            "evaluation_order": "1. Invalid quote guard (b<=0, a<=0, b>=a) -> 2. Hard/Trailing SL -> 3. TP Limit (at exact tp, 0 exit slip) -> 4. MTM/HWM -> 5. Trailing/BE/TIME_SQUEEZE ratchet -> 6. Minute-end E1 (bars_held==15 & mfe_atr<0.4) -> 7. Minute-end TIMEOUT (bars_held>=effective_max_hold)",
            "price_used": "Executable bid (BUY) / ask (SELL) minus/plus exit slippage",
            "future_data_leakage": False,
        },
        "g3_5_sim_e1_semantics_defects": {
            "defect_1_corrupted_atr": "Reconstructed ATR from final ratcheted SL instead of immutable TP/3.0; corrupted 54/112 trades (all BREAKEVEN, TRAILING_SL, TIME_SQUEEZE exits) and caused -14.10 R of the -13.79 R discrepancy.",
            "defect_2_time_squeeze_formula": "Used invented ATR-width squeeze instead of RunBacktestUseCase's sl + progress*(price - sl) and used hour<19 instead of hour<20.",
            "defect_3_tp_exit_slippage": "Charged exit slippage on limit TARGET fills whereas RunBacktestUseCase fills limit TP at exact tp.",
            "defect_4_e1_bar_vs_tick_timing": "Evaluated E1 on first tick elapsed>=900s instead of end of 15th completed post-entry 1m bar.",
            "defect_5_zero_spread_and_comm_rounding": "Allowed b==a zero-spread quotes and did not round per-leg commission to 2 decimals.",
        },
        "e1_trade_count": int(len(e1_rows)),
        "e1_total_diff_r_original": round(float(e1_rows["diff_r_sim_minus_app"].sum()), 6),
        "e1_trades": e1_rows[[
            "opp_id", "entry_time_utc", "exit_time_utc_app", "direction",
            "true_initial_atr", "g35_corrupted_atr", "app_exit_reason", "sim_exit_reason",
            "app_net_r", "sim_net_r", "diff_r_sim_minus_app",
        ]].to_dict(orient="records"),
    }
    (OUT_DIR / "g3_5r_e1_event_timeline.json").write_text(json.dumps(e1_timeline, indent=2), encoding="utf-8")

    print(f"[G3.5R PHASES A-C DONE in {round(time.time() - t0, 2)}s]")
    print(f"  112 Universe Identical: {universe_audit['exact_112_universe_identical']}")
    print(f"  App={universe_audit['unrounded_totals']['app_total_r']:+.6f} R, Sim={universe_audit['unrounded_totals']['sim_total_r']:+.6f} R, Diff={universe_audit['unrounded_totals']['discrepancy_sim_minus_app_r']:+.6f} R")
    print(f"  Corrupted ATR trades: {atr_corrupted_count}/112 (diff={atr_corrupted_diff_r:+.6f} R), Non-corrupted diff={universe_audit['atr_corruption_summary']['total_diff_r_from_non_corrupted_trades']:+.6f} R")
    print(f"  Mismatch classes: {class_counts}")


if __name__ == "__main__":
    main()
