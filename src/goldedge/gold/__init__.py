"""
GoldEdge Spot Gold (XAUUSD) Quantitative Subsystem.
Contains domain models, Kalman filters, exit agents, order management,
and decision loops dedicated specifically to Gold trading.
"""
from .domain.models.gold_pip_engine import GoldPipEngine
from .domain.models.gold_open_trade import GoldOpenTrade
from .domain.services.gold_kalman_exit_agent import GoldKalmanExitAgent
from .domain.services.gold_decision_rules import GoldDecisionRules
from .domain.services.gold_adaptive_sizing import gold_adaptive_position_size
from .adapters.features.gold_kalman_filter import apply_gold_adaptive_kalman, StreamingGoldKalmanFilter
from .adapters.broker.gold_order_manager import GoldOrderManager
from .application.use_cases.gold_decision_loop import GoldDecisionLoopUseCase
from .application.use_cases.gold_tick_replay import GoldTickReplayUseCase
from .config.gold_profiles import GOLD_PROFILES, get_gold_profile

__all__ = [
    "GoldPipEngine",
    "GoldOpenTrade",
    "GoldKalmanExitAgent",
    "GoldDecisionRules",
    "gold_adaptive_position_size",
    "apply_gold_adaptive_kalman",
    "StreamingGoldKalmanFilter",
    "GoldOrderManager",
    "GoldDecisionLoopUseCase",
    "GoldTickReplayUseCase",
    "GOLD_PROFILES",
    "get_gold_profile",
]
