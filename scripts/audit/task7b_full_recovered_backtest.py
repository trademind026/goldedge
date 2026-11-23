"""
Run full recovered-history XAUUSD backtests under Verified Demo Costs (`QUOTE_ARRIVAL_SEMANTICS_V1`)
after cleanly backfilling 2026-08-13..2026-08-19 and 2026-11-17..2026-11-18 outages.
"""

from __future__ import annotations

from datetime import datetime, timezone
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


def _summarize(label: str, res: dict, df: pd.DataFrame) -> dict:
    buy_df = df[df["direction"] == "BUY"]
    sell_df = df[df["direction"] == "SELL"]
    return {
        "run_label": label,
        "trades": int(res["trades"]),
        "wins": int(res["wins"]),
        "losses": int(res["losses"]),
        "breakevens": int(res["breakevens"]),
        "win_rate_pct": round(float(res["win_rate"]) * 100.0, 2),
        "gross_pnl_usd": round(float(df["gross_pnl"].sum()), 2),
        "commission_usd": round(float(res["total_commission_usd"]), 2),
        "slippage_usd": round(float(res["total_slippage_usd"]), 2),
        "swap_usd": round(float(res["total_swap_usd"]), 2),
        "net_pnl_usd": round(float(res["pnl"]), 2),
        "final_equity_usd": round(float(res["final_equity"]), 2),
        "profit_factor": round(float(res["pf"]), 4),
        "closed_max_dd_usd": round(float(res["closed_max_dd"]), 2),
        "closed_max_dd_pct": round(float(res["closed_max_dd_pct"]), 2),
        "mtm_max_dd_usd": round(float(res["mtm_max_dd"]), 2),
        "mtm_max_dd_pct": round(float(res["mtm_max_dd_pct"]), 2),
        "mtm_max_dd_initial_pct": round(float(res["mtm_max_dd_initial_pct"]), 2),
        "buy_trades": int(len(buy_df)),
        "buy_net_pnl_usd": round(float(buy_df["pnl"].sum()), 2),
        "sell_trades": int(len(sell_df)),
        "sell_net_pnl_usd": round(float(sell_df["pnl"].sum()), 2),
    }


def main():
    container = Container()
    tick_repo = container.tick_repo()
    sizing_service = container.sizing_service()

    saved_default_trades = _ROOT / "backtest_results" / "tick_replay_trades.csv"
    saved_default_journeys = _ROOT / "backtest_results" / "journeys_medium_backtest.json"
    orig_trades_bytes = saved_default_trades.read_bytes() if saved_default_trades.exists() else None
    orig_journeys_bytes = saved_default_journeys.read_bytes() if saved_default_journeys.exists() else None

    try:
        def _uc():
            return RunBacktestUseCase(
                tick_repo=tick_repo,
                virtual_broker=VirtualBrokerAdapter(),
                manage_position=None,
                sizing_service=sizing_service,
                profile=MEDIUM_RISK_BACKTEST,
                expected_pnl_model=None,
                min_expected_pnl=None,
            )

        print("\n[RECOVERED TEST 1] Warmed Full History (2026-08-17 -> 2026-11-18 18:00Z, Aug 13..16 warmup)...")
        r1 = _uc().execute(
            "XAUUSD",
            datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 18, 18, 0, 0, tzinfo=timezone.utc),
            initial_capital=10000.0,
            use_kalman_exit=False,
            commission_profile="fusion_ctrader_demo",
            trading_environment="demo",
            account_id=48439552,
            slippage_pips=0.0533,
            apply_swap=True,
            use_tick_intensity_features=True,
        )
        df1 = pd.DataFrame(r1["trade_list"])
        df1.to_csv(AUDIT_DIR / "recovered_aug17_nov18_trades.csv", index=False)

        print("\n[RECOVERED TEST 2] Full Archive Span (2026-08-13 -> 2026-11-16 20:01:23Z)...")
        r2 = _uc().execute(
            "XAUUSD",
            datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc),
            initial_capital=10000.0,
            use_kalman_exit=False,
            commission_profile="fusion_ctrader_demo",
            trading_environment="demo",
            account_id=48439552,
            slippage_pips=0.0533,
            apply_swap=True,
            use_tick_intensity_features=True,
        )
        df2 = pd.DataFrame(r2["trade_list"])
        df2.to_csv(AUDIT_DIR / "recovered_aug13_nov16_trades.csv", index=False)

        rows = [
            _summarize("Recovered Warmed Full (2026-08-17 -> 2026-11-18, Verified Demo)", r1, df1),
            _summarize("Recovered Original Span (2026-08-13 -> 2026-11-16, Verified Demo)", r2, df2),
        ]
        pd.DataFrame(rows).to_csv(AUDIT_DIR / "recovered_full_backtest_summary.csv", index=False)
        (AUDIT_DIR / "recovered_full_backtest_summary.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print("\n[RECOVERED SUMMARY]")
        print(json.dumps(rows, indent=2))
    finally:
        if orig_trades_bytes is not None:
            saved_default_trades.write_bytes(orig_trades_bytes)
        if orig_journeys_bytes is not None:
            saved_default_journeys.write_bytes(orig_journeys_bytes)


if __name__ == "__main__":
    main()
