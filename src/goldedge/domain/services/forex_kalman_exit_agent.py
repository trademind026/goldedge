from collections import deque
from datetime import datetime
from typing import Optional, Tuple
import numpy as np


class ForexKalmanEntropyExitAgent:
    """
    MODULE 2: Real-Time Kalman-Entropy Exit & Trailing Agent.
    Tracks trade lifecycle, velocity exhaustion, and Shannon entropy
    to lock in floating pips and detect trend collapse before a standard SL is triggered.
    """

    def __init__(self, pip_unit: float = 0.0001, q_base: float = 1e-4, r_scale: float = 0.5):
        self.pip_unit = pip_unit
        self.q_base = q_base
        self.q = q_base
        self.r_scale = r_scale
        self.r = 1e-3

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
        self.exit_reason: str = ""
        self.bars_since_peak: int = 0
        self.peak_pips: float = 0.0

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
        self.exit_reason = ""
        self.bars_since_peak = 0
        self.peak_pips = 0.0

    def on_bar_closed(self):
        """Notified on each bar close to increment stagnation counter."""
        self.bars_since_peak += 1


    def update(self, price: float, bid: float, ask: float, direction: str = "BUY") -> bool:
        """
        Processes a live tick and updates Kalman state, velocity z-scores,
        and Shannon entropy.
        """
        if price <= 0 or (bid > ask and bid > 0 and ask > 0):
            return False

        # 1. Update dynamic R
        self.r_window.append(price)
        rolling_std = float(np.std(self.r_window)) if len(self.r_window) >= 5 else 1e-5
        rolling_var = float(np.var(self.r_window)) if len(self.r_window) >= 5 else 1e-6

        spread_penalty = ((ask - bid) / self.pip_unit) * 1e-6 if ask > bid else 0.0
        self.r = float(np.clip(rolling_var * self.r_scale + spread_penalty, 1e-5, 1e-1))

        # 2. Dynamic Q (expand during breakouts, contract in consolidation)
        if rolling_var > 0:
            self.q = float(np.clip(rolling_var * 0.1, self.q_base * 0.1, self.q_base * 50))

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
            self.fast_vel_z = fast_vel / rolling_std if rolling_std > 1e-6 else 0.0
        if len(self.history_slow) == 15:
            slow_vel = self.history_slow[-1] - self.history_slow[0]
            self.slow_vel_z = slow_vel / rolling_std if rolling_std > 1e-6 else 0.0

        # 5. Rolling Shannon Entropy of Returns (Market Noise vs Signal)
        self.raw_history.append(price)
        if len(self.raw_history) >= 10:
            arr = np.array(self.raw_history)
            returns = np.diff(arr) / arr[:-1]
            static_bins = [-np.inf, -0.0005, -0.0002, -0.0001, 0.0, 0.0001, 0.0002, 0.0005, np.inf]
            counts, _ = np.histogram(returns, bins=static_bins)
            if counts.sum() > 0:
                probs = counts / counts.sum()
                probs = probs[probs > 0]
                self.entropy = float(-np.sum(probs * np.log2(probs)))

        # Update favorable peak price and reset stagnation counter
        if self.peak_price is None:
            self.peak_price = price
            self.bars_since_peak = 0
        else:
            if direction.upper() in ("BUY", "CALL"):
                if price > self.peak_price:
                    self.peak_price = price
                    self.bars_since_peak = 0
            else:
                if price < self.peak_price:
                    self.peak_price = price
                    self.bars_since_peak = 0

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
        Determines position management action on every tick.
        Actions: HOLD | TIGHTEN | EARLY_EXIT
        """
        dir_upper = direction.upper()
        is_buy = dir_upper in ("BUY", "CALL")
        pnl_pips = ((current_price - entry_price) if is_buy else (entry_price - current_price)) / self.pip_unit

        # 1. Hard Kalman Velocity Reversal (Extreme Shock: v < -1.8 sigma in trade direction)
        is_hard_reversal = (self.fast_vel_z <= -1.8) if is_buy else (self.fast_vel_z >= 1.8)
        if is_hard_reversal:
            self.exit_reason = "KALMAN_VELOCITY_REVERSAL"
            return "EARLY_EXIT", current_sl

        # 2. Dual Velocity Exhaustion
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

        # High Entropy (> 2.3) indicates random noise / chop
        is_choppy = self.entropy > 2.3

        # Trailing Stop Management
        new_sl = current_sl
        peak = self.peak_price if self.peak_price is not None else entry_price
        xhat_val = self.xhat if self.xhat is not None else current_price

        # ── 6.5 The 4-Tier Profit Locking Dynamic Ratchet ─────────────
        peak_pips = ((peak - entry_price) if is_buy else (entry_price - peak)) / self.pip_unit
        self.peak_pips = max(self.peak_pips, peak_pips)

        if self.peak_pips >= 50.0:
            # Tier 1 (Peak >= +50.0 pips): Ratchet SL to lock in 80% of peak profit
            locked_dist = (peak - entry_price) * 0.80 if is_buy else (entry_price - peak) * 0.80
            candidate_sl = (entry_price + locked_dist) if is_buy else (entry_price - locked_dist)
            new_sl = max(new_sl, candidate_sl) if is_buy else min(new_sl, candidate_sl)
        elif self.peak_pips >= 35.0:
            # Tier 2 (Peak >= +35.0 pips): Ratchet SL to lock in 70% of peak profit
            locked_dist = (peak - entry_price) * 0.70 if is_buy else (entry_price - peak) * 0.70
            candidate_sl = (entry_price + locked_dist) if is_buy else (entry_price - locked_dist)
            new_sl = max(new_sl, candidate_sl) if is_buy else min(new_sl, candidate_sl)
        elif self.peak_pips >= 20.0:
            # Tier 3 (Peak >= +20.0 pips): Ratchet SL to lock in 55% of peak profit
            locked_dist = (peak - entry_price) * 0.55 if is_buy else (entry_price - peak) * 0.55
            candidate_sl = (entry_price + locked_dist) if is_buy else (entry_price - locked_dist)
            new_sl = max(new_sl, candidate_sl) if is_buy else min(new_sl, candidate_sl)
        elif self.peak_pips >= 10.0:
            # Tier 4 (Peak >= +10.0 pips): Ratchet SL to Breakeven + (2 * Spread Pips)
            spread_pips = 1.2
            be_offset = (2.0 * spread_pips) * self.pip_unit
            candidate_sl = (entry_price + be_offset) if is_buy else (entry_price - be_offset)
            new_sl = max(new_sl, candidate_sl) if is_buy else min(new_sl, candidate_sl)
        elif pnl_pips > 15:
            trail_distance = max(10 * self.pip_unit, xhat_val * 0.001)
            candidate_sl = (peak - trail_distance) if is_buy else (peak + trail_distance)
            new_sl = max(current_sl, candidate_sl) if is_buy else min(current_sl, candidate_sl)

        # ── 6.6 15-Bar Linear Stagnation Defense Tightener ──────────────
        if self.bars_since_peak >= 15:
            compression_pips = (self.bars_since_peak - 15) * 0.5
            compression_offset = compression_pips * self.pip_unit
            if is_buy:
                stagnation_sl = new_sl + compression_offset
                if stagnation_sl < current_price:
                    new_sl = max(new_sl, stagnation_sl)
            else:
                stagnation_sl = new_sl - compression_offset
                if stagnation_sl > current_price:
                    new_sl = min(new_sl, stagnation_sl)

        # Early Exit on Sustained Momentum Collapse
        if pnl_pips < -5 and weak_duration >= 5:
            self.exit_reason = "KALMAN_VELOCITY_REVERSAL"
            return "EARLY_EXIT", new_sl

        if is_choppy and weak_duration >= 8 and pnl_pips > 0:
            self.exit_reason = "KALMAN_VELOCITY_REVERSAL"
            return "EARLY_EXIT", new_sl  # Take profit before reversal in choppy regime

        # Scenario B: Velocity slows down and dies (|v| ≈ 0 or weak_duration >= 3) -> TIGHTEN STOP LOSS
        if weak_duration >= 3 or (pnl_pips > 8 and abs(self.fast_vel_z) <= 0.15):
            self.exit_reason = "KALMAN_TIGHTEN"
            if pnl_pips > 5:
                # Lock in +50% of peak move
                lock_dist = max(3 * self.pip_unit, (peak - entry_price) * 0.5 if is_buy else (entry_price - peak) * 0.5)
                tightened_sl = (peak - lock_dist) if is_buy else (peak + lock_dist)
                new_sl = max(new_sl, tightened_sl) if is_buy else min(new_sl, tightened_sl)
            return "TIGHTEN", new_sl

        sl_improved = (new_sl > current_sl) if is_buy else (new_sl < current_sl)
        if sl_improved:
            return "TIGHTEN", new_sl

        return "HOLD", new_sl
