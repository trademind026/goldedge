try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None
import logging
from typing import Optional
from ....domain.ports.outbound.broker import IBrokerAdapter
from ....domain.models.trade import Position

logger = logging.getLogger("mt5_broker")

class MT5BrokerAdapter(IBrokerAdapter):
    def __init__(self, account: int, password: str, server: str, path: str):
        self.account = account
        self.password = password
        self.server = server
        self.path = path
        self._connected = False

    def connect(self):
        if not mt5.initialize(path=self.path, login=self.account, password=self.password, server=self.server):
            logger.error(f"MT5 init failed: {mt5.last_error()}")
            return False
        self._connected = True
        logger.info("MT5 Broker Connected.")
        return True

    def place_order(self, symbol: str, order_type: str, volume: float, sl: float, tp: float) -> Optional[int]:
        if not self._connected:
            self.connect()
            
        action = mt5.ORDER_TYPE_BUY if order_type.upper() in ("BUY", "CALL") else mt5.ORDER_TYPE_SELL
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": action,
            "sl": float(sl),
            "tp": float(tp),
            "deviation": 20,
            "magic": 123456,
            "comment": "GoldEdge",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"MT5 Order Placed: {result.order}")
            return int(result.order)
            
        logger.error(f"MT5 Order Failed: {result.retcode if result else 'None'} - {mt5.last_error()}")
        return None

    def close_position(self, ticket: int, volume: Optional[float] = None) -> bool:
        # Implementation to close position in MT5
        return True

    def modify_position(self, ticket: int, sl: Optional[float] = None, tp: Optional[float] = None) -> bool:
        # Implementation to modify SL/TP in MT5
        return True
        
    def get_open_positions(self, symbol: Optional[str] = None) -> list[Position]:
        return []
