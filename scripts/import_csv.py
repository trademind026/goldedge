"""
import_csv.py — Imports third-party OHLCV CSV files (e.g. from Dukascopy / HistData)
into TimescaleDB via the DAL CandleRepository.

Supported datetime formats:
  - MetaTrader export:  2023.01.02 10:30:00
  - HistData:           2023-01-02 10:30:00
  - Dukascopy:          02.01.2023 10:30:00.000

Usage:
    python scripts/import_csv.py --file data/XAUUSD_2023.csv --symbol XAUUSD
"""
import argparse
import csv
import os
import sys
from datetime import datetime, timezone
from typing import List

# Inject src into path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from goldedge.domain.models.bar import Bar
from goldedge.adapters.secondary.db import build_engine, CandleRepository


def _parse_datetime(time_str: str) -> datetime:
    """Try all known formats — raises ValueError if none match."""
    formats = [
        "%Y.%m.%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S.%f",
        "%d.%m.%Y %H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(time_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised datetime format: {time_str!r}")


def _parse_csv(csv_path: str, symbol: str) -> List[Bar]:
    bars: List[Bar] = []
    skipped = 0

    with open(csv_path, "r") as f:
        reader = csv.reader(f)
        header = next(reader, None)  # skip header row if present

        for i, row in enumerate(reader):
            if not row:
                continue
            try:
                dt       = _parse_datetime(row[0])
                open_p   = float(row[1])
                high_p   = float(row[2])
                low_p    = float(row[3])
                close_p  = float(row[4])
                tick_vol = int(float(row[5])) if len(row) > 5 else 0
                real_vol = int(float(row[6])) if len(row) > 6 else 0
                spread   = int(float(row[7])) if len(row) > 7 else 0

                bars.append(Bar(
                    symbol=symbol,
                    time=dt,
                    open=open_p,
                    high=high_p,
                    low=low_p,
                    close=close_p,
                    tick_volume=tick_vol,
                    real_volume=real_vol,
                    spread=spread
                ))
            except (ValueError, IndexError) as e:
                skipped += 1
                if skipped <= 5:
                    print(f"  [WARN] Row {i+2} skipped: {e}")

    if skipped:
        print(f"  Skipped {skipped} malformed rows total.")
    return bars


def import_csv(csv_path: str, symbol: str, db_url: str):
    if not os.path.exists(csv_path):
        print(f"File not found: {csv_path}")
        return

    print(f"Parsing CSV: {csv_path}")
    bars = _parse_csv(csv_path, symbol)

    if not bars:
        print("No valid rows parsed. Aborting.")
        return

    print(f"Parsed {len(bars)} bars. Connecting to database...")
    engine = build_engine(db_url)
    repo = CandleRepository(engine)

    print(f"Bulk inserting into TimescaleDB...")
    repo.save_bars(bars)

    engine.dispose()
    print(f"Import complete — {len(bars)} bars saved to xau_bars_1m.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import OHLCV CSV into TimescaleDB for Backtesting")
    parser.add_argument("--file",   type=str, required=True, help="Path to CSV file")
    parser.add_argument("--symbol", type=str, default="XAUUSD", help="Symbol to tag rows with")
    args = parser.parse_args()

    db_url = os.environ.get("DB_URL", "postgresql://user:password@localhost:5432/goldedge")
    import_csv(args.file, args.symbol, db_url)
