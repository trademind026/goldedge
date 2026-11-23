try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from ....domain.ports.outbound.broker import IBrokerAdapter
from ....domain.models.trade import Position, TradeStatus
from ....domain.models.pip_calculator import PipCalculator

logger = logging.getLogger("mt5_broker")


class MT5BrokerAdapter(IBrokerAdapter):
    """
    Production-grade MetaTrader 5 Broker Adapter.
    Supports live order execution, SL/TP modification, position closing,
    and real-time pip spread monitoring.
    """

    def __init__(self, account: int = 0, password: str = "", server: str = "", path: str = ""):
        self.account = account
        self.password = password
        self.server = server
        self.path = path
        self._connected = False

    def connect(self) -> bool:
        if mt5 is None:
            logger.error("MetaTrader5 package is not available in environment.")
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
            logger.error(f"MT5 initialization failed: {mt5.last_error()}")
            return False

        self._connected = True
        logger.info("MT5 Broker Connected.")
        return True

    def get_symbol_info(self, symbol: str) -> Dict[str, Any]:
        """Returns symbol metadata (point, digits, spread, etc.)."""
        if not self._connected:
            self.connect()

        if mt5 is not None:
            info = mt5.symbol_info(symbol)
            if info:
                return {
                    "point": info.point,
                    "digits": info.digits,
                    "spread": info.spread,
                    "bid": info.bid,
                    "ask": info.ask,
                    "trade_tick_size": info.trade_tick_size,
                    "trade_tick_value": info.trade_tick_value,
                }

        # Fallback defaults
        pip_unit = PipCalculator.get_pip_unit(symbol)
        digits = 3 if "JPY" in symbol else (2 if "XAU" in symbol else 5)
        return {"point": pip_unit * 0.1, "digits": digits, "spread": 10, "bid": 0.0, "ask": 0.0}

    def get_live_spread_pips(self, symbol: str) -> float:
        """Calculates instantaneous live spread in pips."""
        if not self._connected:
            self.connect()

        if mt5 is not None:
            tick = mt5.symbol_info_tick(symbol)
            if tick and tick.ask > 0 and tick.bid > 0:
                return PipCalculator.calculate_spread_pips(tick.bid, tick.ask, symbol)

        return 1.0  # Safe default if disconnected

    def _resolve_filling_mode(self, symbol: str) -> int:
        """
        Inspects symbol_info.filling_mode bitmask (1=FOK, 2=IOC) and returns a
        supported MT5 ORDER_FILLING_* constant to avoid retcode 10030 errors.
        """
        info = mt5.symbol_info(symbol)
        if info is not None:
            if not info.visible:
                mt5.symbol_select(symbol, True)
            filling_flags = getattr(info, "filling_mode", 0)
            if filling_flags & 2:  # SYMBOL_FILLING_IOC
                return mt5.ORDER_FILLING_IOC
            if filling_flags & 1:  # SYMBOL_FILLING_FOK
                return mt5.ORDER_FILLING_FOK
            return mt5.ORDER_FILLING_RETURN
        return mt5.ORDER_FILLING_IOC

    def _normalize_volume(self, symbol: str, volume: float) -> float:
        """Rounds volume to broker's volume_step and clamps to [volume_min, volume_max]."""
        info = mt5.symbol_info(symbol)
        if info is None:
            return float(volume)
        vol = max(info.volume_min, min(float(volume), info.volume_max))
        step = info.volume_step or 0.01
        steps = round(vol / step)
        return round(steps * step, 8)

    def place_order(
        self,
        symbol: str,
        order_type: str,
        volume: float,
        sl: float,
        tp: float
    ) -> Optional[int]:
        if not self._connected:
            self.connect()

        if mt5 is None or not self._connected:
            logger.error("Cannot place live order: MT5 broker is not connected.")
            return None

        action = mt5.ORDER_TYPE_BUY if order_type.upper() in ("BUY", "CALL") else mt5.ORDER_TYPE_SELL
        filling_type = self._resolve_filling_mode(symbol)
        norm_volume = self._normalize_volume(symbol, volume)
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            logger.error(f"Cannot place order: no live tick available for {symbol}.")
            return None
        price = tick.ask if action == mt5.ORDER_TYPE_BUY else tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": norm_volume,
            "type": action,
            "price": price,
            "sl": float(sl),
            "tp": float(tp),
            "deviation": 20,
            "magic": 123456,
            "comment": "InstitutionalForex",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_type,
        }

        check = mt5.order_check(request)
        if check is not None and check.retcode not in (0, mt5.TRADE_RETCODE_DONE):
            logger.error(f"MT5 order_check failed: {check.retcode} - {check.comment}")
            return None

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"MT5 Order Placed: {result.order}")
            return int(result.order)

        logger.error(f"MT5 Order Failed: {result.retcode if result else 'None'} - {mt5.last_error() if mt5 else ''}")
        return None

    def close_position(self, ticket: int, volume: Optional[float] = None) -> bool:
        if not self._connected:
            self.connect()

        if mt5 is None or not self._connected:
            logger.error(f"Cannot close position #{ticket}: MT5 broker is not connected.")
            return False

        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.warning(f"Position {ticket} not found in MT5.")
            return False

        pos = positions[0]
        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(pos.symbol)
        price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask if tick else pos.price_current
        close_vol = self._normalize_volume(pos.symbol, volume if volume is not None else pos.volume)
        filling_type = self._resolve_filling_mode(pos.symbol)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": pos.symbol,
            "volume": close_vol,
            "type": close_type,
            "price": price,
            "deviation": 20,
            "magic": 123456,
            "comment": "ForexClose",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_type,
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"Position {ticket} successfully closed.")
            return True

        logger.error(f"Failed to close position {ticket}: {result.retcode if result else 'None'}")
        return False

    def modify_position(self, ticket: int, sl: Optional[float] = None, tp: Optional[float] = None) -> bool:
        if not self._connected:
            self.connect()
        if mt5 is None or not self._connected:
            logger.error(f"Cannot modify position #{ticket}: MT5 broker is not connected.")
            return False

        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.warning(f"Cannot modify: position {ticket} not found.")
            return False

        pos = positions[0]
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": pos.symbol,
            "sl": float(sl) if sl is not None else pos.sl,
            "tp": float(tp) if tp is not None else pos.tp,
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.debug(f"Position {ticket} SL/TP modified to SL={sl}, TP={tp}")
            return True

        logger.error(f"Failed to modify position {ticket}: {result.retcode if result else 'None'}")
        return False

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Position]:
        if not self._connected:
            self.connect()

        if mt5 is None:
            return []

        args = {"symbol": symbol} if symbol else {}
        mt5_positions = mt5.positions_get(**args)
        if not mt5_positions:
            return []

        result = []
        for p in mt5_positions:
            pos_type = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"
            result.append(Position(
                ticket=p.ticket,
                symbol=p.symbol,
                type=pos_type,
                volume=p.volume,
                price_open=p.price_open,
                price_sl=p.sl,
                price_tp=p.tp,
                time_setup=datetime.fromtimestamp(p.time, tz=timezone.utc),
                status=TradeStatus.OPEN,
                price_close=p.price_current,
                profit=p.profit
            ))
        return result

    def get_account_info(self) -> Optional[Dict[str, Any]]:
        """Returns live MT5 account details or None if not connected."""
        if not self._connected:
            self.connect()

        if mt5 is None or not self._connected:
            return None

        acc = mt5.account_info()
        if acc is None:
            return None

        return {
            "balance": float(acc.balance),
            "equity": float(acc.equity),
            "margin": float(acc.margin),
            "free_margin": float(acc.margin_free),
            "margin_level": float(acc.margin_level) if acc.margin_level else 999.0,
            "leverage": float(acc.leverage),
            "currency": str(acc.currency)
        }

    def reconnect_with_credentials(
        self,
        account: int,
        password: str,
        server: str,
        path: str = ""
    ) -> Tuple[bool, str]:
        """
        Dynamically connects or switches MT5 terminal to specified credentials
        (e.g., Demo server vs Live server).
        """
        if mt5 is None:
            return False, "MetaTrader5 package is not available in environment."

        try:
            # Shutdown existing connection if active
            if self._connected:
                try:
                    mt5.shutdown()
                except Exception:
                    pass
                self._connected = False

            self.account = int(account)
            self.password = str(password)
            self.server = str(server)
            if path:
                self.path = str(path)

            init_args: Dict[str, Any] = {}
            if self.path:
                init_args["path"] = self.path
            if self.account:
                init_args["login"] = self.account
            if self.password:
                init_args["password"] = self.password
            if self.server:
                init_args["server"] = self.server

            if not mt5.initialize(**init_args):
                err = mt5.last_error()
                self._connected = False
                logger.error(f"MT5 credential switch failed for account {account}: {err}")
                return False, f"MT5 connection failed: {err}"

            self._connected = True
            acc = mt5.account_info()
            mode_label = "DEMO" if acc and getattr(acc, "trade_mode", 0) == 0 else "REAL"
            logger.info(f"MT5 Connected successfully to {self.server} (Account #{self.account}, Mode: {mode_label})")
            return True, f"Connected to {self.server} as {mode_label} account #{self.account}"
        except Exception as e:
            self._connected = False
            return False, str(e)

    def get_status(self) -> Dict[str, Any]:
        """Returns connection and account metadata for the REST API and settings UI."""
        if not self._connected:
            self.connect()

        trade_mode = "UNKNOWN"
        balance = 0.0
        equity = 0.0
        currency = "USD"
        leverage = 100.0

        if mt5 is not None and self._connected:
            acc = mt5.account_info()
            if acc:
                balance = float(acc.balance)
                equity = float(acc.equity)
                currency = str(acc.currency)
                leverage = float(acc.leverage)
                # MT5: 0=DEMO, 1=CONTEST, 2=REAL
                tm = getattr(acc, "trade_mode", 0)
                trade_mode = "DEMO" if tm == 0 else "CONTEST" if tm == 1 else "LIVE"

        return {
            "connected": self._connected,
            "account": str(self.account) if self.account else "",
            "server": self.server,
            "trade_mode": trade_mode,
            "balance": balance,
            "equity": equity,
            "currency": currency,
            "leverage": leverage
        }


