import logging
from datetime import datetime
from typing import Optional

from ...domain.ports.outbound.candle_repo import ICandleRepository
from ...domain.ports.outbound.event_bus import IEventBus
from ...domain.services.indicator_service import IndicatorService
from ...domain.services.regime_detector import RegimeDetectorService
from ...domain.services.signal_generator import SignalGeneratorService
from ...domain.services.trade_scorer import TradeScorer
from ...config.settings import settings
from ...config.risk_profiles import get_profile

from .execute_trade import ExecuteTradeUseCase

logger = logging.getLogger(__name__)

class ScanMarketUseCase:
    """
    Core trading loop. Evaluates M1 bars, computes indicators, 
    detects market regime, and generates signals.
    """
    def __init__(
        self,
        candle_repo: ICandleRepository,
        event_bus: IEventBus,
        execute_trade_uc: ExecuteTradeUseCase
    ):
        self.candle_repo = candle_repo
        self.event_bus = event_bus
        self.execute_trade_uc = execute_trade_uc
        
        self.regime_detector = RegimeDetectorService()
        self.trade_scorer = TradeScorer(w_ml=0.5, w_quant=0.5)
        # Use settings to figure out active profile
        # For simplicity, using "MEDIUM_BACKTEST" if TRADING_MODE=backtest else "MEDIUM_LIVE"
        profile_name = "MEDIUM_BACKTEST" if settings.TRADING_MODE == "backtest" else "MEDIUM_LIVE"
        self.active_profile = get_profile(profile_name)

    def execute(self, symbol: str, kalman_features: dict = None) -> None:
        """
        Called periodically by the ScanScheduler at the close of an M1 bar.
        Accepts kalman_features (velocity, acceleration) calculated from live ticks.
        """
        kalman_features = kalman_features or {}
        
        # 1. Load the last 60 bars (enough for EMA50 and some history)
        bars = self.candle_repo.get_latest_bars(symbol, count=60)
        
        if len(bars) < 50:
            logger.debug(f"Not enough bars for {symbol} to calculate indicators ({len(bars)}/50).")
            return
            
        # 2. Compute Technical Indicators (pandas DataFrame)
        df = IndicatorService.compute_indicators(bars)
        
        if df.empty:
            return
            
        # 3. Detect Market Regime
        regime = self.regime_detector.detect(df)
        
        # 4. Generate Signals
        signals = SignalGeneratorService.generate_signals(df, symbol, regime)
        
        # 5. Filter and Score Signals
        latest_atr = df["atr"].iloc[-1]
        
        # Inject the live kalman features into the scorer
        scored_signals = self.trade_scorer.filter_and_rank(
            signals, 
            min_score_threshold=self.active_profile.min_score_threshold,
            regime=regime.value,
            features=kalman_features
        )
        
        if not scored_signals:
            return
            
        for scored_trade in scored_signals:
            logger.info(f"Signal Approved by Scorer: {scored_trade.strategy} | {scored_trade.direction} | Score: {scored_trade.final_score}")
            
            # Note: ScoredTrade from trade_scorer is not the exact ScoredSignal that ExecuteTradeUseCase wants. 
            # We need to map ScoredTrade -> ScoredSignal
            from ...domain.models.signal import ScoredSignal
            
            scored_sig = ScoredSignal(
                signal=scored_trade.signal,
                score=scored_trade.final_score,
                ml_probability=scored_trade.ml_probability
            )
            
            self.execute_trade_uc.execute(
                scored_signal=scored_sig,
                current_spread=0.0, # Placeholder
                recent_median_spread=0.0, # Placeholder
                account_balance=10000.0, # Placeholder
                current_daily_loss_pct=0.0, # Placeholder
                current_streak=0,
                entry_price=scored_trade.entry_price,
                atr_value=latest_atr
            )
            break
