from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class GoldRiskProfile:
    name: str
    risk_per_trade_pct: float
    max_daily_loss_pct: float
    max_consecutive_losses: int
    max_trades_per_day: int
    sl_atr_multiplier: float
    tp_atr_multiplier: float
    min_score_threshold: float
    max_spread_dollars: float = 0.60
    min_kalman_vel_z: float = 0.50
    min_meta_prob: float = 0.62
    default_leverage: float = 100.0


GOLD_LOW_RISK = GoldRiskProfile(
    name="Gold Low Risk (Conservative)",
    risk_per_trade_pct=0.75,
    max_daily_loss_pct=2.0,
    max_consecutive_losses=2,
    max_trades_per_day=4,
    sl_atr_multiplier=1.5,
    tp_atr_multiplier=2.5,
    min_score_threshold=0.85,
    max_spread_dollars=0.45,
    min_kalman_vel_z=0.60,
    min_meta_prob=0.65
)

GOLD_MEDIUM_RISK = GoldRiskProfile(
    name="Gold Medium Risk (Standard Institutional)",
    risk_per_trade_pct=1.5,
    max_daily_loss_pct=3.5,
    max_consecutive_losses=2,
    max_trades_per_day=6,
    sl_atr_multiplier=2.0,
    tp_atr_multiplier=3.5,
    min_score_threshold=0.75,
    max_spread_dollars=0.60,
    min_kalman_vel_z=0.50,
    min_meta_prob=0.62
)

GOLD_HIGH_RISK = GoldRiskProfile(
    name="Gold High Risk (Aggressive Momentum)",
    risk_per_trade_pct=2.5,
    max_daily_loss_pct=5.0,
    max_consecutive_losses=3,
    max_trades_per_day=10,
    sl_atr_multiplier=2.5,
    tp_atr_multiplier=5.0,
    min_score_threshold=0.70,
    max_spread_dollars=0.60,
    min_kalman_vel_z=0.40,
    min_meta_prob=0.58
)

GOLD_PROFILES: Dict[str, GoldRiskProfile] = {
    "low": GOLD_LOW_RISK,
    "medium": GOLD_MEDIUM_RISK,
    "high": GOLD_HIGH_RISK
}


def get_gold_profile(key: str) -> GoldRiskProfile:
    return GOLD_PROFILES.get(key.lower(), GOLD_MEDIUM_RISK)
