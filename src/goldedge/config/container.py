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
from ..infrastructure.ml.forex_meta_model import ForexMetaLabeler
from ..infrastructure.ml.macro_feature_engine import MacroFeatureEngine
from ..infrastructure.ml.microstructure_feature_engine import MicrostructureFeatureEngine
from ..infrastructure.ml.forex_micro_model import ForexMicroModel
from ..domain.services.forex_decision_rules import ForexDecisionRules
from ..domain.services.forex_micro_gatekeeper import ForexMicroGatekeeper
from ..domain.services.circuit_breaker_service import ForexCircuitBreakerService
from ..domain.services.forex_audit_service import ForexAuditService
from ..adapters.secondary.features.microstructure_feature_repo import MicrostructureFeatureRepository

from ..application.use_cases.scan_market import ScanMarketUseCase
from ..application.use_cases.execute_trade import ExecuteTradeUseCase
from ..application.use_cases.manage_position import ManagePositionUseCase
from ..application.use_cases.forex_decision_loop import ForexDecisionLoopUseCase
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

from ..config.settings import Settings

class Container(containers.DeclarativeContainer):
    
    config = providers.Configuration()
    
    # --- External Config ---
    db_url = providers.Dependency(default=Settings().DB_URL)
    broker_type = providers.Configuration('broker_type') # 'ctrader' or 'mt5'

    # --- Adapters (injected externally or configured via Selector) ---
    event_bus = providers.Dependency(default=None)
    
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
    
    # Gold Order Manager (Dual-mode: Paper + MT5 Live)
    from ..gold.adapters.broker.gold_order_manager import GoldOrderManager
    gold_order_manager = providers.Singleton(GoldOrderManager, mt5_broker=mt5_broker)

    # Dynamic Selectors based on BROKER_TYPE
    market_data_provider = providers.Selector(
        config.BROKER_TYPE,
        ctrader=ctrader_market_data,
        mt5=mt5_market_data,
        gold=mt5_market_data
    )
    
    broker_adapter = providers.Selector(
        config.BROKER_TYPE,
        ctrader=ctrader_broker,
        mt5=mt5_broker,
        gold=gold_order_manager
    )

    # --- Database Abstraction Layer (SQLAlchemy Core) ---
    db_engine = providers.Singleton(build_engine, db_url=db_url)
    candle_repo = providers.Singleton(CandleRepository, engine=db_engine)
    trade_repo = providers.Singleton(TradeRepository, engine=db_engine)
    tick_repo = providers.Singleton(TickRepository, engine=db_engine)
    config_repo = providers.Singleton(SystemConfigRepository, engine=db_engine)
    
    # --- Data Aggregation (Strict Hexagonal Ports & Adapters) ---
    from ..adapters.secondary.database.pandas_aggregator_repo import PandasAggregatorRepository
    from ..application.services.market_aggregator import MarketAggregatorService
    pandas_aggregator_repo = providers.Singleton(PandasAggregatorRepository, engine=db_engine)
    market_aggregator_service = providers.Factory(
        MarketAggregatorService,
        aggregation_repo=pandas_aggregator_repo
    )
    
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
    
    # --- Forex Quant Institutional Decision Engine ---
    macro_feature_engine = providers.Singleton(MacroFeatureEngine)
    micro_feature_engine = providers.Singleton(MicrostructureFeatureEngine)
    microstructure_repo = providers.Singleton(MicrostructureFeatureRepository)
    forex_meta_labeler = providers.Singleton(ForexMetaLabeler)
    forex_micro_model = providers.Singleton(ForexMicroModel)
    forex_micro_gatekeeper = providers.Factory(ForexMicroGatekeeper)
    circuit_breaker_service = providers.Factory(ForexCircuitBreakerService)
    forex_audit_service = providers.Factory(ForexAuditService)
    forex_decision_rules = providers.Factory(
        ForexDecisionRules,
        min_kalman_vel_z=0.5,
        min_meta_prob=0.62,
        max_spread_pips=1.8
    )
    forex_decision_loop_uc = providers.Factory(
        ForexDecisionLoopUseCase,
        broker=broker_adapter,
        market_data=market_data_provider,
        candle_repo=candle_repo,
        meta_labeler=forex_meta_labeler,
        execute_trade_uc=execute_trade_uc,
        manage_position_uc=manage_position_uc,
        decision_rules=forex_decision_rules,
        micro_model=forex_micro_model,
        micro_gatekeeper=forex_micro_gatekeeper
    )

    # --- Gold Dedicated Quant Engine ---
    from ..gold import GoldDecisionRules, GoldDecisionLoopUseCase, GoldTickReplayUseCase
    gold_decision_rules = providers.Factory(
        GoldDecisionRules,
        min_kalman_vel_z=0.5,
        min_meta_prob=0.62,
        max_spread_dollars=0.60
    )
    gold_decision_loop_uc = providers.Factory(
        GoldDecisionLoopUseCase,
        broker=gold_order_manager,
        candle_repo=candle_repo,
        meta_labeler=forex_meta_labeler,
        execute_trade_uc=execute_trade_uc,
        manage_position_uc=manage_position_uc,
        decision_rules=gold_decision_rules,
        symbol="XAUUSD"
    )
    gold_tick_replay_uc = providers.Factory(
        GoldTickReplayUseCase,
        symbol="XAUUSD",
        initial_equity=10000.0,
        daily_loss_limit=1500.0,
        consecutive_loss_limit=2,
        cooldown_bars=45,
        ladder_cooldown_bars=15,
        half_spread_dollars=0.15
    )

    from ..gold.application.live_execution_engine import ForexLiveExecutionEngine
    live_execution_engine = providers.Singleton(
        ForexLiveExecutionEngine,
        order_manager=gold_order_manager,
        symbol="XAUUSD",
        default_mode="paper",
        initial_balance=10000.0,
        leverage=100.0
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
    on_tick_received = providers.Factory(
        OnTickReceivedHandler,
        manage_position_uc=manage_position_uc,
        live_execution_engine=live_execution_engine
    )
    on_signal_generated = providers.Factory(OnSignalGeneratedHandler, execute_trade_uc=execute_trade_uc)
