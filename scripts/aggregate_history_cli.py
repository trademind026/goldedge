import argparse
import logging
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.goldedge.config.container import Container

logger = logging.getLogger("aggregate_history_cli")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

def main():
    parser = argparse.ArgumentParser(description="Aggregate historical ticks into 1-minute bars using DI Container")
    parser.add_argument("--symbol", type=str, default="XAUUSD", help="Symbol to aggregate (e.g., XAUUSD)")
    args = parser.parse_args()

    logger.info("Initializing GoldEdge Dependency Injection Container...")
    
    # 1. Boot up the wiring switchboard
    container = Container()
    
    # 2. Ask the container for the fully-wired service
    # The container automatically figures out that it needs to inject the Pandas adapter!
    aggregator_service = container.market_aggregator_service()
    
    # 3. Run the business logic
    logger.info(f"Triggering aggregation for {args.symbol}...")
    aggregator_service.aggregate_historical_ticks(args.symbol)
    
    logger.info("Aggregation complete.")

if __name__ == "__main__":
    main()
