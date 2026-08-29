"""
seed_historical.py — Fetches XAU/USD data from PU Prime MT5 and stores it via the DAL.
All DB access goes through CandleRepository and TickRepository — zero raw psycopg2 here.

Usage:
    python scripts/seed_historical.py --start 2023-01-01 --end 2024-01-01 --symbol XAUUSD --timeframe 1m
    python scripts/seed_historical.py --start 2026-05-01 --end 2026-08-01 --symbol EURUSD --timeframe tick
"""
import argparse
import os
import sys
from datetime import datetime, timezone

# Inject src into path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from goldedge.adapters.secondary.market_data.mt5_market_data import MT5MarketDataProvider
from goldedge.adapters.secondary.db import build_engine, CandleRepository, TickRepository


def seed_historical_data(
    start_date: str,
    end_date: str,
    symbol: str,
    timeframe: str,
    db_url: str,
    mt5_config: dict
):
    # ── 1. Connect to MT5 ──────────────────────────────────────────
    print("Connecting to MT5...")
    provider = MT5MarketDataProvider(
        login=mt5_config['login'],
        password=mt5_config['password'],
        server=mt5_config['server'],
        path=mt5_config.get('path')
    )
    if not provider.initialize():
        print("Failed to initialize MT5")
        return

    start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    print(f"Fetching {symbol} [{timeframe}] from {start_date} to {end_date}...")
    if timeframe == "1m":
        data = provider.fetch_bars_range(symbol, "1m", start, end)
    else:
        data = provider.fetch_ticks_range(symbol, start, end)
    print(f"Fetched {len(data)} records from MT5.")
    provider.shutdown()

    if not data:
        print("No data found. Check MT5 History Center.")
        return

    # ── 2. Persist via DAL ─────────────────────────────────────────
    print(f"Connecting to database...")
    engine = build_engine(db_url)

    if timeframe == "1m":
        repo = CandleRepository(engine)
        repo.save_bars(data)
        print(f"Saved {len(data)} M1 bars to xau_bars_1m.")
    else:
        repo = TickRepository(engine)
        repo.save_ticks(data)
        print(f"Saved {len(data)} ticks to xau_ticks.")

    engine.dispose()
    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed MT5 Historical Data into TimescaleDB")
    parser.add_argument("--start",     type=str, required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end",       type=str, required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--symbol",    type=str, default="XAUUSD", help="Symbol to seed (e.g. XAUUSD, EURUSD)")
    parser.add_argument("--timeframe", type=str, default="1m", choices=["1m", "tick"])
    args = parser.parse_args()

    db_url = os.environ.get("DB_URL", "postgresql://user:password@localhost:5432/goldedge")
    mt5_config = {
        "login":    int(os.environ.get("MT5_ACCOUNT",  "123456")),
        "password": os.environ.get("MT5_PASSWORD", "password"),
        "server":   os.environ.get("MT5_SERVER",   "Broker-Server"),
        "path":     os.environ.get("MT5_PATH",     None)
    }
    seed_historical_data(args.start, args.end, args.symbol, args.timeframe, db_url, mt5_config)
