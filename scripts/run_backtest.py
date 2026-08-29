import os
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from goldedge.config.container import Container
from goldedge.application.use_cases.run_backtest import RunBacktestUseCase

def run_backtest_cli(
    symbol: str, 
    start_date_str: str, 
    end_date_str: str, 
    initial_capital: float
):
    start_date = datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_date = datetime.strptime(end_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    # Initialize Dependency Injection Container
    container = Container()
    
    # Retrieve the dependencies
    tick_repo = container.pandas_aggregator_repo() # Use pandas aggregator repo since it holds DB connection
    
    # Run
    backtester = RunBacktestUseCase(tick_repo=tick_repo)
    backtester.execute(
        symbol=symbol,
        start=start_date,
        end=end_date,
        initial_capital=initial_capital
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run GoldEdge Backtest")
    parser.add_argument("--symbol", type=str, default="XAUUSD", help="Symbol to backtest (default: XAUUSD)")
    parser.add_argument("--start", type=str, required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--capital", type=float, default=10000.0, help="Initial capital (default: 10000)")
    
    args = parser.parse_args()
    run_backtest_cli(args.symbol, args.start, args.end, args.capital)
