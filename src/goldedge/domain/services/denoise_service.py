import numpy as np
import pandas as pd
from scipy.stats import entropy
from typing import Optional, Dict

class VolumeAdaptiveKalmanFilter:
    """
    HFT-Optimized 2D Kalman Filter (State: [Price, Velocity]).
    Uses a pre-allocated NumPy ring buffer for zero-allocation state tracking
    during live streaming of high-frequency ticks.
    """
    def __init__(self, buffer_size: int = 10000, q_p: float = 1e-4, q_v: float = 3e-5, r_base: float = 1e-2):
        self.buffer_size = buffer_size
        self.q_p = q_p
        self.q_v = q_v
        self.r_base = r_base
        
        # 2D State: [Price, Velocity]
        self.x = np.zeros((2, 1))
        self.P = np.eye(2)
        
        # CPU-Efficient Ring Buffer (Zero Allocation During Runtime)
        self.history = np.empty((buffer_size, 2))
        self.idx = 0
        
        # State transition matrix
        self.F = np.array([[1.0, 1.0], 
                           [0.0, 1.0]])
        # Measurement matrix
        self.H = np.array([[1.0, 0.0]])
        
        # Track previous tick for volatility calculation
        self.last_price: Optional[float] = None

    def update_tick(self, price: float, volume: float) -> tuple[float, float]:
        """
        Processes a single tick. 
        Returns (kalman_price, kalman_velocity).
        """
        # Initialize state on first tick
        if self.idx == 0:
            self.x[0, 0] = price
            self.x[1, 0] = 0.0
            self.last_price = price
            self.history[0] = self.x.flatten()
            self.idx += 1
            return price, 0.0

        # Calculate instantaneous volatility proxy
        price_diff = abs(price - self.last_price) if self.last_price else 0.0
        self.last_price = price
        
        # Simplistic adaptive R based on instantaneous volume/volatility 
        vol_factor = max(volume, 1.0)
        volatility_factor = max(price_diff, 0.0001)
        
        # Higher Volatility -> larger R -> trust model (ignore noise)
        # Higher Volume     -> smaller R -> trust price (follow informed liquidity)
        R_i = self.r_base * (volatility_factor / vol_factor)
        R_i = np.clip(R_i, self.r_base / 3.0, self.r_base * 3.0)
        R_mat = np.array([[R_i]])

        # Process noise covariance
        Q = np.array([[self.q_p, 0.0], 
                      [0.0, self.q_v]])

        # 1. Predict
        x_minus = self.F @ self.x
        P_minus = self.F @ self.P @ self.F.T + Q

        # 2. Update (Kalman Gain)
        S = self.H @ P_minus @ self.H.T + R_mat
        K = P_minus @ self.H.T @ np.linalg.inv(S)

        # Measurement residual
        y = np.array([[price]]) - (self.H @ x_minus)

        # 3. Final State
        self.x = x_minus + (K @ y)
        self.P = (np.eye(2) - K @ self.H) @ P_minus

        # Update Ring Buffer (No memory allocations/appends)
        self.history[self.idx % self.buffer_size] = self.x.flatten()
        self.idx += 1
        
        return float(self.x[0, 0]), float(self.x[1, 0])

    def get_velocity(self) -> float:
        return float(self.x[1, 0])
        
    def get_acceleration(self) -> float:
        if self.idx < 2:
            return 0.0
        # Acceleration is delta of velocity
        curr_vel = self.history[(self.idx - 1) % self.buffer_size][1]
        prev_vel = self.history[(self.idx - 2) % self.buffer_size][1]
        return float(curr_vel - prev_vel)


class DenoiseService:
    """
    Domain service for calculating quantitative features (Entropy, ER, etc.)
    that require historical lookbacks, complementing the streaming Kalman Filter.
    """
    @staticmethod
    def compute_rolling_entropy(returns: pd.Series, window: int = 30) -> float:
        """Calculates Shannon Entropy for the given returns window."""
        if len(returns) < window:
            return 3.5 # Default high entropy for insufficient data
            
        recent = returns.iloc[-window:]
        # Static absolute bins for stability
        bins = [-np.inf, -0.002, -0.001, -0.0005, -0.0002, 0.0, 0.0002, 0.0005, 0.001, 0.002, np.inf]
        
        counts, _ = np.histogram(recent, bins=bins)
        probs = counts / counts.sum()
        probs = probs[probs > 0]
        return float(entropy(probs, base=2))

    @staticmethod
    def compute_kaufman_er(series: pd.Series, window: int = 15) -> float:
        """Calculates Kaufman Efficiency Ratio for the given price series."""
        if len(series) <= window:
            return 0.0
            
        direction = abs(series.iloc[-1] - series.iloc[-(window+1)])
        volatility = series.diff().abs().iloc[-window:].sum()
        
        if volatility == 0:
            return 0.0
            
        return float(direction / volatility)
