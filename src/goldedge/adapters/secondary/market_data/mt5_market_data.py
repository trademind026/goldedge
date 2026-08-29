import MetaTrader5 as mt5
import logging
import asyncio
from typing import List, Callable
from ...domain.ports.outbound.market_data import MarketDataPort
from ...domain.models.bar import Bar

logger = logging.getLogger("mt5_market_data")

class MT5MarketDataProvider(MarketDataPort):
    def __init__(self, account: int, password: str, server: str, path: str):
        self.account = account
        self.password = password
        self.server = server
        self.path = path
        self._connected = False
        self._callbacks = []

    async def initialize(self):
        if not mt5.initialize(path=self.path, login=self.account, password=self.password, server=self.server):
            logger.error(f"MT5 Data init failed: {mt5.last_error()}")
            return False
        self._connected = True
        logger.info("MT5 Market Data Connected.")
        return True

    def register_tick_callback(self, callback: Callable):
        self._callbacks.append(callback)

    async def fetch_historical_bars(self, symbol: str, timeframe: str, start: str, end: str) -> List[Bar]:
        # Implementation to fetch bars from MT5
        return []

    async def stream_live_ticks(self, symbols: List[str]):
        if not self._connected:
            await self.initialize()
            
        logger.info(f"MT5 Streaming ticks for {symbols}")
        # Note: True MT5 tick streaming in Python requires a polling loop or a DLL.
        # This is a stub for the polling loop.
        while True:
            for sym in symbols:
                tick = mt5.symbol_info_tick(sym)
                if tick:
                    for cb in self._callbacks:
                        # cb(tick_data)
                        pass
            await asyncio.sleep(0.1)
