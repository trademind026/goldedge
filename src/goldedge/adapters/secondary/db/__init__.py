"""
Database Abstraction Layer — public API.
Import from here instead of individual modules.
"""
from .connection_pool import build_engine, get_connection
from .candle_repository import CandleRepository
from .trade_repository import TradeRepository
from .tick_repository import TickRepository
from .config_repository import SystemConfigRepository

__all__ = [
    "build_engine",
    "get_connection",
    "CandleRepository",
    "TradeRepository",
    "TickRepository",
    "SystemConfigRepository",
]
