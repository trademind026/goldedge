"""
Task G1 — Phase 1B Post-Fix Verification:
  1. Run post-fix RunBacktestUseCase (export_artifacts=False) on zero-cost 70-trade config
     and reconcile 0/70 mismatches against Clean-Room Mode A (Strict Spec).
  2. Verify W1/W2/W3 H0 (use_kalman_exit=False, Fusion demo costs, 8% HWM) parity vs Task 8.
  3. Run post-fix RunBacktestUseCase WITH Fusion Markets demo realistic costs (E3 fixed impact).
"""

from __future__ import annotations

from datetime import datetime, timezone
import gc
import json
import os
from pathlib import Path
import sys

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
import g1_clean_room_engine as clean

G1_DIR = ROOT / "backtest_results" / "audit" / "task_g1_root_cause"
TASK8_JSON = ROOT / "backtest_results" / "audit" / "task8_app_comparison.json"


def _norm_sec(s: str) -> str:
    ts = pd.Timestamp(s)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.floor("s").isoformat()


def _reconcile_app_vs_clean(app_trades: list[dict], ind_trades: list[dict]) -> dict:
    mismatches = []
    n_cmp = min(len(app_trades), len(ind_trades))
    max_pnl_diff = 0.0
    max_px_diff = 0.0
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
        max_pnl_diff = max(max_pnl_diff, pnl_diff)
        max_px_diff = max(max_px_diff, ep_diff, xp_diff)
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
        ):
            mismatches.append({
                "index": i,
                "app": {
                    "entry_time": a_et, "exit_time": a_xt, "direction": a["direction"],
                    "strategy": a["strategy"], "entry_price": float(a["entry_price"]),
                    "exit_price": float(a["exit_price"]), "qty": float(a["qty"]),
                    "exit_reason": a["exit_reason"], "pnl": round(float(a["pnl"]), 6),
                },
                "ind": {
                    "entry_time": b_et, "exit_time": b_xt, "direction": b["direction"],
                    "strategy": b["strategy"], "entry_price": float(b["entry_price"]),
                    "exit_price": float(b["exit_price"]), "qty": float(b["qty"]),
                    "exit_reason": b["exit_reason"], "pnl": round(float(b["pnl"]), 6),
                },
                "pnl_diff": round(pnl_diff, 6),
            })
    return {
        "app_count": len(app_trades),
        "ind_count": len(ind_trades),
        "count_diff": len(ind_trades) - len(app_trades),
        "matched_count": n_cmp - len(mismatches) if len(app_trades) == len(ind_trades) else None,
        "mismatch_count": len(mismatches) + abs(len(ind_trades) - len(app_trades)),
        "max_pnl_diff_usd": round(max_pnl_diff, 6),
        "max_price_diff": round(max_px_diff, 6),
        "mismatches": mismatches[:25],
    }


