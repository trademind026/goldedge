from typing import Optional, Any
from ...domain.events.market import TickReceived
from ..use_cases.manage_position import ManagePositionUseCase

class OnTickReceivedHandler:
    def __init__(
        self,
        manage_position_uc: ManagePositionUseCase,
        live_execution_engine: Optional[Any] = None
    ):
        self.manage_position_uc = manage_position_uc
        self.live_execution_engine = live_execution_engine

    def handle(self, event: TickReceived) -> None:
        """Triggered on every tick. Useful for trailing stops or dynamic exits."""
        current_price = event.tick.last or event.tick.bid
        self.manage_position_uc.execute(current_price, event.tick.symbol)

        if self.live_execution_engine and hasattr(self.live_execution_engine, "on_tick"):
            bid = event.tick.bid or current_price
            ask = event.tick.ask or current_price
            vol = float(event.tick.volume or 1.0)
            self.live_execution_engine.on_tick(
                symbol=event.tick.symbol,
                price=current_price,
                bid=bid,
                ask=ask,
                volume=vol
            )
