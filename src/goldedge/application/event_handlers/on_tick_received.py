from ...domain.events.market import TickReceived
from ..use_cases.manage_position import ManagePositionUseCase

class OnTickReceivedHandler:
    def __init__(self, manage_position_uc: ManagePositionUseCase):
        self.manage_position_uc = manage_position_uc

    def handle(self, event: TickReceived) -> None:
        """Triggered on every tick. Useful for trailing stops or dynamic exits."""
        current_price = event.tick.last or event.tick.bid
        self.manage_position_uc.execute(current_price, event.tick.symbol)
