"""
Task 8 (Phase 6) — Actual GoldEdge Application Backtest Comparison:
Compares frozen `RECOVERED_FULL_HISTORY_BASELINE_V2` (no 8% kill-switch)
against `RunBacktestUseCase(..., max_hwm_drawdown_pct=8.0)` across W1, W2, and W3,
and verifies `max_hwm_drawdown_pct=None` still reproduces W3 bit-for-bit.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
if not (_ROOT / "src").exists() and Path("/app/src").exists():
    _ROOT = Path("/app")
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from goldedge.adapters.secondary.virtual_broker import VirtualBrokerAdapter
from goldedge.application.use_cases.run_backtest import RunBacktestUseCase
from goldedge.config.container import Container
from goldedge.config.risk_profiles import MEDIUM_RISK_BACKTEST

AUDIT_DIR = _ROOT / "backtest_results" / "audit"
FROZEN_V2_MANIFEST = AUDIT_DIR / "frozen_v2_recovered" / "task6c_frozen_manifest.json"


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _max_consecutive_losses(pnls: list[float]) -> int:
    best = 0
    cur = 0
    for p in pnls:
        if round(p, 2) < 0.0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _summarize_window(wid: str, label: str, start_dt: datetime, end_dt: datetime, res: dict, trades_csv: Path, journeys_json: Path) -> dict:
    trades_bytes = trades_csv.read_bytes()
    journeys_bytes = journeys_json.read_bytes()
    df = pd.read_csv(trades_csv)
    buy_df = df[df["direction"] == "BUY"] if len(df) else df
    sell_df = df[df["direction"] == "SELL"] if len(df) else df
    daily_pnl = df.groupby(pd.to_datetime(df["exit_time"], utc=True).dt.strftime("%Y-%m-%d"))["pnl"].sum() if len(df) else pd.Series(dtype=float)

    return {
        "window_id": wid,
        "label": label,
        "start_utc": start_dt.isoformat(),
        "end_utc": end_dt.isoformat(),
        "trades_csv_sha256": _sha256_bytes(trades_bytes),
        "journeys_json_sha256": _sha256_bytes(journeys_bytes),
        "trades": int(res["trades"]),
        "wins": int(res["wins"]),
        "losses": int(res["losses"]),
        "breakevens": int(res["breakevens"]),
        "win_rate_pct": round(float(res["win_rate"]) * 100.0, 4),
        "gross_pnl_usd": round(float(df["gross_pnl"].sum()), 4) if len(df) else 0.0,
        "commission_usd": round(float(df["commission_usd"].sum()), 4) if len(df) else 0.0,
        "slippage_usd": round(float(df["slippage_usd"].sum()), 4) if len(df) else 0.0,
        "swap_usd": round(float(df["swap_usd"].sum()), 4) if len(df) else 0.0,
        "net_pnl_usd": round(float(res["pnl"]), 4),
        "final_equity_usd": round(float(res["final_equity"]), 4),
        "profit_factor": round(float(res["pf"]), 6),
        "closed_max_dd_usd": round(float(res["closed_max_dd"]), 4),
        "closed_max_dd_pct": round(float(res["closed_max_dd_pct"]), 4),
        "mtm_max_dd_usd": round(float(res["mtm_max_dd"]), 4),
        "mtm_max_dd_pct": round(float(res["mtm_max_dd_pct"]), 4),
        "mtm_max_dd_initial_pct": round(float(res["mtm_max_dd_initial_pct"]), 4),
        "mtm_peak_time": res.get("mtm_peak_time"),
        "mtm_trough_time": res.get("mtm_trough_time"),
        "buy_trades": int(len(buy_df)),
        "buy_net_pnl_usd": round(float(buy_df["pnl"].sum()), 4) if len(buy_df) else 0.0,
        "sell_trades": int(len(sell_df)),
        "sell_net_pnl_usd": round(float(sell_df["pnl"].sum()), 4) if len(sell_df) else 0.0,
        "max_consecutive_losses": _max_consecutive_losses([float(x) for x in df["pnl"].tolist()]) if len(df) else 0,
        "worst_daily_pnl_usd": round(float(daily_pnl.min()), 4) if len(daily_pnl) else 0.0,
        "best_daily_pnl_usd": round(float(daily_pnl.max()), 4) if len(daily_pnl) else 0.0,
        "trades_prevented_by_kill_switch": int((res.get("hwm_kill_switch") or {}).get("trades_prevented", 0)),
        "hwm_kill_switch": res.get("hwm_kill_switch"),
    }


def main() -> None:
    manifest = json.loads(FROZEN_V2_MANIFEST.read_text(encoding="utf-8"))
    frozen_app = manifest["application_summary"]

    container = Container()
    tick_repo = container.tick_repo()
    sizing_service = container.sizing_service()

    saved_default_trades = _ROOT / "backtest_results" / "tick_replay_trades.csv"
    saved_default_journeys = _ROOT / "backtest_results" / "journeys_medium_backtest.json"
    orig_trades_bytes = saved_default_trades.read_bytes() if saved_default_trades.exists() else None
    orig_journeys_bytes = saved_default_journeys.read_bytes() if saved_default_journeys.exists() else None

    windows = [
        ("W1", "Original Task 4 Window (2026-08-24 -> 2026-11-16)", datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc)),
        ("W2", "Recovered Original Span (2026-08-13 -> 2026-11-16)", datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc)),
        ("W3", "Recovered Warmed Full (2026-08-17 -> 2026-11-18)", datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 18, 18, 0, 0, tzinfo=timezone.utc)),
    ]

    comparison = {"windows": {}, "baseline_v2_repro_check": {}}
    csv_rows = []
    try:
        # 1) Verify W3 with max_hwm_drawdown_pct=None still reproduces frozen V2 bit-for-bit
        wid, label, start_dt, end_dt = windows[2]
        print(f"\n[TASK 8 PHASE 6] Verifying baseline parity (max_hwm_drawdown_pct=None) on {wid}...", flush=True)
        uc_base = RunBacktestUseCase(
            tick_repo=tick_repo,
            virtual_broker=VirtualBrokerAdapter(),
            manage_position=None,
            sizing_service=sizing_service,
            profile=MEDIUM_RISK_BACKTEST,
            expected_pnl_model=None,
            min_expected_pnl=None,
        )
        res_base = uc_base.execute(
            "XAUUSD",
            start_dt,
            end_dt,
            initial_capital=10000.0,
            use_kalman_exit=False,
            commission_profile="fusion_ctrader_demo",
            trading_environment="demo",
            account_id=48439552,
            slippage_pips=0.0533,
            apply_swap=True,
            use_tick_intensity_features=True,
            max_hwm_drawdown_pct=None,
        )
        w3_off_csv_sha = _sha256_bytes(saved_default_trades.read_bytes())
        w3_off_jrn_sha = _sha256_bytes(saved_default_journeys.read_bytes())
        comparison["baseline_v2_repro_check"]["W3_kill_switch_disabled"] = {
            "expected_csv_sha256": frozen_app["W3"]["trades_csv_sha256"],
            "actual_csv_sha256": w3_off_csv_sha,
            "csv_bit_exact": w3_off_csv_sha == frozen_app["W3"]["trades_csv_sha256"],
            "expected_journeys_sha256": frozen_app["W3"]["journeys_json_sha256"],
            "actual_journeys_sha256": w3_off_jrn_sha,
            "journeys_bit_exact": w3_off_jrn_sha == frozen_app["W3"]["journeys_json_sha256"],
            "trades": int(res_base["trades"]),
            "net_pnl_usd": round(float(res_base["pnl"]), 4),
        }

        # 2) Run W1, W2, W3 with 8% HWM Kill-Switch enforced
        for wid, label, start_dt, end_dt in windows:
            print(f"\n[TASK 8 PHASE 6] Running {wid} WITH 8.0% HWM Kill-Switch...", flush=True)
            uc = RunBacktestUseCase(
                tick_repo=tick_repo,
                virtual_broker=VirtualBrokerAdapter(),
                manage_position=None,
                sizing_service=sizing_service,
                profile=MEDIUM_RISK_BACKTEST,
                expected_pnl_model=None,
                min_expected_pnl=None,
            )
            res = uc.execute(
                "XAUUSD",
                start_dt,
                end_dt,
                initial_capital=10000.0,
                use_kalman_exit=False,
                commission_profile="fusion_ctrader_demo",
                trading_environment="demo",
                account_id=48439552,
                slippage_pips=0.0533,
                apply_swap=True,
                use_tick_intensity_features=True,
                max_hwm_drawdown_pct=8.0,
            )

            trades_out = AUDIT_DIR / f"task8_app_{wid.lower()}_hwm_trades.csv"
            journeys_out = AUDIT_DIR / f"task8_app_{wid.lower()}_hwm_journeys.json"
            trades_out.write_bytes(saved_default_trades.read_bytes())
            journeys_out.write_bytes(saved_default_journeys.read_bytes())

            with_ks = _summarize_window(wid, label, start_dt, end_dt, res, trades_out, journeys_out)
            base_v2 = frozen_app[wid]
            comparison["windows"][wid] = {
                "baseline_v2_no_kill_switch": base_v2,
                "app_with_8pct_kill_switch": with_ks,
                "delta": {
                    "trades_delta": with_ks["trades"] - base_v2["trades"],
                    "net_pnl_delta_usd": round(with_ks["net_pnl_usd"] - base_v2["net_pnl_usd"], 4),
                    "mtm_max_dd_pct_reduction": round(base_v2["mtm_max_dd_pct"] - with_ks["mtm_max_dd_pct"], 4),
                    "closed_max_dd_pct_reduction": round(base_v2["closed_max_dd_pct"] - with_ks["closed_max_dd_pct"], 4),
                },
            }
            for variant_name, d in [("V2_NO_KILL_SWITCH", base_v2), ("TASK8_WITH_8PCT_HWM_KS", with_ks)]:
                ks = d.get("hwm_kill_switch") or {}
                csv_rows.append({
                    "window": wid,
                    "variant": variant_name,
                    "trades": d["trades"],
                    "win_rate_pct": d["win_rate_pct"],
                    "net_pnl_usd": d["net_pnl_usd"],
                    "final_equity_usd": d["final_equity_usd"],
                    "profit_factor": d["profit_factor"],
                    "closed_max_dd_usd": d["closed_max_dd_usd"],
                    "closed_max_dd_pct": d["closed_max_dd_pct"],
                    "mtm_max_dd_usd": d["mtm_max_dd_usd"],
                    "mtm_max_dd_pct": d["mtm_max_dd_pct"],
                    "trades_prevented": d.get("trades_prevented_by_kill_switch", 0),
                    "trigger_time": ks.get("halt_timestamp") or ks.get("trigger_time"),
                    "trigger_hwm": ks.get("trigger_hwm"),
                    "trigger_equity": ks.get("trigger_equity"),
                    "trigger_drawdown_pct": ks.get("trigger_drawdown_pct"),
                })

        (AUDIT_DIR / "task8_app_comparison.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
        pd.DataFrame(csv_rows).to_csv(AUDIT_DIR / "task8_app_comparison.csv", index=False)
        print("\n[TASK 8 PHASE 6 COMPLETE]")
        print(json.dumps(comparison, indent=2))
    finally:
        if orig_trades_bytes is not None:
            saved_default_trades.write_bytes(orig_trades_bytes)
        if orig_journeys_bytes is not None:
            saved_default_journeys.write_bytes(orig_journeys_bytes)


if __name__ == "__main__":
    main()
