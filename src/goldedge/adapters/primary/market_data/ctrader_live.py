import os
import sys
import logging
from pathlib import Path

from twisted.internet import reactor
from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import *

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', '..')))

from src.goldedge.config.container import Container
from src.goldedge.config.settings import Settings

logger = logging.getLogger("ctrader_live")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


class CTraderLiveStream:
    def __init__(self, target_symbol: str = "XAUUSD"):
        # Dependency Injection Container
        self.container = Container()
        self.settings = Settings()
        self.ticker_service = self.container.live_ticker_service()
        
        # cTrader credentials from Settings
        self.client_id = self.settings.CTRADER_CLIENT_ID
        self.client_secret = self.settings.CTRADER_CLIENT_SECRET
        self.access_token = self.settings.CTRADER_ACCESS_TOKEN
        self.account_id = int(self.settings.CTRADER_ACCOUNT_ID)
        
        self.target_symbol = target_symbol
        self.symbol_id = None
        
        self.host = EndPoints.PROTOBUF_LIVE_HOST if self.settings.TRADING_MODE.lower() == "live" else EndPoints.PROTOBUF_DEMO_HOST
        
        # State tracking for the tick (cTrader sends Bid/Ask separately sometimes in SpotEvent)
        self.current_bid = None
        self.current_ask = None

    def start(self):
        logger.info(f"Connecting to cTrader Live Stream ({self.host})...")
        client = Client(self.host, EndPoints.PROTOBUF_PORT, TcpProtocol)
        
        # Wire up Twisted Callbacks
        client.setConnectedCallback(self.on_connected)
        client.setDisconnectedCallback(self.on_disconnected)
        client.setMessageReceivedCallback(self.on_message_received)
        
        client.startService()
        reactor.run()

    def on_connected(self, client):
        logger.info("Connected to cTrader! Sending Application Auth...")
        request = ProtoOAApplicationAuthReq()
        request.clientId = self.client_id
        request.clientSecret = self.client_secret
        client.send(request)

    def on_disconnected(self, client, reason):
        logger.warning(f"Disconnected from cTrader: {reason}")
        if reactor.running:
            reactor.stop()

    def on_message_received(self, client, message):
        # 1. Application Auth Response
        if message.payloadType == ProtoOAApplicationAuthRes().payloadType:
            logger.info("Application Auth Successful. Sending Account Auth...")
            request = ProtoOAAccountAuthReq()
            request.ctidTraderAccountId = self.account_id
            request.accessToken = self.access_token
            client.send(request)

        # 2. Account Auth Response
        elif message.payloadType == ProtoOAAccountAuthRes().payloadType:
            logger.info("Account Auth Successful. Requesting Symbol List...")
            request = ProtoOASymbolsListReq()
            request.ctidTraderAccountId = self.account_id
            client.send(request)

        # 3. Symbols List Response (To find symbolId for XAUUSD)
        elif message.payloadType == ProtoOASymbolsListRes().payloadType:
            res = Protobuf.extract(message)
            for symbol in res.symbol:
                if symbol.symbolName == self.target_symbol:
                    self.symbol_id = symbol.symbolId
                    logger.info(f"Found {self.target_symbol} with symbolId {self.symbol_id}")
                    break
            
            if self.symbol_id:
                logger.info(f"Subscribing to Live Spot Events for {self.target_symbol}...")
                request = ProtoOASubscribeSpotsReq()
                request.ctidTraderAccountId = self.account_id
                request.symbolId.append(self.symbol_id)
                client.send(request)
            else:
                logger.error(f"Could not find symbol {self.target_symbol}")

        # 4. Subscribe Response
        elif message.payloadType == ProtoOASubscribeSpotsRes().payloadType:
            logger.info(f"Successfully subscribed to {self.target_symbol} ticks. Listening...")

        # 5. Live Spot Event (TICK!)
        elif message.payloadType == ProtoOASpotEvent().payloadType:
            res = Protobuf.extract(message)
            
            # SpotEvents can contain just bid, just ask, or both
            updated = False
            if res.HasField("bid"):
                self.current_bid = res.bid / 100000.0  # Adjust according to cTrader digit format
                updated = True
            if res.HasField("ask"):
                self.current_ask = res.ask / 100000.0
                updated = True
                
            # If we have both, trigger the orchestration service!
            if updated and self.current_bid and self.current_ask:
                # This calls our Hexagonal Application Service, which publishes to Redis!
                self.ticker_service.process_incoming_tick(
                    symbol=self.target_symbol,
                    bid=self.current_bid,
                    ask=self.current_ask
                )

if __name__ == "__main__":
    stream = CTraderLiveStream("XAUUSD")
    stream.start()
