import logging
from typing import Optional
from datetime import datetime

from src.goldedge.domain.ports.outbound.aggregation_repo import IAggregationRepository

logger = logging.getLogger("market_aggregator")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

class MarketAggregatorService:
    """
    Application Service that orchestrates the aggregation of market data.
    It relies entirely on the IAggregationRepository port, completely decoupled 
    from Pandas, SQLAlchemy, or TimescaleDB.
    """
    def __init__(self, aggregation_repo: IAggregationRepository):
        self.repo = aggregation_repo

    def aggregate_historical_ticks(self, symbol: str, start: Optional[datetime] = None, end: Optional[datetime] = None):
        """Triggers the underlying data layer to build 1-minute bars."""
        logger.info(f"Triggering aggregation orchestration for {symbol}...")
        
        bars_created = self.repo.aggregate_ticks_to_bars(symbol, start, end)
        
        if bars_created > 0:
            logger.info(f"Orchestration complete: Successfully aggregated and saved {bars_created} 1-minute bars for {symbol}!")
        else:
            logger.warning(f"Orchestration complete: No bars were created for {symbol}.")

if __name__ == "__main__":
    # Example usage: We manually inject the concrete Pandas adapter here.
    # In production, this injection happens in config/container.py
    import sys, os
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..')))
    
    from src.goldedge.adapters.secondary.database.pandas_aggregator_repo import PandasAggregatorRepository
    
    repo = PandasAggregatorRepository()
    service = MarketAggregatorService(aggregation_repo=repo)
    
    # We won't run it right now since we already saved the 58 bars earlier!
    # service.aggregate_historical_ticks("XAUUSD")
