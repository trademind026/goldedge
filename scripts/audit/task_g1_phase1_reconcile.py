"""
Task G1 — Phase 1A: Exact Baseline Reproduction via RunBacktestUseCase +
Clean-Room Raw-Tick Reconciliation (Pre-Fix Mode B Bug-Parity vs Mode A Strict Spec).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import gc
import hashlib
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
START_DT = datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc)
END_DT = datetime(2026, 11, 19, 23, 59, 59, tzinfo=timezone.utc)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _norm_ts(s: str, trunc_sec: bool = False) -> str:
    ts = pd.Timestamp(s)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    if trunc_sec:
        ts = ts.floor("s")
    return ts.isoformat()


def _reconcile_trade_lists(app_trades: list[dict], ind_trades: list[dict], trunc_sec: bool = False) -> dict:
    mismatches = []
    n_cmp = min(len(app_trades), len(ind_trades))
    max_pnl_diff = 0.0
    max_px_diff = 0.0
    for i in range(n_cmp):
        a = app_trades[i]
        b = ind_trades[i]
        a_et = _norm_ts(str(a["entry_time"]), trunc_sec=trunc_sec)
        b_et = _norm_ts(str(b["entry_time"]), trunc_sec=trunc_sec)
        a_xt = _norm_ts(str(a["exit_time"]), trunc_sec=trunc_sec)
        b_xt = _norm_ts(str(b["exit_time"]), trunc_sec=trunc_sec)
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
    orig_csv = ROOT / "backtest_results" / "tick_replay_trades.csv"
    orig_jrn = ROOT / "backtest_results" / "journeys_medium_backtest.json"
    orig_csv_bytes = orig_csv.read_bytes() if orig_csv.exists() else None
    orig_jrn_bytes = orig_jrn.read_bytes() if orig_jrn.exists() else None

    baseline_csv_df = pd.read_csv(G1_DIR / "g1_original_70_baseline_trades.csv")
    orig_db_run = json.loads((G1_DIR / "g1_original_db_run.json").read_text(encoding="utf-8"))

    container = Container()
    tick_repo = container.tick_repo()
    sizing_service = container.sizing_service()

    try:
        print("[PHASE 1A] 1. Reproducing bt_20261121_191649_a03728 via actual RunBacktestUseCase...", flush=True)
        uc = RunBacktestUseCase(
            tick_repo=tick_repo,
            virtual_broker=VirtualBrokerAdapter(),
            manage_position=None,
            sizing_service=sizing_service,
            profile=MEDIUM_RISK_BACKTEST,
            expected_pnl_model=None,
            min_expected_pnl=None,
        )
        app_res = uc.execute(
            "XAUUSD",
            START_DT,
            END_DT,
            initial_capital=10000.0,
            use_kalman_exit=True,
            session_close_utc=15,
            early_fail_mins=15,
            early_fail_min_mfe_atr=0.4,
            commission_per_lot=0.0,
            slippage_pips=0.0,
            apply_swap=False,
            use_tick_intensity_features=True,
            max_hwm_drawdown_pct=8.0,
        )

        (G1_DIR / "g1_reproduced_70_trades.csv").write_bytes(orig_csv.read_bytes())
        (G1_DIR / "g1_reproduced_70_journeys.json").write_bytes(orig_jrn.read_bytes())
    finally:
        if orig_csv_bytes is not None:
            orig_csv.write_bytes(orig_csv_bytes)
        if orig_jrn_bytes is not None:
            orig_jrn.write_bytes(orig_jrn_bytes)

    repro_csv_df = pd.read_csv(G1_DIR / "g1_reproduced_70_trades.csv")
    orig_csv_sha = _file_sha256(G1_DIR / "g1_original_70_baseline_trades.csv")
    repro_csv_sha = _file_sha256(G1_DIR / "g1_reproduced_70_trades.csv")
    repro_records = repro_csv_df.to_dict(orient="records")
    orig_records = baseline_csv_df.to_dict(orient="records")
    app_trades = app_res["trade_list"]

    repro_csv_diff = _reconcile_trade_lists(orig_records, repro_records, trunc_sec=False)
    db_diff = _reconcile_trade_lists(orig_db_run["trade_list"], app_trades, trunc_sec=False)

    repro_csv_df["entry_dt"] = pd.to_datetime(repro_csv_df["entry_time"], utc=True)
    repro_csv_df["month"] = repro_csv_df["entry_dt"].dt.strftime("%Y-%m")
    sl_df = repro_csv_df[repro_csv_df["exit_reason"] == "SL"]
    tgt_df = repro_csv_df[repro_csv_df["exit_reason"] == "TARGET"]
    oct_df = repro_csv_df[repro_csv_df["month"] == "2026-10"]
    sw_sell_df = repro_csv_df[(repro_csv_df["regime"] == "SIDEWAYS") & (repro_csv_df["direction"] == "SELL")]
    csv_rounded_pnl_sum = round(float(repro_csv_df["pnl"].round(2).sum()), 2)

    repro_summary = {
        "reproduction_exact": (repro_csv_diff["mismatch_count"] == 0 and db_diff["mismatch_count"] == 0 and orig_csv_sha == repro_csv_sha),
        "original_csv_sha256": orig_csv_sha,
        "reproduced_csv_sha256": repro_csv_sha,
        "sha256_identical": orig_csv_sha == repro_csv_sha,
        "vs_original_csv": repro_csv_diff,
        "vs_original_db_run": db_diff,
        "app_trades": int(app_res["trades"]),
        "app_wins": int(app_res["wins"]),
        "app_losses": int(app_res["losses"]),
        "app_breakevens": int(app_res["breakevens"]),
        "app_win_rate_pct": round(float(app_res["win_rate"]) * 100.0, 4),
        "app_pnl_full_precision": round(float(app_res["pnl"]), 6),
        "app_pnl_rounded_ui_csv_sum": csv_rounded_pnl_sum,
        "app_pf": round(float(app_res["pf"]), 6),
        "app_closed_max_dd": round(float(app_res["closed_max_dd"]), 4),
        "app_closed_max_dd_pct": round(float(app_res["closed_max_dd_pct"]), 4),
        "app_mtm_max_dd": round(float(app_res["mtm_max_dd"]), 4),
        "app_mtm_max_dd_pct": round(float(app_res["mtm_max_dd_pct"]), 4),
        "app_mtm_max_dd_initial_pct": round(float(app_res["mtm_max_dd_initial_pct"]), 4),
        "app_mtm_peak_time": app_res.get("mtm_peak_time"),
        "app_mtm_trough_time": app_res.get("mtm_trough_time"),
        "app_hwm_kill_switch": app_res.get("hwm_kill_switch"),
        "prompt_baseline_checks": {
            "total_trades": {"expected": 70, "actual": int(len(repro_csv_df))},
            "net_pnl_ui_csv": {"expected": 162.25, "actual": csv_rounded_pnl_sum},
            "net_pnl_exact": round(float(repro_csv_df["pnl"].sum()), 4),
            "sl_exits": {"expected_count": 13, "actual_count": int(len(sl_df)), "expected_pnl": -1366.03, "actual_pnl": round(float(sl_df["pnl"].sum()), 2)},
            "october_pnl": {"expected": -592.66, "actual": round(float(oct_df["pnl"].sum()), 2), "trades": int(len(oct_df))},
            "sideways_sell": {"expected_count": 23, "actual_count": int(len(sw_sell_df)), "expected_pnl": -160.48, "actual_pnl": round(float(sw_sell_df["pnl"].sum()), 2)},
            "target_exits": {"expected_count": 5, "actual_count": int(len(tgt_df)), "expected_pnl": 998.15, "actual_pnl": round(float(tgt_df["pnl"].sum()), 2)},
        },
        "exit_reason_breakdown": {
            str(r): {"count": int(len(g)), "wins": int((g["pnl"].round(2) > 0).sum()), "pnl": round(float(g["pnl"].sum()), 2)}
            for r, g in repro_csv_df.groupby("exit_reason")
        },
        "equity_curve_timestamp_bug": {
            "first_point_time": app_res["equity_curve"][0]["time"],
            "trade_0_entry_time": app_trades[0]["entry_time"],
            "trade_0_exit_time": app_trades[0]["exit_time"],
            "uses_entry_time_bug": app_res["equity_curve"][0]["time"] == app_trades[0]["entry_time"],
        },
    }
    (G1_DIR / "g1_baseline_reproduction.json").write_text(json.dumps(repro_summary, indent=2, default=str), encoding="utf-8")
    print("[PHASE 1A] Reproduction exact:", repro_summary["reproduction_exact"], "| sha256 identical:", repro_summary["sha256_identical"], flush=True)

    del app_res, uc, container
    gc.collect()

    # 2. Clean-room raw-tick audit (zero src/goldedge/ imports)
    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    conn = psycopg2.connect(db_url)
    try:
        print("[PHASE 1A] 2. Loading flat NumPy span (7-day chunks, <550MB RAM)...", flush=True)
        span_7d = clean.load_prepared_span(conn, "XAUUSD", START_DT, END_DT, chunk_days=7)

        # Verify 7-day chunk boundaries via lightweight SQL minute-straddle check
        boundary_straddles = 0
        cs = START_DT + timedelta(days=7)
        end_ex = END_DT + timedelta(microseconds=1)
        with conn.cursor() as cur:
            while cs < end_ex:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE time >= %s - interval '1 minute' AND time < %s) AS before_ticks,
                        COUNT(*) FILTER (WHERE time >= %s AND time < %s + interval '1 minute') AS after_ticks
                    FROM market_ticks
                    WHERE symbol = 'XAUUSD' AND time >= %s - interval '1 minute' AND time < %s + interval '1 minute'
                    """,
                    (cs, cs, cs, cs, cs, cs),
                )
                r = cur.fetchone()
                # Straddle only matters if chunk boundary isn't on a :00 second minute boundary (all 7d boundaries from 00:00:00Z are exact minute boundaries!)
                if cs.second != 0 or cs.microsecond != 0:
                    if r[0] > 0 and r[1] > 0:
                        boundary_straddles += 1
                cs += timedelta(days=7)
    finally:
        conn.close()

    # Mode B: Bug-Parity Mirror (exact pre-fix app model)
    print("[PHASE 1A] 3. Running Clean-Room Mode B (Pre-Fix Bug Parity)...", flush=True)
    cfg_mode_b = clean.CleanSimConfig(
        use_kalman_exit=True, session_close_utc=15, early_fail_mins=15, early_fail_min_mfe_atr=0.4,
        cost_mode="zero", max_hwm_dd_pct=8.0,
        bug_invert_before_kalman=True,
        bug_kalman_before_sl_tp=True,
        bug_skip_mtm_on_kalman_exit=True,
        bug_len_minus_1_on_timeout_idx=True,
    )
    res_mode_b = clean.run_clean_backtest(span_7d, cfg_mode_b)
    rec_b_vs_csv = _reconcile_trade_lists(repro_records, res_mode_b["trade_list"], trunc_sec=False)

    # Mode A: Strict Fixed Spec (clean quote order + SL/TP priority + MTM update on Kalman exit + valid last idx)
    print("[PHASE 1A] 4. Running Clean-Room Mode A (Strict Spec Post-Fix Model)...", flush=True)
    cfg_mode_a = clean.CleanSimConfig(
        use_kalman_exit=True, session_close_utc=15, early_fail_mins=15, early_fail_min_mfe_atr=0.4,
        cost_mode="zero", max_hwm_dd_pct=8.0,
        bug_invert_before_kalman=False,
        bug_kalman_before_sl_tp=False,
        bug_skip_mtm_on_kalman_exit=False,
        bug_len_minus_1_on_timeout_idx=False,
    )
    res_mode_a = clean.run_clean_backtest(span_7d, cfg_mode_a)
    rec_a_vs_csv = _reconcile_trade_lists(repro_records, res_mode_a["trade_list"], trunc_sec=False)

    # Isolated Defect E1 fix only
    res_e1_only = clean.run_clean_backtest(
        span_7d,
        clean.CleanSimConfig(
            use_kalman_exit=True, session_close_utc=15, early_fail_mins=15, early_fail_min_mfe_atr=0.4,
            cost_mode="zero", max_hwm_dd_pct=8.0,
            bug_invert_before_kalman=False,
            bug_kalman_before_sl_tp=True,
            bug_skip_mtm_on_kalman_exit=True,
            bug_len_minus_1_on_timeout_idx=True,
        ),
    )
    rec_e1_only = _reconcile_trade_lists(repro_records, res_e1_only["trade_list"], trunc_sec=False)

    # Isolated Defect E2 fix only (SL/TP priority before Kalman exit)
    res_e2_only = clean.run_clean_backtest(
        span_7d,
        clean.CleanSimConfig(
            use_kalman_exit=True, session_close_utc=15, early_fail_mins=15, early_fail_min_mfe_atr=0.4,
            cost_mode="zero", max_hwm_dd_pct=8.0,
            bug_invert_before_kalman=True,
            bug_kalman_before_sl_tp=False,
            bug_skip_mtm_on_kalman_exit=False,
            bug_len_minus_1_on_timeout_idx=False,
        ),
    )
    rec_e2_only = _reconcile_trade_lists(repro_records, res_e2_only["trade_list"], trunc_sec=False)

    replay_mins = [ms.minute_ts for ms in span_7d.replay_slices]
    large_weekday_gaps = []
    for i in range(1, len(replay_mins)):
        dt_m = (replay_mins[i] - replay_mins[i - 1]).total_seconds() / 60.0
        if dt_m > 10.0 and not (replay_mins[i - 1].weekday() == 4 and replay_mins[i].weekday() == 6):
            large_weekday_gaps.append({
                "from": replay_mins[i - 1].isoformat(),
                "to": replay_mins[i].isoformat(),
                "gap_mins": round(dt_m, 2),
            })

    exit_min_ticks = [t["exit_minute_valid_ticks"] for t in res_mode_b["trade_list"]]

    rec_out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "chunk_audit": {
            "chunk_7d_minute_splits": span_7d.chunk_boundary_splits,
            "chunk_boundary_minute_straddles": boundary_straddles,
            "large_weekday_gaps_gt_10m": large_weekday_gaps[:20],
            "total_large_weekday_gaps": len(large_weekday_gaps),
            "replay_minute_buckets": len(span_7d.replay_slices),
            "warmup_minute_buckets": len(span_7d.warmup_slices),
        },
        "mode_b_bug_parity_vs_app": {
            "reconciliation": rec_b_vs_csv,
            "ind_trades": res_mode_b["trades"],
            "ind_pnl": round(float(res_mode_b["pnl"]), 6),
            "ind_pf": round(float(res_mode_b["pf"]), 4),
            "ind_closed_max_dd": round(float(res_mode_b["closed_max_dd"]), 4),
            "ind_mtm_max_dd": round(float(res_mode_b["mtm_max_dd"]), 4),
            "ind_mtm_max_dd_pct": round(float(res_mode_b["mtm_max_dd_pct"]), 4),
            "ind_hwm": res_mode_b["hwm_kill_switch"],
        },
        "isolated_e1_fix_vs_app": {
            "reconciliation": rec_e1_only,
            "ind_trades": res_e1_only["trades"],
            "ind_pnl": round(float(res_e1_only["pnl"]), 4),
            "ind_mtm_max_dd_pct": round(float(res_e1_only["mtm_max_dd_pct"]), 4),
        },
        "isolated_e2_fix_vs_app": {
            "reconciliation": rec_e2_only,
            "ind_trades": res_e2_only["trades"],
            "ind_pnl": round(float(res_e2_only["pnl"]), 4),
            "ind_mtm_max_dd_pct": round(float(res_e2_only["mtm_max_dd_pct"]), 4),
        },
        "mode_a_strict_spec_vs_app": {
            "reconciliation": rec_a_vs_csv,
            "ind_trades": res_mode_a["trades"],
            "ind_pnl": round(float(res_mode_a["pnl"]), 4),
            "ind_pf": round(float(res_mode_a["pf"]), 4),
            "ind_closed_max_dd": round(float(res_mode_a["closed_max_dd"]), 4),
            "ind_mtm_max_dd": round(float(res_mode_a["mtm_max_dd"]), 4),
            "ind_mtm_max_dd_pct": round(float(res_mode_a["mtm_max_dd_pct"]), 4),
            "ind_hwm": res_mode_a["hwm_kill_switch"],
            "exit_breakdown": res_mode_a["exit_breakdown"],
        },
        "exit_minute_tick_rate_stats": {
            "min": int(min(exit_min_ticks)) if exit_min_ticks else 0,
            "median": float(pd.Series(exit_min_ticks).median()) if exit_min_ticks else 0.0,
            "mean": round(float(pd.Series(exit_min_ticks).mean()), 1) if exit_min_ticks else 0.0,
            "max": int(max(exit_min_ticks)) if exit_min_ticks else 0,
            "hardcoded_denominator": 60,
        },
    }
    (G1_DIR / "g1_phase1_reconciliation_prefix.json").write_text(
        json.dumps(rec_out, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps({
        "reproduction_exact": repro_summary["reproduction_exact"],
        "mode_b_mismatches": rec_b_vs_csv["mismatch_count"],
        "mode_b_max_pnl_diff": rec_b_vs_csv["max_pnl_diff_usd"],
        "mode_b_mtm_dd": round(float(res_mode_b["mtm_max_dd"]), 4),
        "chunk_splits": span_7d.chunk_boundary_splits,
        "e1_only_mismatches_vs_prefix": rec_e1_only["mismatch_count"],
        "e2_only_mismatches_vs_prefix": rec_e2_only["mismatch_count"],
        "mode_a_trades": res_mode_a["trades"],
        "mode_a_pnl": round(float(res_mode_a["pnl"]), 4),
        "mode_a_mtm_dd_pct": round(float(res_mode_a["mtm_max_dd_pct"]), 4),
        "mode_a_mismatches_vs_prefix": rec_a_vs_csv["mismatch_count"],
    }, indent=2))


if __name__ == "__main__":
    main()
