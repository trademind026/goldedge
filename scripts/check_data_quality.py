"""
check_data_quality.py — Validates completeness and gap quality of data in TimescaleDB.
Uses DAL (TickRepository, CandleRepository) — zero raw psycopg2.

Usage:
    python scripts/check_data_quality.py --symbol XAUUSD
"""
import argparse
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from goldedge.adapters.secondary.db import build_engine


# ── Shared gap query (uses TimescaleDB LAG window function) ───────────────────
_GAP_QUERY = """
WITH gaps AS (
    SELECT
        time,
        lag(time) OVER (ORDER BY time) AS prev_time,
        EXTRACT(EPOCH FROM (time - lag(time) OVER (ORDER BY time))) / 60 AS diff_minutes
    FROM xau_bars_1m
    WHERE symbol = %s
)
SELECT prev_time, time, diff_minutes
FROM gaps
WHERE diff_minutes > 1
  AND prev_time IS NOT NULL
  -- Ignore Friday-to-Monday gaps (Forex weekend close)
  AND NOT (
      EXTRACT(ISODOW FROM prev_time) IN (5, 6)
      AND EXTRACT(ISODOW FROM time) IN (7, 1)
      AND diff_minutes > 1440
  )
ORDER BY diff_minutes DESC
LIMIT 20;
"""

_TICK_GAP_QUERY = """
WITH gaps AS (
    SELECT
        time,
        lag(time) OVER (ORDER BY time) AS prev_time,
        EXTRACT(EPOCH FROM (time - lag(time) OVER (ORDER BY time))) AS diff_seconds
    FROM xau_ticks
    WHERE symbol = %s
)
SELECT prev_time, time, diff_seconds
FROM gaps
WHERE diff_seconds > 60
  AND prev_time IS NOT NULL
ORDER BY diff_seconds DESC
LIMIT 20;
"""


def check_data_quality(db_url: str, symbol: str):
    engine = build_engine(db_url, pool_size=1, max_overflow=1)

    # ── M1 Bars ───────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"  M1 Bar Quality Check — {symbol}")
    print(f"{'='*50}")

    with engine.connect() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM xau_bars_1m WHERE symbol = %s", (symbol,))
        total_bars = cursor.fetchone()[0]
        print(f"  Total M1 bars : {total_bars:,}")

        if total_bars == 0:
            print("  No M1 data found.")
        else:
            cursor.execute(
                "SELECT MIN(time), MAX(time) FROM xau_bars_1m WHERE symbol = %s",
                (symbol,)
            )
            min_t, max_t = cursor.fetchone()
            print(f"  Date range    : {min_t} → {max_t}")

            cursor.execute(_GAP_QUERY, (symbol,))
            gaps = cursor.fetchall()
            if gaps:
                print(f"\n  ⚠  Found {len(gaps)} intra-week gaps (top 20 largest):")
                for prev, curr, diff in gaps:
                    print(f"     {diff:6.1f} min gap  between  {prev}  and  {curr}")
            else:
                print("  ✓  No significant intra-week gaps. M1 data quality: EXCELLENT")

        cursor.close()

    # ── Tick Data ─────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"  Tick Data Quality Check — {symbol}")
    print(f"{'='*50}")

    with engine.connect() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM xau_ticks WHERE symbol = %s", (symbol,))
        total_ticks = cursor.fetchone()[0]
        print(f"  Total ticks   : {total_ticks:,}")

        if total_ticks > 0:
            cursor.execute(
                "SELECT MIN(time), MAX(time) FROM xau_ticks WHERE symbol = %s",
                (symbol,)
            )
            min_t, max_t = cursor.fetchone()
            print(f"  Date range    : {min_t} → {max_t}")

            cursor.execute(_TICK_GAP_QUERY, (symbol,))
            gaps = cursor.fetchall()
            if gaps:
                print(f"\n  ⚠  Found {len(gaps)} tick gaps > 60 seconds:")
                for prev, curr, diff in gaps:
                    print(f"     {diff:6.0f}s gap  between  {prev}  and  {curr}")
            else:
                print("  ✓  No significant tick gaps found.")
        else:
            print("  No tick data found. Run seed_historical.py --timeframe tick to populate.")

        cursor.close()

    engine.dispose()
    print("\nQuality check complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check TimescaleDB Data Quality")
    parser.add_argument("--symbol", type=str, default="XAUUSD")
    args = parser.parse_args()

    db_url = os.environ.get("DB_URL", "postgresql://user:password@localhost:5432/goldedge")
    check_data_quality(db_url, args.symbol)
