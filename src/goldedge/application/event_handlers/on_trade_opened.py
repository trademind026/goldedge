from ...domain.events.trading import TradeOpened

class OnTradeOpenedHandler:
    def __init__(self):
        pass

    def handle(self, event: TradeOpened) -> None:
        """Log trade opening and update risk limits if necessary"""
        print(f"Trade Opened: {event.position.ticket} {event.position.type} {event.position.volume} lots")
