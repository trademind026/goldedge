"""
Task G2 — Phase G: Full Regression Suite, Live Backend API Check & Safety Verification.

Writes Deliverable 8: `backtest_results/audit/task_g2_scorer/g2_regression_results.json`.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
import urllib.error
import urllib.request

from sqlalchemy import text

ROOT = Path("/app") if Path("/app/src").exists() else Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from goldedge.config.container import Container  # noqa: E402
from goldedge.config.settings import settings  # noqa: E402

OUT_DIR = ROOT / "backtest_results" / "audit" / "task_g2_scorer"
TEST_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/g2_tests")

TEST_FILES = [
    "test_task_g2_validation.py",
    "test_task_g1_validation.py",
    "test_remediation_b1_b6.py",
    "test_shadow_validation.py",
    "test_task6c_validation.py",
    "test_task8_hwm_kill_switch.py",
]


def _run_suite(test_path: Path) -> dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{ROOT}:{SRC}:{test_path.parent}"
    proc = subprocess.run(
        [sys.executable, "-u", str(test_path), "-v"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    m = re.search(r"Ran\s+(\d+)\s+tests?\s+in\s+([0-9.]+)s", combined)
    ran = int(m.group(1)) if m else 0
    dur = float(m.group(2)) if m else 0.0
    skipped_m = re.search(r"skipped=(\d+)", combined)
    skipped = int(skipped_m.group(1)) if skipped_m else 0
    return {
        "file": test_path.name,
        "exit_code": int(proc.returncode),
        "passed": bool(proc.returncode == 0),
        "tests_ran": ran,
        "skipped": skipped,
        "duration_secs": dur,
        "tail_output": "\n".join(combined.strip().splitlines()[-12:]),
    }


def main() -> None:
    suites = []
    for tf in TEST_FILES:
        tp = TEST_DIR / tf
        assert tp.exists(), f"Missing copied test file: {tp}"
        print(f"[G2 Phase G] Running {tf}...", flush=True)
        res = _run_suite(tp)
        print(f"[G2 Phase G]   exit={res['exit_code']} ran={res['tests_ran']} skipped={res['skipped']} ({res['duration_secs']}s)", flush=True)
        suites.append(res)

    container = Container()
    container.config.from_dict(settings.model_dump())

    with container.db_engine().connect() as conn:
        market_orders = int(conn.execute(text("SELECT COUNT(*) FROM market_orders")).scalar() or 0)
        market_trades = int(conn.execute(text("SELECT COUNT(*) FROM market_trades")).scalar() or 0)
        bt_row = conn.execute(
            text(
                "SELECT run_id, trades, pnl, pf, metrics->>'scorer_mode' AS scorer_mode, "
                "metrics->>'mtm_max_dd_pct' AS mtm_max_dd_pct "
                "FROM backtest_runs WHERE run_id = :rid"
            ),
            {"rid": "g2_verified_full_20261122"},
        ).mappings().first()

    # Verify live restarted backend HTTP endpoints on localhost:5000
    http_checks: dict[str, Any] = {}
    try:
        with urllib.request.urlopen("http://localhost:5000/api/backtest/history/g2_verified_full_20261122", timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            http_checks["history_detail_status"] = int(resp.status)
            http_checks["history_detail_trades"] = int(body.get("trades", -1))
            http_checks["history_detail_pnl"] = float(body.get("pnl", 0.0))
            http_checks["history_detail_scorer_mode"] = (body.get("metrics") or {}).get("scorer_mode")
    except Exception as exc:
        http_checks["history_detail_error"] = str(exc)

    try:
        req = urllib.request.Request(
            "http://localhost:5000/api/backtest/run",
            data=json.dumps({"start_date": "2026-10-01", "end_date": "2026-10-15", "scorer_mode": "invalid_mode"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        http_checks["invalid_scorer_mode_status"] = 200
    except urllib.error.HTTPError as he:
        http_checks["invalid_scorer_mode_status"] = int(he.code)
        http_checks["invalid_scorer_mode_body"] = json.loads(he.read().decode("utf-8"))
    except Exception as exc:
        http_checks["invalid_scorer_mode_error"] = str(exc)

    shadow_path = ROOT / "backtest_results" / "shadow_live_rollovers.jsonl"
    shadow_lines = 0
    shadow_last_ts = None
    if shadow_path.exists():
        lines = [ln for ln in shadow_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        shadow_lines = len(lines)
        if lines:
            try:
                last_obj = json.loads(lines[-1])
                shadow_last_ts = last_obj.get("timestamp") or last_obj.get("ts")
            except Exception:
                shadow_last_ts = None

    all_passed = (
        all(s["passed"] for s in suites)
        and market_orders == 0
        and market_trades == 0
        and settings.SHADOW_MODE is True
        and settings.TRADING_MODE == "demo"
        and float(settings.HWM_MAX_DRAWDOWN_PCT) == 8.0
        and http_checks.get("history_detail_status") == 200
        and http_checks.get("history_detail_scorer_mode") == "g2_calibrated"
        and http_checks.get("invalid_scorer_mode_status") == 400
    )

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "total_suites": len(suites),
        "total_tests_ran": sum(s["tests_ran"] for s in suites),
        "total_skipped": sum(s["skipped"] for s in suites),
        "all_suites_passed": all(s["passed"] for s in suites),
        "suites": suites,
        "safety_locks": {
            "SHADOW_MODE": bool(settings.SHADOW_MODE),
            "TRADING_MODE": str(settings.TRADING_MODE),
            "HWM_MAX_DRAWDOWN_PCT": float(settings.HWM_MAX_DRAWDOWN_PCT),
            "market_orders_count": market_orders,
            "market_trades_count": market_trades,
        },
        "shadow_journal": {
            "path": str(shadow_path),
            "event_lines": shadow_lines,
            "last_timestamp": shadow_last_ts,
        },
        "postgres_backtest_run_row": dict(bt_row) if bt_row else None,
        "live_http_api_checks": http_checks,
        "all_checks_passed": bool(all_passed),
    }

    out_path = OUT_DIR / "g2_regression_results.json"
    out_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "all_suites_passed": out["all_suites_passed"],
        "total_tests_ran": out["total_tests_ran"],
        "total_skipped": out["total_skipped"],
        "market_orders": market_orders,
        "market_trades": market_trades,
        "live_http_api_checks": http_checks,
        "all_checks_passed": out["all_checks_passed"],
    }, indent=2))
    assert out["all_checks_passed"], "One or more Phase G checks failed"


if __name__ == "__main__":
    main()
