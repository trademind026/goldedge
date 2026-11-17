import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ....domain.models.trade import Position, TradeStatus
from ....domain.ports.outbound.broker import IBrokerAdapter

logger = logging.getLogger("ctrader_broker")


def _import_ctrader_messages():
    """Import cTrader Open API Protobuf definitions from installed SDK."""
    from ctrader_open_api.messages import OpenApiMessages_pb2 as msg_pb2
    from ctrader_open_api.messages import OpenApiModelMessages_pb2 as model_pb2
    return msg_pb2, model_pb2


class CTraderBrokerAdapter(IBrokerAdapter):
    """
    Production cTrader Open API Broker Adapter implementing IBrokerAdapter.
    Uses thread-safe asyncio futures over CTraderOpenAPIClient to send
    Protobuf requests and await official ProtoOAExecutionEvent / Reconcile
    confirmations without any mock responses.
    """

    def __init__(
        self,
        api_client: Any,
        account_id: str,
        client_id: str = "",
        client_secret: str = "",
        access_token: str = "",
    ):
        self.api = api_client
        self.account_id = int(account_id) if str(account_id).isdigit() else 0
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self._connected = False
        self._symbol_cache: Dict[str, Tuple[int, int]] = {}  # symbol -> (symbolId, lotSize)
        self._pending_futures: Dict[str, asyncio.Future] = {}
        self._pending_by_payload: Dict[int, asyncio.Future] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _run_async(self, coro, timeout: float = 10.0):
        """Run an async coroutine on the API client's event loop."""
        loop = getattr(self.api, "_loop", None) or self._loop
        if loop is None or not loop.is_running():
            logger.error("cTrader API event loop is not running; call connect() first.")
            return None
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except Exception as exc:
            logger.error(f"cTrader async execution failed: {exc}")
            return None

    async def _on_payload(self, raw_bytes: bytes) -> None:
        """Dispatch incoming ProtoMessage frames to awaiting futures."""
        try:
            msg_pb2, _ = _import_ctrader_messages()
            proto_msg = msg_pb2.ProtoMessage()
            proto_msg.ParseFromString(raw_bytes)
        except Exception as exc:
            logger.error(f"Failed to parse incoming cTrader ProtoMessage: {exc}")
            return

        client_msg_id = getattr(proto_msg, "clientMsgId", "")
        if client_msg_id and client_msg_id in self._pending_futures:
            fut = self._pending_futures.pop(client_msg_id)
            if not fut.done():
                fut.set_result(proto_msg)
            return

        payload_type = proto_msg.payloadType
        if payload_type in self._pending_by_payload:
            fut = self._pending_by_payload.pop(payload_type)
            if not fut.done():
                fut.set_result(proto_msg)

    async def _send_and_wait(self, payload_type: int, req_msg, expected_response_type: int, timeout: float = 8.0):
        """Send a ProtoMessage with a unique clientMsgId and await the response."""
        msg_pb2, _ = _import_ctrader_messages()
        loop = asyncio.get_running_loop()
        client_msg_id = f"ge_{int(time.time() * 1000000)}"
        fut = loop.create_future()
        self._pending_futures[client_msg_id] = fut
        self._pending_by_payload[expected_response_type] = fut

        envelope = msg_pb2.ProtoMessage(
            payloadType=payload_type,
            payload=req_msg.SerializeToString(),
            clientMsgId=client_msg_id,
        )
        await self.api.send_message(envelope)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_futures.pop(client_msg_id, None)
            self._pending_by_payload.pop(expected_response_type, None)

    async def _connect_and_auth_async(self) -> bool:
        if self.api is None or self.account_id <= 0:
            logger.error("Cannot connect cTrader: missing API client or numeric account_id.")
            return False

        try:
            msg_pb2, _ = _import_ctrader_messages()
        except ImportError as exc:
            logger.error(f"ctrader_open_api protobufs unavailable: {exc}")
            return False

        self._loop = asyncio.get_running_loop()
        if not getattr(self.api, "_connected", False):
            await self.api.connect()
        if not getattr(self.api, "_connected", False):
            return False

        if self._on_payload not in self.api._msg_callbacks:
            self.api._msg_callbacks.append(self._on_payload)

        app_req = msg_pb2.ProtoOAApplicationAuthReq(
            clientId=self.client_id,
            clientSecret=self.client_secret,
        )
        await self._send_and_wait(
            msg_pb2.PROTO_OA_APPLICATION_AUTH_REQ,
            app_req,
            msg_pb2.PROTO_OA_APPLICATION_AUTH_RES,
        )

        acct_req = msg_pb2.ProtoOAAccountAuthReq(
            ctidTraderAccountId=self.account_id,
            accessToken=self.access_token,
        )
        await self._send_and_wait(
            msg_pb2.PROTO_OA_ACCOUNT_AUTH_REQ,
            acct_req,
            msg_pb2.PROTO_OA_ACCOUNT_AUTH_RES,
        )
        self._connected = True
        return True

    def connect(self) -> bool:
        if self.api is None:
            logger.error("Cannot connect cTrader: api_client is None.")
            return False
        res = self._run_async(self._connect_and_auth_async())
        return bool(res)

    def reconnect_with_credentials(self, account: int, password: str, server: str, path: str = "") -> Tuple[bool, str]:
        self.account_id = int(account)
        if password:
            self.access_token = password
        ok = self.connect()
        return ok, ("Connected to cTrader" if ok else "cTrader connection failed")

    def get_status(self) -> Dict[str, Any]:
        return {
            "broker": "ctrader",
            "connected": self._connected,
            "account_id": self.account_id,
        }

    async def _resolve_symbol_spec(self, symbol: str) -> Optional[Tuple[int, int]]:
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]

        msg_pb2, _ = _import_ctrader_messages()
        req = msg_pb2.ProtoOASymbolsListReq(ctidTraderAccountId=self.account_id)
        resp_env = await self._send_and_wait(
            msg_pb2.PROTO_OA_SYMBOLS_LIST_REQ,
            req,
            msg_pb2.PROTO_OA_SYMBOLS_LIST_RES,
        )
        res = msg_pb2.ProtoOASymbolsListRes()
        res.ParseFromString(resp_env.payload)
        for sym in res.symbol:
            if sym.symbolName.upper() == symbol.upper():
                lot_size = int(getattr(sym, "lotSize", 100000) or 100000)
                self._symbol_cache[symbol] = (int(sym.symbolId), lot_size)
                return self._symbol_cache[symbol]
        logger.error(f"Symbol {symbol} not found on cTrader account {self.account_id}.")
        return None

    async def _get_account_info_async(self) -> Optional[Dict[str, Any]]:
        msg_pb2, _ = _import_ctrader_messages()
        req = msg_pb2.ProtoOATraderReq(ctidTraderAccountId=self.account_id)
        resp_env = await self._send_and_wait(
            msg_pb2.PROTO_OA_TRADER_REQ,
            req,
            msg_pb2.PROTO_OA_TRADER_RES,
        )
        res = msg_pb2.ProtoOATraderRes()
        res.ParseFromString(resp_env.payload)
        trader = res.trader
        digits = int(getattr(trader, "moneyDigits", 2) or 2)
        divisor = float(10 ** digits)
        balance = float(trader.balance) / divisor
        return {
            "balance": balance,
            "equity": balance,
            "margin": 0.0,
            "leverage": float(getattr(trader, "leverageInCents", 10000)) / 100.0,
        }

    def get_account_info(self) -> Optional[Dict[str, Any]]:
        if not self._connected and not self.connect():
            return None
        return self._run_async(self._get_account_info_async())

    async def _place_order_async(self, symbol: str, order_type: str, volume: float, sl: float, tp: float) -> Optional[int]:
        msg_pb2, model_pb2 = _import_ctrader_messages()
        spec = await self._resolve_symbol_spec(symbol)
        if spec is None:
            return None
        symbol_id, lot_size = spec

        trade_side = (
            model_pb2.ProtoOATradeSide.BUY
            if order_type.upper() in ("BUY", "CALL")
            else model_pb2.ProtoOATradeSide.SELL
        )
        protocol_volume = int(round(float(volume) * lot_size * 100))

        req = msg_pb2.ProtoOANewOrderReq(
            ctidTraderAccountId=self.account_id,
            symbolId=symbol_id,
            orderType=model_pb2.ProtoOAOrderType.MARKET,
            tradeSide=trade_side,
            volume=protocol_volume,
            stopLoss=float(sl) if sl else 0.0,
            takeProfit=float(tp) if tp else 0.0,
        )
        resp_env = await self._send_and_wait(
            msg_pb2.PROTO_OA_NEW_ORDER_REQ,
            req,
            msg_pb2.PROTO_OA_EXECUTION_EVENT,
        )
        ev = msg_pb2.ProtoOAExecutionEvent()
        ev.ParseFromString(resp_env.payload)
        if ev.HasField("position") and ev.position.positionId:
            return int(ev.position.positionId)
        if ev.HasField("order") and ev.order.orderId:
            return int(ev.order.orderId)
        logger.error(f"cTrader execution did not return order/position ID: {ev.errorCode}")
        return None

    def place_order(self, symbol: str, order_type: str, volume: float, sl: float, tp: float) -> Optional[int]:
        if not self._connected and not self.connect():
            return None
        return self._run_async(self._place_order_async(symbol, order_type, volume, sl, tp))

    async def _close_position_async(self, ticket: int, volume: Optional[float] = None) -> bool:
        msg_pb2, _ = _import_ctrader_messages()
        positions = await self._get_open_positions_async()
        target = next((p for p in positions if p.ticket == ticket), None)
        if target is None:
            logger.warning(f"cTrader position {ticket} not found for close.")
            return False

        spec = await self._resolve_symbol_spec(target.symbol)
        lot_size = spec[1] if spec else 100000
        close_lots = volume if volume is not None else target.volume
        protocol_volume = int(round(float(close_lots) * lot_size * 100))

        req = msg_pb2.ProtoOAClosePositionReq(
            ctidTraderAccountId=self.account_id,
            positionId=int(ticket),
            volume=protocol_volume,
        )
        resp_env = await self._send_and_wait(
            msg_pb2.PROTO_OA_CLOSE_POSITION_REQ,
            req,
            msg_pb2.PROTO_OA_EXECUTION_EVENT,
        )
        ev = msg_pb2.ProtoOAExecutionEvent()
        ev.ParseFromString(resp_env.payload)
        return not bool(getattr(ev, "errorCode", ""))

    def close_position(self, ticket: int, volume: Optional[float] = None) -> bool:
        if not self._connected and not self.connect():
            return False
        return bool(self._run_async(self._close_position_async(ticket, volume)))

    async def _get_open_positions_async(self, symbol: Optional[str] = None) -> List[Position]:
        msg_pb2, model_pb2 = _import_ctrader_messages()
        req = msg_pb2.ProtoOAReconcileReq(ctidTraderAccountId=self.account_id)
        resp_env = await self._send_and_wait(
            msg_pb2.PROTO_OA_RECONCILE_REQ,
            req,
            msg_pb2.PROTO_OA_RECONCILE_RES,
        )
        res = msg_pb2.ProtoOAReconcileRes()
        res.ParseFromString(resp_env.payload)

        id_to_symbol = {v[0]: (k, v[1]) for k, v in self._symbol_cache.items()}
        positions: List[Position] = []
        for pos in res.position:
            sym_id = int(pos.tradeData.symbolId)
            sym_name, lot_size = id_to_symbol.get(sym_id, (str(sym_id), 100000))
            if symbol and sym_name.upper() != symbol.upper():
                continue
            side = "BUY" if pos.tradeData.tradeSide == model_pb2.ProtoOATradeSide.BUY else "SELL"
            lots = float(pos.tradeData.volume) / (lot_size * 100.0)
            open_ts = datetime.fromtimestamp(pos.tradeData.openTimestamp / 1000.0, tz=timezone.utc)
            positions.append(
                Position(
                    ticket=int(pos.positionId),
                    symbol=sym_name,
                    type=side,
                    volume=lots,
                    price_open=float(pos.price),
                    price_sl=float(getattr(pos, "stopLoss", 0.0) or 0.0),
                    price_tp=float(getattr(pos, "takeProfit", 0.0) or 0.0),
                    time_setup=open_ts,
                    status=TradeStatus.OPEN,
                )
            )
        return positions

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Position]:
        if not self._connected and not self.connect():
            return []
        res = self._run_async(self._get_open_positions_async(symbol))
        return res or []

    async def _modify_position_async(self, ticket: int, sl: Optional[float] = None, tp: Optional[float] = None) -> bool:
        msg_pb2, _ = _import_ctrader_messages()
        req = msg_pb2.ProtoOAAmendPositionSLTPReq(
            ctidTraderAccountId=self.account_id,
            positionId=int(ticket),
        )
        if sl is not None:
            req.stopLoss = float(sl)
        if tp is not None:
            req.takeProfit = float(tp)

        resp_env = await self._send_and_wait(
            msg_pb2.PROTO_OA_AMEND_POSITION_SLTP_REQ,
            req,
            msg_pb2.PROTO_OA_EXECUTION_EVENT,
        )
        ev = msg_pb2.ProtoOAExecutionEvent()
        ev.ParseFromString(resp_env.payload)
        return not bool(getattr(ev, "errorCode", ""))

    def modify_position(self, ticket: int, sl: Optional[float] = None, tp: Optional[float] = None) -> bool:
        if not self._connected and not self.connect():
            return False
        return bool(self._run_async(self._modify_position_async(ticket, sl, tp)))
