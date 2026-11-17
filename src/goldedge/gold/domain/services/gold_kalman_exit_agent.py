import math
from collections import deque
from datetime import datetime
from typing import Optional, Tuple


class GoldKalmanExitAgent:
    """
    Sub-Second Kalman-Entropy Dynamic Trailing & Exit Agent for Spot Gold (XAUUSD).
    Calibrated specifically for Gold's $2400-$2700 dollar scale:
    - Ratchets trailing stop after $2.00 (20 pips) favorable excursion.
    - Dynamic trailing distance: max($1.20, xhat * 0.0008).
    - Early exit on velocity collapse when trade is down > $1.50 with >= 4s exhaustion.
    - High Shannon entropy chop exit (> 2.3) when profit >= $1.00 and momentum stalls.
    - 100% Pure Python domain service (Hexagonal Architecture compliant).
    """

    def __init__(
        self,
        pip_unit: float = 0.10,
        q_base: float = 2e-4,
        r_scale: float = 0.5
    ):
        self.pip_unit = pip_unit
        self.q_base = q_base
        self.q = q_base
        self.r_scale = r_scale
        self.r = 0.04

        self.xhat: Optional[float] = None
        self.p: float = 1.0

        self.r_window: deque[float] = deque(maxlen=15)
        self.history_fast: deque[float] = deque(maxlen=5)
        self.history_slow: deque[float] = deque(maxlen=15)
        self.raw_history: deque[float] = deque(maxlen=20)

        self.fast_vel_z: float = 0.0
        self.slow_vel_z: float = 0.0
        self.entropy: float = 0.0
        self.peak_price: Optional[float] = None
        self.weak_start_time: Optional[datetime] = None

    def reset(self, entry_price: float):
        """Resets agent state on trade entry."""
        self.xhat = None
        self.p = 1.0
        self.q = self.q_base
        self.r_window.clear()
        self.history_fast.clear()
        self.history_slow.clear()
        self.raw_history.clear()
        self.fast_vel_z = 0.0
        self.slow_vel_z = 0.0
        self.entropy = 0.0
        self.peak_price = entry_price
        self.weak_start_time = None

    def update(
        self,
        price: float,
        bid: float,
        ask: float,
        direction: str = "BUY"
    ) -> bool:
        """
        Processes an incoming tick and updates Kalman state, velocity z-scores,
        and rolling Shannon entropy using pure Python standard library.
        """
        if price <= 0 or (bid > ask and bid > 0 and ask > 0):
            return False

        # 1. Update dynamic R
        self.r_window.append(price)
        n = len(self.r_window)
        if n >= 5:
            mean = sum(self.r_window) / n
            rolling_var = sum((x - mean) ** 2 for x in self.r_window) / n
            rolling_std = math.sqrt(rolling_var)
        else:
            rolling_std = 0.05
            rolling_var = 0.0025

        spread_penalty = ((ask - bid) / self.pip_unit) * 1e-4 if ask > bid else 0.0
        raw_r = rolling_var * self.r_scale + spread_penalty
        self.r = max(0.01, min(1.0, raw_r))

        # 2. Dynamic Q (expand during breakouts, contract in consolidation)
        if rolling_var > 0:
            self.q = max(self.q_base * 0.1, min(self.q_base * 50.0, rolling_var * 0.1))

        # 3. Kalman Recursion
        if self.xhat is None:
            self.xhat = price
            self.p = 1.0
        else:
            pminus = self.p + self.q
            k = pminus / (pminus + self.r)
            self.xhat = self.xhat + k * (price - self.xhat)
            self.p = (1 - k) * pminus

        # 4. Volatility-Normalized Velocity (Z-Scores)
        self.history_fast.append(self.xhat)
        self.history_slow.append(self.xhat)
        if len(self.history_fast) == 5:
            fast_vel = self.history_fast[-1] - self.history_fast[0]
            self.fast_vel_z = fast_vel / rolling_std if rolling_std > 1e-4 else 0.0
        if len(self.history_slow) == 15:
            slow_vel = self.history_slow[-1] - self.history_slow[0]
            self.slow_vel_z = slow_vel / rolling_std if rolling_std > 1e-4 else 0.0

        # 5. Rolling Shannon Entropy of Returns (Market Noise vs Signal)
        self.raw_history.append(price)
        if len(self.raw_history) >= 10:
            raw_list = list(self.raw_history)
            returns = [(raw_list[i] - raw_list[i - 1]) / raw_list[i - 1] for i in range(1, len(raw_list))]
            # Static bins: [-inf, -0.0005, -0.0002, -0.0001, 0.0, 0.0001, 0.0002, 0.0005, inf]
            bin_cutoffs = [-0.0005, -0.0002, -0.0001, 0.0, 0.0001, 0.0002, 0.0005]
            counts = [0] * 8
            for r in returns:
                b_idx = 0
                while b_idx < 7 and r > bin_cutoffs[b_idx]:
                    b_idx += 1
                counts[b_idx] += 1
            total_counts = len(returns)
            if total_counts > 0:
                ent = 0.0
                for c in counts:
                    if c > 0:
                        prob = c / total_counts
                        ent -= prob * math.log2(prob)
                self.entropy = ent

        # Update favorable peak price
        if self.peak_price is None:
            self.peak_price = price
        else:
            if direction.upper() in ("BUY", "CALL", "LONG"):
                if price > self.peak_price:
                    self.peak_price = price
            else:
                if price < self.peak_price:
                    self.peak_price = price

        return True

    def decide(
        self,
        entry_price: float,
        current_price: float,
        current_sl: float,
        tick_time: datetime,
        direction: str = "BUY"
    ) -> Tuple[str, float]:
        """
        Determines position management action on every tick for Gold.
        Actions: HOLD | TIGHTEN | EARLY_EXIT
        """
        dir_upper = direction.upper()
        is_buy = dir_upper in ("BUY", "CALL", "LONG")
        price_gain = (current_price - entry_price) if is_buy else (entry_price - current_price)
        pnl_pips = price_gain / self.pip_unit

        # Dual Velocity Exhaustion
        is_exhausted = (
            (self.fast_vel_z < -0.15 and self.slow_vel_z < -0.15)
            if is_buy
            else (self.fast_vel_z > 0.15 and self.slow_vel_z > 0.15)
        )

        if is_exhausted:
            if self.weak_start_time is None:
                self.weak_start_time = tick_time
            weak_duration = (tick_time - self.weak_start_time).total_seconds()
        else:
            self.weak_start_time = None
            weak_duration = 0.0

        # High Entropy (> 2.3) indicates chop / indecision
        is_choppy = self.entropy > 2.3

        # Trailing Stop Management for Gold:
        # Activate after $2.00 profit (20 pips)
        new_sl = current_sl
        peak = self.peak_price if self.peak_price is not None else entry_price
        xhat_val = self.xhat if self.xhat is not None else current_price

        if price_gain >= 2.00:
            trail_distance = max(1.20, xhat_val * 0.0008)
            candidate_sl = round((peak - trail_distance) if is_buy else (peak + trail_distance), 2)
            if current_sl <= 0.0:
                new_sl = candidate_sl
            elif is_buy:
                new_sl = max(current_sl, candidate_sl)
            else:
                new_sl = min(current_sl, candidate_sl)

        # Early Exit on Velocity Collapse:
        # Price dropped > $1.50 (-15 pips) with persistent exhaustion >= 4s
        if price_gain < -1.50 and weak_duration >= 4.0:
            return "EARLY_EXIT", new_sl

        # Entropy Chop Exit: Take profit when momentum stalls in noise regime
        if is_choppy and weak_duration >= 6.0 and price_gain >= 1.00:
            return "EARLY_EXIT", new_sl

        if weak_duration >= 2.5:
            return "TIGHTEN", new_sl

        return "HOLD", new_sl
