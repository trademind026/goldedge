import logging
import pandas as pd
from typing import List, Optional
from ..models.signal import Signal, SignalDirection
from ..models.regime import MarketRegime

logger = logging.getLogger(__name__)

# ── XAUUSD Session Gate ───────────────────────────────────────────────────────
# Only trade during London open to NY close (UTC).
# Asia session (00:00–07:00 UTC) has wider spreads, low volume, and false signals.
_SESSION_OPEN_UTC  = 7   # London opens 08:00 BST = 07:00 UTC
_SESSION_CLOSE_UTC = 20  # NY closes 16:00 EDT = 20:00 UTC


def _in_session(row: dict) -> bool:
    """Return True if the bar's timestamp is in the active trading session."""
    ts = row.get("time")
    if ts is None:
        return True  # fail open — don't block if timestamp missing
    hour = ts.hour if hasattr(ts, "hour") else 12
    return _SESSION_OPEN_UTC <= hour < _SESSION_CLOSE_UTC


class SignalGeneratorService:
    """
    Evaluates trading strategies against technical indicator rows.

    XAUUSD-specific optimizations vs Nifty Options reference:
      - Session gate: no signals during Asia dead zone (00:00–07:00 UTC)
      - mean_reversion: RSI bounds 38/62 instead of 30/70 (gold rarely extremes)
      - bullish/bearish_momentum: volume spike raised to 2.0×; EMA slope as alternative
      - vwap_momentum_breakout: requires 2.5× volume (institutional breakout only)
    """

    # Strategy-Regime Mapping
    REGIME_STRATEGIES = {
        MarketRegime.TRENDING_BULL:  ["vwap_momentum_breakout", "bullish_momentum"],
        MarketRegime.TRENDING_BEAR:  ["bearish_momentum"],
        MarketRegime.SIDEWAYS:       ["bullish_momentum", "bearish_momentum"],
        MarketRegime.HIGH_VOLATILITY: ["bearish_momentum", "vwap_momentum_breakout", "bullish_momentum"],
        MarketRegime.LOW_VOLATILITY:  ["vwap_momentum_breakout"],
        MarketRegime.UNKNOWN:         ["vwap_momentum_breakout", "bearish_momentum", "bullish_momentum"],
    }

    @staticmethod
    def generate_signals(df: pd.DataFrame, symbol: str, regime: MarketRegime) -> List[Signal]:
        """
        Runs the active strategies for the current regime against the latest row in df.
        """
        if df.empty:
            return []

        latest_row = df.iloc[-1].to_dict()

        # ── Session Gate — block signals during Asia dead zone ────────────────
        if not _in_session(latest_row):
            return []

        active_strategies = SignalGeneratorService.REGIME_STRATEGIES.get(
            regime, SignalGeneratorService.REGIME_STRATEGIES[MarketRegime.UNKNOWN]
        )

        signals = []

        if "vwap_momentum_breakout" in active_strategies:
            sig = SignalGeneratorService._vwap_momentum_breakout(latest_row, symbol)
            if sig:
                signals.append(sig)

        if "bullish_momentum" in active_strategies:
            sig = SignalGeneratorService._bullish_momentum(latest_row, symbol)
            if sig:
                signals.append(sig)

        if "bearish_momentum" in active_strategies:
            sig = SignalGeneratorService._bearish_momentum(latest_row, symbol)
            if sig:
                signals.append(sig)

        if "mean_reversion" in active_strategies:
            sig = SignalGeneratorService._mean_reversion(latest_row, symbol, regime)
            if sig:
                signals.append(sig)

        return signals

    # ──────────────────────────────────────────────────────────────────────────
    # STRATEGIES
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _vwap_momentum_breakout(row: dict, symbol: str) -> Optional[Signal]:
        """
        XAUUSD VWAP Breakout: requires strong institutional volume (2.5×).
        Nifty reference had 2.0× which is too easy for gold's normal intraday swings.
        """
        required = ["close", "vwap", "rsi", "ema20", "ema50"]
        if not all(k in row and not pd.isna(row[k]) for k in required):
            return None

        conditions = {
            "price_above_vwap":  row["close"] > row.get("vwap", 0),
            "vwap_slope_rising": row.get("ema20_slope", 0) > 0.00005,   # meaningful slope
            "rsi_above_60":      row["rsi"] > 60,
            "volume_surge_2x5": row.get("volume_ratio", 0) > 2.5,       # raised: real breakout only
            "ema20_above_ema50": row["ema20"] > row["ema50"],
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
        """
        XAUUSD Bullish Momentum.
        Volume raised to 2.0× OR strong EMA slope (trend conviction without volume spike).
        """
        required = ["close", "vwap", "rsi", "ema20", "ema50"]
        if not all(k in row and not pd.isna(row[k]) for k in required):
            return None

        ema20_slope = row.get("ema20_slope", 0.0)
        volume_ratio = row.get("volume_ratio", 0.0)

        # Volume OR slope — either is sufficient conviction signal
        strong_volume = volume_ratio > 2.0
        strong_slope  = ema20_slope > 0.0002   # decisive uptrend on 1m bars

        conditions = {
            "price_above_vwap":   row["close"] > row.get("vwap", 0),
            "rsi_above_55":       row["rsi"] > 55,
            "ema20_above_ema50":  row["ema20"] > row["ema50"],
            "volume_or_slope":    strong_volume or strong_slope,
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
        """
        XAUUSD Bearish Momentum.
        Volume raised to 2.0× OR strong negative EMA slope.
        """
        required = ["close", "vwap", "rsi", "ema20", "ema50"]
        if not all(k in row and not pd.isna(row[k]) for k in required):
            return None

        ema20_slope  = row.get("ema20_slope", 0.0)
        volume_ratio = row.get("volume_ratio", 0.0)

        strong_volume = volume_ratio > 2.0
        strong_slope  = ema20_slope < -0.0002   # decisive downtrend on 1m bars

        conditions = {
            "price_below_vwap":  row["close"] < row.get("vwap", float("inf")),
            "rsi_below_45":      row["rsi"] < 45,
            "ema20_below_ema50": row["ema20"] < row["ema50"],
            "volume_or_slope":   strong_volume or strong_slope,
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
    def _mean_reversion(row: dict, symbol: str, regime: MarketRegime = MarketRegime.SIDEWAYS) -> Optional[Signal]:
        """
        XAUUSD Mean Reversion.

        Key changes from Nifty reference:
          - RSI extremes: 38/62 (not 30/70). Gold RSI rarely hits 30/70 in a session;
            using 30/70 gave only 23% win rate because signals fired mid-trend.
          - EMA slope veto: blocks counter-trend signals when slope is aggressive.
            If slope > 0.0003, don't sell. If slope < -0.0003, don't buy.
          - Bollinger touch requirement tightened to 0.001 (price within 0.1% of band).
        """
        required = ["close", "rsi"]
        if not all(k in row and not pd.isna(row[k]) for k in required):
            return None

        ema20_slope = row.get("ema20_slope", 0.0)

        # XAUUSD-calibrated RSI extremes: 38/62 instead of 30/70
        is_oversold   = row["rsi"] < 38
        is_overbought = row["rsi"] > 62

        if not (is_oversold or is_overbought):
            return None

        bb_lower = row.get("bollinger_lower")
        bb_upper = row.get("bollinger_upper")

        conditions = {}
        if is_oversold:
            # Veto: don't buy if price is in a strong downtrend
            if ema20_slope < -0.0003:
                return None
            conditions["rsi_oversold"]    = True
            conditions["near_bb_lower"]   = (
                bb_lower is not None and row["close"] <= bb_lower * 1.001
            )
            conditions["ema_slope_ok"]    = ema20_slope >= -0.0001  # not aggressively down
            direction = SignalDirection.BUY

        else:  # is_overbought
            # Veto: don't sell if price is in a strong uptrend
            if ema20_slope > 0.0003:
                return None
            conditions["rsi_overbought"]  = True
            conditions["near_bb_upper"]   = (
                bb_upper is not None and row["close"] >= bb_upper * 0.999
            )
            conditions["ema_slope_ok"]    = ema20_slope <= 0.0001   # not aggressively up
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
