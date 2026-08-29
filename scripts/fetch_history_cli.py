import argparse
import asyncio
import logging
from datetime import datetime, timedelta
import sys
import os

# Add src to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.goldedge.adapters.secondary.market_data.ctrader_provider import CTraderMarketDataProvider
from src.goldedge.config.settings import Settings

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("fetch_history")

async def main():
    parser = argparse.ArgumentParser(description="cTrader Historical Tick Fetcher")
    parser.add_argument("--symbol", type=str, default="XAUUSD", help="Symbol to fetch (e.g. XAUUSD)")
    parser.add_argument("--days", type=int, default=30, help="Number of days to look back")
    args = parser.parse_args()

    settings = Settings()
    
    # Initialize provider
    provider = CTraderMarketDataProvider(
        client_id=settings.CTRADER_CLIENT_ID,
        client_secret=settings.CTRADER_CLIENT_SECRET,
        account_id=settings.CTRADER_ACCOUNT_ID,
        access_token=settings.CTRADER_ACCESS_TOKEN,
        refresh_token=settings.CTRADER_REFRESH_TOKEN,
        token_expires_at=settings.CTRADER_TOKEN_EXPIRES_AT,
        redis_url=settings.REDIS_URL
    )
    
    await provider.initialize()
    
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=args.days)
    
    logger.info(f"Starting historical tick fetch for {args.symbol} ({args.days} days)")
    logger.info(f"Using cTrader Open API limits: 1-week chunks, 5 requests/sec max")
    
    await provider.fetch_historical_ticks(args.symbol, start_dt, end_dt)
    
    logger.info("Historical fetch complete.")

if __name__ == "__main__":
    asyncio.run(main())
