import argparse
import logging
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.goldedge.config.container import Container

logger = logging.getLogger("generate_features_cli")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

def main():
    parser = argparse.ArgumentParser(description="Generate ML Features using DI Container")
    parser.add_argument("--symbol", type=str, default="XAUUSD", help="Symbol to process (e.g., XAUUSD)")
    args = parser.parse_args()

    logger.info("Initializing GoldEdge Dependency Injection Container...")
    container = Container()
    
    feature_engine = container.feature_engine_service()
    
    logger.info(f"Triggering feature engine for {args.symbol}...")
    feature_engine.generate_features(args.symbol)
    
    logger.info("Feature generation complete.")

if __name__ == "__main__":
    main()
