from ...domain.events.market import BarClosed
from ..use_cases.scan_market import ScanMarketUseCase

class OnBarClosedHandler:
    def __init__(self, scan_market_uc: ScanMarketUseCase):
        self.scan_market_uc = scan_market_uc

    def handle(self, event: BarClosed) -> None:
        """Triggered when a new 1M bar is formed"""
        self.scan_market_uc.execute(event.bar.symbol)
