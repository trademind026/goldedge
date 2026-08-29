import logging
import pandas as pd
import numpy as np
import pandas_ta as ta
from typing import Optional
from datetime import datetime
from sqlalchemy import create_engine
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', '..')))
from src.goldedge.config.settings import Settings
from src.goldedge.domain.ports.outbound.feature_repo import IFeatureRepository

logger = logging.getLogger("pandas_feature_repo")

class PandasFeatureRepository(IFeatureRepository):
    def __init__(self):
        self.settings = Settings()
        self.engine = create_engine(self.settings.DB_URL)

    def _apply_volume_adaptive_kalman(self, price_series: pd.Series, volume_series: pd.Series, 
                                      q_p: float = 1e-4, q_v: float = 1e-5, r_base: float = 1.5e-2) -> pd.DataFrame:
        """
        True 2D Kalman Filter ported exactly from AI-trader.
        Uses Tick Volume to dynamically adjust the R (noise covariance).
        """
        n = len(price_series)
        if n == 0:
            return pd.DataFrame({"close_kalman": [], "kalman_vel": []})

        xhat = np.zeros((2, n))
        p = np.zeros((2, 2, n))

        A = np.array([[1.0, 1.0], [0.0, 1.0]])
        H = np.array([[1.0, 0.0]])
        Q = np.array([[q_p, 0.0], [0.0, q_v]])

        xhat[0, 0] = price_series.iloc[0]
        xhat[1, 0] = 0.0
        p[:, :, 0] = np.eye(2)

        prices = price_series.values
        price_s = pd.Series(prices)
        
        # Volatility processing
        price_diff = price_s.diff().abs()
        rolling_std = price_diff.shift(1).rolling(15, min_periods=5).mean().replace(0, np.nan)
        baseline_std = rolling_std.expanding(min_periods=5).median().replace(0, np.nan)
        rel_volatility = (rolling_std / baseline_std).fillna(1.0).clip(0.5, 3.0).values
        
        # Volume processing (Tick Volume for Forex)
        vols = volume_series.values
        vol_s = pd.Series(vols)
        avg_vol = vol_s.shift(1).rolling(20, min_periods=5).median().replace(0, np.nan)
        rel_vol = (vol_s / avg_vol).fillna(1.0).clip(0.5, 3.0).values

        for i in range(1, n):
            xhat_minus = A @ xhat[:, i-1]
            p_minus = A @ p[:, :, i-1] @ A.T + Q

            # Dual-Adaptive logic
            R_i = r_base * (rel_volatility[i] / rel_vol[i])
            R_i = np.clip(R_i, r_base / 3.0, r_base * 3.0) 
            R_mat = np.array([[R_i]])

            S = H @ p_minus @ H.T + R_mat
            K = p_minus @ H.T @ np.linalg.inv(S)

            y = np.array([prices[i]]) - (H @ xhat_minus)

            xhat[:, i] = xhat_minus + (K @ y).flatten()
            p[:, :, i] = (np.eye(2) - K @ H) @ p_minus

        return pd.DataFrame({
            "close_kalman": xhat[0, :],
            "kalman_vel": xhat[1, :]
        }, index=price_series.index)

    def generate_features(self, symbol: str, start: Optional[datetime] = None, end: Optional[datetime] = None) -> int:
        logger.info(f"Generating features for {symbol}...")
        
        # 1. Fetch 1m bars
        query = "SELECT time, open, high, low, close, tick_volume FROM market_bars_1m WHERE symbol = %(symbol)s"
        params = {"symbol": symbol}
        if start:
            query += " AND time >= %(start)s"
            params["start"] = start
        if end:
            query += " AND time <= %(end)s"
            params["end"] = end
        query += " ORDER BY time ASC"
        
        df = pd.read_sql(query, self.engine, params=params)
        
        if len(df) < 20: # Need some history for indicators
            logger.warning(f"Not enough bars to generate features for {symbol}.")
            return 0
            
        df['time'] = pd.to_datetime(df['time'], utc=True)
        df = df.set_index('time')

        # 2. Technical Indicators (pandas-ta)
        df["atr_14"] = ta.atr(df["high"], df["low"], df["close"], length=14)
        df["rsi_14"] = ta.rsi(df["close"], length=14)
        
        macd = ta.macd(df["close"])
        if macd is not None and not macd.empty:
            df["macd_line"] = macd.iloc[:, 0]
            df["macd_signal"] = macd.iloc[:, 2]
        else:
            df["macd_line"] = np.nan
            df["macd_signal"] = np.nan

        # 3. Apply AI-trader Kalman Filter
        kalman_df = self._apply_volume_adaptive_kalman(df["close"], df["tick_volume"])
        df["close_kalman"] = kalman_df["close_kalman"]
        df["kalman_vel"] = kalman_df["kalman_vel"]

        # 4. Session Flags (UTC based)
        hours = df.index.hour
        df["session_asia"] = ((hours >= 0) & (hours < 9)).astype(bool)
        df["session_london"] = ((hours >= 8) & (hours < 17)).astype(bool)
        df["session_ny"] = ((hours >= 13) & (hours < 22)).astype(bool)
        df["session_overlap"] = df["session_london"] & df["session_ny"]

        # Cleanup and prepare for SQL
        df = df.dropna()
        if df.empty:
            logger.warning("All rows dropped due to NaN (need more historical data).")
            return 0
            
        df["symbol"] = symbol
        df = df.reset_index()
        
        # Ensure we save all ML features, including the newly added Kalman columns
        cols = [
            "time", "symbol", "atr_14", "rsi_14", "macd_line", "macd_signal", 
            "session_asia", "session_london", "session_ny", "session_overlap",
            "close_kalman", "kalman_vel"
        ]
        
        features_df = df[cols]

        # 5. Save to Database
        try:
            features_df.to_sql("market_features", self.engine, if_exists="append", index=False)
            logger.info(f"Successfully saved {len(features_df)} feature rows for {symbol}!")
            return len(features_df)
        except Exception as e:
            logger.error(f"Failed to save features: {e}")
            return 0
