import warnings
import pandas as pd
import numpy as np
try:
    import pandas_ta as ta
except ImportError:
    import pandas_ta_classic as ta

warnings.filterwarnings("ignore", category=UserWarning, module="pandas_ta_classic")
warnings.filterwarnings("ignore", category=UserWarning, module="pandas_ta")
from typing import List
from ..models.bar import Bar


class IndicatorService:
    """
    Computes technical indicators from raw OHLCV bars using pandas_ta.

    XAUUSD 1m Calibration Research Summary:
      EMA 20/50    — standard, confirmed for gold intraday
      RSI period   — changed 14 → 9 (faster for 1m scalping; 7–9 recommended by research)
      Bollinger    — 20 period, 2σ confirmed universally for gold scalping
      ATR period   — kept at 14 for SL stability (research: 14 best for consistent SL/TP)
      VWAP         — reset daily at 00:00 UTC (correct for 24h Forex spot)
      Kaufman ER   — window 10 (Perry Kaufman's original specification)
      Volume spike — threshold raised from 1.5× to 2.0× to match signal generator
    """

    @staticmethod
    def compute_indicators(bars) -> pd.DataFrame:
        """
        Takes a chronological list of Bars (or a DataFrame) and returns a DataFrame enriched
        with technical indicators required by the RegimeDetector and SignalGenerator.
        """
        if isinstance(bars, pd.DataFrame):
            if bars.empty:
                return pd.DataFrame()
            df = bars.copy()
        else:
            if not bars:
                return pd.DataFrame()

            data = []
            for b in bars:
                if isinstance(b, dict):
                    tc = int(b.get("tick_count", b.get("tick_volume", 0)) or 0)
                    tr = float(b["tick_rate"]) if b.get("tick_rate") is not None else float(tc)
                    rv = b.get("real_volume", None)
                    data.append({
                        "time":        b["time"],
                        "symbol":      b.get("symbol", ""),
                        "open":        b["open"],
                        "high":        b["high"],
                        "low":         b["low"],
                        "close":       b["close"],
                        "tick_count":  tc,
                        "tick_rate":   tr,
                        "real_volume": rv,
                    })
                else:
                    tc = int(getattr(b, "tick_count", None) if getattr(b, "tick_count", None) is not None else getattr(b, "tick_volume", 0) or 0)
                    tr_raw = getattr(b, "tick_rate", None)
                    tr = float(tr_raw) if tr_raw is not None else float(tc)
                    rv = getattr(b, "real_volume", None)
                    data.append({
                        "time":        b.time,
                        "symbol":      b.symbol,
                        "open":        b.open,
                        "high":        b.high,
                        "low":         b.low,
                        "close":       b.close,
                        "tick_count":  tc,
                        "tick_rate":   tr,
                        "real_volume": rv,
                    })
            df = pd.DataFrame(data)
        if "tick_count" not in df.columns:
            df["tick_count"] = df["tick_volume"] if "tick_volume" in df.columns else 0
        if "tick_rate" not in df.columns:
            df["tick_rate"] = df["tick_count"].astype(float)
        if "real_volume" not in df.columns:
            df["real_volume"] = None

        if "time" in df.columns:
            df.set_index("time", inplace=True)

        # Minimum bars required = longest lookback indicator (EMA 50)
        if len(df) < 50:
            return df

        # ── 1. EMAs ──────────────────────────────────────────────────────────
        # 20/50 EMA pairing confirmed optimal for XAUUSD intraday trend detection.
        # Research: 20 EMA as immediate trend; 50 EMA as medium-term bias filter.
        df["ema20"] = ta.ema(df["close"], length=20)
        df["ema50"] = ta.ema(df["close"], length=50)

        # ── 2. RSI ───────────────────────────────────────────────────────────
        # RSI period changed 14 → 9.
        # Research (arongroups.co, stockgro.club): On 1m XAUUSD, RSI-14 is too slow;
        # by the time it hits 70/30, the move is already 80% over.
        # RSI-9 gives a 5-bar lead on reversals while still filtering random noise.
        # Overbought/oversold levels adjusted in signal_generator (62/38, not 70/30).
        df["rsi"] = ta.rsi(df["close"], length=9)

        # ── 3. Bollinger Bands ───────────────────────────────────────────────
        # Period 20, 2σ confirmed universally by research for gold scalping.
        # Captures ~95% of 1m price action within bands. No change from original.
        bbands = ta.bbands(df["close"], length=20, std=2.0)
        if bbands is not None:
            df["bollinger_lower"] = bbands.iloc[:, 0]
            df["bollinger_mid"]   = bbands.iloc[:, 1]
            df["bollinger_upper"] = bbands.iloc[:, 2]
            # Bollinger Width: measures band squeeze (low = potential breakout)
            df["bb_width"] = (df["bollinger_upper"] - df["bollinger_lower"]) / df["bollinger_mid"]

        # ── 4. Tick-Weighted Session Average Price vs Genuine VWAP ───────────
        # cTrader spot gold provides quote updates (`tick_count`) with NO traded volume.
        # Rename quote-weighted price explicitly to `tick_weighted_price` and only
        # compute genuine `vwap` when non-null positive `real_volume` is provided.
        try:
            df["tick_weighted_price"] = ta.vwap(df["high"], df["low"], df["close"], df["tick_count"])
            df["tick_weighted_price_dist"] = (df["close"] - df["tick_weighted_price"]) / df["tick_weighted_price"]
        except Exception:
            cumcnt = df["tick_count"].cumsum()
            cumtpc = (df["close"] * df["tick_count"]).cumsum()
            df["tick_weighted_price"] = cumtpc / cumcnt.replace(0, np.nan)
            df["tick_weighted_price_dist"] = (
                (df["close"] - df["tick_weighted_price"]) / df["tick_weighted_price"].replace(0, np.nan)
            ).fillna(0.0)

        has_genuine_real_vol = bool(
            df["real_volume"].notna().any() and (pd.to_numeric(df["real_volume"], errors="coerce").fillna(0) > 0).any()
        )
        if has_genuine_real_vol:
            rv_pos = pd.to_numeric(df["real_volume"], errors="coerce").fillna(0.0)
            try:
                df["vwap"] = ta.vwap(df["high"], df["low"], df["close"], rv_pos)
            except Exception:
                cum_rv = rv_pos.cumsum()
                df["vwap"] = (df["close"] * rv_pos).cumsum() / cum_rv.replace(0, np.nan)
            df["vwap_dist"] = ((df["close"] - df["vwap"]) / df["vwap"].replace(0, np.nan)).fillna(0.0)
        else:
            df["vwap"] = np.nan
            df["vwap_dist"] = np.nan

        # ── 5. Quote-Arrival Rate Ratio (quotes/min vs 20-bar SMA; NOT traded volume) ─
        df["tick_rate_sma20"] = ta.sma(df["tick_rate"], length=20)
        df["tick_rate_ratio"] = df["tick_rate"] / df["tick_rate_sma20"].replace(0, np.nan)
        df["tick_rate_spike"] = (df["tick_rate_ratio"] > 2.0).astype(int)

        # ── 6. EMA-20 Slope ──────────────────────────────────────────────────
        # Measures 5-bar % change in EMA20. For XAUUSD 1m bars at $4500:
        #   Typical trending slope: 0.00005–0.0003 (up)  / -0.00005 to -0.0003 (down)
        #   Flat/sideways slope:   ±0.00000–0.00003
        # This is correctly calibrated for XAUUSD. No change from original.
        df["ema20_slope"] = df["ema20"].diff(5) / df["ema20"].shift(5)

        # ── 7. ATR ───────────────────────────────────────────────────────────
        # Period 14 kept per research recommendation.
        # Research: ATR-14 provides stable SL/TP baseline; shorter periods
        # (5-10) more responsive but cause SL whipsaw on XAUUSD news events.
        # Since GoldEdge uses ATR for SL/TP placement, stability > responsiveness.
        df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)

        # ── 8. Normalized Volatility ─────────────────────────────────────────
        # ATR expressed as a percentage of price, invariant to long-term price shifts.
        df["atr_pct"] = (df["atr"] / df["close"]) * 100

        # ── 9. Session Timing (Forex 24/5) ───────────────────────────────────
        # Assumes df.index is a UTC DatetimeIndex.
        if hasattr(df.index, 'hour'):
            hours = df.index.hour
            df["session_asia"]    = ((hours >= 0) & (hours < 7)).astype(int)
            df["session_london"]  = ((hours >= 7) & (hours < 12)).astype(int)
            df["session_overlap"] = ((hours >= 12) & (hours < 17)).astype(int)
            df["session_ny"]      = ((hours >= 17) & (hours < 21)).astype(int)
        else:
            # Fallback if index isn't a proper DatetimeIndex
            df["session_asia"] = 0
            df["session_london"] = 0
            df["session_overlap"] = 0
            df["session_ny"] = 0

        # ── 10. Smart Money Concepts (SMC) ───────────────────────────────────
        # Fair Value Gaps (FVG) - Structural imbalances
        df["fvg_bullish"] = (df["low"] > df["high"].shift(2)).astype(int)
        df["fvg_bearish"] = (df["high"] < df["low"].shift(2)).astype(int)

        # Asian Range Liquidity Sweeps
        try:
            asian_h = df["high"].where(df["session_asia"] == 1)
            asian_l = df["low"].where(df["session_asia"] == 1)
            
            # Find the max/min strictly during the Asian session per day
            daily_max = asian_h.groupby(df.index.date).max()
            daily_min = asian_l.groupby(df.index.date).min()
            
            # Broadcast those values to the entire day
            df["daily_asian_high"] = pd.Series(df.index.date, index=df.index).map(daily_max)
            df["daily_asian_low"]  = pd.Series(df.index.date, index=df.index).map(daily_min)
            
            # Distance from Asian boundaries (Breakout/Sweep proxy)
            df["dist_to_asian_high"] = (df["close"] - df["daily_asian_high"]) / df["close"] * 100
            df["dist_to_asian_low"]  = (df["close"] - df["daily_asian_low"]) / df["close"] * 100
            
            # Fill NaNs for early periods or days without Asian data
            df["dist_to_asian_high"] = df["dist_to_asian_high"].fillna(0.0)
            df["dist_to_asian_low"]  = df["dist_to_asian_low"].fillna(0.0)
        except Exception:
            df["daily_asian_high"] = np.nan
            df["daily_asian_low"] = np.nan
            df["dist_to_asian_high"] = 0.0
            df["dist_to_asian_low"] = 0.0

        return df
