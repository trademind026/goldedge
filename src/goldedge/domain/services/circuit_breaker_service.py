from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Dict, List, Optional, Tuple, Set
from collections import deque


@dataclass
class CircuitBreakerConfig:
    max_daily_drawdown_pct: float = 0.03       # 3% Equity Cap
    max_consecutive_losses: int = 2            # 2 SL hits triggers cooling-off
    consecutive_loss_cooldown_bars: int = 45   # 45 bars suspension
    ladder_cooldown_bars: int = 15             # 15 bars between identical setups
    rollover_start_utc: time = time(21, 0, 0)  # 21:00 UTC
    rollover_end_utc: time = time(22, 15, 0)   # 22:15 UTC
    max_spread_multiplier: float = 2.5         # 2.5x 24h median
    max_usd_pairs: int = 2                     # Max 2 concurrent USD pairs


@dataclass
class CircuitBreakerStatus:
    allowed: bool
    rejection_reasons: List[str]
    is_locked_out_for_day: bool = False
    cooldown_bars_remaining: int = 0


class ForexCircuitBreakerService:
    """
    STAGE 1: Global Circuit Breakers & Portfolio Sanity (Pre-Flight Filter).
    Evaluates 6 institutional pre-flight constraints before any signal generation
    or mathematical calculations take place. Pure Python domain logic.
    """

    def __init__(self, config: Optional[CircuitBreakerConfig] = None):
        self.config = config or CircuitBreakerConfig()
        self.session_locked: bool = False
        self.lockout_date: Optional[datetime.date] = None

        # Tracking state
        self.closed_trade_reasons: deque[str] = deque(maxlen=20)
        self.last_sl_bar_idx: Optional[int] = None
        self.last_setup_bars: Dict[Tuple[str, str, str], int] = {}  # (symbol, strategy, direction) -> bar_idx

    def reset_session(self, current_date: Optional[datetime.date] = None):
        """Resets the daily drawdown lockout at 00:00:00 UTC."""
        self.session_locked = False
        self.lockout_date = current_date

    def record_trade_close(self, exit_reason: str, bar_idx: int):
        """Records a closed trade's exit reason to update consecutive loss tracking."""
        self.closed_trade_reasons.append(exit_reason)
        # Check if last two were STOP_LOSS / SL
        if len(self.closed_trade_reasons) >= self.config.max_consecutive_losses:
            last_reasons = list(self.closed_trade_reasons)[-self.config.max_consecutive_losses:]
            if all(r in ("STOP_LOSS", "SL") for r in last_reasons):
                self.last_sl_bar_idx = bar_idx

    def record_entry(self, symbol: str, strategy_id: str, direction: str, bar_idx: int):
        """Logs entry bar index to enforce ladder re-entry spacing."""
        key = (symbol.upper(), strategy_id.lower(), direction.upper())
        self.last_setup_bars[key] = bar_idx

    def check_preflight(
        self,
        symbol: str,
        strategy_id: str,
        direction: str,
        current_bar_idx: int,
        current_utc_time: datetime,
        starting_session_equity: float,
        current_closed_equity: float,
        current_spread_pips: float,
        median_24h_spread_pips: float,
        active_open_symbols: List[str]
    ) -> CircuitBreakerStatus:
        """
        Validates all 6 institutional pre-flight constraints.
        Returns CircuitBreakerStatus (allowed=True if all pass).
        """
        rejections: List[str] = []

        # ── 0. Daily Reset Check (at 00:00 UTC) ─────────────────────────
        curr_date = current_utc_time.date()
        if self.lockout_date != curr_date:
            self.reset_session(curr_date)

        # ── 1.1 Daily Drawdown Hard Breaker (3% Equity Cap) ─────────────
        if self.session_locked:
            rejections.append("1.1 Daily Drawdown Lockout Active: Engine in read-only lock until 00:00:00 UTC")
            return CircuitBreakerStatus(allowed=False, rejection_reasons=rejections, is_locked_out_for_day=True)

        if starting_session_equity > 0:
            realized_daily_loss = starting_session_equity - current_closed_equity
            daily_loss_pct = realized_daily_loss / starting_session_equity
            if daily_loss_pct >= self.config.max_daily_drawdown_pct:
                self.session_locked = True
                self.lockout_date = curr_date
                rejections.append(
                    f"1.1 Daily Drawdown Hard Breaker Tripped: Loss {daily_loss_pct*100:.2f}% >= "
                    f"{self.config.max_daily_drawdown_pct*100:.1f}%. Engine locked until 00:00:00 UTC"
                )
                return CircuitBreakerStatus(allowed=False, rejection_reasons=rejections, is_locked_out_for_day=True)

        # ── 1.2 Consecutive Loss Mandatory Cooling-off (2 SL = 45 Bars) ──
        cooldown_remaining = 0
        if self.last_sl_bar_idx is not None:
            bars_since_consec_sl = current_bar_idx - self.last_sl_bar_idx
            if bars_since_consec_sl < self.config.consecutive_loss_cooldown_bars:
                cooldown_remaining = self.config.consecutive_loss_cooldown_bars - bars_since_consec_sl
                rejections.append(
                    f"1.2 Consecutive Loss Cooling-off Active: 2 consecutive SL hits. "
                    f"{cooldown_remaining} bars remaining of {self.config.consecutive_loss_cooldown_bars}-bar freeze"
                )

        # ── 1.3 Ladder Re-entry Spacing Guard (15 Bars Between Same Setups)
        key = (symbol.upper(), strategy_id.lower(), direction.upper())
        if key in self.last_setup_bars:
            bars_since_last_setup = current_bar_idx - self.last_setup_bars[key]
            if bars_since_last_setup < self.config.ladder_cooldown_bars:
                remaining = self.config.ladder_cooldown_bars - bars_since_last_setup
                rejections.append(
                    f"1.3 Ladder Re-entry Spacing Guard: {bars_since_last_setup} bars since identical setup "
                    f"({key[0]} {key[1]} {key[2]}). Must wait {remaining} more bars (min {self.config.ladder_cooldown_bars})"
                )

        # ── 1.4 Forex Rollover Blackout Window (21:00–22:15 UTC) ────────
        t = current_utc_time.time()
        if self.config.rollover_start_utc <= t <= self.config.rollover_end_utc:
            rejections.append(
                f"1.4 Rollover Blackout Window: Current time {t.strftime('%H:%M:%S')} UTC is inside "
                f"liquidity roll window [21:00:00, 22:15:00 UTC]. Quote spreads wide."
            )

        # ── 1.5 Dynamic Floating Spread Spike Guard (> 2.5x Rolling Median)
        if median_24h_spread_pips > 0:
            max_allowed_spread = median_24h_spread_pips * self.config.max_spread_multiplier
            if current_spread_pips > max_allowed_spread:
                rejections.append(
                    f"1.5 Floating Spread Spike Guard: Spread {current_spread_pips:.2f} pips > "
                    f"{self.config.max_spread_multiplier:.1f}x 24h median ({median_24h_spread_pips:.2f} pips; max {max_allowed_spread:.2f} pips)"
                )

        # ── 1.6 Currency Basket Concentration Cap (Max 2 USD Pairs) ─────
        if "USD" in symbol.upper():
            active_usd_count = sum(1 for s in active_open_symbols if "USD" in s.upper())
            if active_usd_count >= self.config.max_usd_pairs and symbol.upper() not in [s.upper() for s in active_open_symbols]:
                rejections.append(
                    f"1.6 Currency Basket Concentration Cap: {active_usd_count} active USD pairs open "
                    f"({', '.join(active_open_symbols)}). Cap is {self.config.max_usd_pairs} pairs."
                )

        return CircuitBreakerStatus(
            allowed=(len(rejections) == 0),
            rejection_reasons=rejections,
            is_locked_out_for_day=self.session_locked,
            cooldown_bars_remaining=cooldown_remaining
        )
