import logging
from datetime import datetime, timezone

from src.goldedge.domain.ports.outbound.event_bus import IEventBus
from src.goldedge.domain.models.bar import Tick
from src.goldedge.domain.events.market import TickReceived

logger = logging.getLogger("live_ticker_service")

class LiveTickerService:
    """
    Application Service that processes incoming live ticks from the broker,
    formats them into Domain Events, and pushes them to the Event Bus.
    """
    def __init__(self, event_bus: IEventBus):
        self.event_bus = event_bus

    def process_incoming_tick(self, symbol: str, bid: float, ask: float, timestamp_ms: int = None):
        """
        Receives raw data from the WebSocket, builds a Domain model,
        and broadcasts it to the rest of the application.
        """
        # 1. Calculate mid-price
        last_price = round((bid + ask) / 2.0, 5)
        
        # 2. Parse timestamp
        if timestamp_ms:
            time_obj = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
        else:
            time_obj = datetime.now(timezone.utc)
            
        # 3. Create Domain Model
        tick_model = Tick(
            symbol=symbol,
            time=time_obj,
            bid=bid,
            ask=ask,
            last=last_price
        )
        
        # 4. Create Domain Event
        event = TickReceived(tick=tick_model)
        
        # 5. Broadcast to the system
        self.event_bus.publish(event)
        
        # For debugging/logging at the service level (throttle this in production)
        logger.info(f"[LIVE TICK] {symbol} | Bid: {bid} | Ask: {ask} | Mid: {last_price}")
