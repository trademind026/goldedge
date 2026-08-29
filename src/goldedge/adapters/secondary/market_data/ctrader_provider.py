import asyncio
import logging
from typing import Callable, List, Optional
from datetime import datetime, timedelta
import redis.asyncio as redis
from src.goldedge.domain.ports.outbound.market_data import IMarketDataProvider
from src.goldedge.domain.models.bar import Bar
from src.goldedge.adapters.primary.ctrader_open_api import CTraderOpenAPIClient

logger = logging.getLogger("ctrader_provider")

class CTraderMarketDataProvider(IMarketDataProvider):
    """
    Market Data Port implementation for cTrader using Open API.
    Streams live ticks to a Redis Stream (XADD pipelining) and fetches historical chunks.
    """
    def __init__(self, client_id: str, client_secret: str, account_id: str,
                 access_token: str, refresh_token: str, token_expires_at: float, redis_url: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_id = account_id
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_expires_at = token_expires_at
        
        self.api = CTraderOpenAPIClient()
        self.redis = redis.from_url(redis_url, decode_responses=True)
        self.stream_key = "market_data:ticks:XAUUSD"

    async def initialize(self):
        """Connect to cTrader and authenticate."""
        await self.api.connect()
        # In a full implementation, you would construct and send ProtoOAApplicationAuthReq
        # and ProtoOAAccountAuthReq here.
        # await self.api.send_message(app_auth_req)
        # await self.api.send_message(account_auth_req)
        logger.info("CTraderMarketDataProvider Initialized")

    async def _on_raw_message(self, payload: bytes):
        """Parse incoming protobuf payload from cTrader."""
        # Unpack ProtoMessage and handle ProtoOASpotEvent (Live Ticks)
        # For each tick, pipeline it to Redis:
        # await self._push_to_redis(tick_dict)
        pass

    async def _push_to_redis(self, tick_data: dict):
        """HFT Pipeline: Push tick to Redis stream without blocking Event Loop."""
        try:
            # transaction=False bypasses MULTI/EXEC overhead
            pipe = self.redis.pipeline(transaction=False)
            
            # maxlen=100000 with approximate=True (~) for O(1) CPU efficiency
            pipe.xadd(
                self.stream_key, 
                tick_data, 
                maxlen=100000, 
                approximate=True
            )
            await pipe.execute()
        except Exception as e:
            logger.error(f"Redis pipeline error: {e}")

    async def fetch_historical_ticks(self, symbol: str, start: datetime, end: datetime):
        from ctrader_api_client import CTraderClient, ClientConfig, AccountCredentials
        import pandas as pd
        from sqlalchemy import create_engine
        from sqlalchemy.dialects.postgresql import insert
        from datetime import timezone

        import sys, os
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..')))
        from src.goldedge.config.settings import Settings
        settings = Settings()

        # Ensure timezone-aware UTC
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        host = "demo.ctraderapi.com"
        config = ClientConfig(client_id=self.client_id, client_secret=self.client_secret, host=host)
        engine = create_engine(settings.DB_URL)

        def insert_on_conflict_nothing(table, conn, keys, data_iter):
            data = [dict(zip(keys, row)) for row in data_iter]
            stmt = insert(table.table).values(data)
            stmt = stmt.on_conflict_do_nothing(index_elements=['symbol', 'time'])
            result = conn.execute(stmt)
            return result.rowcount

        def save_chunk(bids, asks):
            if not bids or not asks:
                return 0
            df_bids = pd.DataFrame(bids).sort_values("timestamp")
            df_asks = pd.DataFrame(asks).sort_values("timestamp")
            df = pd.merge_asof(df_bids, df_asks, on="timestamp", direction="backward")
            df = df.ffill().bfill()
            df["last"] = (df["bid"] + df["ask"]) / 2
            df["symbol"] = symbol
            df = df[["timestamp", "symbol", "bid", "ask", "last"]]
            df.rename(columns={"timestamp": "time"}, inplace=True)
            df.to_sql("market_ticks", engine, if_exists="append", index=False,
                      method=insert_on_conflict_nothing, chunksize=5000)
            return len(df)

        # Split into daily windows — each day opens a FRESH connection
        # This prevents the 64s heartbeat timeout from ever being hit
        total_saved = 0
        day_windows = []
        cursor = end
        while cursor > start:
            window_start = max(cursor - timedelta(days=1), start)
            day_windows.append((window_start, cursor))
            cursor = window_start

        logger.info(f"Fetching {len(day_windows)} daily windows from {start.date()} to {end.date()}")

        for (win_start, win_end) in day_windows:
            logger.info(f"--- Opening fresh connection for window {win_start.date()} → {win_end.date()} ---")

            try:
                client = CTraderClient(config)
                async with client:
                    await client.auth.authenticate_trader(
                        AccountCredentials(
                            account_id=int(self.account_id),
                            access_token=self.access_token,
                            refresh_token=self.refresh_token,
                            expires_at=self.token_expires_at
                        )
                    )

                    # Resolve symbol_id once per session
                    symbol_id = None
                    syms = await client.symbols.list_all(int(self.account_id))
                    for s in syms:
                        if s.name == symbol:
                            symbol_id = s.symbol_id
                            break

                    if not symbol_id:
                        logger.error(f"Symbol '{symbol}' not found.")
                        return

                    # Paginate backwards within this single day window
                    current_end = win_end
                    while current_end > win_start:
                        bids, asks = [], []
                        logger.info(f"  Chunk: up to {current_end}")

                        b_ticks = await client.market_data.get_tick_data(
                            account_id=int(self.account_id),
                            symbol_id=symbol_id,
                            from_timestamp=win_start,
                            to_timestamp=current_end,
                            quote_type='BID',
                            timeout=45.0
                        )
                        for t in b_ticks:
                            bids.append({"timestamp": t.timestamp, "bid": float(t.price)})

                        a_ticks = await client.market_data.get_tick_data(
                            account_id=int(self.account_id),
                            symbol_id=symbol_id,
                            from_timestamp=win_start,
                            to_timestamp=current_end,
                            quote_type='ASK',
                            timeout=45.0
                        )
                        for t in a_ticks:
                            asks.append({"timestamp": t.timestamp, "ask": float(t.price)})

                        max_returned = max(len(b_ticks), len(a_ticks))
                        if max_returned == 0:
                            break

                        saved = save_chunk(bids, asks)
                        total_saved += saved
                        logger.info(f"  Saved {saved} ticks. Grand Total: {total_saved}")

                        # Walk cursor backwards
                        oldest_b = min(t.timestamp for t in b_ticks) if b_ticks else current_end
                        oldest_a = min(t.timestamp for t in a_ticks) if a_ticks else current_end
                        earliest = min(oldest_b, oldest_a)

                        if current_end == earliest:
                            current_end = earliest - timedelta(milliseconds=1)
                        else:
                            current_end = earliest

                        if max_returned < 1000:
                            break

                        await asyncio.sleep(1.0)

            except Exception as e:
                logger.error(f"Window {win_start.date()} failed: {e}. Skipping to next day.")
                continue

        logger.info(f"All done! Grand Total: {total_saved} ticks saved to DB.")

