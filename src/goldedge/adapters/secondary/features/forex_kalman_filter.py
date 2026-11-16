import numpy as np
import pandas as pd
from typing import Optional, Tuple


def apply_forex_adaptive_kalman(
    price_series: pd.Series,
    volume_series: Optional[pd.Series] = None,
    spread_series: Optional[pd.Series] = None,
    q_price: float = 1e-4,
    q_velocity: float = 1e-5,
    r_base: float = 1.5e-4
) -> pd.DataFrame:
    """
    MODULE 1: 2D Kinematic Kalman Filter (State: [Price, Velocity]) adapted for Forex.
    - Tracks price and true instantaneous velocity (pips/bar).
    - Measurement variance R expands during wide spreads & volatility spikes.
    - Measurement variance R contracts during high tick volume (informed liquidity).
    """
    n = len(price_series)
    if n == 0:
        return pd.DataFrame({"kalman_price": [], "kalman_velocity": []})

    # State vector: [price, velocity]
    xhat = np.zeros((2, n))
    p = np.zeros((2, 2, n))

    # Kinematic constant-velocity transition matrix
    A = np.array([[1.0, 1.0],
                  [0.0, 1.0]])
    H = np.array([[1.0, 0.0]])
    Q = np.array([[q_price, 0.0],
                  [0.0, q_velocity]])

    # Initial state
    xhat[0, 0] = price_series.iloc[0]
    xhat[1, 0] = 0.0
    p[:, :, 0] = np.eye(2) * 1.0
    prices = price_series.values

    # 1. Volatility Scaling (Shifted to eliminate look-ahead leakage)
    price_diff = pd.Series(prices).diff().abs()
    rolling_std = price_diff.shift(1).rolling(15, min_periods=5).mean().replace(0, np.nan)
    baseline_std = rolling_std.expanding(min_periods=5).median().replace(0, np.nan)
    rel_volatility = (rolling_std / baseline_std).fillna(1.0).clip(0.5, 3.0).values

    # 2. Tick Volume Scaling
    if volume_series is not None and not volume_series.empty and volume_series.sum() > 0:
        vol_s = pd.Series(volume_series.values)
        avg_vol = vol_s.shift(1).rolling(20, min_periods=5).median().replace(0, np.nan)
        rel_vol = (vol_s / avg_vol).fillna(1.0).clip(0.5, 3.0).values
    else:
        rel_vol = np.ones(n)

    # 3. Spread Penalty
    if spread_series is not None and not spread_series.empty:
        spread_s = pd.Series(spread_series.values)
        med_val = float(spread_s.median())
        med_val = np.nan if med_val == 0 else med_val
        spread_penalty = (spread_s / med_val).fillna(1.0).clip(1.0, 5.0).values
    else:
        spread_penalty = np.ones(n)

    for i in range(1, n):
        # Predict step
        xhat_minus = A @ xhat[:, i - 1]
        p_minus = A @ p[:, :, i - 1] @ A.T + Q

        # Adaptive Measurement Variance
        # High volatility or wide spread -> increase R (distrust raw price)
        # High volume -> decrease R (trust the market surge)
        R_i = r_base * (rel_volatility[i] * spread_penalty[i] / rel_vol[i])
        R_i = np.clip(R_i, r_base / 3.0, r_base * 5.0)
        R_mat = np.array([[R_i]])

        # Kalman Gain
        S = H @ p_minus @ H.T + R_mat
        K = p_minus @ H.T @ np.linalg.inv(S)

        # Innovation (Residual)
        y = np.array([prices[i]]) - (H @ xhat_minus)

        # Update step
        xhat[:, i] = xhat_minus + (K @ y).flatten()
        p[:, :, i] = (np.eye(2) - K @ H) @ p_minus

    return pd.DataFrame({
        "kalman_price": xhat[0, :],
        "kalman_velocity": xhat[1, :]
    }, index=price_series.index)


class StreamingForexKalmanFilter:
    """
    Streaming 2D Kinematic Kalman Filter for real-time tick-by-tick evaluation.
    Provides instant price, velocity, velocity Z-score, and acceleration.
    """

    def __init__(
        self,
        q_price: float = 1e-4,
        q_velocity: float = 1e-5,
        r_base: float = 1.5e-4,
        window_size: int = 20
    ):
        self.q_price = q_price
        self.q_velocity = q_velocity
        self.r_base = r_base
        self.window_size = window_size

        self.A = np.array([[1.0, 1.0], [0.0, 1.0]])
        self.H = np.array([[1.0, 0.0]])
        self.Q = np.array([[q_price, 0.0], [0.0, q_velocity]])

        self.x = np.zeros((2, 1))
        self.P = np.eye(2)
        self.initialized = False

        self.vel_history: list[float] = []
        self.last_price: Optional[float] = None
        self.last_velocity: float = 0.0

    @property
    def p(self) -> float:
        """Returns the primary state error covariance P[0, 0]."""
        return float(self.P[0, 0])

    def update(self, price: float, volume: float = 1.0, spread: float = 1.0) -> Tuple[float, float, float]:
        """
        Updates filter with a live price tick.
        Returns: (kalman_price, kalman_velocity, velocity_z_score)
        """
        if not self.initialized:
            self.x[0, 0] = price
            self.x[1, 0] = 0.0
            self.initialized = True
            self.last_price = price
            self.vel_history.append(0.0)
            return price, 0.0, 0.0

        # Predict
        x_minus = self.A @ self.x
        P_minus = self.A @ self.P @ self.A.T + self.Q

        # Adaptive R calculation
        price_diff = abs(price - (self.last_price or price))
        self.last_price = price
        vol_factor = max(volume, 1.0)
        spread_factor = max(spread, 0.1)

        R_i = self.r_base * (max(price_diff, 1e-5) * spread_factor / vol_factor)
        R_i = np.clip(R_i, self.r_base / 3.0, self.r_base * 5.0)
        R_mat = np.array([[R_i]])

        # Gain & Update
        S = self.H @ P_minus @ self.H.T + R_mat
        K = P_minus @ self.H.T @ np.linalg.inv(S)

        y = np.array([[price]]) - (self.H @ x_minus)
        self.x = x_minus + (K @ y)
        self.P = (np.eye(2) - K @ self.H) @ P_minus

        k_price = float(self.x[0, 0])
        k_vel = float(self.x[1, 0])

        self.vel_history.append(k_vel)
        if len(self.vel_history) > self.window_size:
            self.vel_history.pop(0)

        # Velocity Z-Score
        if len(self.vel_history) >= 5:
            std = float(np.std(self.vel_history))
            mean = float(np.mean(self.vel_history))
            z_score = (k_vel - mean) / std if std > 1e-6 else 0.0
        else:
            z_score = 0.0

        self.last_velocity = k_vel
        return k_price, k_vel, z_score

    def get_acceleration(self) -> float:
        """Returns instantaneous acceleration (change in velocity between last 2 ticks)."""
        if len(self.vel_history) < 2:
            return 0.0
        return float(self.vel_history[-1] - self.vel_history[-2])

    def get_uncertainty(self) -> float:
        """Returns the 1-sigma price state uncertainty sqrt(P[0,0])."""
        return float(np.sqrt(max(self.P[0, 0], 1e-8)))

