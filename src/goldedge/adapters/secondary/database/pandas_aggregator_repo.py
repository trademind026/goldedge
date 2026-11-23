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

from sqlalchemy import create_engine, text

logger = logging.getLogger("pandas_aggregator_repo")

class PandasAggregatorRepository(IAggregationRepository):
    """
    Concrete adapter that uses Pandas and SQLAlchemy to perform blazing fast
    vectorized aggregations from TimescaleDB tick data to OHLCV bars.
    """
    def __init__(self, engine=None):
        self.settings = Settings()
        self.engine = engine if engine is not None else create_engine(self.settings.DB_URL)

    def aggregate_ticks_to_bars(self, symbol: str, start: Optional[datetime] = None, end: Optional[datetime] = None) -> int:
        # 1. Fetch Ticks (preserving nullable real_volume)
        query = "SELECT time, bid, ask, last, volume AS real_volume FROM market_ticks WHERE symbol = %(symbol)s"
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

        # Prepare DataFrame for resampling with strict consistent quote validity
        df_ticks['time'] = pd.to_datetime(df_ticks['time'], utc=True)
        df_ticks = df_ticks[
            (df_ticks["bid"] > 0.0) & (df_ticks["ask"] > 0.0) & (df_ticks["bid"] < df_ticks["ask"])
        ].copy()
        if df_ticks.empty:
            logger.warning(f"No valid quotes (bid > 0 & ask > 0 & bid < ask) found for {symbol}.")
            return 0
        df_ticks = df_ticks.set_index('time')

        # 2. Resample to 1 Minute
        freq = '1min'
        
        # OHLC using valid BID price (Matches cTrader chart)
        ohlcv = df_ticks["bid"].resample(freq).ohlc()
        ohlcv.columns = ["open", "high", "low", "close"]
        
        # Quote-arrival frequency (valid quote updates per minute)
        ohlcv["tick_volume"] = df_ticks["bid"].resample(freq).count()
        
        # Genuine traded volume only when supplied; keep NULL otherwise
        has_real_vol = df_ticks["real_volume"].notna().any()
        ohlcv["real_volume"] = df_ticks["real_volume"].resample(freq).sum(min_count=1) if has_real_vol else None
        
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

        # 3. Save to Database with Conflict-Safe Upsert
        try:
            records = ohlcv.to_dict(orient="records")
            insert_sql = text("""
                INSERT INTO market_bars_1m
                    (time, symbol, open, high, low, close, tick_volume, real_volume, spread)
                VALUES
                    (:time, :symbol, :open, :high, :low, :close, :tick_volume, :real_volume, :spread)
                ON CONFLICT (symbol, time) DO NOTHING
            """)
            batch_size = 5000
            with self.engine.begin() as conn:
                for i in range(0, len(records), batch_size):
                    conn.execute(insert_sql, records[i:i + batch_size])
            logger.info(f"Successfully processed {len(ohlcv)} 1-minute bars for {symbol} into market_bars_1m.")
            return len(ohlcv)
        except Exception as e:
            logger.error(f"Failed to save aggregated bars: {e}")
            return 0