def main() -> None:
    container = Container()
    tick_repo = container.tick_repo()
    sizing_service = container.sizing_service()

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

    start_g1 = datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc)
    end_g1 = datetime(2026, 11, 19, 23, 59, 59, tzinfo=timezone.utc)

    print("[PHASE 1B] 1. Running post-fix RunBacktestUseCase (Zero-cost G1 config)...", flush=True)
    app_postfix_zero = _new_uc().execute(
        "XAUUSD", start_g1, end_g1, initial_capital=10000.0, use_kalman_exit=True,
        session_close_utc=15, early_fail_mins=15, early_fail_min_mfe_atr=0.4,
        commission_per_lot=0.0, slippage_pips=0.0, apply_swap=False,
        max_hwm_drawdown_pct=8.0, export_artifacts=False,
    )

    print("[PHASE 1B] 2. Running post-fix RunBacktestUseCase (Realistic Fusion demo costs)...", flush=True)
    app_postfix_fusion = _new_uc().execute(
        "XAUUSD", start_g1, end_g1, initial_capital=10000.0, use_kalman_exit=True,
        session_close_utc=15, early_fail_mins=15, early_fail_min_mfe_atr=0.4,
        commission_profile="fusion_ctrader_demo", trading_environment="demo", account_id=48439552,
        slippage_pips=0.0533, apply_swap=True, max_hwm_drawdown_pct=8.0, export_artifacts=False,
    )

    # Check W1/W2/W3 H0 parity (Task 8 windows with 8% HWM, use_kalman_exit=False, Fusion demo costs)
    task8_data = json.loads(TASK8_JSON.read_text(encoding="utf-8"))
    w_defs = [
        ("W1", datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc)),
        ("W2", datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc)),
        ("W3", datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 18, 18, 0, 0, tzinfo=timezone.utc)),
    ]
    w_parity = {}
    for wid, ws, we in w_defs:
        print(f"[PHASE 1B] 3. Checking post-fix {wid} H0 parity vs Task 8...", flush=True)
        wres = _new_uc().execute(
            "XAUUSD", ws, we, initial_capital=10000.0, use_kalman_exit=False,
            commission_profile="fusion_ctrader_demo", trading_environment="demo", account_id=48439552,
            slippage_pips=0.0533, apply_swap=True, max_hwm_drawdown_pct=8.0, export_artifacts=False,
        )
        t8_ref = task8_data["windows"][wid]["app_with_8pct_kill_switch"]
        w_parity[wid] = {
            "task8_trades": t8_ref["trades"],
            "postfix_trades": int(wres["trades"]),
            "trade_diff": int(wres["trades"]) - int(t8_ref["trades"]),
            "task8_net_pnl": t8_ref["net_pnl_usd"],
            "postfix_net_pnl": round(float(wres["pnl"]), 4),
            "pnl_diff": round(float(wres["pnl"]) - float(t8_ref["net_pnl_usd"]), 4),
            "task8_mtm_dd_pct": t8_ref["mtm_max_dd_pct"],
            "postfix_mtm_dd_pct": round(float(wres["mtm_max_dd_pct"]), 4),
            "hwm_halted": bool((wres.get("hwm_kill_switch") or {}).get("halted")),
        }

    # Check V2 unprotected W3 baseline (143 trades) parity too
    print("[PHASE 1B] 4. Checking post-fix W3 unprotected (no HWM cap) V2 parity (143 trades)...", flush=True)
    w3_unprot = _new_uc().execute(
        "XAUUSD", w_defs[2][1], w_defs[2][2], initial_capital=10000.0, use_kalman_exit=False,
        commission_profile="fusion_ctrader_demo", trading_environment="demo", account_id=48439552,
        slippage_pips=0.0533, apply_swap=True, max_hwm_drawdown_pct=None, export_artifacts=False,
    )

    del container, tick_repo, sizing_service
    gc.collect()

    # Now reconcile post-fix app outputs against Clean-Room Mode A (Strict Spec)
    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    conn = psycopg2.connect(db_url)
    try:
        span_7d = clean.load_prepared_span(conn, "XAUUSD", start_g1, end_g1, chunk_days=7)
    finally:
        conn.close()

    res_clean_zero = clean.run_clean_backtest(
        span_7d,
        clean.CleanSimConfig(
            use_kalman_exit=True, session_close_utc=15, early_fail_mins=15, early_fail_min_mfe_atr=0.4,
            cost_mode="zero", max_hwm_dd_pct=8.0,
        ),
    )
    res_clean_fusion = clean.run_clean_backtest(
        span_7d,
        clean.CleanSimConfig(
            use_kalman_exit=True, session_close_utc=15, early_fail_mins=15, early_fail_min_mfe_atr=0.4,
            cost_mode="fusion_ctrader_demo", max_hwm_dd_pct=8.0,
        ),
    )

    rec_zero = _reconcile_app_vs_clean(app_postfix_zero["trade_list"], res_clean_zero["trade_list"])
    rec_fusion = _reconcile_app_vs_clean(app_postfix_fusion["trade_list"], res_clean_fusion["trade_list"])

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "postfix_zero_cost": {
            "app_trades": int(app_postfix_zero["trades"]),
            "app_pnl": round(float(app_postfix_zero["pnl"]), 6),
            "app_pf": round(float(app_postfix_zero["pf"]), 4),
            "app_closed_max_dd": round(float(app_postfix_zero["closed_max_dd"]), 4),
            "app_mtm_max_dd": round(float(app_postfix_zero["mtm_max_dd"]), 4),
            "app_mtm_max_dd_pct": round(float(app_postfix_zero["mtm_max_dd_pct"]), 4),
            "app_hwm": app_postfix_zero["hwm_kill_switch"],
            "equity_curve_first_point_uses_exit_time": (
                app_postfix_zero["equity_curve"][0]["time"] == app_postfix_zero["trade_list"][0]["exit_time"]
            ),
            "reconciliation_vs_clean_mode_a": rec_zero,
        },
        "postfix_fusion_demo_costs": {
            "app_trades": int(app_postfix_fusion["trades"]),
            "app_pnl": round(float(app_postfix_fusion["pnl"]), 6),
            "app_pf": round(float(app_postfix_fusion["pf"]), 4),
            "app_closed_max_dd": round(float(app_postfix_fusion["closed_max_dd"]), 4),
            "app_mtm_max_dd": round(float(app_postfix_fusion["mtm_max_dd"]), 4),
            "app_mtm_max_dd_pct": round(float(app_postfix_fusion["mtm_max_dd_pct"]), 4),
            "total_commission_usd": round(float(app_postfix_fusion["total_commission_usd"]), 4),
            "total_slippage_usd": round(float(app_postfix_fusion["total_slippage_usd"]), 4),
            "total_swap_usd": round(float(app_postfix_fusion["total_swap_usd"]), 4),
            "app_hwm": app_postfix_fusion["hwm_kill_switch"],
            "reconciliation_vs_clean_mode_a_fusion": rec_fusion,
        },
        "v2_hwm_parity_check": w_parity,
        "v2_w3_unprotected_parity": {
            "frozen_trades": 143,
            "postfix_trades": int(w3_unprot["trades"]),
            "frozen_net_pnl": 211.4828,
            "postfix_net_pnl": round(float(w3_unprot["pnl"]), 4),
            "frozen_pf": 1.0414,
            "postfix_pf": round(float(w3_unprot["pf"]), 4),
            "frozen_mtm_dd_pct": 12.7825,
            "postfix_mtm_dd_pct": round(float(w3_unprot["mtm_max_dd_pct"]), 4),
        },
    }
    (G1_DIR / "g1_phase1_reconciliation_postfix.json").write_text(
        json.dumps(out, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps({
        "zero_mismatches": rec_zero["mismatch_count"],
        "zero_pnl": out["postfix_zero_cost"]["app_pnl"],
        "equity_curve_exit_time_fixed": out["postfix_zero_cost"]["equity_curve_first_point_uses_exit_time"],
        "fusion_mismatches": rec_fusion["mismatch_count"],
        "fusion_trades": out["postfix_fusion_demo_costs"]["app_trades"],
        "fusion_pnl": out["postfix_fusion_demo_costs"]["app_pnl"],
        "fusion_comm": out["postfix_fusion_demo_costs"]["total_commission_usd"],
        "fusion_slip": out["postfix_fusion_demo_costs"]["total_slippage_usd"],
        "fusion_swap": out["postfix_fusion_demo_costs"]["total_swap_usd"],
        "v2_w3_unprot_trades": out["v2_w3_unprotected_parity"]["postfix_trades"],
        "v2_w3_unprot_pnl": out["v2_w3_unprotected_parity"]["postfix_net_pnl"],
        "w_parity": w_parity,
    }, indent=2))


if __name__ == "__main__":
    main()
