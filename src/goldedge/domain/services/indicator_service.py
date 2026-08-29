import pandas as pd
import pandas_ta as ta
import numpy as np
from typing import List
from ..models.bar import Bar

class IndicatorService:
    """
    Computes technical indicators from raw OHLCV bars using pandas_ta.
    """
    
    @staticmethod
    def compute_indicators(bars: List[Bar]) -> pd.DataFrame:
        """
        Takes a chronological list of Bars and returns a DataFrame enriched
        with technical indicators required by the RegimeDetector and SignalGenerator.
        """
        if not bars:
            return pd.DataFrame()
            
        data = []
        for b in bars:
            data.append({
                "time": b.time,
                "symbol": b.symbol,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.tick_volume,  # Using tick_volume as proxy for volume
            })
            
        df = pd.DataFrame(data)
        df.set_index("time", inplace=True)
        
        # We need at least enough bars to calculate the longest EMA (50)
        if len(df) < 50:
            return df
            
        # 1. EMAs
        df["ema20"] = ta.ema(df["close"], length=20)
        df["ema50"] = ta.ema(df["close"], length=50)
        
        # 2. RSI
        df["rsi"] = ta.rsi(df["close"], length=14)
        
        # 3. Bollinger Bands (20, 2)
        bbands = ta.bbands(df["close"], length=20, std=2.0)
        if bbands is not None:
            # pandas_ta returns columns like BBL_20_2.0, BBM_20_2.0, BBU_20_2.0
            df["bollinger_lower"] = bbands.iloc[:, 0]
            df["bollinger_mid"] = bbands.iloc[:, 1]
            df["bollinger_upper"] = bbands.iloc[:, 2]
        
        # 4. VWAP (Intraday)
        # Note: True VWAP resets daily. pandas_ta vwap needs high, low, close, volume, and datetime index.
        try:
            df["vwap"] = ta.vwap(df["high"], df["low"], df["close"], df["volume"])
            df["vwap_dist"] = (df["close"] - df["vwap"]) / df["vwap"]
        except Exception:
            # Fallback if vwap calculation fails (e.g., missing index properties)
            df["vwap"] = df["close"]
            df["vwap_dist"] = 0.0
            
        # 5. Volume Spike / Ratio (relative to 20-period moving average)
        df["vol_sma20"] = ta.sma(df["volume"], length=20)
        df["volume_ratio"] = df["volume"] / df["vol_sma20"].replace(0, np.nan)
        df["volume_spike"] = (df["volume_ratio"] > 1.5).astype(int)
        
        # 6. EMA Slopes (momentum)
        # Difference between current EMA and EMA 5 bars ago, normalized
        df["ema20_slope"] = df["ema20"].diff(5) / df["ema20"].shift(5)
        
        # 7. ATR (Average True Range) - Used for dynamic Risk Management (SL/TP)
        df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
        
        return df
