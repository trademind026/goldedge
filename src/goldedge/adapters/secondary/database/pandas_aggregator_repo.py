import logging
import pandas as pd
from typing import Optional
from datetime import datetime
from sqlalchemy import create_engine
import sys
import os

# Add project root to path for absolute imports if needed
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', '..')))
from src.goldedge.config.settings import Settings
from src.goldedge.domain.ports.outbound.aggregation_repo import IAggregationRepository

logger = logging.getLogger("pandas_aggregator_repo")

class PandasAggregatorRepository(IAggregationRepository):
    """
    Concrete adapter that uses Pandas and SQLAlchemy to perform blazing fast
    vectorized aggregations from TimescaleDB tick data to OHLCV bars.
    """
    def __init__(self):
        self.settings = Settings()
        self.engine = create_engine(self.settings.DB_URL)

    def aggregate_ticks_to_bars(self, symbol: str, start: Optional[datetime] = None, end: Optional[datetime] = None) -> int:
        # 1. Fetch Ticks
        query = "SELECT time, bid, ask, last FROM market_ticks WHERE symbol = %(symbol)s"
        params = {"symbol": symbol}
        
        if start:
            query += " AND time >= %(start)s"
            params["start"] = start
        if end:
            query += " AND time <= %(end)s"
            params["end"] = end
            
        query += " ORDER BY time ASC"
        
        df_ticks = pd.read_sql(query, self.engine, params=params)
        
        if df_ticks.empty:
            logger.warning(f"No ticks found for {symbol} in the given range.")
            return 0

        # Prepare DataFrame for resampling
        df_ticks['time'] = pd.to_datetime(df_ticks['time'], utc=True)
        df_ticks = df_ticks.set_index('time')

        # 2. Resample to 1 Minute
        freq = '1min'
        
        # OHLC using BID price (Matches cTrader chart)
        ohlcv = df_ticks["bid"].resample(freq).ohlc()
        ohlcv.columns = ["open", "high", "low", "close"]
        
        # Tick Volume (Count of price changes in that minute)
        ohlcv["tick_volume"] = df_ticks["bid"].resample(freq).count()
        
        # Real Volume (NULL for Forex)
        ohlcv["real_volume"] = None
        
        # Spread calculation (Multiplier logic for Points)
        pip_multiplier = 100 if ('XAU' in symbol or 'JPY' in symbol) else 10000
        df_ticks["spread_raw"] = (df_ticks["ask"] - df_ticks["bid"]) * pip_multiplier
        ohlcv["spread"] = df_ticks["spread_raw"].resample(freq).mean().round().fillna(0).astype(int)
        
        # Clean empty minutes
        ohlcv = ohlcv.dropna(subset=["open"])
        ohlcv["symbol"] = symbol
        
        # Formatting for SQL Insert
        ohlcv = ohlcv.reset_index()
        final_cols = ["time", "symbol", "open", "high", "low", "close", "tick_volume", "real_volume", "spread"]
        ohlcv = ohlcv[final_cols]

        # 3. Save to Database
        try:
            ohlcv.to_sql("market_bars_1m", self.engine, if_exists="append", index=False)
            return len(ohlcv)
        except Exception as e:
            logger.error(f"Failed to save aggregated bars: {e}")
            return 0
