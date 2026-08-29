import MetaTrader5 as mt5
import logging
from typing import Optional
from ...domain.ports.outbound.broker import BrokerPort
from ...domain.models.order import Order

logger = logging.getLogger("mt5_broker")

class MT5BrokerAdapter(BrokerPort):
    def __init__(self, account: int, password: str, server: str, path: str):
        self.account = account
        self.password = password
        self.server = server
        self.path = path
        self._connected = False

    async def connect(self):
        if not mt5.initialize(path=self.path, login=self.account, password=self.password, server=self.server):
            logger.error(f"MT5 init failed: {mt5.last_error()}")
            return False
        self._connected = True
        logger.info("MT5 Broker Connected.")
        return True

    async def submit_order(self, order: Order) -> Optional[str]:
        if not self._connected:
            await self.connect()
            
        action = mt5.ORDER_TYPE_BUY if order.direction in ("BUY", "CALL") else mt5.ORDER_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": order.quantity,
            "type": action,
            "sl": order.stop_loss,
            "tp": order.take_profit,
            "deviation": 10,
            "magic": 123456,
            "comment": "GoldEdge",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"MT5 Order Placed: {result.order}")
            return str(result.order)
        logger.error(f"MT5 Order Failed: {result.retcode if result else 'None'}")
        return None

    async def close_position(self, trade_id: str) -> bool:
        # Implementation to close position in MT5
        return True

    async def modify_position(self, trade_id: str, new_sl: float, new_tp: float) -> bool:
        # Implementation to modify SL/TP in MT5
        return True
