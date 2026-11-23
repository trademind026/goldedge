"""
Isolated single-process runner for Task 4 Phase 2 reproducibility runs.

Executed via fresh OS subprocess per run so Python interpreter memory,
Kalman filter state, indicator rolling buffers, regime detector state,
virtual broker state, and HWM drawdown trackers start completely from scratch.
"""
from __future__ import annotations

import argparse
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

from goldedge.adapters.secondary.db.tick_repository import TickRepository
from goldedge.adapters.secondary.virtual_broker import VirtualBrokerAdapter
from goldedge.application.use_cases.run_backtest import RunBacktestUseCase
from goldedge.config.risk_profiles import MEDIUM_RISK_BACKTEST
from goldedge.domain.services.sizing_service import PositionSizingService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-idx", type=int, required=True)
    parser.add_argument("--comm", type=float, default=7.0)
    parser.add_argument("--slip", type=float, default=0.3)
    parser.add_argument("--out-prefix", type=str, required=True)
    args = parser.parse_args()

    raw_db_url = os.environ.get(
        "DB_URL", "postgresql+psycopg2://postgres:password@timescaledb:5432/goldedge"
    )
    if raw_db_url.startswith("postgresql://"):
        raw_db_url = raw_db_url.replace("postgresql://", "postgresql+psycopg2://", 1)

    engine = create_engine(raw_db_url)
    tick_repo = TickRepository(engine)
    uc = RunBacktestUseCase(
        tick_repo=tick_repo,
        virtual_broker=VirtualBrokerAdapter(initial_equity=10000.0),
        manage_position=None,
        sizing_service=PositionSizingService(contract_size=100.0),
        profile=MEDIUM_RISK_BACKTEST,
    )

    start_dt = datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc)

    t0 = datetime.now(timezone.utc)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = uc.execute(
            "XAUUSD",
            start_dt,
            end_dt,
            initial_capital=10000.0,
            use_kalman_exit=False,
            session_close_utc=None,
            early_fail_mins=None,
            early_fail_min_mfe_atr=None,
            commission_per_lot=args.comm,
            slippage_pips=args.slip,
        )
    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()

    trades = res["trade_list"]
    for idx, t in enumerate(trades, start=1):
        t["trade_idx"] = idx
        t["signal_id"] = f"{t['entry_time']}|{t['direction']}|{t['strategy']}"

    csv_path = Path(f"{args.out_prefix}.csv")
    json_path = Path(f"{args.out_prefix}.json")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(trades)
    df.to_csv(csv_path, index=False)
    csv_sha = hashlib.sha256(csv_path.read_bytes()).hexdigest()

    metrics_payload = {
        "run_idx": args.run_idx,
        "elapsed_sec": round(elapsed, 2),
        "commission_per_lot": args.comm,
        "slippage_pips": args.slip,
        "trades": int(res["trades"]),
        "wins": int(res.get("wins", sum(1 for t in trades if round(float(t["pnl"]), 2) > 0))),
        "losses": int(res.get("losses", sum(1 for t in trades if round(float(t["pnl"]), 2) < 0))),
        "breakevens": int(res.get("breakevens", sum(1 for t in trades if round(float(t["pnl"]), 2) == 0))),
        "win_rate": float(res["win_rate"]),
        "win_rate_pct": round(float(res["win_rate"]) * 100.0, 4),
        "pf": float(res["pf"]),
        "pnl": float(res["pnl"]),
        "final_equity": float(res["final_equity"]),
        "closed_max_dd": float(res["closed_max_dd"]),
        "closed_max_dd_pct": float(res["closed_max_dd_pct"]),
        "mtm_max_dd": float(res["mtm_max_dd"]),
        "mtm_max_dd_pct": float(res["mtm_max_dd_pct"]),
        "mtm_max_dd_initial_pct": float(res["mtm_max_dd_initial_pct"]),
        "total_commission_usd": float(res["total_commission_usd"]),
        "total_slippage_usd": float(res["total_slippage_usd"]),
        "total_swap_usd": float(res["total_swap_usd"]),
        "sl_slippage_events": int(res["sl_slippage_events"]),
        "worst_sl_gap_usd": float(res["worst_sl_gap_usd"]),
        "inverted_quotes_skipped": int(res["inverted_quotes_skipped"]),
        "zero_spread_ticks_skipped": int(res["zero_spread_ticks_skipped"]),
        "csv_sha256": csv_sha,
    }
    json_path.write_text(json.dumps(metrics_payload, indent=2), encoding="utf-8")
    print(json.dumps(metrics_payload))


if __name__ == "__main__":
    main()
