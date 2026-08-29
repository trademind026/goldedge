import logging
import pandas as pd
from typing import List, Optional
from ..models.signal import Signal, SignalDirection
from ..models.regime import MarketRegime

logger = logging.getLogger(__name__)

class SignalGeneratorService:
    """
    Evaluates trading strategies against technical indicator rows.
    Adapted from AI-trader reference architecture.
    """

    # Strategy-Regime Mapping dictating which strategies are active per environment
    REGIME_STRATEGIES = {
        MarketRegime.TRENDING_BULL: ["vwap_momentum_breakout", "bullish_momentum"],
        MarketRegime.TRENDING_BEAR: ["bearish_momentum"],
        MarketRegime.SIDEWAYS: ["mean_reversion"],
        MarketRegime.HIGH_VOLATILITY: ["mean_reversion", "bearish_momentum", "vwap_momentum_breakout", "bullish_momentum"],
        MarketRegime.LOW_VOLATILITY: ["vwap_momentum_breakout", "bullish_momentum"],
        MarketRegime.UNKNOWN: ["vwap_momentum_breakout", "bearish_momentum", "bullish_momentum", "mean_reversion"],
    }

    @staticmethod
    def generate_signals(df: pd.DataFrame, symbol: str, regime: MarketRegime) -> List[Signal]:
        """
        Runs the active strategies for the current regime against the latest row in df.
        """
        if df.empty:
            return []
            
        latest_row = df.iloc[-1].to_dict()
        active_strategies = SignalGeneratorService.REGIME_STRATEGIES.get(
            regime, SignalGeneratorService.REGIME_STRATEGIES[MarketRegime.UNKNOWN]
        )
        
        signals = []
        
        if "vwap_momentum_breakout" in active_strategies:
            sig = SignalGeneratorService._vwap_momentum_breakout(latest_row, symbol)
            if sig: signals.append(sig)
            
        if "bullish_momentum" in active_strategies:
            sig = SignalGeneratorService._bullish_momentum(latest_row, symbol)
            if sig: signals.append(sig)
            
        if "bearish_momentum" in active_strategies:
            sig = SignalGeneratorService._bearish_momentum(latest_row, symbol)
            if sig: signals.append(sig)
            
        if "mean_reversion" in active_strategies:
            sig = SignalGeneratorService._mean_reversion(latest_row, symbol)
            if sig: signals.append(sig)
            
        return signals

    # ──────────────────────────────────────────────────────────────
    # STRATEGIES
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _vwap_momentum_breakout(row: dict, symbol: str) -> Optional[Signal]:
        required = ["close", "vwap", "rsi", "ema20", "ema50"]
        if not all(k in row and not pd.isna(row[k]) for k in required):
            return None

        conditions = {
            "price_above_vwap":    row["close"] > row.get("vwap", 0),
            "vwap_slope_rising":   row.get("ema20_slope", 0) > 0,
            "rsi_above_60":        row["rsi"] > 60,
            "volume_surge_2x":     row.get("volume_ratio", 0) > 2.0,
            "ema20_above_ema50":   row["ema20"] > row["ema50"],
        }

        if sum(conditions.values()) >= 5:
            return Signal(
                strategy="vwap_momentum_breakout",
                direction=SignalDirection.BUY,
                symbol=symbol,
                entry_price=row["close"],
                technical_strength=1.0,
                details=conditions,
            )
        return None

    @staticmethod
    def _bullish_momentum(row: dict, symbol: str) -> Optional[Signal]:
        required = ["close", "vwap", "rsi", "ema20", "ema50"]
        if not all(k in row and not pd.isna(row[k]) for k in required):
            return None

        conditions = {
            "price_above_vwap": row["close"] > row.get("vwap", 0),
            "rsi_above_55":     row["rsi"] > 55,
            "ema20_above_ema50": row["ema20"] > row["ema50"],
            "volume_spike":     row.get("volume_spike", 0) == 1 or row.get("volume_ratio", 0) > 1.5,
        }

        if sum(conditions.values()) >= 4:
            return Signal(
                strategy="bullish_momentum",
                direction=SignalDirection.BUY,
                symbol=symbol,
                entry_price=row["close"],
                technical_strength=1.0,
                details=conditions,
            )
        return None

    @staticmethod
    def _bearish_momentum(row: dict, symbol: str) -> Optional[Signal]:
        required = ["close", "vwap", "rsi", "ema20", "ema50"]
        if not all(k in row and not pd.isna(row[k]) for k in required):
            return None

        conditions = {
            "price_below_vwap": row["close"] < row.get("vwap", float("inf")),
            "rsi_below_45": row["rsi"] < 45,
            "ema20_below_ema50": row["ema20"] < row["ema50"],
            "volume_spike": row.get("volume_spike", 0) == 1 or row.get("volume_ratio", 0) > 1.5,
        }

        met = sum(conditions.values())
        if met >= 3:
            return Signal(
                strategy="bearish_momentum",
                direction=SignalDirection.SELL,
                symbol=symbol,
                entry_price=row["close"],
                technical_strength=round(met / len(conditions), 2),
                details=conditions,
            )
        return None

    @staticmethod
    def _mean_reversion(row: dict, symbol: str) -> Optional[Signal]:
        required = ["close", "rsi"]
        if not all(k in row and not pd.isna(row[k]) for k in required):
            return None

        is_oversold = row["rsi"] < 30
        is_overbought = row["rsi"] > 70

        if not (is_oversold or is_overbought):
            return None

        bb_lower = row.get("bollinger_lower")
        bb_upper = row.get("bollinger_upper")
        vwap_dist = abs(row.get("vwap_dist", 0))

        conditions = {}
        if is_oversold:
            conditions["rsi_extreme"] = True
            conditions["near_bb_lower"] = (bb_lower is not None and row["close"] <= bb_lower * 1.002)
            conditions["far_from_vwap"] = vwap_dist > 0.003
            direction = SignalDirection.BUY
        else:
            conditions["rsi_extreme"] = True
            conditions["near_bb_upper"] = (bb_upper is not None and row["close"] >= bb_upper * 0.998)
            conditions["far_from_vwap"] = vwap_dist > 0.003
            direction = SignalDirection.SELL

        met = sum(conditions.values())
        if met >= 2:
            return Signal(
                strategy="mean_reversion",
                direction=direction,
                symbol=symbol,
                entry_price=row["close"],
                technical_strength=round(met / len(conditions), 2),
                details=conditions,
            )
        return None
