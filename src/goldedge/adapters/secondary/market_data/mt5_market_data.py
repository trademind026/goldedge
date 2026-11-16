try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None
import logging
from datetime import datetime, timezone
from typing import List, Callable, Optional
from ....domain.ports.outbound.market_data import IMarketDataProvider
from ....domain.models.bar import Bar, Tick

logger = logging.getLogger("mt5_market_data")


class MT5MarketDataProvider(IMarketDataProvider):
    """
    MT5 Market Data Provider implementing IMarketDataProvider.
    Provides live tick querying and historical bar fetching from MetaTrader 5.
    """

    def __init__(self, account: int = 0, password: str = "", server: str = "", path: str = ""):
        self.account = account
        self.password = password
        self.server = server
        self.path = path
        self._connected = False
        self._callbacks: List[Callable] = []

    def connect(self) -> bool:
        if mt5 is None:
            logger.error("MetaTrader5 package is not available.")
            return False

        init_args = {}
        if self.path:
            init_args["path"] = self.path
        if self.account:
            init_args["login"] = self.account
        if self.password:
            init_args["password"] = self.password
        if self.server:
            init_args["server"] = self.server

        if not mt5.initialize(**init_args):
            logger.error(f"MT5 Data init failed: {mt5.last_error()}")
            return False

        self._connected = True
        logger.info("MT5 Market Data Connected.")
        return True

    def register_tick_callback(self, callback: Callable):
        self._callbacks.append(callback)

    def get_current_tick(self, symbol: str) -> Optional[Tick]:
        """Fetches the latest live tick for the symbol."""
        if not self._connected:
            self.connect()

        if mt5 is None:
            return None

        mt5_tick = mt5.symbol_info_tick(symbol)
        if mt5_tick is None:
            return None

        return Tick(
            symbol=symbol,
            time=datetime.fromtimestamp(mt5_tick.time, tz=timezone.utc),
            bid=float(mt5_tick.bid),
            ask=float(mt5_tick.ask),
            last=float(mt5_tick.last) if mt5_tick.last else (float(mt5_tick.bid) + float(mt5_tick.ask)) / 2.0,
            volume=int(mt5_tick.volume),
            flags=int(mt5_tick.flags)
        )

    def fetch_historical_bars(self, symbol: str, timeframe: str, count: int) -> List[Bar]:
        """Fetches historical bars from MT5."""
        if not self._connected:
            self.connect()

        if mt5 is None:
            return []

        # Map timeframe string to MT5 timeframe
        tf_map = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "H1": mt5.TIMEFRAME_H1,
            "D1": mt5.TIMEFRAME_D1,
        }
        mt5_tf = tf_map.get(timeframe.upper(), mt5.TIMEFRAME_M1)

        rates = mt5.copy_rates_from_pos(symbol, mt5_tf, 0, count)
        if rates is None or len(rates) == 0:
            return []

        bars = []
        for r in rates:
            bars.append(Bar(
                symbol=symbol,
                time=datetime.fromtimestamp(r["time"], tz=timezone.utc),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                tick_volume=int(r["tick_volume"]),
                real_volume=int(r["real_volume"]) if "real_volume" in r.dtype.names else 0,
                spread=int(r["spread"]) if "spread" in r.dtype.names else 0
            ))
        return bars

    def fetch_bars_range(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> List[Bar]:
        """Fetches historical bars within a date range."""
        if not self._connected:
            self.connect()

        if mt5 is None:
            return []

        tf_map = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "H1": mt5.TIMEFRAME_H1,
        }
        mt5_tf = tf_map.get(timeframe.upper(), mt5.TIMEFRAME_M1)

        rates = mt5.copy_rates_range(symbol, mt5_tf, start, end)
        if rates is None or len(rates) == 0:
            return []

        bars = []
        for r in rates:
            bars.append(Bar(
                symbol=symbol,
                time=datetime.fromtimestamp(r["time"], tz=timezone.utc),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                tick_volume=int(r["tick_volume"]),
                real_volume=int(r["real_volume"]) if "real_volume" in r.dtype.names else 0,
                spread=int(r["spread"]) if "spread" in r.dtype.names else 0
            ))
        return bars
