import logging
from typing import Dict, List, Any

from ....domain.events.trading import TradeOpened, TradeClosed, SLMoved, JourneySnapshot
from ....domain.ports.outbound.event_bus import IEventBus
from ....adapters.secondary.db.trade_repository import TradeRepository

logger = logging.getLogger(__name__)


class JourneyLoggerService:
    """
    Subscribes to trading events and builds a minute-by-minute journey log for every trade.
    When a trade is closed, flushes the complete JSON array to the `journey_log`
    JSONB column via TradeRepository (no raw SQL here).
    """

    def __init__(self, event_bus: IEventBus, trade_repo: TradeRepository):
        self.event_bus = event_bus
        self.trade_repo = trade_repo
        self.active_journeys: Dict[int, List[Dict[str, Any]]] = {}

    def start(self):
        """Registers event listeners."""
        self.event_bus.consume("TradeOpened", self.handle_trade_opened)
        self.event_bus.consume("JourneySnapshot", self.handle_journey_snapshot)
        self.event_bus.consume("SLMoved", self.handle_sl_moved)
        self.event_bus.consume("TradeClosed", self.handle_trade_closed)

    def handle_trade_opened(self, event: TradeOpened):
        # Initialize empty journey for this ticket
        self.active_journeys[event.position.ticket] = []

    def handle_journey_snapshot(self, event: JourneySnapshot):
        if event.ticket not in self.active_journeys:
            # We might have missed the TradeOpened if the logger restarted mid-trade.
            # In a resilient setup, we would query the DB to rebuild state. 
            # For now, just initialize it.
            self.active_journeys[event.ticket] = []
            
        self.active_journeys[event.ticket].append({
            "type": "MINUTE_CLOSE",
            "time": event.time,
            "price": round(event.price, 3),
            "sl": round(event.sl, 3),
            "bars_held": event.bars_held
        })

    def handle_sl_moved(self, event: SLMoved):
        if event.ticket in self.active_journeys:
            # A snapshot of the SL moving mid-minute
            self.active_journeys[event.ticket].append({
                "type": "SL_UPDATE",
                "old_sl": round(event.old_sl, 3),
                "new_sl": round(event.new_sl, 3)
            })

    def handle_trade_closed(self, event: TradeClosed):
        if event.position.ticket in self.active_journeys:
            journey_data = self.active_journeys.pop(event.position.ticket)
            
            # Append final exit state
            journey_data.append({
                "type": "EXIT",
                "time": event.position.time_closing.isoformat() if event.position.time_closing else None,
                "price": round(event.position.price_close, 3) if event.position.price_close else None,
                "reason": event.exit_reason.value
            })
            
            self._flush_to_db(event.position.ticket, journey_data)

    def _flush_to_db(self, ticket: int, journey_data: List[Dict[str, Any]]):
        try:
            self.trade_repo.flush_journey(ticket, journey_data)
        except Exception as e:
            logger.error(f"Failed to flush journey log for ticket {ticket}: {e}")
