"""
Task G1 — Step 0: Provenance, Frozen Baseline Integrity, Safety Locks, and Dataset Fingerprint.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

import psycopg2

ROOT = Path(__file__).resolve().parent.parent.parent
if not (ROOT / "src").exists() and Path("/app/src").exists():
    ROOT = Path("/app")
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from goldedge.config.settings import settings

G1_DIR = ROOT / "backtest_results" / "audit" / "task_g1_root_cause"
G1_DIR.mkdir(parents=True, exist_ok=True)


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _stream_fingerprint(conn, start_iso: str, end_iso: str, end_inclusive: bool = True) -> dict:
    op = "<=" if end_inclusive else "<"
    h_clean = hashlib.sha256()
    cnt = 0
    with conn.cursor(name="g1_stream_fp") as cur:
        cur.itersize = 50000
        cur.execute(
            f"""
            SELECT time, bid, ask, last, volume, flags
            FROM market_ticks
            WHERE symbol = 'XAUUSD'
              AND time >= %s AND time {op} %s
            ORDER BY time ASC
            """,
            (start_iso, end_iso),
        )
        for t, bid, ask, last, vol, flags in cur:
            cnt += 1
            t_utc = t.astimezone(timezone.utc)
            h_clean.update(f"{t_utc.isoformat()}|{bid}|{ask}|{last}|{vol}|{flags}\n".encode("utf-8"))
    return {"rows": cnt, "stream_sha256": h_clean.hexdigest().upper()}


def main() -> None:
    db_url = os.environ.get("DB_URL", settings.DB_URL).replace("+psycopg2", "")
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            # 1. Export exact baseline run bt_20261121_191649_a03728
            cur.execute(
                """
                SELECT run_id, symbol, risk, exit_agent, start_date, end_date,
                       trades, win_rate, pnl, max_dd, pf, avg_win, avg_loss, rr,
                       status, error, strategy_mode, metrics, equity_curve, trade_list, created_at
                FROM backtest_runs
                WHERE run_id = 'bt_20261121_191649_a03728'
                """
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("Baseline DB run bt_20261121_191649_a03728 not found")
            cols = [d[0] for d in cur.description]
            db_run = dict(zip(cols, row))
            for k in ("start_date", "end_date", "created_at"):
                if db_run[k] is not None:
                    db_run[k] = str(db_run[k])
            (G1_DIR / "g1_original_db_run.json").write_text(
                json.dumps(db_run, indent=2, default=str), encoding="utf-8"
            )

            # 2. Safety checks
            cur.execute("SELECT COUNT(*) FROM market_orders")
            market_orders = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM market_trades")
            market_trades = int(cur.fetchone()[0])

            # 3. Quote quality & warmup checks
            cur.execute(
                """
                SELECT
                    COUNT(*) AS total_rows,
                    COUNT(*) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS valid_rows,
                    COUNT(*) FILTER (WHERE bid > 0 AND ask > 0 AND bid > ask) AS inverted_rows,
                    COUNT(*) FILTER (WHERE bid > 0 AND ask > 0 AND bid = ask) AS zero_spread_rows,
                    COUNT(*) FILTER (WHERE bid <= 0 OR ask <= 0 OR bid IS NULL OR ask IS NULL) AS non_positive_rows,
                    COUNT(DISTINCT time::date) AS distinct_days,
                    COUNT(volume) AS non_null_volume_rows,
                    MIN(time) AS min_time,
                    MAX(time) AS max_time
                FROM market_ticks
                WHERE symbol = 'XAUUSD'
                  AND time >= '2026-08-13T00:00:00Z'
                  AND time <= '2026-11-19T23:59:59Z'
                """
            )
            stats_row = cur.fetchone()

            cur.execute(
                """
                SELECT COUNT(*), MIN(time), MAX(time)
                FROM market_ticks
                WHERE symbol = 'XAUUSD'
                  AND time >= '2026-08-09T00:00:00Z'
                  AND time < '2026-08-13T00:00:00Z'
                """
            )
            wu_row = cur.fetchone()

            cur.execute(
                """
                SELECT COUNT(*), MIN(time), MAX(time)
                FROM market_ticks
                WHERE symbol = 'XAUUSD'
                """
            )
            all_row = cur.fetchone()

            # Check duplicate timestamps & out-of-order / chunk boundaries
            cur.execute(
                """
                WITH dup AS (
                    SELECT time, COUNT(*) AS c
                    FROM market_ticks
                    WHERE symbol = 'XAUUSD'
                      AND time >= '2026-08-13T00:00:00Z'
                      AND time <= '2026-11-19T23:59:59Z'
                    GROUP BY time
                    HAVING COUNT(*) > 1
                )
                SELECT COUNT(*), COALESCE(SUM(c), 0) FROM dup
                """
            )
            dup_ts_count, dup_row_total = cur.fetchone()

        fp_full = _stream_fingerprint(conn, "2026-08-13T00:00:00Z", "2026-11-19T23:59:59Z", end_inclusive=True)
        fp_warm_dev = _stream_fingerprint(conn, "2026-08-13T00:00:00Z", "2026-08-17T00:00:00Z", end_inclusive=False)
        fp_warmed_main = _stream_fingerprint(conn, "2026-08-17T00:00:00Z", "2026-11-19T23:59:59Z", end_inclusive=True)

        provenance = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "git_commit": "1bf6c45b3f5b790e3212c9b5b7a683d6fd5e3622",
            "safety": {
                "SHADOW_MODE": bool(settings.SHADOW_MODE),
                "TRADING_MODE": str(settings.TRADING_MODE),
                "market_orders": market_orders,
                "market_trades": market_trades,
            },
            "frozen_hashes_sha256": {
                "frozen_111_baseline_trades.csv": _file_sha256(ROOT / "backtest_results" / "frozen_111_baseline_trades.csv"),
                "frozen_v2_w1_trades.csv": _file_sha256(ROOT / "backtest_results" / "audit" / "frozen_v2_recovered" / "task6c_app_w1_trades.csv"),
                "frozen_v2_w2_trades.csv": _file_sha256(ROOT / "backtest_results" / "audit" / "frozen_v2_recovered" / "task6c_app_w2_trades.csv"),
                "frozen_v2_w3_trades.csv": _file_sha256(ROOT / "backtest_results" / "audit" / "frozen_v2_recovered" / "task6c_app_w3_trades.csv"),
                "frozen_v2_manifest.json": _file_sha256(ROOT / "backtest_results" / "audit" / "frozen_v2_recovered" / "task6c_frozen_manifest.json"),
                "task8_app_comparison.json": _file_sha256(ROOT / "backtest_results" / "audit" / "task8_app_comparison.json"),
                "task11a_candidate_reconciliation.json": _file_sha256(ROOT / "backtest_results" / "audit" / "task11a_candidate_reconciliation.json"),
                "task12_trade_count_reconciliation.json": _file_sha256(ROOT / "backtest_results" / "audit" / "task12_session_analysis" / "task12_trade_count_reconciliation.json"),
                "g1_original_70_baseline_trades.csv": _file_sha256(G1_DIR / "g1_original_70_baseline_trades.csv"),
                "g1_original_70_baseline_journeys.json": _file_sha256(G1_DIR / "g1_original_70_baseline_journeys.json"),
                "g1_original_db_run.json": _file_sha256(G1_DIR / "g1_original_db_run.json"),
            },
            "baseline_db_run_summary": {
                "run_id": db_run["run_id"],
                "symbol": db_run["symbol"],
                "risk": db_run["risk"],
                "exit_agent": db_run["exit_agent"],
                "strategy_mode": db_run["strategy_mode"],
                "start_date": db_run["start_date"],
                "end_date": db_run["end_date"],
                "trades": int(db_run["trades"]),
                "win_rate": float(db_run["win_rate"]),
                "pnl": float(db_run["pnl"]),
                "max_dd": float(db_run["max_dd"]),
                "pf": float(db_run["pf"]),
                "metrics_summary": {k: v for k, v in (db_run["metrics"] or {}).items() if k not in ("daily_breakdown",)},
            },
            "dataset_xauusd_2026_08_13_to_2026_11_19": {
                "total_rows": int(stats_row[0]),
                "valid_rows": int(stats_row[1]),
                "inverted_rows": int(stats_row[2]),
                "zero_spread_rows": int(stats_row[3]),
                "non_positive_rows": int(stats_row[4]),
                "distinct_days": int(stats_row[5]),
                "non_null_volume_rows": int(stats_row[6]),
                "min_time_utc": stats_row[7].astimezone(timezone.utc).isoformat() if stats_row[7] else None,
                "max_time_utc": stats_row[8].astimezone(timezone.utc).isoformat() if stats_row[8] else None,
                "duplicate_timestamps": int(dup_ts_count),
                "duplicate_rows_total": int(dup_row_total),
                "warmup_2026_08_09_to_13_rows": int(wu_row[0]),
                "all_db_rows": int(all_row[0]),
                "all_db_min_time_utc": all_row[1].astimezone(timezone.utc).isoformat() if all_row[1] else None,
                "all_db_max_time_utc": all_row[2].astimezone(timezone.utc).isoformat() if all_row[2] else None,
                "fingerprint_full_20260813_20261119": fp_full,
                "fingerprint_warmup_20260813_20260817": fp_warm_dev,
                "fingerprint_warmed_20260817_20261119": fp_warmed_main,
            },
        }
        (G1_DIR / "g1_provenance_snapshot.json").write_text(
            json.dumps(provenance, indent=2), encoding="utf-8"
        )
        print(json.dumps({
            "safety": provenance["safety"],
            "frozen_111": provenance["frozen_hashes_sha256"]["frozen_111_baseline_trades.csv"],
            "g1_70_csv": provenance["frozen_hashes_sha256"]["g1_original_70_baseline_trades.csv"],
            "dataset": provenance["dataset_xauusd_2026_08_13_to_2026_11_19"],
        }, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
