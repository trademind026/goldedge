from dependency_injector import containers, providers

from ..domain.services.indicator_service import IndicatorService
from ..domain.services.session_service import SessionService
from ..domain.services.regime_detector import RegimeDetectorService
from ..domain.services.signal_generator import SignalGeneratorService
from ..domain.services.trade_scorer import TradeScorer
from ..domain.services.risk_service import RiskValidatorService
from ..domain.services.sizing_service import PositionSizingService
from ..domain.services.hwm_kill_switch_service import HwmKillSwitchService
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
from ..adapters.secondary.ml.ridge_pnl_adapter import RidgeExpectedPnlAdapter

from ..application.services.live_ticker import LiveTickerService
from ..application.services.hwm_emergency_exit import HwmEmergencyLiquidator
from ..application.use_cases.scan_market import ScanMarketUseCase
from ..application.use_cases.execute_trade import ExecuteTradeUseCase
from ..application.use_cases.manage_position import ManagePositionUseCase
from ..application.use_cases.forex_decision_loop import ForexDecisionLoopUseCase
from ..application.use_cases.compute_metrics import ComputeMetricsUseCase
from ..application.use_cases.run_backtest import RunBacktestUseCase
from ..domain.services.shadow_tracker import ShadowValidationTracker
from ..adapters.secondary.persistence.jsonl_shadow_journal import JsonlShadowJournal
from ..adapters.primary.scheduler.scan_scheduler import ScanScheduler

from ..application.event_handlers.on_bar_closed import OnBarClosedHandler
from ..application.event_handlers.on_tick_received import OnTickReceivedHandler
from ..application.event_handlers.on_signal_generated import OnSignalGeneratedHandler

