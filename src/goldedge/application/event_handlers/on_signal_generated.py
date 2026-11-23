import logging
from typing import Optional

from ...domain.events.trading import SignalGenerated
from ...domain.ports.outbound.broker import IBrokerAdapter
from ...domain.ports.outbound.candle_repo import ICandleRepository
from ...domain.ports.outbound.market_data import IMarketDataProvider
from ...domain.ports.outbound.trade_repo import ITradeRepository
from ...domain.services.indicator_service import IndicatorService
from ..services.execution_context import resolve_execution_context
from ..use_cases.execute_trade import ExecuteTradeUseCase

logger = logging.getLogger(__name__)


class OnSignalGeneratedHandler:
    def __init__(
        self,
        execute_trade_uc: ExecuteTradeUseCase,
        broker: IBrokerAdapter,
        trade_repo: ITradeRepository,
        candle_repo: ICandleRepository,
        market_data: Optional[IMarketDataProvider] = None,
    ):
        self.execute_trade_uc = execute_trade_uc
        self.broker = broker
        self.trade_repo = trade_repo
        self.candle_repo = candle_repo
        self.market_data = market_data

    def handle(self, event: SignalGenerated) -> None:
        """Triggered when a signal passes the scorer."""
        sig = event.scored_signal.signal
        ctx = resolve_execution_context(
            symbol=sig.symbol,
            broker=self.broker,
            trade_repo=self.trade_repo,
            market_data=self.market_data,
        )
        if ctx is None:
            logger.warning(f"Skipping signal {sig.signal_id}: live execution context unavailable.")
            return

        atr_value = float(sig.details.get("atr", 0.0)) if sig.details else 0.0
        if atr_value <= 0.0:
            bars = self.candle_repo.get_latest_bars(sig.symbol, count=60)
            if len(bars) >= 15:
                df = IndicatorService.compute_indicators(bars)
                if not df.empty and "atr" in df.columns:
                    atr_value = float(df["atr"].iloc[-1])

        if atr_value <= 0.0:
            logger.warning(f"Skipping signal {sig.signal_id}: unable to compute ATR for {sig.symbol}.")
            return

        self.execute_trade_uc.execute(
            scored_signal=event.scored_signal,
            current_spread=ctx.current_spread_pips,
            recent_median_spread=ctx.recent_median_spread_pips,
            account_balance=ctx.account_balance,
            current_daily_loss_pct=ctx.current_daily_loss_pct,
            current_streak=ctx.current_streak,
            entry_price=sig.entry_price,
            atr_value=atr_value,
            account_equity=ctx.account_equity,
        )
