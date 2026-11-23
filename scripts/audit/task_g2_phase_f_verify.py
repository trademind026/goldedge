"""
Task G2 — Phase F: Mandatory Actual Application Verification & Clean-Room Reconciliation.

Runs clean-room reference export in a separate subprocess WITHOUT `/app/src` in PYTHONPATH,
runs the actual `RunBacktestUseCase` for H0, H3b, and G2 (`scorer_mode="g2_calibrated"`),
reconciles all 2,482 candidate scores + all executed trades/costs/MTM-DD/HWM triggers with
zero tolerated discrepancies, verifies PostgreSQL persistence and Flask API contract, and writes:
  `backtest_results/audit/task_g2_scorer/g2_application_reconciliation.json`.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
if not (ROOT / "src").exists() and Path("/app/src").exists():
    ROOT = Path("/app")
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask  # noqa: E402
from sqlalchemy import text  # noqa: E402

from goldedge.adapters.primary.rest_api.routes import backtest as bt_routes  # noqa: E402
from goldedge.config.container import Container  # noqa: E402
from goldedge.config.settings import settings  # noqa: E402
from goldedge.domain.models.signal import Signal, SignalDirection  # noqa: E402
from goldedge.domain.services.trade_scorer import TradeScorer  # noqa: E402

OUT_DIR = ROOT / "backtest_results" / "audit" / "task_g2_scorer"
OUT_DIR.mkdir(parents=True, exist_ok=True)

REPLAY_START = datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc)
REPLAY_END = datetime(2026, 11, 19, 23, 59, 59, tzinfo=timezone.utc)
PERSIST_RUN_ID = "g2_verified_full_20261122"


def _git_sha() -> str:
    try:
        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return "unavailable_in_container"


def _reconcile_scenario(app_res: dict[str, Any], clean_ref: dict[str, Any], name: str) -> dict[str, Any]:
    mismatches: list[str] = []

    if int(app_res["trades"]) != int(clean_ref["trades"]):
        mismatches.append(f"trades app={app_res['trades']} clean={clean_ref['trades']}")
    if int(app_res["wins"]) != int(clean_ref["wins"]):
        mismatches.append(f"wins app={app_res['wins']} clean={clean_ref['wins']}")
    if int(app_res["losses"]) != int(clean_ref["losses"]):
        mismatches.append(f"losses app={app_res['losses']} clean={clean_ref['losses']}")
    if abs(float(app_res["pnl"]) - float(clean_ref["pnl"])) > 0.01:
        mismatches.append(f"pnl app={app_res['pnl']:.4f} clean={clean_ref['pnl']:.4f}")
    if abs(float(app_res["pf"]) - float(clean_ref["pf"])) > 0.002:
        mismatches.append(f"pf app={app_res['pf']:.4f} clean={clean_ref['pf']:.4f}")
    if abs(float(app_res["closed_max_dd"]) - float(clean_ref["closed_max_dd"])) > 0.01:
        mismatches.append(f"closed_max_dd app={app_res['closed_max_dd']:.4f} clean={clean_ref['closed_max_dd']:.4f}")
    if abs(float(app_res["mtm_max_dd"]) - float(clean_ref["mtm_max_dd"])) > 0.01:
        mismatches.append(f"mtm_max_dd app={app_res['mtm_max_dd']:.4f} clean={clean_ref['mtm_max_dd']:.4f}")
    if abs(float(app_res["mtm_max_dd_pct"]) - float(clean_ref["mtm_max_dd_pct"])) > 0.005:
        mismatches.append(f"mtm_max_dd_pct app={app_res['mtm_max_dd_pct']:.4f} clean={clean_ref['mtm_max_dd_pct']:.4f}")
    if abs(float(app_res["total_commission_usd"]) - float(clean_ref["total_commission_usd"])) > 0.02:
        mismatches.append(f"comm app={app_res['total_commission_usd']:.4f} clean={clean_ref['total_commission_usd']:.4f}")
    if abs(float(app_res["total_slippage_usd"]) - float(clean_ref["total_slippage_usd"])) > 0.02:
        mismatches.append(f"slip app={app_res['total_slippage_usd']:.4f} clean={clean_ref['total_slippage_usd']:.4f}")
    if abs(float(app_res["total_swap_usd"]) - float(clean_ref["total_swap_usd"])) > 0.02:
        mismatches.append(f"swap app={app_res['total_swap_usd']:.4f} clean={clean_ref['total_swap_usd']:.4f}")

    app_hwm = app_res.get("hwm_kill_switch") or {}
    cln_hwm = clean_ref.get("hwm_kill_switch") or {}
    if bool(app_hwm.get("halted")) != bool(cln_hwm.get("halted")):
        mismatches.append(f"hwm.halted app={app_hwm.get('halted')} clean={cln_hwm.get('halted')}")
    if app_hwm.get("halt_timestamp") and cln_hwm.get("trigger_time"):
        t_app = datetime.fromisoformat(str(app_hwm["halt_timestamp"]))
        t_cln = datetime.fromisoformat(str(cln_hwm["trigger_time"]))
        if abs((t_app - t_cln).total_seconds()) > 1e-3:
            mismatches.append(f"hwm.ts app={t_app.isoformat()} clean={t_cln.isoformat()}")

    app_trades = app_res.get("trade_list", [])
    cln_trades = clean_ref.get("trade_list", [])
    max_pnl_diff = 0.0
    max_score_diff = 0.0
    max_px_diff = 0.0
    max_qty_diff = 0.0
    for idx, (at, ct) in enumerate(zip(app_trades, cln_trades)):
        for fld in ("entry_time", "exit_time", "direction", "strategy", "regime", "exit_reason"):
            if str(at[fld]) != str(ct[fld]):
                mismatches.append(f"trade[{idx}].{fld} app={at[fld]} clean={ct[fld]}")
        sd = abs(float(at["score"]) - float(ct["score"]))
        pd_ = abs(float(at["pnl"]) - float(ct["pnl"]))
        epd = max(abs(float(at["entry_price"]) - float(ct["entry_price"])), abs(float(at["exit_price"]) - float(ct["exit_price"])))
        qd = abs(float(at["qty"]) - float(ct["qty"]))
        max_score_diff = max(max_score_diff, sd)
        max_pnl_diff = max(max_pnl_diff, pd_)
        max_px_diff = max(max_px_diff, epd)
        max_qty_diff = max(max_qty_diff, qd)
        if sd > 1e-4:
            mismatches.append(f"trade[{idx}].score diff={sd:.6f}")
        if pd_ > 0.01:
            mismatches.append(f"trade[{idx}].pnl diff={pd_:.4f} app={at['pnl']} clean={ct['pnl']}")
        if epd > 1e-4:
            mismatches.append(f"trade[{idx}].price diff={epd:.6f}")
        if qd > 1e-4:
            mismatches.append(f"trade[{idx}].qty diff={qd:.6f}")

    return {
        "scenario": name,
        "app_summary": {
            "trades": int(app_res["trades"]),
            "wins": int(app_res["wins"]),
            "losses": int(app_res["losses"]),
            "pnl": round(float(app_res["pnl"]), 4),
            "pf": round(float(app_res["pf"]), 4),
            "closed_max_dd": round(float(app_res["closed_max_dd"]), 4),
            "closed_max_dd_pct": round(float(app_res["closed_max_dd_pct"]), 4),
            "mtm_max_dd": round(float(app_res["mtm_max_dd"]), 4),
            "mtm_max_dd_pct": round(float(app_res["mtm_max_dd_pct"]), 4),
            "total_commission_usd": round(float(app_res["total_commission_usd"]), 4),
            "total_slippage_usd": round(float(app_res["total_slippage_usd"]), 4),
            "total_swap_usd": round(float(app_res["total_swap_usd"]), 4),
            "scorer_mode": app_res.get("scorer_mode"),
            "max_score_threshold": app_res.get("max_score_threshold"),
            "hwm_halted": bool(app_hwm.get("halted")),
            "hwm_halt_timestamp": app_hwm.get("halt_timestamp"),
        },
        "clean_room_summary": {
            "trades": int(clean_ref["trades"]),
            "wins": int(clean_ref["wins"]),
            "losses": int(clean_ref["losses"]),
            "pnl": round(float(clean_ref["pnl"]), 4),
            "pf": round(float(clean_ref["pf"]), 4),
            "closed_max_dd": round(float(clean_ref["closed_max_dd"]), 4),
            "mtm_max_dd": round(float(clean_ref["mtm_max_dd"]), 4),
            "mtm_max_dd_pct": round(float(clean_ref["mtm_max_dd_pct"]), 4),
            "hwm_halted": bool(cln_hwm.get("halted")),
            "hwm_trigger_time": cln_hwm.get("trigger_time"),
        },
        "max_abs_trade_score_diff": round(max_score_diff, 8),
        "max_abs_trade_pnl_diff": round(max_pnl_diff, 6),
        "max_abs_trade_price_diff": round(max_px_diff, 6),
        "max_abs_trade_qty_diff": round(max_qty_diff, 6),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:25],
    }


def _reconcile_candidate_scores(candidate_samples: list[dict[str, Any]]) -> dict[str, Any]:
    scorer = TradeScorer(w_ml=0.0, w_quant=1.0)

    max_leg_diff = 0.0
    max_g2_diff = 0.0
    max_sub_diff = 0.0
    leg_mismatches = 0
    g2_mismatches = 0
    sub_mismatches = 0
    eligible_count = 0

    for row in candidate_samples:
        sig = Signal(
            strategy=str(row["strategy"]),
            direction=SignalDirection(str(row["direction"])),
            symbol="XAUUSD",
            entry_price=float(row["close"]),
            technical_strength=1.0,
        )
        feat = dict(row["feat"])
        reg = str(row["regime"])
        if bool(row["is_eligible_setup"]):
            eligible_count += 1

        st_leg = scorer.score_signal(sig, regime=reg, features=feat, scorer_mode="legacy")
        dl = abs(float(st_leg.final_score) - float(row["legacy_score"]))
        max_leg_diff = max(max_leg_diff, dl)
        if dl > 1e-6:
            leg_mismatches += 1

        st_g2 = scorer.score_signal(sig, regime=reg, features=feat, scorer_mode="g2_calibrated")
        dg = abs(float(st_g2.final_score) - float(row["g2_score"]))
        max_g2_diff = max(max_g2_diff, dg)
        if dg > 1e-6:
            g2_mismatches += 1

        det = st_g2.details
        comp_ref = row["g2_comp"]
        for app_k, ref_k in (
            ("sub_mom_kalman", "sub_mom_kalman"),
            ("sub_er", "sub_er"),
            ("sub_entropy", "sub_entropy"),
            ("sub_ema_slope", "sub_ema_slope"),
            ("sub_twp_breakout", "sub_twp_or_ext"),
            ("sub_session", "sub_session"),
            ("trr_mult", "trr_mult"),
            ("ext_mult", "ext_mult"),
        ):
            ds = abs(float(det[app_k]) - float(comp_ref[ref_k]))
            max_sub_diff = max(max_sub_diff, ds)
            if ds > 1e-6:
                sub_mismatches += 1

    return {
        "total_candidate_rows_checked": int(len(candidate_samples)),
        "eligible_rows_checked": int(eligible_count),
        "legacy_max_abs_diff": round(max_leg_diff, 8),
        "g2_max_abs_diff": round(max_g2_diff, 8),
        "g2_subcomponent_max_abs_diff": round(max_sub_diff, 8),
        "legacy_mismatches": int(leg_mismatches),
        "g2_mismatches": int(g2_mismatches),
        "subcomponent_mismatches": int(sub_mismatches),
    }


def main() -> None:
    ref_path = Path("/tmp/g2_clean_room_refs.json")
    clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    clean_env["PYTHONPATH"] = f"{ROOT}:{ROOT / 'scripts' / 'audit'}"
    print("[G2 Phase F] Exporting clean-room references in isolated subprocess (without src/)...", flush=True)
    proc = subprocess.run(
        [sys.executable, "-u", str(ROOT / "scripts" / "audit" / "g2_clean_room_export_refs.py"), str(ref_path)],
        cwd=str(ROOT),
        env=clean_env,
        check=True,
    )
    assert proc.returncode == 0
    refs = json.loads(ref_path.read_text(encoding="utf-8"))

    cand_recon = _reconcile_candidate_scores(refs["candidate_samples"])
    print(f"[G2 Phase F] Candidate Score Reconciliation (n={cand_recon['total_candidate_rows_checked']}): {cand_recon}", flush=True)

    container = Container()
    container.config.from_dict(settings.model_dump())

    def _run_app(scorer_mode: str, max_score_threshold: float | None) -> dict[str, Any]:
        uc = container.run_backtest_uc()
        return uc.execute(
            "XAUUSD",
            REPLAY_START,
            REPLAY_END,
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
            max_score_threshold=max_score_threshold,
            scorer_mode=scorer_mode,
        )

    print("[G2 Phase F] Running actual RunBacktestUseCase for H0...", flush=True)
    app_h0 = _run_app(scorer_mode="legacy", max_score_threshold=None)
    print("[G2 Phase F] Running actual RunBacktestUseCase for H3b...", flush=True)
    app_h3b = _run_app(scorer_mode="legacy", max_score_threshold=0.80)
    print("[G2 Phase F] Running actual RunBacktestUseCase for G2 (g2_calibrated)...", flush=True)
    app_g2 = _run_app(scorer_mode="g2_calibrated", max_score_threshold=None)

    rec_h0 = _reconcile_scenario(app_h0, refs["H0"], "H0_LEGACY")
    rec_h3b = _reconcile_scenario(app_h3b, refs["H3b"], "H3b_LEGACY_CAP_080")
    rec_g2 = _reconcile_scenario(app_g2, refs["G2"], "G2_CALIBRATED")

    # PostgreSQL persistence + REST API exposure check for verified G2 run
    backtest_repo = container.backtest_repo()
    metrics_g2 = bt_routes._extract_run_metrics(
        app_g2,
        strategy_mode="candidate_15_e1",
        commission_per_lot=0.0,
        kalman_comp=None,
        commission_profile="fusion_ctrader_demo",
        slippage_pips=0.0533,
        apply_swap=True,
        max_score_threshold=None,
        scorer_mode="g2_calibrated",
    )
    backtest_repo.save_run({
        "run_id": PERSIST_RUN_ID,
        "symbol": "XAUUSD",
        "risk": "medium",
        "exit_agent": "Kalman",
        "start_date": REPLAY_START.strftime("%Y-%m-%d"),
        "end_date": REPLAY_END.strftime("%Y-%m-%d"),
        "trades": app_g2["trades"],
        "win_rate": app_g2["win_rate"],
        "pnl": app_g2["pnl"],
        "max_dd": app_g2["max_dd"],
        "pf": app_g2["pf"],
        "avg_win": app_g2["avg_win"],
        "avg_loss": app_g2["avg_loss"],
        "rr": app_g2["rr"],
        "equity_curve": app_g2["equity_curve"],
        "trade_list": app_g2["trade_list"],
        "status": "done",
        "strategy_mode": "candidate_15_e1",
        "metrics": metrics_g2,
    })

    # Verify PostgreSQL SQL readback & Flask endpoint
    with container.db_engine().connect() as conn:
        db_row = conn.execute(
            text(
                "SELECT run_id, trades, pnl, pf, metrics->>'scorer_mode' AS sm, "
                "metrics->>'mtm_max_dd_pct' AS mtm_dd, jsonb_array_length(trade_list) AS tl_len "
                "FROM backtest_runs WHERE run_id = :rid"
            ),
            {"rid": PERSIST_RUN_ID},
        ).mappings().first()

    app = Flask(__name__)
    app.register_blueprint(bt_routes.bp)
    with app.test_request_context(f"/api/backtest/history/{PERSIST_RUN_ID}", method="GET"):
        raw_fn = bt_routes.get_backtest_history_detail.__wrapped__.__wrapped__
        api_detail = raw_fn(PERSIST_RUN_ID, backtest_repo=backtest_repo).get_json()
    with app.test_request_context("/api/backtest/run", method="POST", json={"start_date": "2026-10-01", "end_date": "2026-10-15", "scorer_mode": "invalid_mode"}):
        raw_run = bt_routes.run_backtest.__wrapped__.__wrapped__
        inv_resp, inv_status = raw_run(run_backtest_uc=container.run_backtest_uc(), backtest_repo=backtest_repo)

    pg_api_ok = (
        db_row is not None
        and int(db_row["trades"]) == int(app_g2["trades"])
        and int(db_row["tl_len"]) == int(app_g2["trades"])
        and abs(float(db_row["pnl"]) - float(app_g2["pnl"])) < 0.01
        and str(db_row["sm"]) == "g2_calibrated"
        and api_detail is not None
        and int(api_detail["trades"]) == int(app_g2["trades"])
        and len(api_detail["trade_list"]) == int(app_g2["trades"])
        and api_detail["metrics"]["scorer_mode"] == "g2_calibrated"
        and inv_status == 400
    )

    total_mismatches = (
        rec_h0["mismatch_count"]
        + rec_h3b["mismatch_count"]
        + rec_g2["mismatch_count"]
        + cand_recon["legacy_mismatches"]
        + cand_recon["g2_mismatches"]
        + cand_recon["subcomponent_mismatches"]
    )

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_head_sha": _git_sha(),
        "dataset_fingerprint": refs["dataset_fingerprint"],
        "config_and_costs": {
            "profile": "MEDIUM_BACKTEST",
            "use_kalman_exit": True,
            "session_close_utc": 15,
            "early_fail_mins": 15,
            "early_fail_min_mfe_atr": 0.4,
            "commission_profile": "fusion_ctrader_demo",
            "account_id": 48439552,
            "slippage_pips": 0.0533,
            "apply_swap": True,
            "max_hwm_drawdown_pct": 8.0,
        },
        "candidate_score_reconciliation": cand_recon,
        "scenarios": {
            "H0": rec_h0,
            "H3b": rec_h3b,
            "G2": rec_g2,
        },
        "postgres_and_api_verification": {
            "persisted_run_id": PERSIST_RUN_ID,
            "db_row": dict(db_row) if db_row else None,
            "api_detail_trades": len(api_detail.get("trade_list", [])) if api_detail else 0,
            "api_detail_scorer_mode": (api_detail.get("metrics") or {}).get("scorer_mode") if api_detail else None,
            "invalid_scorer_mode_status": int(inv_status),
            "invalid_scorer_mode_error": inv_resp.get_json(),
            "verified": bool(pg_api_ok),
        },
        "total_mismatches": int(total_mismatches),
        "all_scenarios_zero_mismatch": bool(total_mismatches == 0 and pg_api_ok),
    }

    out_path = OUT_DIR / "g2_application_reconciliation.json"
    out_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "H0_mismatch": rec_h0["mismatch_count"],
        "H3b_mismatch": rec_h3b["mismatch_count"],
        "G2_mismatch": rec_g2["mismatch_count"],
        "candidate_score_mismatches": cand_recon["legacy_mismatches"] + cand_recon["g2_mismatches"] + cand_recon["subcomponent_mismatches"],
        "postgres_and_api_verified": pg_api_ok,
        "all_scenarios_zero_mismatch": out["all_scenarios_zero_mismatch"],
    }, indent=2))
    assert out["all_scenarios_zero_mismatch"], f"Phase F verification failed: mismatches={total_mismatches}, pg_api_ok={pg_api_ok}"


if __name__ == "__main__":
    main()