from ..adapters.secondary.logger.journey_logger import JourneyLoggerService
from ..adapters.secondary.logger.market_data_logger import MarketDataLoggerService
from ..adapters.secondary.db import (
    build_engine, CandleRepository, TradeRepository, TickRepository, SystemConfigRepository, BacktestRepository, HwmRiskRepository
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
    from ..adapters.primary.ctrader_open_api import CTraderOpenAPIClient
    from ..adapters.secondary.market_data.ctrader_provider import CTraderMarketDataProvider
    from ..adapters.secondary.broker.ctrader_broker_adapter import CTraderBrokerAdapter
    from ..adapters.secondary.market_data.mt5_market_data import MT5MarketDataProvider
    from ..adapters.secondary.broker.mt5_broker import MT5BrokerAdapter

    _settings = Settings()
    _is_live_mode = _settings.TRADING_MODE.lower() == "live"
    _ctrader_host = "live.ctraderapi.com" if _is_live_mode else "demo.ctraderapi.com"
    _mt5_account = (
        (_settings.MT5_LIVE_ACCOUNT or _settings.MT5_ACCOUNT)
        if _is_live_mode
        else (_settings.MT5_DEMO_ACCOUNT or _settings.MT5_ACCOUNT)
    )
    _mt5_password = (
        (_settings.MT5_LIVE_PASSWORD or _settings.MT5_PASSWORD)
        if _is_live_mode
        else (_settings.MT5_DEMO_PASSWORD or _settings.MT5_PASSWORD)
    )
    _mt5_server = (
        (_settings.MT5_LIVE_SERVER or _settings.MT5_SERVER)
        if _is_live_mode
        else (_settings.MT5_DEMO_SERVER or _settings.MT5_SERVER)
    )
    _engine_default_mode = (
        "paper" if _settings.TRADING_MODE.lower() in ("backtest", "test", "paper")
        else _settings.TRADING_MODE.lower()
    )

    ctrader_api_client = providers.Singleton(
        CTraderOpenAPIClient,
        host=_ctrader_host,
        port=5035,
    )

    # cTrader Specific (wired from Settings / .env)
    ctrader_market_data = providers.Factory(
        CTraderMarketDataProvider,
        client_id=_settings.CTRADER_CLIENT_ID,
        client_secret=_settings.CTRADER_CLIENT_SECRET,
        account_id=_settings.CTRADER_ACCOUNT_ID,
        access_token=_settings.CTRADER_ACCESS_TOKEN,
        refresh_token=_settings.CTRADER_REFRESH_TOKEN,
        token_expires_at=_settings.CTRADER_TOKEN_EXPIRES_AT,
        redis_url=_settings.REDIS_URL,
    )
    ctrader_broker = providers.Singleton(
        CTraderBrokerAdapter,
        api_client=ctrader_api_client,
        account_id=_settings.CTRADER_ACCOUNT_ID,
        client_id=_settings.CTRADER_CLIENT_ID,
        client_secret=_settings.CTRADER_CLIENT_SECRET,
        access_token=_settings.CTRADER_ACCESS_TOKEN,
    )

    # MT5 Specific (wired from Settings / .env)
    mt5_market_data = providers.Factory(
        MT5MarketDataProvider,
        account=_mt5_account,
        password=_mt5_password,
        server=_mt5_server,
        path=_settings.MT5_PATH,
    )
    mt5_broker = providers.Singleton(
        MT5BrokerAdapter,
        account=_mt5_account,
        password=_mt5_password,
        server=_mt5_server,
        path=_settings.MT5_PATH,
    )
    
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
    backtest_repo = providers.Singleton(BacktestRepository, engine=db_engine)
    hwm_risk_repo = providers.Singleton(HwmRiskRepository, engine=db_engine)
    hwm_kill_switch = providers.Singleton(
        HwmKillSwitchService,
        max_drawdown_pct=_settings.HWM_MAX_DRAWDOWN_PCT,
        initial_equity=_settings.HWM_INITIAL_EQUITY,
        default_account_scope=f"{_settings.TRADING_MODE.lower()}:{_settings.CTRADER_ACCOUNT_ID}:USD",
        default_account_id=str(_settings.CTRADER_ACCOUNT_ID),
        account_currency="USD",
        repo=hwm_risk_repo,
        reset_auth_secret=(_settings.HWM_RESET_SECRET or None),
    )
    hwm_emergency_liquidator = providers.Singleton(
        HwmEmergencyLiquidator,
        kill_switch=hwm_kill_switch,
    )
    
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
        active_profile=MEDIUM_RISK_LIVE,
        hwm_kill_switch=hwm_kill_switch,
    )

    ridge_pnl_model = providers.Singleton(
        RidgeExpectedPnlAdapter,
        model_path="models/gold_ridge_pnl.pkl",
    )

    shadow_journal = providers.Singleton(
        JsonlShadowJournal,
        log_path=_settings.SHADOW_LOG_PATH,
    )

    shadow_tracker = providers.Singleton(
        ShadowValidationTracker,
        contract_size=100.0,
    )

    scan_market_uc = providers.Factory(
        ScanMarketUseCase,
        candle_repo=candle_repo,
        event_bus=event_bus,
        execute_trade_uc=execute_trade_uc,
        broker=broker_adapter,
        trade_repo=trade_repo,
        market_data=market_data_provider,
        expected_pnl_model=ridge_pnl_model,
        min_expected_pnl=None,
        sizing_service=sizing_service,
        shadow_mode=_settings.SHADOW_MODE,
        shadow_journal=shadow_journal,
        shadow_tracker=shadow_tracker,
        hwm_kill_switch=hwm_kill_switch,
        emergency_liquidator=hwm_emergency_liquidator,
    )
    
    scan_scheduler = providers.Singleton(
        ScanScheduler,
        scan_market_uc=scan_market_uc,
        event_bus=event_bus,
        tick_repo=tick_repo,
        candle_repo=candle_repo,
    )
    
    manage_position_uc = providers.Factory(
        ManagePositionUseCase,
        broker=broker_adapter,
        trade_repo=trade_repo,
        event_bus=event_bus,
        market_data=market_data_provider,
        hwm_kill_switch=hwm_kill_switch,
        emergency_liquidator=hwm_emergency_liquidator,
        initial_capital=_settings.HWM_INITIAL_EQUITY,
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
        min_meta_prob=0.0,
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
        default_mode=_engine_default_mode,
        initial_balance=10000.0,
        leverage=100.0,
        hwm_kill_switch=hwm_kill_switch,
        emergency_liquidator=hwm_emergency_liquidator,
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
        expected_pnl_model=ridge_pnl_model,
        min_expected_pnl=None,
    )
    
    # --- Loggers & Streaming Services ---
    live_ticker_service = providers.Factory(LiveTickerService, event_bus=event_bus)
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
    on_signal_generated = providers.Factory(
        OnSignalGeneratedHandler,
        execute_trade_uc=execute_trade_uc,
        broker=broker_adapter,
        trade_repo=trade_repo,
        candle_repo=candle_repo,
        market_data=market_data_provider,
    )
