from dependency_injector import containers, providers
from src.goldedge.config.settings import Settings

# Adapters
from src.goldedge.adapters.secondary.database.pandas_aggregator_repo import PandasAggregatorRepository
from src.goldedge.adapters.secondary.database.pandas_feature_repo import PandasFeatureRepository
from src.goldedge.adapters.secondary.event_bus.redis_event_bus import RedisStreamEventBus

# Services
from src.goldedge.application.services.market_aggregator import MarketAggregatorService
from src.goldedge.application.services.feature_engine import FeatureEngineService
from src.goldedge.application.services.live_ticker import LiveTickerService

# Use Cases & Schedulers
from src.goldedge.application.use_cases.scan_market import ScanMarketUseCase
from src.goldedge.adapters.primary.scheduler.scan_scheduler import ScanScheduler


from src.goldedge.application.use_cases.execute_trade import ExecuteTradeUseCase
from src.goldedge.domain.services.risk_service import RiskValidatorService
from src.goldedge.domain.services.sizing_service import PositionSizingService
from src.goldedge.adapters.secondary.broker.ctrader_broker_adapter import CTraderBrokerAdapter
from src.goldedge.config.risk_profiles import get_profile

class Container(containers.DeclarativeContainer):
    """
    The main Dependency Injection Container.
    Acts as the 'wiring switchboard' for the entire GoldEdge application.
    """
    
    # Configuration
    config = providers.Configuration()
    
    # -------------------------------------------------------------
    # 1. OUTBOUND ADAPTERS (Database, cTrader APIs, Redis)
    # -------------------------------------------------------------
    
    # We define the adapter as a Singleton so it's only instantiated once
    pandas_aggregator_repo = providers.Singleton(
        PandasAggregatorRepository
    )
    
    pandas_feature_repo = providers.Singleton(
        PandasFeatureRepository
    )
    
    redis_event_bus = providers.Singleton(
        RedisStreamEventBus,
        redis_url="redis://localhost:6379/0",
        consumer_group="live_ticker_group",
        consumer_name="ticker_1"
    )
    
    ctrader_broker = providers.Singleton(
        CTraderBrokerAdapter,
        api_client=None, # This gets passed from ctrader_live when connected
        account_id="placeholder_account"
    )
    
    # -------------------------------------------------------------
    # 2. APPLICATION SERVICES (Business Logic Orchestrators)
    # -------------------------------------------------------------
    
    market_aggregator_service = providers.Factory(
        MarketAggregatorService,
        aggregation_repo=pandas_aggregator_repo
    )
    
    feature_engine_service = providers.Factory(
        FeatureEngineService,
        feature_repo=pandas_feature_repo
    )
    
    live_ticker_service = providers.Factory(
        LiveTickerService,
        event_bus=redis_event_bus
    )
    
    # -------------------------------------------------------------
    # 3. USE CASES & SCHEDULERS
    # -------------------------------------------------------------
    
    execute_trade_uc = providers.Factory(
        ExecuteTradeUseCase,
        broker=ctrader_broker,
        trade_repo=None, # Placeholder
        event_bus=redis_event_bus,
        risk_validator=RiskValidatorService(),
        sizing_service=PositionSizingService(),
        active_profile=get_profile("MEDIUM_LIVE")
    )
    
    scan_market_uc = providers.Factory(
        ScanMarketUseCase,
        candle_repo=pandas_aggregator_repo, # Reusing aggregator repo as candle repo for now
        event_bus=redis_event_bus,
        execute_trade_uc=execute_trade_uc 
    )
    
    scan_scheduler = providers.Singleton(
        ScanScheduler,
        scan_market_uc=scan_market_uc,
        event_bus=redis_event_bus
    )

