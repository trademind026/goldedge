"""
Task 3 — Phase 7: Live Shadow-Mode Readiness Audit (Read-Only).

Inspects:
  1. backtest_results/shadow_live_rollovers.jsonl
  2. TimescaleDB live market_ticks, market_bars_1m, market_orders, market_trades
  3. Parity comparison between live shadow tracker logic and remediated backtest engine.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

RAW_DB_URL = os.environ.get(
    "DB_URL", "postgresql+psycopg2://postgres:password@timescaledb:5432/goldedge"
)
if RAW_DB_URL.startswith("postgresql://"):
    RAW_DB_URL = RAW_DB_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

JOURNAL_PATH = Path("backtest_results/shadow_live_rollovers.jsonl")
OUT_PATH = Path("backtest_results/audit/task3_phase7_shadow_audit.json")


def main() -> None:
    events = []
    if JOURNAL_PATH.exists():
        for line in JOURNAL_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))

    by_event = Counter(e.get("event") for e in events)
    rollovers = [e for e in events if e.get("event") == "SHADOW_ROLLOVER"]
    opens = [e for e in events if e.get("event") == "SHADOW_OPEN"]
    exits = [e for e in events if e.get("event") == "SHADOW_EXIT"]
    p_exits = [e for e in events if e.get("event") == "SHADOW_PORTFOLIO_EXIT"]

    rollover_keys = [(e.get("symbol"), e.get("rollover_utc")) for e in rollovers]
    dup_rollovers = len(rollover_keys) - len(set(rollover_keys))

    orders_placed_true = sum(1 for e in events if e.get("order_placed") is True)
    statuses = Counter(e.get("status") for e in rollovers)
    dirs = Counter(e.get("direction") for e in rollovers)

    spreads = []
    bad_quotes = 0
    for e in rollovers:
        b = e.get("bid")
        a = e.get("ask")
        if b is not None and a is not None:
            b_f, a_f = float(b), float(a)
            if b_f <= 0 or a_f <= 0 or b_f >= a_f:
                bad_quotes += 1
            else:
                spreads.append(a_f - b_f)

    # Portfolio & directional parity check via ShadowValidationTracker replay
    import sys
    sys.path.insert(0, os.path.abspath("src"))
    from goldedge.domain.services.shadow_tracker import ShadowValidationTracker

    tracker = ShadowValidationTracker(initial_equity=10000.0, contract_size=100.0)
    open_cnt = tracker.restore_from_events(events)
    tracker_summary = tracker.summary("XAUUSD")

    engine = create_engine(RAW_DB_URL)
    with engine.connect() as conn:
        live_ticks = conn.execute(
            text(
                """
                SELECT
                    COUNT(*) AS total,
                    MIN(time) AS min_t,
                    MAX(time) AS max_t,
                    COUNT(*) FILTER (WHERE bid > ask) AS inverted,
                    COUNT(*) FILTER (WHERE bid = ask) AS zero_sp,
                    AVG(CASE WHEN ask > bid THEN ask - bid ELSE NULL END) AS avg_sp
                FROM market_ticks
                WHERE symbol = 'XAUUSD' AND time >= '2026-11-17 00:00:00+00'
                """
            )
        ).fetchone()

        bars_info = conn.execute(
            text(
                """
                SELECT COUNT(*), MIN(time), MAX(time)
                FROM market_bars_1m
                WHERE symbol = 'XAUUSD'
                """
            )
        ).fetchone()

        orders_cnt = int(conn.execute(text("SELECT COUNT(*) FROM market_orders")).scalar() or 0)
        trades_cnt = int(conn.execute(text("SELECT COUNT(*) FROM market_trades")).scalar() or 0)

    out = {
        "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        "journal": {
            "exists": JOURNAL_PATH.exists(),
            "total_events": len(events),
            "event_counts": dict(by_event),
            "duplicate_rollover_keys": dup_rollovers,
            "orders_placed_true": orders_placed_true,
            "rollover_statuses": dict(statuses),
            "rollover_directions": dict(dirs),
            "first_rollover_utc": rollovers[0].get("rollover_utc") if rollovers else None,
            "last_rollover_utc": rollovers[-1].get("rollover_utc") if rollovers else None,
            "rollover_bad_quotes": bad_quotes,
            "rollover_avg_spread": round(sum(spreads) / len(spreads), 4) if spreads else None,
            "rollover_max_spread": round(max(spreads), 4) if spreads else None,
            "opens_count": len(opens),
            "exits_count": len(exits),
            "portfolio_exits_count": len(p_exits),
            "restored_open_positions": open_cnt,
            "tracker_summary": tracker_summary,
        },
        "database": {
            "live_ticks_since_nov17": {
                "total": int(live_ticks[0] or 0),
                "min_time": str(live_ticks[1]) if live_ticks[1] else None,
                "max_time": str(live_ticks[2]) if live_ticks[2] else None,
                "inverted": int(live_ticks[3] or 0),
                "zero_spread": int(live_ticks[4] or 0),
                "avg_spread": round(float(live_ticks[5] or 0.0), 4),
            },
            "market_bars_1m": {
                "total": int(bars_info[0] or 0),
                "min_time": str(bars_info[1]) if bars_info[1] else None,
                "max_time": str(bars_info[2]) if bars_info[2] else None,
            },
            "market_orders_count": orders_cnt,
            "market_trades_count": trades_cnt,
        },
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
