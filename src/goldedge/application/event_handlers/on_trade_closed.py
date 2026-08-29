from ...domain.events.trading import TradeClosed

class OnTradeClosedHandler:
    def __init__(self):
        pass

    def handle(self, event: TradeClosed) -> None:
        """Update metrics, ML loop feedback, etc."""
        print(f"Trade Closed: {event.position.ticket} Reason: {event.exit_reason}")
