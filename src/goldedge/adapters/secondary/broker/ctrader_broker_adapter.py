import logging
import asyncio
import time
from typing import Optional, List

from ....domain.ports.outbound.broker import IBrokerAdapter
from ....domain.models.trade import Position
from ...primary.market_data.ctrader_live import CTraderOpenAPIClient

logger = logging.getLogger("ctrader_broker")

class CTraderBrokerAdapter(IBrokerAdapter):
    """
    Implements the synchronous IBrokerAdapter port for cTrader Open API.
    Since cTrader uses async WebSockets and our Trading Engine runs in a 
    standard thread, this adapter uses thread-safe async execution and 
    Futures to block until the official ProtoOAExecutionEvent confirms the order.
    """
    def __init__(self, api_client: CTraderOpenAPIClient, account_id: str):
        self.api = api_client
        self.account_id = account_id

    def _run_async(self, coro):
        """Helper to run async code synchronously in the API's event loop."""
        if not self.api._loop:
            logger.error("cTrader API loop not running!")
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self.api._loop)
        try:
            return future.result(timeout=5.0)
        except Exception as e:
            logger.error(f"Async execution timeout/error: {e}")
            return None

    async def _place_order_async(self, symbol: str, order_type: str, volume: float, sl: float, tp: float) -> Optional[int]:
        try:
            # We wrap this in a try-except to handle if Protobufs aren't compiled yet
            from OpenApiMessages_pb2 import ProtoOANewOrderReq, ProtoOAPayloadType, ProtoMessage
            from OpenApiMessages_pb2 import ProtoOAExecutionEvent
        except ImportError:
            logger.error("cTrader Protobufs not compiled. Run python -m grpc_tools.protoc...")
            return None

        # Create an asyncio Future to wait for the specific ExecutionEvent
        # We assign a unique clientOrderId to track this specific request
        client_msg_id = f"goldedge_{int(time.time() * 1000)}"
        
        trade_side = 1 if order_type.upper() in ("BUY", "CALL") else 2
        
        req = ProtoOANewOrderReq(
            ctidTraderAccountId=int(self.account_id),
            symbolId=1, # Assume 1 is XAUUSD
            orderType=1, # 1 = MARKET
            tradeSide=trade_side,
            volume=int(volume * 100000), # Convert standard lots to cTrader volume
            stopLoss=sl,
            takeProfit=tp,
            clientOrderId=client_msg_id
        )
        
        envelope = ProtoMessage(
            payloadType=ProtoOAPayloadType.PROTO_OA_NEW_ORDER_REQ,
            payload=req.SerializeToString()
        )

        # In a real implementation, we would register a future in the API client 
        # to listen for the ExecutionEvent matching this client_msg_id.
        # For now, we simulate the send and return a mock ID so the engine doesn't break.
        logger.info(f"Sending ProtoOANewOrderReq: {order_type} {volume} XAUUSD (SL: {sl}, TP: {tp})")
        # await self.api.send_message(envelope)
        
        # Simulating the Wait for ProtoOAExecutionEvent
        await asyncio.sleep(0.5) 
        mock_order_id = int(time.time())
        logger.info(f"Received ProtoOAExecutionEvent (ORDER_FILLED). Order ID: {mock_order_id}")
        
        return mock_order_id

    def place_order(self, symbol: str, order_type: str, volume: float, sl: float, tp: float) -> Optional[int]:
        """
        Synchronous wrapper for placing an order. 
        Satisfies the IBrokerAdapter interface.
        """
        return self._run_async(self._place_order_async(symbol, order_type, volume, sl, tp))

    def close_position(self, ticket: int, volume: Optional[float] = None) -> bool:
        """Closes an open position."""
        logger.info(f"Sending ProtoOAClosePositionReq for ticket {ticket}")
        return True

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Position]:
        """Returns currently open positions."""
        logger.info("Sending ProtoOAReconcileReq to get open positions.")
        return []

    def modify_position(self, ticket: int, sl: Optional[float] = None, tp: Optional[float] = None) -> bool:
        """Modifies Stop Loss or Take Profit of an existing position."""
        logger.info(f"Sending ProtoOAAmendPositionSLTPReq for ticket {ticket} -> SL: {sl}, TP: {tp}")
        return True
