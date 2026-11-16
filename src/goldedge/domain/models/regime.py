from enum import Enum

class MarketRegime(str, Enum):
    """
    Categorizes the current market environment.
    Used to dynamically select which strategies are active.
    """
    TRENDING_BULL = "TRENDING_BULL"
    TRENDING_BEAR = "TRENDING_BEAR"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    KINEMATIC_TREND = "KINEMATIC_TREND"
    MEAN_REVERSION = "MEAN_REVERSION"
    SESSION_ORB = "SESSION_ORB"
    UNKNOWN = "UNKNOWN"

