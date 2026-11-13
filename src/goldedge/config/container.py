from dependency_injector import containers, providers

from ..domain.services.indicator_service import IndicatorService
from ..domain.services.session_service import SessionService
from ..domain.services.regime_detector import RegimeDetectorService
from ..domain.services.signal_generator import SignalGeneratorService
from ..domain.services.trade_scorer import TradeScorer
from ..domain.services.risk_service import RiskValidatorService
from ..domain.services.sizing_service import PositionSizingService
from .risk_profiles import MEDIUM_RISK_LIVE, MEDIUM_RISK_BACKTEST

from ..infrastructure.ml.lgbm_meta_labeler import LightGBMMetaLabeler

from ..application.use_cases.scan_market import ScanMarketUseCase
from ..application.use_cases.execute_trade import ExecuteTradeUseCase
from ..application.use_cases.manage_position import ManagePositionUseCase
from ..application.use_cases.compute_metrics import ComputeMetricsUseCase
from ..application.use_cases.run_backtest import RunBacktestUseCase
from ..adapters.primary.scheduler.scan_scheduler import ScanScheduler

from ..application.event_handlers.on_bar_closed import OnBarClosedHandler
from ..application.event_handlers.on_tick_received import OnTickReceivedHandler
from ..application.event_handlers.on_signal_generated import OnSignalGeneratedHandler

from ..adapters.secondary.logger.journey_logger import JourneyLoggerService
from ..adapters.secondary.logger.market_data_logger import MarketDataLoggerService
from ..adapters.secondary.db import (
    build_engine, CandleRepository, TradeRepository, TickRepository, SystemConfigRepository
)

# Backtest-specific adapters (no external infra needed)
from ..adapters.secondary.virtual_broker import VirtualBrokerAdapter
from ..adapters.secondary.in_memory_trade_repo import InMemoryTradeRepository
from ..adapters.secondary.in_memory_event_bus import InMemoryEventBus

class Container(containers.DeclarativeContainer):
    
    config = providers.Configuration()
    
    # --- External Config ---
    db_url = providers.Dependency()
    broker_type = providers.Configuration('broker_type') # 'ctrader' or 'mt5'

    # --- Adapters (injected externally or configured via Selector) ---
    event_bus = providers.Dependency()
    
    # Import Adapters
    from ..adapters.secondary.market_data.ctrader_provider import CTraderMarketDataProvider
    from ..adapters.secondary.broker.ctrader_broker_adapter import CTraderBrokerAdapter
    from ..adapters.secondary.market_data.mt5_market_data import MT5MarketDataProvider
    from ..adapters.secondary.broker.mt5_broker import MT5BrokerAdapter
    
    # cTrader Specific
    ctrader_market_data = providers.Factory(CTraderMarketDataProvider, client_id="mock", client_secret="mock", account_id="mock", access_token="mock", refresh_token="mock", token_expires_at=0.0, redis_url="redis://mock")
    ctrader_broker = providers.Factory(CTraderBrokerAdapter, api_client=None, account_id="mock")
    
    # MT5 Specific
    mt5_market_data = providers.Factory(MT5MarketDataProvider, account=0, password="", server="", path="")
    mt5_broker = providers.Factory(MT5BrokerAdapter, account=0, password="", server="", path="")
    
    # Dynamic Selectors based on BROKER_TYPE
    market_data_provider = providers.Selector(
        config.BROKER_TYPE,
        ctrader=ctrader_market_data,
        mt5=mt5_market_data
    )
    
    broker_adapter = providers.Selector(
        config.BROKER_TYPE,
        ctrader=ctrader_broker,
        mt5=mt5_broker
    )

    # --- Database Abstraction Layer (SQLAlchemy Core) ---
    db_engine = providers.Singleton(build_engine, db_url=db_url)
    candle_repo = providers.Singleton(CandleRepository, engine=db_engine)
    trade_repo = providers.Singleton(TradeRepository, engine=db_engine)
    tick_repo = providers.Singleton(TickRepository, engine=db_engine)
    config_repo = providers.Singleton(SystemConfigRepository, engine=db_engine)
    
    # --- Infrastructure / ML ---
    ml_labeler = providers.Singleton(LightGBMMetaLabeler)
    
    # --- Domain Services ---
    indicator_service = providers.Factory(IndicatorService)
    session_service = providers.Factory(SessionService)
    regime_service = providers.Factory(RegimeDetectorService)
    signal_service = providers.Factory(SignalGeneratorService)
    
    scoring_service = providers.Factory(TradeScorer)
    
    risk_validator = providers.Factory(
        RiskValidatorService,
        risk_profile=MEDIUM_RISK_LIVE
    )
    
    sizing_service = providers.Factory(
        PositionSizingService,
        contract_size=100.0
    )
    
    # --- Use Cases (LIVE — cTrader) ---
    execute_trade_uc = providers.Factory(
        ExecuteTradeUseCase,
        broker=broker_adapter,
        trade_repo=trade_repo,
        event_bus=event_bus,
        risk_validator=risk_validator,
        sizing_service=sizing_service,
        active_profile=MEDIUM_RISK_LIVE
    )

    scan_market_uc = providers.Factory(
        ScanMarketUseCase,
        candle_repo=candle_repo,
        event_bus=event_bus,
        execute_trade_uc=execute_trade_uc
    )
    
    scan_scheduler = providers.Singleton(
        ScanScheduler,
        scan_market_uc=scan_market_uc,
        event_bus=event_bus
    )
    
    manage_position_uc = providers.Factory(
        ManagePositionUseCase,
        broker=broker_adapter,
        trade_repo=trade_repo,
        event_bus=event_bus,
        market_data=market_data_provider
    )
    
    compute_metrics_uc = providers.Factory(ComputeMetricsUseCase, trade_repo=trade_repo)

    # --- Backtest Adapters (in-memory, zero external infra) ---
    virtual_broker = providers.Singleton(VirtualBrokerAdapter, initial_equity=10000.0)
    bt_trade_repo = providers.Singleton(InMemoryTradeRepository)
    bt_event_bus = providers.Singleton(InMemoryEventBus)
    
    # Backtest ManagePositionUseCase — SAME class as live, different adapters
    bt_manage_position = providers.Factory(
        ManagePositionUseCase,
        broker=virtual_broker,
        trade_repo=bt_trade_repo,
        event_bus=bt_event_bus,
        market_data=virtual_broker,  # VirtualBroker also provides get_current_tick()
    )
    
    run_backtest_uc = providers.Factory(
        RunBacktestUseCase,
        tick_repo=tick_repo,
        virtual_broker=virtual_broker,
        manage_position=bt_manage_position,
        sizing_service=sizing_service,
        profile=MEDIUM_RISK_BACKTEST,
    )
    
    # --- Loggers ---
    journey_logger = providers.Singleton(JourneyLoggerService, event_bus=event_bus, trade_repo=trade_repo)
    market_data_logger = providers.Singleton(
        MarketDataLoggerService,
        event_bus=event_bus,
        tick_repo=tick_repo,
        candle_repo=candle_repo,
        batch_size=500,
        flush_interval_sec=5.0
    )
    
    # --- Event Handlers ---
    on_bar_closed = providers.Factory(OnBarClosedHandler, scan_market_uc=scan_market_uc)
    on_tick_received = providers.Factory(OnTickReceivedHandler, manage_position_uc=manage_position_uc)
    on_signal_generated = providers.Factory(OnSignalGeneratedHandler, execute_trade_uc=execute_trade_uc)
