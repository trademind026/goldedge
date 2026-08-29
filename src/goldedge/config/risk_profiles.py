from dataclasses import dataclass
from enum import Enum


class ProfileContext(str, Enum):
    """
    LIVE     — real money. Conservative thresholds, strict score gate.
    BACKTEST — historical simulation. Looser thresholds to surface historical
               edge. Do NOT apply backtest thresholds to real money.
    """
    LIVE = "live"
    BACKTEST = "backtest"


@dataclass(frozen=True)
class RiskProfile:
    """
    Immutable risk parameter set for GoldEdge XAU/USD (Spot Gold CFD).

    score_threshold:
        Configuration-driven. Default V1 values: LIVE=0.85, BACKTEST=0.70.
        Backtest at 0.70 / 0.75 / 0.80 / 0.85 to find real edge.
        Never over-fit thresholds to historical data.

    RR (Risk-Reward):
        Controlled by sl_atr_multiplier vs tp_atr_multiplier.
        Backtest at 1:1, 1:1.5, 1:2, and ATR trailing before freezing.
        A 1:1 profile requires a higher win rate to maintain edge after
        spread and slippage.
    """
    name: str
    context: ProfileContext

    risk_per_trade_pct: float       # Max equity risk per trade. V1 start: 0.5%
    max_daily_loss_pct: float       # Stop trading for the day above this.
    max_consecutive_losses: int     # Pause after this many consecutive losses.
    max_trades_per_day: int         # Hard cap on daily trade count.

    sl_atr_multiplier: float        # SL = ATR × this value
    tp_atr_multiplier: float        # TP = ATR × this value

    min_score_threshold: float      # Minimum composite signal score to trade.


# ══════════════════════════════════════════════════════════════════════════
# LOW RISK — LIVE context (real money, conservative)
# ══════════════════════════════════════════════════════════════════════════

LOW_RISK_LIVE = RiskProfile(
    name="LOW_LIVE",
    context=ProfileContext.LIVE,
    risk_per_trade_pct=0.5,
    max_daily_loss_pct=2.0,
    max_consecutive_losses=3,
    max_trades_per_day=5,
    sl_atr_multiplier=2.0,
    tp_atr_multiplier=2.0,          # 1:1 RR baseline
    min_score_threshold=0.85,       # Strict for real capital.
)

# ══════════════════════════════════════════════════════════════════════════
# LOW RISK — BACKTEST context (historical research, looser entry filter)
# ══════════════════════════════════════════════════════════════════════════

LOW_RISK_BACKTEST = RiskProfile(
    name="LOW_BACKTEST",
    context=ProfileContext.BACKTEST,
    risk_per_trade_pct=0.5,
    max_daily_loss_pct=2.0,
    max_consecutive_losses=3,
    max_trades_per_day=5,
    sl_atr_multiplier=2.0,
    tp_atr_multiplier=2.0,
    min_score_threshold=0.70,       # Looser to surface historical edge.
                                    # Do NOT use this threshold live.
)

# ══════════════════════════════════════════════════════════════════════════
# RR RESEARCH VARIANTS (BACKTEST only)
# Use these to find optimal RR before committing to a live profile.
# ══════════════════════════════════════════════════════════════════════════

LOW_RR15_BACKTEST = RiskProfile(
    name="LOW_RR15_BACKTEST",
    context=ProfileContext.BACKTEST,
    risk_per_trade_pct=0.5,
    max_daily_loss_pct=2.0,
    max_consecutive_losses=3,
    max_trades_per_day=5,
    sl_atr_multiplier=2.0,
    tp_atr_multiplier=3.0,          # 1:1.5 RR
    min_score_threshold=0.70,
)

LOW_RR20_BACKTEST = RiskProfile(
    name="LOW_RR20_BACKTEST",
    context=ProfileContext.BACKTEST,
    risk_per_trade_pct=0.5,
    max_daily_loss_pct=2.0,
    max_consecutive_losses=3,
    max_trades_per_day=5,
    sl_atr_multiplier=2.0,
    tp_atr_multiplier=4.0,          # 1:2 RR
    min_score_threshold=0.70,
)

# ══════════════════════════════════════════════════════════════════════════
# MEDIUM RISK — research and validated live
# ══════════════════════════════════════════════════════════════════════════

MEDIUM_RISK_LIVE = RiskProfile(
    name="MEDIUM_LIVE",
    context=ProfileContext.LIVE,
    risk_per_trade_pct=1.0,
    max_daily_loss_pct=3.0,
    max_consecutive_losses=3,
    max_trades_per_day=5,
    sl_atr_multiplier=1.5,
    tp_atr_multiplier=3.0,          # 1:2 RR
    min_score_threshold=0.85,
)

MEDIUM_RISK_BACKTEST = RiskProfile(
    name="MEDIUM_BACKTEST",
    context=ProfileContext.BACKTEST,
    risk_per_trade_pct=1.0,
    max_daily_loss_pct=3.0,
    max_consecutive_losses=3,
    max_trades_per_day=8,           # More trades allowed in backtest to explore edge
    sl_atr_multiplier=1.5,
    tp_atr_multiplier=3.0,
    min_score_threshold=0.72,
)

# ══════════════════════════════════════════════════════════════════════════
# Profile registry
# ══════════════════════════════════════════════════════════════════════════

PROFILES = {
    # Live
    "LOW_LIVE":     LOW_RISK_LIVE,
    "MEDIUM_LIVE":  MEDIUM_RISK_LIVE,
    # Backtest research
    "LOW_BACKTEST":      LOW_RISK_BACKTEST,
    "LOW_RR15_BACKTEST": LOW_RR15_BACKTEST,
    "LOW_RR20_BACKTEST": LOW_RR20_BACKTEST,
    "MEDIUM_BACKTEST":   MEDIUM_RISK_BACKTEST,
}


def get_profile(name: str) -> RiskProfile:
    """Retrieve a profile by name. Raises KeyError if not found."""
    if name not in PROFILES:
        raise KeyError(
            f"Unknown profile '{name}'. Available: {list(PROFILES.keys())}"
        )
    return PROFILES[name]
