"""
Task G1 — Phase 5 Mandatory Application Implementation & Verification.

Runs BOTH the actual `RunBacktestUseCase` (`export_artifacts=False`) and the
independent clean-room simulator (`g1_clean_room_engine.py`) across:
  1. FULL (2026-08-17 .. 2026-11-19, warmed Aug 13..16), 1.0x Fusion demo costs:
     - H0 Baseline (use_kalman_exit=True, max_score_threshold=None)
     - H3b_KAL_CAP80 Approved (use_kalman_exit=True, max_score_threshold=0.80)
     - H3a_STD_CAP80 Reference (use_kalman_exit=False, max_score_threshold=0.80)
  2. Untouched Holdout HO (2026-10-26 .. 2026-11-19, warmed Oct 22..25), 1.0x Fusion demo costs:
     - H0 Baseline & H3b_KAL_CAP80
  3. 1.5x Cost Stress (commission $18/M/side, slippage 0.07995p, 1.5x swap) on FULL and HO:
     - H0 Baseline & H3b_KAL_CAP80

Also measures the clean-room `use_kalman_exit=True` sensitivity plateau over
`max_score in (0.75, 0.78, 0.80, 0.82, 0.85, None)` across DEV/VAL/HO/FULL, and
verifies 0 trade/cost/DD/HWM mismatches plus a 10-trade causal spot-check.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import gc
import json
import os
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd
import psycopg2

ROOT = Path(__file__).resolve().parent.parent.parent
if not (ROOT / "src").exists() and Path("/app/src").exists():
    ROOT = Path("/app")
for p in (str(ROOT), str(ROOT / "src"), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

from goldedge.adapters.secondary.virtual_broker import VirtualBrokerAdapter
from goldedge.application.use_cases.run_backtest import RunBacktestUseCase
from goldedge.config.container import Container
from goldedge.config.risk_profiles import MEDIUM_RISK_BACKTEST
from goldedge.domain.services.broker_cost_service import (
    BrokerCostService,
    FUSION_CTRADER_DEMO_48439552,
    FUSION_XAUUSD_SWAP_SPEC,
)
import g1_clean_room_engine as clean

G1_DIR = ROOT / "backtest_results" / "audit" / "task_g1_root_cause"
FULL_LOAD_START = datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc)
FULL_LOAD_END = datetime(2026, 11, 19, 23, 59, 59, tzinfo=timezone.utc)

SPLITS = {
    "DEV": (datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 9, 25, 23, 59, 59, tzinfo=timezone.utc)),
    "VAL": (datetime(2026, 9, 26, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 10, 25, 23, 59, 59, tzinfo=timezone.utc)),
    "HO": (datetime(2026, 10, 26, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 19, 23, 59, 59, tzinfo=timezone.utc)),
    "FULL": (datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 19, 23, 59, 59, tzinfo=timezone.utc)),
}


def _norm_sec(s: str) -> str:
    ts = pd.Timestamp(s)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.floor("s").isoformat()


def _summarize_app(res: dict[str, Any]) -> dict[str, Any]:
    hwm = res.get("hwm_kill_switch") or {}
    return {
        "trades": int(res["trades"]),
        "wins": int(res.get("wins", 0)),
        "losses": int(res.get("losses", 0)),
        "win_rate_pct": round(float(res.get("win_rate", 0.0)) * 100.0, 2),
        "pnl": round(float(res["pnl"]), 4),
        "pf": round(float(res["pf"]), 4),
        "closed_max_dd": round(float(res.get("closed_max_dd", res["max_dd"])), 4),
        "closed_max_dd_pct": round(float(res.get("closed_max_dd_pct", 0.0)), 4),
        "mtm_max_dd": round(float(res.get("mtm_max_dd", res["max_dd"])), 4),
        "mtm_max_dd_pct": round(float(res.get("mtm_max_dd_pct", 0.0)), 4),
        "total_commission_usd": round(float(res.get("total_commission_usd", 0.0)), 4),
        "total_slippage_usd": round(float(res.get("total_slippage_usd", 0.0)), 4),
        "total_swap_usd": round(float(res.get("total_swap_usd", 0.0)), 4),
        "max_score_threshold": res.get("max_score_threshold"),
        "hwm_halted": bool(hwm.get("halted", False)),
        "hwm_trigger_time": hwm.get("halt_timestamp") or hwm.get("trigger_time"),
        "hwm_trades_prevented": int(hwm.get("trades_prevented", 0)),
    }


def _summarize_clean(res: dict[str, Any]) -> dict[str, Any]:
    hwm = res.get("hwm_kill_switch") or {}
    return {
        "trades": int(res["trades"]),
        "wins": int(res["wins"]),
        "losses": int(res["losses"]),
        "win_rate_pct": round(float(res["win_rate_pct"]), 2),
        "pnl": round(float(res["pnl"]), 4),
        "pf": round(float(res["pf"]), 4),
        "closed_max_dd": round(float(res["closed_max_dd"]), 4),
        "closed_max_dd_pct": round(float(res["closed_max_dd_pct"]), 4),
        "mtm_max_dd": round(float(res["mtm_max_dd"]), 4),
        "mtm_max_dd_pct": round(float(res["mtm_max_dd_pct"]), 4),
        "total_commission_usd": round(float(res["commission_usd"]), 4),
        "total_slippage_usd": round(float(res["slippage_usd"]), 4),
        "total_swap_usd": round(float(res["swap_usd"]), 4),
        "hwm_halted": bool(hwm.get("halted", False)),
        "hwm_trigger_time": hwm.get("trigger_time"),
        "hwm_trades_prevented": int(hwm.get("trades_prevented", 0)),
    }


def _reconcile(app_res: dict[str, Any], clean_res: dict[str, Any]) -> dict[str, Any]:
    app_trades = app_res["trade_list"]
    ind_trades = clean_res["trade_list"]
    mismatches: list[dict[str, Any]] = []
    n_cmp = min(len(app_trades), len(ind_trades))
    max_pnl_diff = 0.0
    max_px_diff = 0.0
    max_qty_diff = 0.0
    max_cost_diff = 0.0

    for i in range(n_cmp):
        a = app_trades[i]
        b = ind_trades[i]
        a_et = _norm_sec(str(a["entry_time"]))
        b_et = _norm_sec(str(b["entry_time"]))
        a_xt = _norm_sec(str(a["exit_time"]))
        b_xt = _norm_sec(str(b["exit_time"]))
        pnl_diff = abs(float(a["pnl"]) - float(b["pnl"]))
        ep_diff = abs(float(a["entry_price"]) - float(b["entry_price"]))
        xp_diff = abs(float(a["exit_price"]) - float(b["exit_price"]))
        qty_diff = abs(float(a["qty"]) - float(b["qty"]))
        comm_diff = abs(float(a.get("commission_usd", 0.0)) - float(b.get("commission_usd", 0.0)))
        slip_diff = abs(float(a.get("slippage_usd", 0.0)) - float(b.get("slippage_usd", 0.0)))
        swap_diff = abs(float(a.get("swap_usd", 0.0)) - float(b.get("swap_usd", 0.0)))
        max_pnl_diff = max(max_pnl_diff, pnl_diff)
        max_px_diff = max(max_px_diff, ep_diff, xp_diff)
        max_qty_diff = max(max_qty_diff, qty_diff)
        max_cost_diff = max(max_cost_diff, comm_diff, slip_diff, swap_diff)

        if (
            a_et != b_et
            or a_xt != b_xt
            or str(a["direction"]) != str(b["direction"])
            or str(a["strategy"]) != str(b["strategy"])
            or str(a["exit_reason"]) != str(b["exit_reason"])
            or pnl_diff > 0.01
            or ep_diff > 1e-6
            or xp_diff > 1e-6
            or qty_diff > 1e-6
            or comm_diff > 0.01
            or slip_diff > 0.01
            or swap_diff > 0.01
        ):
            mismatches.append({
                "index": i,
                "app": {
                    "entry_time": a_et, "exit_time": a_xt, "direction": a["direction"],
                    "strategy": a["strategy"], "entry_price": float(a["entry_price"]),
                    "exit_price": float(a["exit_price"]), "qty": float(a["qty"]),
                    "exit_reason": a["exit_reason"], "pnl": round(float(a["pnl"]), 6),
                },
                "clean": {
                    "entry_time": b_et, "exit_time": b_xt, "direction": b["direction"],
                    "strategy": b["strategy"], "entry_price": float(b["entry_price"]),
                    "exit_price": float(b["exit_price"]), "qty": float(b["qty"]),
                    "exit_reason": b["exit_reason"], "pnl": round(float(b["pnl"]), 6),
                },
                "pnl_diff": round(pnl_diff, 6),
            })

    app_s = _summarize_app(app_res)
    clean_s = _summarize_clean(clean_res)
    summary_diffs = {
        "pnl_diff": round(abs(app_s["pnl"] - clean_s["pnl"]), 4),
        "closed_max_dd_diff": round(abs(app_s["closed_max_dd"] - clean_s["closed_max_dd"]), 4),
        "mtm_max_dd_diff": round(abs(app_s["mtm_max_dd"] - clean_s["mtm_max_dd"]), 4),
        "comm_diff": round(abs(app_s["total_commission_usd"] - clean_s["total_commission_usd"]), 4),
        "slip_diff": round(abs(app_s["total_slippage_usd"] - clean_s["total_slippage_usd"]), 4),
        "swap_diff": round(abs(app_s["total_swap_usd"] - clean_s["total_swap_usd"]), 4),
        "hwm_halted_match": (app_s["hwm_halted"] == clean_s["hwm_halted"]),
        "hwm_prevented_match": (app_s["hwm_trades_prevented"] == clean_s["hwm_trades_prevented"]),
        "hwm_trigger_time_match": (
            (_norm_sec(app_s["hwm_trigger_time"]) if app_s["hwm_trigger_time"] else None)
            == (_norm_sec(clean_s["hwm_trigger_time"]) if clean_s["hwm_trigger_time"] else None)
        ),
    }
    trade_mm_count = len(mismatches) + abs(len(ind_trades) - len(app_trades))
    agg_ok = (
        summary_diffs["pnl_diff"] <= 0.02
        and summary_diffs["mtm_max_dd_diff"] <= 0.02
        and summary_diffs["hwm_halted_match"]
        and summary_diffs["hwm_prevented_match"]
        and summary_diffs["hwm_trigger_time_match"]
    )
    return {
        "app_summary": app_s,
        "clean_summary": clean_s,
        "mismatch_count": int(trade_mm_count),
        "aggregate_parity_ok": bool(agg_ok),
        "max_pnl_diff_usd": round(max_pnl_diff, 6),
        "max_price_diff": round(max_px_diff, 6),
        "max_qty_diff": round(max_qty_diff, 6),
        "max_cost_diff_usd": round(max_cost_diff, 6),
        "summary_diffs": summary_diffs,
        "mismatches": mismatches[:20],
    }


def _spot_check_10_trades(app_res: dict[str, Any], clean_res: dict[str, Any]) -> list[dict[str, Any]]:
    app_t = app_res["trade_list"]
    cln_t = clean_res["trade_list"]
    n = min(len(app_t), len(cln_t))
    if n == 0:
        return []
    indices = np.linspace(0, n - 1, num=min(10, n), dtype=int).tolist()
    seen: set[int] = set()
    rows = []
    for idx in indices:
        if idx in seen:
            continue
        seen.add(idx)
        a, b = app_t[idx], cln_t[idx]
        rows.append({
            "trade_index": int(idx),
            "app_entry_time": _norm_sec(str(a["entry_time"])),
            "clean_entry_time": _norm_sec(str(b["entry_time"])),
            "app_exit_time": _norm_sec(str(a["exit_time"])),
            "clean_exit_time": _norm_sec(str(b["exit_time"])),
            "direction": str(a["direction"]),
            "strategy": str(a["strategy"]),
            "app_score": round(float(a["score"]), 4),
            "clean_score": round(float(b["score"]), 4),
            "app_entry_price": round(float(a["entry_price"]), 4),
            "clean_entry_price": round(float(b["entry_price"]), 4),
            "app_exit_price": round(float(a["exit_price"]), 4),
            "clean_exit_price": round(float(b["exit_price"]), 4),
            "app_sl": round(float(a["sl"]), 4),
            "clean_final_sl": round(float(b["final_sl"]), 4),
            "app_target": round(float(a["target"]), 4),
            "clean_tp": round(float(b["tp"]), 4),
            "app_qty": round(float(a["qty"]), 4),
            "clean_qty": round(float(b["qty"]), 4),
            "app_pnl": round(float(a["pnl"]), 4),
            "clean_pnl": round(float(b["pnl"]), 4),
            "exact_match": bool(
                _norm_sec(str(a["entry_time"])) == _norm_sec(str(b["entry_time"]))
                and abs(float(a["pnl"]) - float(b["pnl"])) <= 0.01
                and abs(float(a["score"]) - float(b["score"])) <= 1e-4
                and abs(float(a["qty"]) - float(b["qty"])) <= 1e-6
            ),
        })
    return rows


def main() -> None:
    refresh_hwm_only = "--refresh-hwm" in sys.argv
    p5_path = G1_DIR / "g1_phase5_verification.json"
    scenarios = [
        ("FULL_H0_BASELINE", "FULL", True, None, 1.0),
        ("FULL_H3b_KAL_CAP80", "FULL", True, 0.80, 1.0),
        ("FULL_H3a_STD_CAP80", "FULL", False, 0.80, 1.0),
        ("HO_H0_BASELINE", "HO", True, None, 1.0),
        ("HO_H3b_KAL_CAP80", "HO", True, 0.80, 1.0),
        ("STRESS_1_5x_FULL_H0", "FULL", True, None, 1.5),
        ("STRESS_1_5x_FULL_H3b", "FULL", True, 0.80, 1.5),
        ("STRESS_1_5x_HO_H0", "HO", True, None, 1.5),
        ("STRESS_1_5x_HO_H3b", "HO", True, 0.80, 1.5),
    ]
    active_scenarios = (
        [s for s in scenarios if s[0] in ("FULL_H0_BASELINE", "STRESS_1_5x_FULL_H0")]
        if refresh_hwm_only
        else scenarios
    )

    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    conn = psycopg2.connect(db_url)
    try:
        print("[PHASE 5] Loading prepared span for independent Clean-Room engine...", flush=True)
        full_raw_span = clean.load_prepared_span(conn, "XAUUSD", FULL_LOAD_START, FULL_LOAD_END, chunk_days=7)
    finally:
        conn.close()

    split_spans = {
        k: clean.slice_prepared_span(full_raw_span, sd, ed)
        for k, (sd, ed) in SPLITS.items()
    }

    clean_runs: dict[str, dict[str, Any]] = {}
    for name, sp_name, use_k, max_sc, cmult in active_scenarios:
        print(f"[PHASE 5] Clean-Room running {name} (uncached)...", flush=True)
        cfg = clean.CleanSimConfig(
            use_kalman_exit=use_k,
            session_close_utc=15,
            early_fail_mins=15,
            early_fail_min_mfe_atr=0.4,
            max_score=max_sc,
            cost_mode="fusion_ctrader_demo",
            cost_multiplier=cmult,
            max_hwm_dd_pct=8.0,
            compute_adx_features=False,
        )
        clean_runs[name] = clean.run_clean_backtest(split_spans[sp_name], cfg, cache=None)

    # Sensitivity plateau for use_kalman_exit=True (H3b family) via SpanCache across DEV/VAL/HO/FULL
    kal_cap_sweep: dict[str, dict[str, Any]] = {}
    if not refresh_hwm_only:
        caps = (0.75, 0.78, 0.80, 0.82, 0.85, None)
        for c in caps:
            kal_cap_sweep[f"cap_{c}" if c is not None else "no_cap"] = {}
        for sp_name in ("DEV", "VAL", "HO", "FULL"):
            print(f"[PHASE 5] Precomputing SpanCache for {sp_name} Kalman cap plateau...", flush=True)
            cache = clean.precompute_span_cache(split_spans[sp_name], compute_adx=False)
            for c in caps:
                lbl = f"cap_{c}" if c is not None else "no_cap"
                rc = clean.run_clean_backtest(
                    split_spans[sp_name],
                    clean.CleanSimConfig(
                        use_kalman_exit=True,
                        session_close_utc=15,
                        early_fail_mins=15,
                        early_fail_min_mfe_atr=0.4,
                        max_score=c,
                        cost_mode="fusion_ctrader_demo",
                        cost_multiplier=1.0,
                        max_hwm_dd_pct=8.0,
                        compute_adx_features=False,
                    ),
                    cache=cache,
                )
                kal_cap_sweep[lbl][sp_name] = _summarize_clean(rc)
            del cache
            gc.collect()

    del full_raw_span, split_spans
    gc.collect()

    # Now run actual RunBacktestUseCase across all 9 scenarios
    container = Container()
    tick_repo = container.tick_repo()
    sizing_service = container.sizing_service()

    stress_cost_service = BrokerCostService(
        commission_schedule=replace(FUSION_CTRADER_DEMO_48439552, rate_value=18.0),
        swap_schedule=replace(
            FUSION_XAUUSD_SWAP_SPEC,
            swap_long_pips=FUSION_XAUUSD_SWAP_SPEC.swap_long_pips * 1.5,
            swap_short_pips=FUSION_XAUUSD_SWAP_SPEC.swap_short_pips * 1.5,
        ),
    )

    def _new_uc() -> RunBacktestUseCase:
        return RunBacktestUseCase(
            tick_repo=tick_repo,
            virtual_broker=VirtualBrokerAdapter(),
            manage_position=None,
            sizing_service=sizing_service,
            profile=MEDIUM_RISK_BACKTEST,
            expected_pnl_model=None,
            min_expected_pnl=None,
        )

    app_runs: dict[str, dict[str, Any]] = {}
    reconciliations: dict[str, Any] = {}
    if refresh_hwm_only and p5_path.exists():
        prev_out = json.loads(p5_path.read_text(encoding="utf-8"))
        reconciliations = prev_out["reconciliations"]
        spot_check_10 = prev_out["spot_check_10_trades_FULL_H3b"]
        kal_cap_sweep = prev_out["kalman_cap_sensitivity_plateau"]

    for name, sp_name, use_k, max_sc, cmult in active_scenarios:
        sd, ed = SPLITS[sp_name]
        print(f"[PHASE 5] RunBacktestUseCase running {name}...", flush=True)
        if cmult == 1.0:
            app_res = _new_uc().execute(
                "XAUUSD",
                sd,
                ed,
                initial_capital=10000.0,
                use_kalman_exit=use_k,
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
                max_score_threshold=max_sc,
            )
        else:
            app_res = _new_uc().execute(
                "XAUUSD",
                sd,
                ed,
                initial_capital=10000.0,
                use_kalman_exit=use_k,
                session_close_utc=15,
                early_fail_mins=15,
                early_fail_min_mfe_atr=0.4,
                cost_service=stress_cost_service,
                slippage_pips=0.0533 * cmult,
                apply_swap=True,
                max_hwm_drawdown_pct=8.0,
                export_artifacts=False,
                max_score_threshold=max_sc,
            )
        app_runs[name] = app_res
        reconciliations[name] = _reconcile(app_res, clean_runs[name])
        rec = reconciliations[name]
        print(
            f"  [RECON] {name:<24s} trades={rec['app_summary']['trades']:>3d} "
            f"pnl=${rec['app_summary']['pnl']:>+8.2f} dd={rec['app_summary']['mtm_max_dd_pct']:.2f}% "
            f"mismatches={rec['mismatch_count']} agg_ok={rec['aggregate_parity_ok']}",
            flush=True,
        )

    if not refresh_hwm_only:
        spot_check_10 = _spot_check_10_trades(app_runs["FULL_H3b_KAL_CAP80"], clean_runs["FULL_H3b_KAL_CAP80"])
    all_zero = all(
        r["mismatch_count"] == 0 and r["aggregate_parity_ok"]
        for r in reconciliations.values()
    ) and all(row["exact_match"] for row in spot_check_10)

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "approved_candidate": "H3b_KAL_CAP80 (use_kalman_exit=True, max_score_threshold=0.80, 0.60 <= score < 0.80)",
        "all_scenarios_zero_mismatch": bool(all_zero),
        "reconciliations": reconciliations,
        "spot_check_10_trades_FULL_H3b": spot_check_10,
        "kalman_cap_sensitivity_plateau": kal_cap_sweep,
    }
    p5_path.write_text(
        json.dumps(out, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps({
        "all_scenarios_zero_mismatch": out["all_scenarios_zero_mismatch"],
        "summary": {
            k: {
                "trades": v["app_summary"]["trades"],
                "pnl": v["app_summary"]["pnl"],
                "pf": v["app_summary"]["pf"],
                "mtm_dd_pct": v["app_summary"]["mtm_max_dd_pct"],
                "halted": v["app_summary"]["hwm_halted"],
                "mismatches": v["mismatch_count"],
                "agg_ok": v["aggregate_parity_ok"],
            }
            for k, v in reconciliations.items()
        },
        "kalman_cap_plateau_FULL": {lbl: d["FULL"] for lbl, d in kal_cap_sweep.items()},
    }, indent=2))


if __name__ == "__main__":
    main()
