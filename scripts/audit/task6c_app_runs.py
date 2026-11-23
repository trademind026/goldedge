"""
Task 6C (Phase 4) — Actual GoldEdge Application Backtest Runner across all 3 Windows:
  - W1 (Original Task 4 window): 2026-08-24T00:00:00Z -> 2026-11-16T20:01:23Z (warmup Aug 20..24)
  - W2 (Recovered original span): 2026-08-13T00:00:00Z -> 2026-11-16T20:01:23Z
  - W3 (Recovered warmed full): 2026-08-17T00:00:00Z -> 2026-11-18T18:00:00Z (warmup Aug 13..17)

Captures full-precision trade ledgers (`tick_replay_trades.csv`), per-bar trailing-SL journeys
(`journeys_medium_backtest.json`), and complete MTM/closed/risk metrics for each window.
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
AUDIT_DIR.mkdir(parents=True, exist_ok=True)


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


def main() -> None:
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

    summary = {}
    try:
        for wid, label, start_dt, end_dt in windows:
            print(f"\n[TASK 6C APP] Running {wid}: {label}...", flush=True)
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
            )

            trades_bytes = saved_default_trades.read_bytes()
            journeys_bytes = saved_default_journeys.read_bytes()
            trades_out = AUDIT_DIR / f"task6c_app_{wid.lower()}_trades.csv"
            journeys_out = AUDIT_DIR / f"task6c_app_{wid.lower()}_journeys.json"
            trades_out.write_bytes(trades_bytes)
            journeys_out.write_bytes(journeys_bytes)

            df = pd.read_csv(trades_out)
            buy_df = df[df["direction"] == "BUY"]
            sell_df = df[df["direction"] == "SELL"]
            daily_pnl = df.groupby(pd.to_datetime(df["exit_time"], utc=True).dt.strftime("%Y-%m-%d"))["pnl"].sum()

            summary[wid] = {
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
                "gross_pnl_usd": round(float(df["gross_pnl"].sum()), 4),
                "commission_usd": round(float(df["commission_usd"].sum()), 4),
                "slippage_usd": round(float(df["slippage_usd"].sum()), 4),
                "swap_usd": round(float(df["swap_usd"].sum()), 4),
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
                "buy_net_pnl_usd": round(float(buy_df["pnl"].sum()), 4),
                "sell_trades": int(len(sell_df)),
                "sell_net_pnl_usd": round(float(sell_df["pnl"].sum()), 4),
                "max_consecutive_losses": _max_consecutive_losses([float(x) for x in df["pnl"].tolist()]),
                "worst_daily_pnl_usd": round(float(daily_pnl.min()), 4) if len(daily_pnl) else 0.0,
                "best_daily_pnl_usd": round(float(daily_pnl.max()), 4) if len(daily_pnl) else 0.0,
                "losing_days": int((daily_pnl.round(2) < 0.0).sum()) if len(daily_pnl) else 0,
                "winning_days": int((daily_pnl.round(2) > 0.0).sum()) if len(daily_pnl) else 0,
                "inverted_quotes_skipped": int(res.get("inverted_quotes_skipped", 0)),
                "zero_spread_ticks_skipped": int(res.get("zero_spread_ticks_skipped", 0)),
            }

        (AUDIT_DIR / "task6c_app_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print("\n[TASK 6C APP COMPLETE]")
        print(json.dumps(summary, indent=2))
    finally:
        if orig_trades_bytes is not None:
            saved_default_trades.write_bytes(orig_trades_bytes)
        if orig_journeys_bytes is not None:
            saved_default_journeys.write_bytes(orig_journeys_bytes)


if __name__ == "__main__":
    main()
