"""
Phase 2 — Reproducible Historical XAUUSD Tick Data Integrity Audit.

Inspects TimescaleDB `market_ticks` and `market_bars_1m`, compares against
available backup archive `backups/csv_goldedge_20261117_143146.zip`, and produces:
  - backtest_results/audit/daily_data_quality.csv
  - backtest_results/audit/phase2_summary.json

Read-only with respect to database tables and strategy code.
"""
import json
import os
import sys
import zipfile
from pathlib import Path

from decimal import Decimal
from sqlalchemy import create_engine, text

raw_db_url = os.environ.get("DB_URL", "postgresql+psycopg2://postgres:password@timescaledb:5432/goldedge")
if raw_db_url.startswith("postgresql://"):
    raw_db_url = raw_db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
DB_URL = raw_db_url
SYMBOL = os.environ.get("AUDIT_SYMBOL", "XAUUSD")
OUT_DIR = Path("backtest_results/audit")


def _json_val(v):
    if v is None or isinstance(v, (int, float, bool, str)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    return str(v)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    engine = create_engine(DB_URL)

    with engine.connect() as conn:
        # 1. Global overview
        overview = conn.execute(
            text("""
                SELECT
                    COUNT(*) AS total_ticks,
                    MIN(time) AS min_time,
                    MAX(time) AS max_time,
                    COUNT(*) FILTER (WHERE bid <= 0 OR ask <= 0 OR bid IS NULL OR ask IS NULL) AS invalid_price_ticks,
                    COUNT(*) FILTER (WHERE bid > ask) AS inverted_quote_ticks,
                    COUNT(*) FILTER (WHERE bid = ask) AS zero_spread_ticks,
                    COUNT(*) FILTER (WHERE (ask - bid) >= 1.0) AS spread_ge_1_00,
                    COUNT(*) FILTER (WHERE (ask - bid) >= 2.0) AS spread_ge_2_00,
                    COUNT(*) FILTER (WHERE (ask - bid) >= 5.0) AS spread_ge_5_00,
                    COUNT(*) FILTER (WHERE volume IS NULL) AS null_volume_ticks,
                    COUNT(*) FILTER (WHERE volume = 0) AS zero_volume_ticks,
                    MIN(ask - bid) AS min_spread,
                    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY ask - bid) AS median_spread,
                    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY ask - bid) AS p95_spread,
                    PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY ask - bid) AS p99_spread,
                    MAX(ask - bid) AS max_spread
                FROM market_ticks
                WHERE symbol = :symbol
            """),
            {"symbol": SYMBOL},
        ).mappings().first()

        # 2. Check chunk boundary duplicates (exactly at 7-day chunk boundaries from 2026-08-13 00:00:00Z)
        boundary_overlap = conn.execute(
            text("""
                WITH boundaries AS (
                    SELECT generate_series(
                        TIMESTAMPTZ '2026-08-13 00:00:00+00',
                        TIMESTAMPTZ '2026-11-18 00:00:00+00',
                        INTERVAL '7 days'
                    ) AS b_time
                )
                SELECT COUNT(*) AS exact_boundary_ticks
                FROM market_ticks m
                JOIN boundaries b ON m.time = b.b_time
                WHERE m.symbol = :symbol
            """),
            {"symbol": SYMBOL},
        ).scalar()

        # 3. Microsecond vs millisecond resolution & daily stats
        daily_rows = conn.execute(
            text("""
                WITH ordered AS (
                    SELECT
                        (time AT TIME ZONE 'UTC')::date AS utc_day,
                        time,
                        bid,
                        ask,
                        (ask - bid) AS spread,
                        LAG(time) OVER (ORDER BY time) AS prev_time
                    FROM market_ticks
                    WHERE symbol = :symbol
                ),
                per_day AS (
                    SELECT
                        utc_day,
                        COUNT(*) AS ticks,
                        COUNT(DISTINCT date_trunc('minute', time)) AS active_minutes,
                        MIN(time) AS first_tick_utc,
                        MAX(time) AS last_tick_utc,
                        COUNT(*) FILTER (WHERE bid <= 0 OR ask <= 0) AS invalid_px,
                        COUNT(*) FILTER (WHERE bid > ask) AS inverted_px,
                        COUNT(*) FILTER (WHERE bid = ask) AS zero_spread,
                        COUNT(*) FILTER (WHERE spread >= 1.0) AS spike_ge_1,
                        ROUND(MIN(spread)::numeric, 4) AS min_spread,
                        ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY spread)::numeric, 4) AS median_spread,
                        ROUND(PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY spread)::numeric, 4) AS p99_spread,
                        ROUND(MAX(spread)::numeric, 4) AS max_spread,
                        COUNT(*) FILTER (
                            WHERE prev_time IS NOT NULL
                              AND (prev_time AT TIME ZONE 'UTC')::date = utc_day
                              AND EXTRACT(EPOCH FROM (time - prev_time)) > 60
                        ) AS gaps_gt_60s,
                        COUNT(*) FILTER (
                            WHERE prev_time IS NOT NULL
                              AND (prev_time AT TIME ZONE 'UTC')::date = utc_day
                              AND EXTRACT(EPOCH FROM (time - prev_time)) > 300
                        ) AS gaps_gt_300s,
                        COALESCE(
                            MAX(
                                CASE
                                    WHEN prev_time IS NOT NULL AND (prev_time AT TIME ZONE 'UTC')::date = utc_day
                                    THEN EXTRACT(EPOCH FROM (time - prev_time))
                                    ELSE 0
                                END
                            ),
                            0
                        ) AS max_intraday_gap_s
                    FROM ordered
                    GROUP BY utc_day
                    ORDER BY utc_day
                )
                SELECT * FROM per_day
            """),
            {"symbol": SYMBOL},
        ).mappings().all()

        # 4. Top 25 intra-week gaps (> 120s, excluding Fri->Sun weekend close)
        top_gaps = conn.execute(
            text("""
                WITH gaps AS (
                    SELECT
                        time,
                        LAG(time) OVER (ORDER BY time) AS prev_time,
                        EXTRACT(EPOCH FROM (time - LAG(time) OVER (ORDER BY time))) AS gap_s
                    FROM market_ticks
                    WHERE symbol = :symbol
                )
                SELECT
                    prev_time,
                    time,
                    ROUND(gap_s::numeric, 1) AS gap_seconds
                FROM gaps
                WHERE prev_time IS NOT NULL
                  AND gap_s > 120
                  AND NOT (
                      EXTRACT(ISODOW FROM prev_time AT TIME ZONE 'UTC') IN (5, 6)
                      AND EXTRACT(ISODOW FROM time AT TIME ZONE 'UTC') IN (7, 1)
                      AND gap_s > 36000
                  )
                ORDER BY gap_s DESC
                LIMIT 25
            """),
            {"symbol": SYMBOL},
        ).mappings().all()

        # 5. Hour-of-day UTC coverage check (rollover & session completeness)
        hourly_cov = conn.execute(
            text("""
                SELECT
                    EXTRACT(HOUR FROM time AT TIME ZONE 'UTC')::int AS utc_hour,
                    COUNT(*) AS ticks,
                    ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY ask - bid)::numeric, 4) AS median_spread,
                    ROUND(MAX(ask - bid)::numeric, 4) AS max_spread
                FROM market_ticks
                WHERE symbol = :symbol
                GROUP BY 1
                ORDER BY 1
            """),
            {"symbol": SYMBOL},
        ).mappings().all()

        # 6. Check market_bars_1m comparison
        bars_overview = conn.execute(
            text("""
                SELECT COUNT(*) AS total_bars_1m, MIN(time) AS min_bar, MAX(time) AS max_bar
                FROM market_bars_1m
                WHERE symbol = :symbol
            """),
            {"symbol": SYMBOL},
        ).mappings().first()

        # 7. Check sub-second precision distribution
        subsec = conn.execute(
            text("""
                SELECT
                    COUNT(*) FILTER (WHERE EXTRACT(MICROSECONDS FROM time)::int % 1000000 = 0) AS whole_second_ticks,
                    COUNT(*) FILTER (WHERE EXTRACT(MICROSECONDS FROM time)::int % 1000 = 0) AS ms_aligned_ticks,
                    COUNT(*) FILTER (WHERE EXTRACT(MICROSECONDS FROM time)::int % 1000 != 0) AS sub_ms_ticks
                FROM market_ticks
                WHERE symbol = :symbol
            """),
            {"symbol": SYMBOL},
        ).mappings().first()

    # Write daily CSV
    import csv

    csv_path = OUT_DIR / "daily_data_quality.csv"
    if daily_rows:
        fieldnames = list(daily_rows[0].keys())
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in daily_rows:
                writer.writerow({k: str(v) if not isinstance(v, (int, float)) else v for k, v in r.items()})

    # Check backup zip if mounted/present
    backup_zip = Path("backups/csv_goldedge_20261117_143146.zip")
    backup_info = {"present": backup_zip.exists()}
    if backup_zip.exists():
        with zipfile.ZipFile(backup_zip) as zf:
            entries = {e.filename: e.file_size for e in zf.infolist()}
            backup_info["market_ticks_csv_bytes"] = entries.get("market_ticks.csv")
            if "market_ticks.csv" in entries:
                with zf.open("market_ticks.csv") as fh:
                    # Count lines fast via buffered read
                    lines = 0
                    while buf := fh.read(1024 * 1024):
                        lines += buf.count(b"\n")
                backup_info["market_ticks_csv_rows_excl_header"] = max(0, lines - 1)

    summary = {
        "symbol": SYMBOL,
        "overview": {k: _json_val(v) for k, v in dict(overview).items()},
        "subsecond_precision": {k: _json_val(v) for k, v in dict(subsec).items()},
        "exact_7d_chunk_boundary_ticks": int(boundary_overlap or 0),
        "bars_1m_overview": {k: _json_val(v) for k, v in dict(bars_overview).items()},
        "total_utc_days": len(daily_rows),
        "days_with_gaps_gt_60s": sum(1 for r in daily_rows if int(r["gaps_gt_60s"]) > 0),
        "days_with_gaps_gt_300s": sum(1 for r in daily_rows if int(r["gaps_gt_300s"]) > 0),
        "top_intraweek_gaps": [
            {"prev_time": str(r["prev_time"]), "time": str(r["time"]), "gap_seconds": float(r["gap_seconds"])}
            for r in top_gaps
        ],
        "hourly_utc_coverage": [
            {k: float(v) if isinstance(v, Decimal) else v for k, v in dict(r).items()}
            for r in hourly_cov
        ],
        "backup_archive": backup_info,
    }

    json_path = OUT_DIR / "phase2_summary.json"
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"Wrote {csv_path} ({len(daily_rows)} days) and {json_path}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
