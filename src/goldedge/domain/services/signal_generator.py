import logging
import pandas as pd
from typing import List, Optional
from ..models.signal import Signal, SignalDirection
from ..models.regime import MarketRegime
from ..models.pip_calculator import PipCalculator

logger = logging.getLogger(__name__)

# ── XAUUSD Session Gate ───────────────────────────────────────────────────────
# Only trade during London open to NY close (UTC).
# Asia session (00:00–07:00 UTC) has wider spreads, low volume, and false signals.
_SESSION_OPEN_UTC  = 7   # London opens 08:00 BST = 07:00 UTC
_SESSION_CLOSE_UTC = 17  # Close entries at 17:00 UTC (avoids post-London late-NY chop)


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
        MarketRegime.TRENDING_BULL:   ["kinematic_kalman_trend", "vwap_momentum_breakout", "bullish_momentum"],
        MarketRegime.TRENDING_BEAR:   ["kinematic_kalman_trend", "bearish_momentum"],
        MarketRegime.KINEMATIC_TREND: ["kinematic_kalman_trend", "bullish_momentum", "bearish_momentum"],
        MarketRegime.MEAN_REVERSION:  ["statistical_mean_reversion", "mean_reversion"],
        MarketRegime.SESSION_ORB:     ["session_orb", "vwap_momentum_breakout"],
        MarketRegime.SIDEWAYS:        ["statistical_mean_reversion", "bullish_momentum", "bearish_momentum"],
        MarketRegime.HIGH_VOLATILITY: ["session_orb", "bearish_momentum", "vwap_momentum_breakout", "bullish_momentum"],
        MarketRegime.LOW_VOLATILITY:  ["statistical_mean_reversion", "vwap_momentum_breakout"],
        MarketRegime.UNKNOWN:         ["kinematic_kalman_trend", "vwap_momentum_breakout", "bearish_momentum", "bullish_momentum"],
    }


    @staticmethod
    def generate_signals(df: pd.DataFrame, symbol: str, regime: MarketRegime) -> List[Signal]:
        """
        Runs the active strategies for the current regime against the latest row in df.
        """
        if df.empty:
            return []

        latest_row = df.iloc[-1].to_dict()
        if "time" not in latest_row or latest_row["time"] is None:
            latest_row["time"] = df.index[-1]

        # ── Session Gate — block signals during Asia dead zone & post-London chop ──
        if not _in_session(latest_row):
            return []

        active_strategies = SignalGeneratorService.REGIME_STRATEGIES.get(
            regime, SignalGeneratorService.REGIME_STRATEGIES[MarketRegime.UNKNOWN]
        )

        signals = []

        if "kinematic_kalman_trend" in active_strategies:
            sig = SignalGeneratorService._kinematic_kalman_trend(df, symbol)
            if sig:
                signals.append(sig)

        if "statistical_mean_reversion" in active_strategies:
            sig = SignalGeneratorService._statistical_mean_reversion(df, symbol)
            if sig:
                signals.append(sig)

        if "session_orb" in active_strategies:
            sig = SignalGeneratorService._session_orb(df, symbol)
            if sig:
                signals.append(sig)

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
        XAUUSD Bearish Momentum (Experiment E — symmetric 4/4 + bearish candle confirmation).
        Volume raised to 2.0× OR strong negative EMA slope, plus bearish candle close < open.
        """
        required = ["close", "vwap", "rsi", "ema20", "ema50"]
        if not all(k in row and not pd.isna(row[k]) for k in required):
            return None

        open_px = row.get("open")
        if open_px is not None and not pd.isna(open_px) and float(row["close"]) >= float(open_px):
            return None

        ema20_slope  = row.get("ema20_slope", 0.0)
        volume_ratio = row.get("volume_ratio", 0.0)

        strong_volume = volume_ratio > 2.0
        strong_slope  = ema20_slope < -0.0002   # decisive downtrend

        conditions = {
            "price_below_vwap":  row["close"] < row.get("vwap", float("inf")),
            "rsi_below_45":      row["rsi"] < 45,
            "ema20_below_ema50": row["ema20"] < row["ema50"],
            "volume_or_slope":   strong_volume or strong_slope,
        }

        met = sum(conditions.values())
        if met == 4:
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

    # ──────────────────────────────────────────────────────────────────────────
    # INSTITUTIONAL ENGINES (STAGE 2)
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _kinematic_kalman_trend(df: pd.DataFrame, symbol: str) -> Optional[Signal]:
        """
        Stage 2.2: Kinematic Kalman Trend-Following Engine.
        Enforces macro trend consensus, Kalman velocity & acceleration gating,
        pullback to value zone, and >40% rejection wick confirmation.
        """
        if len(df) < 50:
            return None

        row = df.iloc[-1]
        prev_row = df.iloc[-2] if len(df) >= 2 else row
        pip_unit = PipCalculator.get_pip_unit(symbol)

        close = float(row.get("close", 0.0))
        open_px = float(row.get("open", close))
        high = float(row.get("high", close))
        low = float(row.get("low", close))
        ema20 = float(row.get("ema20", 0.0))
        ema50 = float(row.get("ema50", 0.0))
        ema200 = float(row.get("ema200", 0.0))
        atr14 = float(row.get("atr", close * 0.0005))
        kalman_price = float(row.get("kalman_price", ema20))
        vel_z = float(row.get("kalman_velocity_z", row.get("kalman_vel_z", 0.0)))
        accel = float(row.get("kalman_accel", 0.0))

        candle_range = max(high - low, pip_unit * 0.5)

        # Bullish Kinematic Trend
        if close > ema50 > ema200 and vel_z >= 1.5 and accel >= 0.0:
            # Pullback into value zone between EMA20 and Kalman price
            val_zone_high = max(ema20, kalman_price)
            if low <= val_zone_high + (pip_unit * 2.0):
                lower_wick = min(open_px, close) - low
                if (lower_wick / candle_range) >= 0.40:
                    sl_dist = 1.8 * atr14
                    tp_dist = 3.6 * atr14
                    sl_px = close - sl_dist
                    tp_px = close + tp_dist
                    return Signal(
                        strategy="kinematic_kalman_trend",
                        direction=SignalDirection.BUY,
                        symbol=symbol,
                        entry_price=close,
                        technical_strength=1.0,
                        initial_sl_price=sl_px,
                        target_price=tp_px,
                        base_risk_pips=sl_dist / pip_unit,
                        base_reward_pips=tp_dist / pip_unit,
                        regime="KINEMATIC_TREND",
                        timestamp=row.get("time")
                    )

        # Bearish Kinematic Trend
        if close < ema50 < ema200 and vel_z <= -1.5 and accel <= 0.0:
            val_zone_low = min(ema20, kalman_price)
            if high >= val_zone_low - (pip_unit * 2.0):
                upper_wick = high - max(open_px, close)
                if (upper_wick / candle_range) >= 0.40:
                    sl_dist = 1.8 * atr14
                    tp_dist = 3.6 * atr14
                    sl_px = close + sl_dist
                    tp_px = close - tp_dist
                    return Signal(
                        strategy="kinematic_kalman_trend",
                        direction=SignalDirection.SELL,
                        symbol=symbol,
                        entry_price=close,
                        technical_strength=1.0,
                        initial_sl_price=sl_px,
                        target_price=tp_px,
                        base_risk_pips=sl_dist / pip_unit,
                        base_reward_pips=tp_dist / pip_unit,
                        regime="KINEMATIC_TREND",
                        timestamp=row.get("time")
                    )

        return None

    @staticmethod
    def _statistical_mean_reversion(df: pd.DataFrame, symbol: str) -> Optional[Signal]:
        """
        Stage 2.3: Statistical Mean Reversion Engine.
        Active during range/chop regimes: 3.0 std Bollinger band breach + RSI divergence
        + candle close back inside band + Kalman velocity curl.
        """
        if len(df) < 25:
            return None

        pip_unit = PipCalculator.get_pip_unit(symbol)
        close_series = df["close"]
        rolling_mean = close_series.rolling(20).mean()
        rolling_std = close_series.rolling(20).std()

        mid_band = float(rolling_mean.iloc[-1])
        upper_3std = float(rolling_mean.iloc[-1] + 3.0 * rolling_std.iloc[-1])
        lower_3std = float(rolling_mean.iloc[-1] - 3.0 * rolling_std.iloc[-1])

        row = df.iloc[-1]
        prev_row = df.iloc[-2]
        close = float(row.get("close", 0.0))
        prev_close = float(prev_row.get("close", close))
        prev_low = float(prev_row.get("low", prev_close))
        prev_high = float(prev_row.get("high", prev_close))
        rsi = float(row.get("rsi", 50.0))

        curr_vel = float(row.get("kalman_velocity", row.get("kalman_velocity_z", 0.0)))
        prev_vel = float(prev_row.get("kalman_velocity", prev_row.get("kalman_velocity_z", 0.0)))

        # Bullish Mean Reversion: breached lower 3.0 std, now closes back inside, velocity curls up
        if prev_low <= lower_3std and close > lower_3std and rsi < 28 and curr_vel > prev_vel:
            swing_low = float(df["low"].tail(5).min())
            sl_px = swing_low - (3.0 * pip_unit)
            tp_px = mid_band
            sl_dist = abs(close - sl_px)
            tp_dist = abs(tp_px - close)
            return Signal(
                strategy="statistical_mean_reversion",
                direction=SignalDirection.BUY,
                symbol=symbol,
                entry_price=close,
                technical_strength=0.95,
                initial_sl_price=sl_px,
                target_price=tp_px,
                base_risk_pips=sl_dist / pip_unit,
                base_reward_pips=tp_dist / pip_unit,
                regime="MEAN_REVERSION",
                timestamp=row.get("time")
            )

        # Bearish Mean Reversion: breached upper 3.0 std, now closes back inside, velocity curls down
        if prev_high >= upper_3std and close < upper_3std and rsi > 72 and curr_vel < prev_vel:
            swing_high = float(df["high"].tail(5).max())
            sl_px = swing_high + (3.0 * pip_unit)
            tp_px = mid_band
            sl_dist = abs(sl_px - close)
            tp_dist = abs(close - tp_px)
            return Signal(
                strategy="statistical_mean_reversion",
                direction=SignalDirection.SELL,
                symbol=symbol,
                entry_price=close,
                technical_strength=0.95,
                initial_sl_price=sl_px,
                target_price=tp_px,
                base_risk_pips=sl_dist / pip_unit,
                base_reward_pips=tp_dist / pip_unit,
                regime="MEAN_REVERSION",
                timestamp=row.get("time")
            )

        return None

    @staticmethod
    def _session_orb(df: pd.DataFrame, symbol: str) -> Optional[Signal]:
        """
        Stage 2.4: Session Opening Range Breakout (ORB) Engine.
        Evaluated during London (07:00–08:30 UTC) and NY (12:30–14:00 UTC) opens.
        """
        if len(df) < 20:
            return None

        row = df.iloc[-1]
        ts = row.get("time")
        if ts is None:
            return None

        hour = ts.hour if hasattr(ts, "hour") else 12
        minute = ts.minute if hasattr(ts, "minute") else 0

        # Session Open Window
        is_london = (hour == 7) or (hour == 8 and minute <= 30)
        is_ny = (hour == 12 and minute >= 30) or (hour == 13) or (hour == 14 and minute == 0)

        if not (is_london or is_ny):
            return None

        pip_unit = PipCalculator.get_pip_unit(symbol)
        close = float(row.get("close", 0.0))
        vol = float(row.get("tick_volume", row.get("volume", 1.0)))
        atr14 = float(row.get("atr", close * 0.0005))
        adr_est = atr14 * 10.0  # approximate Average Daily Range

        # 15-minute Opening Range prior to current bar
        prior_df = df.iloc[:-1]
        recent_15m = prior_df.tail(15)
        or_high = float(recent_15m["high"].max())
        or_low = float(recent_15m["low"].min())
        or_range = or_high - or_low

        # Compression check: OR range < 0.75 * ADR
        if or_range >= 0.75 * adr_est:
            return None

        vol_avg = float(prior_df["tick_volume"].tail(20).mean()) if "tick_volume" in prior_df.columns else 1.0
        vol_surge = vol >= 2.0 * vol_avg

        # BUY Breakout
        if close > or_high and vol_surge:
            sl_px = (or_high + or_low) / 2.0
            tp_px = close + (1.5 * or_range)
            sl_dist = abs(close - sl_px)
            tp_dist = abs(tp_px - close)
            return Signal(
                strategy="session_orb",
                direction=SignalDirection.BUY,
                symbol=symbol,
                entry_price=close,
                technical_strength=1.0,
                initial_sl_price=sl_px,
                target_price=tp_px,
                base_risk_pips=sl_dist / pip_unit,
                base_reward_pips=tp_dist / pip_unit,
                regime="SESSION_ORB",
                timestamp=ts
            )

        # SELL Breakout
        if close < or_low and vol_surge:
            sl_px = (or_high + or_low) / 2.0
            tp_px = close - (1.5 * or_range)
            sl_dist = abs(sl_px - close)
            tp_dist = abs(close - tp_px)
            return Signal(
                strategy="session_orb",
                direction=SignalDirection.SELL,
                symbol=symbol,
                entry_price=close,
                technical_strength=1.0,
                initial_sl_price=sl_px,
                target_price=tp_px,
                base_risk_pips=sl_dist / pip_unit,
                base_reward_pips=tp_dist / pip_unit,
                regime="SESSION_ORB",
                timestamp=ts
            )

        return None

