from ...domain.events.trading import SignalGenerated
from ..use_cases.execute_trade import ExecuteTradeUseCase

class OnSignalGeneratedHandler:
    def __init__(self, execute_trade_uc: ExecuteTradeUseCase):
        self.execute_trade_uc = execute_trade_uc

    def handle(self, event: SignalGenerated) -> None:
        """Triggered when a signal passes the scorer"""
        # In a real scenario, we'd query account balance and spreads dynamically here
        self.execute_trade_uc.execute(
            scored_signal=event.scored_signal,
            current_spread=15.0, # Dummy data
            recent_median_spread=12.0,
            account_balance=10000.0,
            current_daily_loss_pct=0.0,
            current_streak=0
        )
