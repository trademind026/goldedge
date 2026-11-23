"""
High-Fidelity Tick-Replay Backtest Engine — Minute-Grouped Architecture.

Architecture (mirrors AI-Trader's approach):
  LAYER 1: Pre-group 1.9M ticks into minute buckets  (one Pandas operation)
  LAYER 2: Outer loop over ~10,000 minute bars       (signal pipeline — same as live)
  LAYER 3: NumPy vectorized tick scan per minute      (SL/TP exit — tick-accurate)

ZERO CODE DUPLICATION:
  - TradeScorer, RegimeDetector, SignalGeneratorService — same as live
  - PositionSizingService — same as live
  - VirtualBrokerAdapter  — swapped via DI (vs CTraderBrokerAdapter in live)

Speed: ~100-300× faster than tick-by-tick Python loop.
Accuracy: Identical SL/TP precision (NumPy vectorized scan over ticks per minute).
"""
import logging
import time
import numpy as np
import pandas as pd
from collections import deque
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Optional

from ...domain.ports.outbound.expected_pnl_model import IExpectedPnlModel
from ...domain.ports.outbound.tick_repo import ITickRepository
from ...domain.services.denoise_service import VolumeAdaptiveKalmanFilter, DenoiseService
from ...domain.services.indicator_service import IndicatorService
from ...domain.services.mtf_trend import RollingBarAggregator, compute_htf_ema_trend, passes_regime_filters
from ...domain.services.regime_detector import RegimeDetectorService
from ...domain.services.signal_generator import SignalGeneratorService
from ...domain.services.trade_scorer import TradeScorer, VALID_SCORER_MODES
from ...domain.services.sizing_service import PositionSizingService
from ...domain.services.hwm_kill_switch_service import HwmKillSwitchService
from ...domain.models.hwm_risk import EmergencyExitRecord
from ...domain.services.broker_cost_service import (
    BrokerCostService,
    FUSION_XAUUSD_SWAP_SPEC,
    SwapSchedule,
    ZERO_COMMISSION_SCHEDULE,
    resolve_commission_schedule,
)
from ...domain.models.trade import Position, TradeStatus, ExitReason
from ...domain.models.signal import SignalDirection
from ...config.risk_profiles import get_profile, RiskProfile
from ...adapters.secondary.virtual_broker import VirtualBrokerAdapter

logger = logging.getLogger("backtest_engine")

# XAUUSD contract spec (cTrader standard)
_CONTRACT_SIZE = 100.0   # 1 lot = 100 oz


def _safe_float(val, default: float = 0.0) -> float:
    """Return val as float, or default if None/NaN."""
    try:
        f = float(val)
        return default if (f != f) else f   # NaN check
    except (TypeError, ValueError):
        return default


class _MtmDrawdownTracker:
    """Tracks true tick-by-tick mark-to-market equity and HWM drawdown."""

    def __init__(self, initial_capital: float, start_time: datetime):
        self.initial_capital = float(initial_capital)
        self.hwm_equity = float(initial_capital)
        self.hwm_time = start_time
        self.max_dd = 0.0
        self.max_dd_pct = 0.0
        self.max_dd_initial_pct = 0.0
        self.peak_equity = float(initial_capital)
        self.peak_time = start_time
        self.trough_equity = float(initial_capital)
        self.trough_time = start_time

        self.raw_hwm_equity = float(initial_capital)
        self.raw_max_dd = 0.0
        self.sl_slippage_events = 0
        self.total_sl_gap_usd = 0.0
        self.worst_sl_gap_usd = 0.0

    def update(
        self,
        mtm_equity: float,
        raw_equity: float,
        ts: datetime,
        sl_gap_usd: float = 0.0,
    ) -> None:
        if sl_gap_usd < -1e-6:
            self.sl_slippage_events += 1
            self.total_sl_gap_usd += float(sl_gap_usd)
            if sl_gap_usd < self.worst_sl_gap_usd:
                self.worst_sl_gap_usd = float(sl_gap_usd)

        if mtm_equity > self.hwm_equity:
            self.hwm_equity = float(mtm_equity)
            self.hwm_time = ts

        dd = float(mtm_equity) - self.hwm_equity
        if dd < self.max_dd:
            self.max_dd = dd
            self.max_dd_initial_pct = (
                abs(dd) / self.initial_capital * 100.0 if self.initial_capital > 0 else 0.0
            )
            self.peak_equity = self.hwm_equity
            self.peak_time = self.hwm_time
            self.trough_equity = float(mtm_equity)
            self.trough_time = ts

        if self.hwm_equity > 0:
            hwm_dd_pct = max(0.0, -dd / self.hwm_equity * 100.0)
            if hwm_dd_pct > self.max_dd_pct:
                self.max_dd_pct = hwm_dd_pct

        if raw_equity > self.raw_hwm_equity:
            self.raw_hwm_equity = float(raw_equity)
        raw_dd = float(raw_equity) - self.raw_hwm_equity
        if raw_dd < self.raw_max_dd:
            self.raw_max_dd = raw_dd


class _OpenPosition:
    """
    Lightweight in-backtest position tracker.
    Handles trailing SL, breakeven, timeout entirely in Python without
    calling the full ManagePositionUseCase on every tick.
    Mirrors the ATR trailing logic from the live ManagePositionUseCase.
    """
    __slots__ = (
        "ticket", "symbol", "direction", "strategy",
        "entry_price", "sl", "initial_sl", "theoretical_stop_price",
        "tp", "atr", "volume", "entry_time", "entry_bar_idx",
        "trailing_active", "peak_price", "peak_bar_idx",
        "bars_held", "final_score", "regime", "exit_reason_flag", "journey",
        "planned_risk_usd", "planned_risk_pct", "commission_usd",
        "commission_entry_usd", "commission_exit_usd", "initial_volume",
        "slippage_usd", "swap_usd", "swap_pnl_usd", "last_swap_date",
        "last_rollover_check_ts", "_entry_comm_recorded", "_exit_comm_lots_charged",
        "last_sl_gap_usd", "be_trigger_atr",
    )

    def __init__(
        self, ticket: int, symbol: str, direction: str, strategy: str,
        entry_price: float, sl: float, tp: float, atr: float, volume: float,
        entry_time: datetime, entry_bar_idx: int,
        final_score: float, regime: str,
        planned_risk_usd: float = 0.0, planned_risk_pct: float = 0.0,
        commission_usd: float = 0.0, commission_entry_usd: float = 0.0,
        slippage_usd: float = 0.0, rollover_utc_hour: int = 21,
        be_trigger_atr: float = 1.0,
    ):
        self.ticket = ticket
        self.symbol = symbol
        self.direction = direction          # "BUY" or "SELL"
        self.strategy = strategy
        self.entry_price = entry_price
        self.sl = sl
        self.initial_sl = sl
        self.theoretical_stop_price = sl
        self.tp = tp
        self.atr = atr
        self.initial_volume = float(volume)
        self.volume = float(volume)
        self.entry_time = entry_time
        self.entry_bar_idx = entry_bar_idx
        self.trailing_active = False
        self.peak_price = entry_price
        self.peak_bar_idx = entry_bar_idx
        self.bars_held = 0
        self.final_score = final_score
        self.regime = regime
        self.exit_reason_flag = ""
        self.journey = []
        self.planned_risk_usd = float(planned_risk_usd)
        self.planned_risk_pct = float(planned_risk_pct)
        self.commission_entry_usd = float(commission_entry_usd)
        self.commission_exit_usd = 0.0
        self.commission_usd = float(commission_usd) if commission_usd else float(commission_entry_usd)
        self._entry_comm_recorded = self.commission_entry_usd > 0.0 or float(commission_usd) > 0.0
        self._exit_comm_lots_charged = 0.0
        self.slippage_usd = float(slippage_usd)
        self.swap_usd = 0.0
        self.swap_pnl_usd = 0.0
        self.last_rollover_check_ts = entry_time
        if hasattr(entry_time, "hour") and hasattr(entry_time, "date") and entry_time.hour >= rollover_utc_hour:
            self.last_swap_date = entry_time.date()
        else:
            self.last_swap_date = None
        self.last_sl_gap_usd = 0.0
        self.be_trigger_atr = float(be_trigger_atr)

    def record_entry_commission(self, cost_service: BrokerCostService | None) -> float:
        """Charge entry commission strictly once at entry fill price."""
        if self._entry_comm_recorded or cost_service is None:
            return 0.0
        fee = cost_service.calculate_leg_commission(self.initial_volume, self.entry_price)
        self.commission_entry_usd = fee
        self.commission_usd = round(self.commission_entry_usd + self.commission_exit_usd, 6)
        self._entry_comm_recorded = True
        return fee

    def record_exit_leg_commission(self, exit_lots: float, exit_price: float, cost_service: BrokerCostService | None) -> float:
        """Charge closing-leg commission proportionally on remaining unclosed volume (idempotent guard)."""
        if cost_service is None or exit_lots <= 0.0:
            return 0.0
        unrecorded_cap = max(0.0, self.initial_volume - self._exit_comm_lots_charged)
        chargeable = min(float(exit_lots), unrecorded_cap)
        if chargeable <= 1e-12:
            return 0.0
        fee = cost_service.calculate_leg_commission(chargeable, exit_price)
        self._exit_comm_lots_charged += chargeable
        self.commission_exit_usd = round(self.commission_exit_usd + fee, 6)
        self.commission_usd = round(self.commission_entry_usd + self.commission_exit_usd, 6)
        return fee

    def close_partial(self, close_lots: float, exit_price: float, cost_service: BrokerCostService | None) -> float:
        """Execute a partial close without duplicate entry or exit commission deductions."""
        close_lots = min(float(close_lots), self.volume)
        if close_lots <= 0.0:
            return 0.0
        ratio = close_lots / max(self.volume, 1e-12)
        gross = ((exit_price - self.entry_price) if self.direction == "BUY" else (self.entry_price - exit_price)) * close_lots * _CONTRACT_SIZE
        part_entry_comm = round(self.commission_entry_usd * (close_lots / max(self.initial_volume, 1e-12)), 6)
        part_exit_comm = self.record_exit_leg_commission(close_lots, exit_price, cost_service)
        part_swap_pnl = round(self.swap_pnl_usd * ratio, 6)
        self.swap_pnl_usd -= part_swap_pnl
        self.swap_usd = round(-self.swap_pnl_usd, 6)
        self.volume = round(self.volume - close_lots, 8)
        return gross - part_entry_comm - part_exit_comm + part_swap_pnl

    def apply_rollovers_until(self, curr_ts: datetime, cost_service: BrokerCostService | None) -> float:
        """Apply broker-timezone rollover swap events crossed strictly inside (last_rollover_check_ts, curr_ts]."""
        if cost_service is None or curr_ts is None or not isinstance(curr_ts, datetime) or not isinstance(self.last_rollover_check_ts, datetime):
            return 0.0
        events = cost_service.rollover_events_between(self.last_rollover_check_ts, curr_ts)
        added_pnl = 0.0
        for _r_utc, ny_date, mult in events:
            added_pnl += cost_service.calculate_swap_pnl_usd(self.direction, self.volume, ny_date, mult)
        if added_pnl != 0.0:
            self.swap_pnl_usd = round(self.swap_pnl_usd + added_pnl, 6)
            self.swap_usd = round(-self.swap_pnl_usd, 6)
        self.last_rollover_check_ts = curr_ts
        return added_pnl

    # ── Trailing SL ratchet (XAUUSD-calibrated ATR mechanics) ────

    def _ratchet_trailing(self, candidate_price: float, bar_idx: int, effective_max_hold: int) -> None:
        """Update peak and ratchet trailing SL upward using ATR mechanics.
        
        XAUUSD-specific calibration:
          - Breakeven at +1.5 ATR (not 1.0) to avoid noise-triggered BE on $1.5 ATR bars
          - Trail at 1.5 ATR behind peak
          - Time-Based Tightening: as bars_held approaches max_hold, squeeze SL to current price
        """
        is_bull = self.direction == "BUY"

        new_peak = (
            candidate_price > self.peak_price if is_bull
            else candidate_price < self.peak_price
        )
        if new_peak:
            self.peak_price = candidate_price
            self.peak_bar_idx = bar_idx

        gain_abs = (
            (self.peak_price - self.entry_price) if is_bull
            else (self.entry_price - self.peak_price)
        )

        pip_unit = 0.10 if "XAU" in self.symbol else (0.01 if "JPY" in self.symbol else 0.0001)

        # ── ATR-Scaled Breakeven & Trailing SL (gives XAUUSD room to reach TP) ──
        if gain_abs >= self.atr * 2.5:
            self.trailing_active = True
            trail_sl = (self.peak_price - (self.atr * 0.75)) if is_bull else (self.peak_price + (self.atr * 0.75))
            if (is_bull and trail_sl > self.sl) or (not is_bull and trail_sl < self.sl):
                self.sl = trail_sl
                self.exit_reason_flag = "TRAILING_SL"
        elif gain_abs >= self.atr * 1.6:
            self.trailing_active = True
            trail_sl = (self.peak_price - (self.atr * 1.10)) if is_bull else (self.peak_price + (self.atr * 1.10))
            if (is_bull and trail_sl > self.sl) or (not is_bull and trail_sl < self.sl):
                self.sl = trail_sl
                self.exit_reason_flag = "TRAILING_SL"
        elif gain_abs >= self.atr * self.be_trigger_atr:
            be_offset = 2.0 * pip_unit  # Cover spread + 2 pips
            candidate_sl = (self.entry_price + be_offset) if is_bull else (self.entry_price - be_offset)
            if (is_bull and candidate_sl > self.sl) or (not is_bull and candidate_sl < self.sl):
                self.sl = candidate_sl
                self.exit_reason_flag = "BREAKEVEN"

        # 3. Session-Aware Clock Ratchet
        if effective_max_hold > 0:
            hold_pct = self.bars_held / effective_max_hold
            if hold_pct >= 0.75:
                progress = min((hold_pct - 0.75) / 0.25, 1.0)
                if is_bull:
                    time_sl = self.sl + progress * (candidate_price - self.sl)
                    if time_sl > self.sl:
                        self.sl = time_sl
                        self.exit_reason_flag = "TIME_SQUEEZE"
                else:
                    time_sl = self.sl - progress * (self.sl - candidate_price)
                    if time_sl < self.sl:
                        self.sl = time_sl
                        self.exit_reason_flag = "TIME_SQUEEZE"


    # ── Tick-level exit check (called once per minute) ────────────────────────

    def check_exit_ticks(
        self,
        bids: np.ndarray,
        asks: np.ndarray,
        vols: np.ndarray,
        bar_idx: int,
        max_hold_bars: int,
        kalman,
        entry_utc_hour: int = 12,
        use_kalman_exit: bool = True,
        closed_equity: float = 0.0,
        mtm_tracker: _MtmDrawdownTracker | None = None,
        minute_ts: datetime | None = None,
        early_fail_mins: int | None = None,
        early_fail_min_mfe_atr: float | None = None,
        start_tick_idx: int = 0,
        evaluate_timeout: bool = True,
        slippage_pips: float = 0.0,
        tick_times: Any = None,
        cost_service: BrokerCostService | None = None,
        hwm_kill_switch: HwmKillSwitchService | None = None,
    ) -> tuple[bool, str, float, int]:
        """
        Scan post-entry ticks in this minute for SL/TP/trailing exits.
        Evaluates strictly ticks >= start_tick_idx chronologically, skips corrupted
        quotes (bid >= ask or <= 0), and fills stops at actual market bid/ask
        (preserving theoretical stop price & gap slippage separately).
        """
        if start_tick_idx >= len(bids):
            return False, "", 0.0, len(bids) - 1

        is_bull = self.direction == "BUY"
        pip_unit = 0.10 if "XAU" in self.symbol else (0.01 if "JPY" in self.symbol else 0.0001)
        slip_offset = float(slippage_pips) * pip_unit

        # Per-tick ATR unit: spread the 1m ATR across ~60 ticks in a minute
        atr_per_tick = max(self.atr / 60.0, 0.01)

        # Session-aware timeout: tighter during London/NY overlap
        if 7 <= entry_utc_hour < 20:
            effective_max_hold = min(max_hold_bars, 60)   # London + NY: 1h max
        else:
            effective_max_hold = min(max_hold_bars, 90)   # Asia/off-hours: 1.5h max

        last_valid_q: Optional[float] = None
        last_valid_idx: Optional[int] = None
        best_valid_q: Optional[float] = None

        for i in range(start_tick_idx, len(bids)):
            b = float(bids[i])
            a = float(asks[i])
            v = float(vols[i]) if vols is not None else np.nan
            t_ts = tick_times[i] if tick_times is not None else minute_ts

            # B4 guard MUST run BEFORE updating Kalman so corrupted/inverted quotes never poison Kalman state
            if b <= 0.0 or a <= 0.0 or b >= a:
                continue

            # 1. Advance Kalman causally on each valid tick (None when real_volume is NaN/unavailable)
            k_price, k_vel = kalman.update_tick(b, None if np.isnan(v) else v)

            if cost_service is not None and t_ts is not None:
                self.apply_rollovers_until(t_ts, cost_service)

            exec_q = b if is_bull else a
            last_valid_q = exec_q
            last_valid_idx = i
            if best_valid_q is None or (is_bull and exec_q > best_valid_q) or (not is_bull and exec_q < best_valid_q):
                best_valid_q = exec_q

            vel_norm = k_vel / atr_per_tick
            raw_unr = self.compute_pnl(exec_q, cost_service=cost_service)

            # B3 Realistic Stop-Loss Execution BEFORE soft Kalman exits: BUY fills at BID <= sl; SELL fills at ASK >= sl
            if is_bull and b <= self.sl:
                flag = getattr(self, "exit_reason_flag", "")
                reason = flag if flag else ("TRAILING_SL" if self.trailing_active else "SL")
                theoretical_sl = float(self.sl)
                fill_px = b - slip_offset
                if slip_offset > 0.0:
                    self.slippage_usd += slip_offset * self.volume * _CONTRACT_SIZE
                fill_unr = self.compute_pnl(fill_px, cost_service=cost_service)
                theo_unr = self.compute_pnl(theoretical_sl, cost_service=cost_service)
                gap_usd = fill_unr - theo_unr
                self.theoretical_stop_price = theoretical_sl
                self.last_sl_gap_usd = gap_usd
                if mtm_tracker is not None and t_ts is not None:
                    mtm_tracker.update(closed_equity + fill_unr, closed_equity + raw_unr, t_ts, gap_usd)
                return True, reason, fill_px, i

            if not is_bull and a >= self.sl:
                flag = getattr(self, "exit_reason_flag", "")
                reason = flag if flag else ("TRAILING_SL" if self.trailing_active else "SL")
                theoretical_sl = float(self.sl)
                fill_px = a + slip_offset
                if slip_offset > 0.0:
                    self.slippage_usd += slip_offset * self.volume * _CONTRACT_SIZE
                fill_unr = self.compute_pnl(fill_px, cost_service=cost_service)
                theo_unr = self.compute_pnl(theoretical_sl, cost_service=cost_service)
                gap_usd = fill_unr - theo_unr
                self.theoretical_stop_price = theoretical_sl
                self.last_sl_gap_usd = gap_usd
                if mtm_tracker is not None and t_ts is not None:
                    mtm_tracker.update(closed_equity + fill_unr, closed_equity + raw_unr, t_ts, gap_usd)
                return True, reason, fill_px, i

            # 6. TP limit check AFTER hard SL check & BEFORE soft Kalman exits (BUY closes at Bid, SELL closes at Ask)
            if is_bull and b >= self.tp:
                fill_px = float(self.tp)
                fill_unr = self.compute_pnl(fill_px, cost_service=cost_service)
                if mtm_tracker is not None and t_ts is not None:
                    mtm_tracker.update(closed_equity + fill_unr, closed_equity + raw_unr, t_ts)
                return True, "TARGET", fill_px, i
            if not is_bull and a <= self.tp:
                fill_px = float(self.tp)
                fill_unr = self.compute_pnl(fill_px, cost_service=cost_service)
                if mtm_tracker is not None and t_ts is not None:
                    mtm_tracker.update(closed_equity + fill_unr, closed_equity + raw_unr, t_ts)
                return True, "TARGET", fill_px, i

            # 6a. Optional Kalman velocity reversal / profit tighten (only if hard SL/TP did not trigger)
            if use_kalman_exit:
                if is_bull and vel_norm <= -2.5:
                    ex_px = exec_q - slip_offset
                    if slip_offset > 0.0:
                        self.slippage_usd += slip_offset * self.volume * _CONTRACT_SIZE
                    fill_unr = self.compute_pnl(ex_px, cost_service=cost_service)
                    if mtm_tracker is not None and t_ts is not None:
                        mtm_tracker.update(closed_equity + fill_unr, closed_equity + raw_unr, t_ts)
                    return True, "KALMAN_VELOCITY_REVERSAL", ex_px, i
                elif not is_bull and vel_norm >= 2.5:
                    ex_px = exec_q + slip_offset
                    if slip_offset > 0.0:
                        self.slippage_usd += slip_offset * self.volume * _CONTRACT_SIZE
                    fill_unr = self.compute_pnl(ex_px, cost_service=cost_service)
                    if mtm_tracker is not None and t_ts is not None:
                        mtm_tracker.update(closed_equity + fill_unr, closed_equity + raw_unr, t_ts)
                    return True, "KALMAN_VELOCITY_REVERSAL", ex_px, i

                gain = (b - self.entry_price) if is_bull else (self.entry_price - a)
                if gain >= self.atr * 1.5:
                    if (is_bull and vel_norm < -0.5) or (not is_bull and vel_norm > 0.5):
                        tight_sl = (b - (self.atr * 0.75)) if is_bull else (a + (self.atr * 0.75))
                        if (is_bull and tight_sl > self.sl) or (not is_bull and tight_sl < self.sl):
                            self.sl = tight_sl
                            self.trailing_active = True
                            self.exit_reason_flag = "KALMAN_TIGHTEN"

            if mtm_tracker is not None and t_ts is not None:
                mtm_tracker.update(closed_equity + raw_unr, closed_equity + raw_unr, t_ts)

            # 6b. Enforce persistent 8% HWM Hard Risk Kill-Switch on intrabar MTM equity
            if hwm_kill_switch is not None:
                eval_res = hwm_kill_switch.evaluate_equity(
                    current_equity=closed_equity + raw_unr,
                    timestamp=t_ts,
                    open_positions_count=1,
                    symbol=self.symbol,
                )
                if eval_res.halted:
                    fill_px = (exec_q - slip_offset) if is_bull else (exec_q + slip_offset)
                    if slip_offset > 0.0:
                        self.slippage_usd += slip_offset * self.volume * _CONTRACT_SIZE
                    fill_unr = self.compute_pnl(fill_px, cost_service=cost_service)
                    if mtm_tracker is not None and t_ts is not None:
                        mtm_tracker.update(closed_equity + fill_unr, closed_equity + raw_unr, t_ts)
                    req_ts = t_ts if isinstance(t_ts, datetime) else datetime.utcnow()
                    hwm_kill_switch.record_emergency_exit_outcomes(
                        [
                            EmergencyExitRecord(
                                ticket=int(self.ticket),
                                symbol=self.symbol,
                                direction=self.direction,
                                requested_volume=float(self.volume),
                                filled_volume=float(self.volume),
                                remaining_volume=0.0,
                                requested_at=req_ts,
                                requested_price=float(exec_q),
                                actual_execution_price=float(fill_px),
                                exit_reason="HWM_KILL_SWITCH_8PCT",
                                status="CONFIRMED_CLOSED",
                                retry_attempts=1,
                            )
                        ],
                        timestamp=req_ts,
                    )
                    return True, "HWM_KILL_SWITCH_8PCT", fill_px, i

            # 7. Ratchet standard ATR trailing SL
            self._ratchet_trailing(exec_q, bar_idx, effective_max_hold)

        if evaluate_timeout:
            exit_tick_idx = last_valid_idx if last_valid_idx is not None else (len(bids) - 1)
            # 7b. Optional early failure exit at exact N minutes (e.g. 15m MFE < 0.4 ATR)
            if early_fail_mins is not None and early_fail_min_mfe_atr is not None and self.bars_held == early_fail_mins:
                gain_abs = (self.peak_price - self.entry_price) if is_bull else (self.entry_price - self.peak_price)
                mfe_atr = gain_abs / max(self.atr, 1e-9)
                if mfe_atr < early_fail_min_mfe_atr:
                    base_px = last_valid_q if last_valid_q is not None else self.entry_price
                    ex_px = (base_px - slip_offset) if is_bull else (base_px + slip_offset)
                    if slip_offset > 0.0 and last_valid_q is not None:
                        self.slippage_usd += slip_offset * self.volume * _CONTRACT_SIZE
                    return True, f"EARLY_FAIL_{early_fail_mins}M", ex_px, exit_tick_idx

            # 8. TIMEOUT — session-aware (BUY closes at Bid, SELL closes at Ask)
            if self.bars_held >= effective_max_hold:
                base_px = last_valid_q if last_valid_q is not None else self.entry_price
                ex_px = (base_px - slip_offset) if is_bull else (base_px + slip_offset)
                if slip_offset > 0.0 and last_valid_q is not None:
                    self.slippage_usd += slip_offset * self.volume * _CONTRACT_SIZE
                return True, "TIMEOUT", ex_px, exit_tick_idx

        if best_valid_q is not None:
            self._ratchet_trailing(best_valid_q, bar_idx, effective_max_hold)

        return False, "", 0.0, len(bids) - 1

    def compute_gross_pnl(self, exit_price: float) -> float:
        if self.direction == "BUY":
            return (exit_price - self.entry_price) * self.volume * _CONTRACT_SIZE
        return (self.entry_price - exit_price) * self.volume * _CONTRACT_SIZE

    def compute_pnl(self, exit_price: float, cost_service: BrokerCostService | None = None) -> float:
        # When cost_service calculates separate entry & exit legs, include pending exit commission estimate for MTM/exit precision
        pending_exit_comm = 0.0
        if cost_service is not None and self._exit_comm_lots_charged < self.initial_volume - 1e-12:
            pending_exit_comm = cost_service.calculate_leg_commission(self.volume, exit_price)
        return self.compute_gross_pnl(exit_price) - self.commission_usd - pending_exit_comm - self.swap_usd


class RunBacktestUseCase:
    """
    Minute-Grouped Tick-Replay Backtest Engine for XAUUSD.

    Architecture:
      - Pre-group 1.9M ticks into ~10,000 minute buckets ONCE (vectorized)
      - Outer loop: iterate minute bars (signal pipeline — same services as live)
      - Inner check: NumPy scan of ticks per minute (tick-accurate SL/TP)

    ~100-300× faster than old tick-by-tick loop with identical accuracy.
    """

    def __init__(
        self,
        tick_repo: ITickRepository,
        virtual_broker: VirtualBrokerAdapter,
        manage_position,       # unused now but kept for DI compatibility
        sizing_service: PositionSizingService,
        profile: RiskProfile = None,
        expected_pnl_model: IExpectedPnlModel = None,
        min_expected_pnl: float = None,
    ):
        self.tick_repo = tick_repo
        self.virtual_broker = virtual_broker
        self.sizing_service = sizing_service
        self.profile = profile or get_profile("MEDIUM_BACKTEST")
        self.expected_pnl_model = expected_pnl_model
        self.min_expected_pnl = min_expected_pnl

        self.regime_detector = RegimeDetectorService()
        self.trade_scorer = TradeScorer(w_ml=0.0, w_quant=1.0)

    # ─────────────────────────────────────────────────────────────────────────

    def execute(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        initial_capital: float = 10000.0,
        use_kalman_exit: bool = False,
        progress_callback=None,
        session_close_utc: int | None = None,
        early_fail_mins: int | None = None,
        early_fail_min_mfe_atr: float | None = None,
        commission_per_lot: float = 0.0,
        slippage_pips: float = 0.0,
        swap_long_per_lot: float = 0.0,
        swap_short_per_lot: float = 0.0,
        rollover_utc_hour: int = 21,
        commission_profile: str | None = None,
        trading_environment: str = "demo",
        account_id: int | None = None,
        cost_service: BrokerCostService | None = None,
        apply_swap: bool = False,
        use_tick_intensity_features: bool = True,
        max_hwm_drawdown_pct: float | None = 8.0,
        stop_check: Any = None,
        export_artifacts: bool = True,
        max_score_threshold: float | None = None,
        scorer_mode: str = "legacy",
        max_kaufman_er: float | None = None,
        be_trigger_atr: float = 1.0,
    ) -> dict:

        scorer_mode_norm = str(scorer_mode or "legacy").strip().lower()
        if scorer_mode_norm not in VALID_SCORER_MODES:
            raise ValueError(f"Unsupported scorer_mode={scorer_mode!r}; expected one of {VALID_SCORER_MODES}")

        active_cost_service = cost_service
        if active_cost_service is None and (commission_profile is not None or apply_swap):
            if commission_profile is not None:
                sched = resolve_commission_schedule(
                    commission_profile,
                    trading_environment=trading_environment,
                    account_id=account_id,
                    custom_per_lot_rt=(commission_per_lot if commission_profile in ("fixed_per_lot", "custom_per_lot") else None),
                )
            elif commission_per_lot > 0.0:
                sched = resolve_commission_schedule(
                    "fixed_per_lot",
                    trading_environment=trading_environment,
                    custom_per_lot_rt=float(commission_per_lot),
                )
            else:
                sched = ZERO_COMMISSION_SCHEDULE
            swap_sched = FUSION_XAUUSD_SWAP_SPEC if apply_swap else SwapSchedule(
                swap_calculation_type="NONE", swap_long_pips=0.0, swap_short_pips=0.0, triple_swap_weekday=2
            )
            active_cost_service = BrokerCostService(commission_schedule=sched, swap_schedule=swap_sched)

        print(f"\n{'='*60}")
        print(f"  TICK REPLAY BACKTEST  (Causal Minute-Grouped Architecture)")
        print(f"  Symbol: {symbol} | Period: {start.date()} to {end.date()}")
        print(f"  Profile: {self.profile.name} | Score >= {self.profile.min_score_threshold}")
        print(f"  SL: {self.profile.sl_atr_multiplier}x ATR | TP: {self.profile.tp_atr_multiplier}x ATR")
        print(f"  Risk/trade: {self.profile.risk_per_trade_pct}% | Max daily loss: {self.profile.max_daily_loss_pct}%")
        comm_label = active_cost_service.commission_schedule.name if active_cost_service else f"fixed=${commission_per_lot:.2f}/lot"
        print(f"  Costs: model={comm_label} | slip={slippage_pips:.2f}p | apply_swap={bool(apply_swap or swap_long_per_lot or swap_short_per_lot)}")
        print(f"{'='*60}")

        self.virtual_broker.reset(equity=initial_capital)
        # B6: Always reset hysteresis state at run start so sequential runs never leak regime state
        self.regime_detector = RegimeDetectorService()
        pip_unit = 0.10 if "XAU" in symbol else (0.01 if "JPY" in symbol else 0.0001)
        slip_offset = float(slippage_pips) * pip_unit

        # ── 1. Load warmup ticks ─────────────────────────────────────────────
        warmup_start = start - timedelta(days=4)
        print(f"  [1/5] Loading warmup ticks ({warmup_start.date()} to {start.date()})...")
        warmup_df = self.tick_repo.get_ticks_as_df(symbol, warmup_start, start)

        # ── 2. Warmup: seed Kalman + candle buffers ──────────────────────────
        print(f"  [2/5] Processing warmup candles...")
        kalman = VolumeAdaptiveKalmanFilter()
        candle_buffer: deque = deque(maxlen=200)   # fixed-size, O(1) append
        agg_5m = RollingBarAggregator(5, maxlen=200)
        agg_1h = RollingBarAggregator(60, maxlen=100)

        if not warmup_df.empty:
            warmup_df["minute"] = warmup_df["time"].dt.floor("min")
            for minute_ts, grp in warmup_df.groupby("minute", sort=True):
                w_bids = grp["bid"].values.astype(np.float64)
                w_asks = grp["ask"].values.astype(np.float64) if "ask" in grp.columns else (w_bids + 0.30)
                w_valid = (w_bids > 0.0) & (w_asks > 0.0) & (w_bids < w_asks)
                w_valid_idxs = np.flatnonzero(w_valid)
                if len(w_valid_idxs) == 0:
                    continue
                real_vols = grp["real_volume"].values if "real_volume" in grp.columns else (grp["volume"].values if "volume" in grp.columns else None)
                if real_vols is not None:
                    for j in w_valid_idxs:
                        v = real_vols[j]
                        kalman.update_tick(float(w_bids[j]), None if pd.isna(v) else float(v))
                else:
                    for j in w_valid_idxs:
                        kalman.update_tick(float(w_bids[j]), None)
                w_valid_bids = w_bids[w_valid_idxs]
                tick_cnt = int(len(w_valid_idxs))
                w_candle = {
                    "time": minute_ts,
                    "open":   float(w_valid_bids[0]),
                    "high":   float(w_valid_bids.max()),
                    "low":    float(w_valid_bids.min()),
                    "close":  float(w_valid_bids[-1]),
                    "tick_count": tick_cnt,
                    "tick_volume": tick_cnt,
                    "tick_rate": float(tick_cnt),
                    "real_volume": None,
                }
                candle_buffer.append(w_candle)
                agg_5m.update(w_candle)
                agg_1h.update(w_candle)
        del warmup_df

        print(f"  Warmup complete: {len(candle_buffer)} bars seeded, Kalman initialized.")

        # ── 3. STREAMING REPLAY LOOP (7-day chunks — low RAM, fast progress) ─
        print(f"  [3/5] Streaming replay ticks in 7-day chunks ({start.date()} to {end.date()})...")

        open_pos: _OpenPosition | None = None
        closed_trades: list[dict] = []

        equity = initial_capital
        day_start_equity = initial_capital
        mtm_tracker = _MtmDrawdownTracker(initial_capital, start)
        hwm_ks = (
            HwmKillSwitchService(
                max_drawdown_pct=float(max_hwm_drawdown_pct),
                initial_equity=float(initial_capital),
                default_account_scope=f"backtest:{symbol}",
            )
            if max_hwm_drawdown_pct is not None
            else None
        )
        trades_prevented_by_kill_switch = 0
        bars_processed = 0
        signals_seen = 0
        signals_passed = 0
        inverted_quotes_skipped = 0
        zero_spread_ticks_skipped = 0
        daily_pnl = 0.0
        daily_trades = 0
        consecutive_losses = 0
        prev_regime = None
        current_regime = self.regime_detector._committed_regime
        last_strat_dir_bar: dict[tuple, int] = {}    # cooldown tracker
        COOLDOWN_BARS = 15
        current_day = None
        last_valid_tick: tuple[float, float, datetime] | None = None
        start_naive = start.replace(tzinfo=None)
        end_naive = end.replace(tzinfo=None)
        total_days = max(1, (end_naive.date() - start_naive.date()).days + 1)
        total_span_secs = max(1.0, (end_naive - start_naive).total_seconds())
        run_started_ts = time.time()

        def _record_exit(pos: _OpenPosition, reason: str, exit_px: float, exit_ts: datetime) -> None:
            nonlocal equity, daily_pnl, consecutive_losses
            if active_cost_service is not None:
                pos.apply_rollovers_until(exit_ts, active_cost_service)
                pos.record_exit_leg_commission(pos.volume, exit_px, active_cost_service)
            pnl = pos.compute_pnl(exit_px, cost_service=None)
            gross_pnl = pos.compute_gross_pnl(exit_px)
            eq_before = equity
            equity += pnl
            daily_pnl += pnl
            consecutive_losses = 0 if pnl > 0 else consecutive_losses + 1
            mtm_tracker.update(equity, equity, exit_ts)
            if hwm_ks is not None:
                hwm_ks.evaluate_equity(
                    current_equity=equity,
                    timestamp=exit_ts,
                    open_positions_count=0,
                    symbol=symbol,
                )
            realized_loss_pct = (pnl / eq_before * 100.0) if (eq_before > 0 and pnl < 0) else 0.0

            color = "\033[92m" if pnl > 0 else "\033[91m"
            reset = "\033[0m"
            print(
                f"    EXIT  {reason:<12s} {symbol}  {pos.direction}  "
                f"{color}${pnl:+,.2f}{reset}  "
                f"entry=${pos.entry_price:.2f}  exit=${exit_px:.2f}  "
                f"vol={pos.volume:.2f}  equity=${equity:,.2f}"
            )
            closed_trades.append({
                "entry_time":             pos.entry_time,
                "exit_time":              exit_ts,
                "symbol":                 pos.symbol,
                "direction":              pos.direction,
                "strategy":               pos.strategy,
                "entry_price":            pos.entry_price,
                "exit_price":             exit_px,
                "sl":                     pos.initial_sl,
                "theoretical_stop_price": pos.theoretical_stop_price,
                "tp":                     pos.tp,
                "qty":                    pos.volume,
                "gross_pnl":              gross_pnl,
                "pnl":                    pnl,
                "commission_entry_usd":   pos.commission_entry_usd,
                "commission_exit_usd":    pos.commission_exit_usd,
                "commission_usd":         pos.commission_usd,
                "slippage_usd":           pos.slippage_usd,
                "swap_usd":               pos.swap_usd,
                "swap_pnl_usd":           pos.swap_pnl_usd,
                "planned_risk_usd":       pos.planned_risk_usd,
                "planned_risk_pct":       pos.planned_risk_pct,
                "realized_loss_pct":      realized_loss_pct,
                "sl_gap_usd":             pos.last_sl_gap_usd,
                "exit_reason":            reason,
                "score":                  pos.final_score,
                "regime":                 pos.regime,
                "journey":                list(pos.journey),
            })

        def _emit_progress(m_ts):
            if progress_callback is None:
                return
            m_naive = m_ts.replace(tzinfo=None) if getattr(m_ts, "tzinfo", None) is not None else m_ts
            frac = min(0.99, max(0.001, (m_naive - start_naive).total_seconds() / total_span_secs))
            pct = round(frac * 100.0, 1)
            elapsed = max(0.01, time.time() - run_started_ts)
            eta = int(elapsed * (1.0 - frac) / frac)
            wins_count = sum(1 for t in closed_trades if t["pnl"] > 0)
            cur_day_num = min(total_days, max(1, (m_naive.date() - start_naive.date()).days + 1))
            progress_callback({
                "pct_complete": pct,
                "current_day": cur_day_num,
                "total_days": total_days,
                "current_date": str(m_ts.date()),
                "live_trades": len(closed_trades),
                "live_wins": wins_count,
                "live_pnl": round(equity - initial_capital, 2),
                "live_equity": round(equity, 2),
                "elapsed_secs": int(elapsed),
                "eta_secs": max(0, eta),
            })

        bar_idx = -1
        cancelled = False
        end_exclusive = end + timedelta(microseconds=1)
        chunk_start = start
        while chunk_start < end_exclusive:
            if stop_check is not None and stop_check():
                cancelled = True
                break
            chunk_end = min(chunk_start + timedelta(days=7), end_exclusive)
            ticks_df = self.tick_repo.get_ticks_as_df(symbol, chunk_start, chunk_end)
            chunk_start = chunk_end
            if ticks_df.empty:
                continue

            ticks_df["minute"] = ticks_df["time"].dt.floor("min")
            for minute_ts, grp in ticks_df.groupby("minute", sort=True):
                if stop_check is not None and stop_check():
                    cancelled = True
                    break
                bar_idx += 1
                if bar_idx % 200 == 0:
                    time.sleep(0.001)
                if bar_idx % 100 == 0:
                    _emit_progress(minute_ts)

                # ── Daily Reset ──────────────────────────────────────────────
                if current_day != minute_ts.date():
                    current_day = minute_ts.date()
                    daily_pnl = 0.0
                    daily_trades = 0
                    consecutive_losses = 0
                    day_start_equity = max(equity, 1.0)

                bids = grp["bid"].values.astype(np.float64)
                asks = (
                    grp["ask"].values.astype(np.float64)
                    if "ask" in grp.columns
                    else bids + 0.30
                )
                vols = (
                    grp["real_volume"].values.astype(np.float64)
                    if "real_volume" in grp.columns
                    else (
                        grp["volume"].values.astype(np.float64)
                        if "volume" in grp.columns
                        else np.full(len(bids), np.nan, dtype=np.float64)
                    )
                )
                tick_times = (
                    [pd.Timestamp(t).to_pydatetime() for t in grp["time"].values]
                    if "time" in grp.columns
                    else [minute_ts] * len(bids)
                )

                valid_mask = (bids > 0.0) & (asks > 0.0) & (bids < asks)
                if not bool(valid_mask.all()):
                    inverted_quotes_skipped += int(((bids > 0.0) & (asks > 0.0) & (bids > asks)).sum())
                    zero_spread_ticks_skipped += int(((bids > 0.0) & (asks > 0.0) & (bids == asks)).sum())

                valid_idxs = np.flatnonzero(valid_mask)
                if len(valid_idxs) > 0:
                    lv = int(valid_idxs[-1])
                    last_valid_tick = (float(bids[lv]), float(asks[lv]), tick_times[lv])

                # ── Build candle from strictly valid quotes (bid > 0 & ask > 0 & bid < ask) ──
                tick_cnt = int(len(valid_idxs))
                closed_5m = False
                df_ind = pd.DataFrame()
                candle = None
                if tick_cnt > 0:
                    valid_bids = bids[valid_idxs]
                    valid_vols = vols[valid_idxs]
                    non_null_rv = valid_vols[~np.isnan(valid_vols)]
                    real_vol_sum = int(non_null_rv.sum()) if len(non_null_rv) > 0 else None
                    candle = {
                        "time":        minute_ts,
                        "open":        float(valid_bids[0]),
                        "high":        float(valid_bids.max()),
                        "low":         float(valid_bids.min()),
                        "close":       float(valid_bids[-1]),
                        "tick_count":  tick_cnt,
                        "tick_volume": tick_cnt,
                        "tick_rate":   float(tick_cnt),
                        "real_volume": real_vol_sum,
                    }
                    candle_buffer.append(candle)
                    closed_5m = agg_5m.update(candle)
                    agg_1h.update(candle)
                    bars_processed += 1

                    # B6: Always update RegimeDetector hysteresis on every closed 5m bar (parity with ScanMarketUseCase)
                    if closed_5m and len(agg_5m.bars) >= 60:
                        df_bars = pd.DataFrame(list(agg_5m.bars))
                        df_ind = IndicatorService.compute_indicators(df_bars)
                        if not df_ind.empty:
                            current_regime = self.regime_detector.detect(df_ind)
                            if current_regime != prev_regime:
                                print(f"  REGIME  {prev_regime} -> {current_regime.value if hasattr(current_regime, 'value') else current_regime}")
                                prev_regime = current_regime

                if candle is not None and bars_processed % 100 == 0:
                    print(
                        f"  [{bars_processed} bars]  {candle['time']}  "
                        f"O={candle['open']:.2f} H={candle['high']:.2f} "
                        f"L={candle['low']:.2f} C={candle['close']:.2f}  "
                        f"tick_rate={candle['tick_rate']:.0f}/m  equity=${equity:,.2f}"
                    )

                # ── Check existing open position on all ticks [0 .. N-1] ─────
                if open_pos is not None:
                    # Rollover swap: timezone-aware (17:00 NY -> 21:00Z EDT / 22:00Z EST) via active_cost_service,
                    # or legacy fallback if only manual swap_long_per_lot / swap_short_per_lot are supplied.
                    if active_cost_service is None and (
                        (swap_long_per_lot != 0.0 or swap_short_per_lot != 0.0)
                        and hasattr(minute_ts, "hour")
                        and minute_ts.hour >= rollover_utc_hour
                        and open_pos.last_swap_date != minute_ts.date()
                    ):
                        nights = 3 if minute_ts.weekday() == 2 else 1
                        rate = swap_long_per_lot if open_pos.direction == "BUY" else swap_short_per_lot
                        open_pos.swap_usd += float(rate) * open_pos.volume * nights
                        open_pos.swap_pnl_usd = -open_pos.swap_usd
                        open_pos.last_swap_date = minute_ts.date()

                    open_pos.bars_held += 1
                    max_hold = getattr(self.profile, "max_hold_bars", 120)
                    entry_hour = open_pos.entry_time.hour if hasattr(open_pos.entry_time, "hour") else 12
                    exited, reason, exit_px, last_idx = open_pos.check_exit_ticks(
                        bids, asks, vols, bar_idx, max_hold, kalman,
                        entry_utc_hour=entry_hour,
                        use_kalman_exit=use_kalman_exit,
                        closed_equity=equity,
                        mtm_tracker=mtm_tracker,
                        minute_ts=minute_ts,
                        early_fail_mins=early_fail_mins,
                        early_fail_min_mfe_atr=early_fail_min_mfe_atr,
                        start_tick_idx=0,
                        evaluate_timeout=True,
                        slippage_pips=slippage_pips,
                        tick_times=tick_times,
                        cost_service=active_cost_service,
                        hwm_kill_switch=hwm_ks,
                    )

                    current_price = exit_px if exited else float(bids[-1])
                    open_pos.journey.append({
                        "time": minute_ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(minute_ts, "strftime") else str(minute_ts),
                        "price": round(current_price, 2),
                        "sl": round(open_pos.sl, 2),
                        "bars_held": open_pos.bars_held
                    })

                    if exited:
                        for j in range(last_idx + 1, len(bids)):
                            if valid_mask[j]:
                                kalman.update_tick(float(bids[j]), None if np.isnan(vols[j]) else float(vols[j]))
                        _record_exit(open_pos, reason, exit_px, tick_times[last_idx])
                        open_pos = None
                        _emit_progress(minute_ts)
                    continue

                # ── Flat Position: B1 Causal Rollover Decision at First Valid Tick ──
                if len(valid_idxs) == 0:
                    continue

                en_i = int(valid_idxs[0])
                # Advance Kalman ONLY up through the causal valid rollover tick en_i (NEVER corrupted or future ticks)
                for j in range(en_i + 1):
                    if valid_mask[j]:
                        kalman.update_tick(float(bids[j]), None if np.isnan(vols[j]) else float(vols[j]))
                kalman_consumed_until = en_i + 1

                def _flush_remaining_kalman() -> None:
                    for j in range(kalman_consumed_until, len(bids)):
                        if valid_mask[j]:
                            kalman.update_tick(float(bids[j]), None if np.isnan(vols[j]) else float(vols[j]))

                if not closed_5m or len(agg_5m.bars) < 60 or df_ind.empty:
                    _flush_remaining_kalman()
                    continue

                if session_close_utc is not None and minute_ts.hour >= session_close_utc:
                    _flush_remaining_kalman()
                    continue

                daily_loss_pct = abs(min(0.0, daily_pnl)) / max(day_start_equity, 1.0) * 100.0
                if daily_loss_pct >= self.profile.max_daily_loss_pct:
                    _flush_remaining_kalman()
                    continue
                if daily_trades >= self.profile.max_trades_per_day:
                    _flush_remaining_kalman()
                    continue
                if consecutive_losses >= self.profile.max_consecutive_losses:
                    _flush_remaining_kalman()
                    continue

                htf_trend = compute_htf_ema_trend(agg_1h.closed_closes())
                if htf_trend == 0:
                    _flush_remaining_kalman()
                    continue

                regime = current_regime
                signals = SignalGeneratorService.generate_signals(
                    df_ind, symbol, regime, use_tick_intensity_features=use_tick_intensity_features
                )
                if not signals:
                    _flush_remaining_kalman()
                    continue

                signals_seen += len(signals)

                last_row    = df_ind.iloc[-1]
                close_series = df_ind["close"] if "close" in df_ind.columns else pd.Series(dtype=float)
                closed_5m_ts = agg_5m.bars[-1]["time"]

                features = {
                    "kalman_vel":               kalman.get_velocity(),
                    "kalman_accel":             kalman.get_acceleration(),
                    "shannon_entropy":          (
                        DenoiseService.compute_rolling_entropy(close_series.pct_change().dropna())
                        if len(close_series) > 30 else 2.0
                    ),
                    "kaufman_er":               (
                        DenoiseService.compute_kaufman_er(close_series)
                        if len(close_series) > 15 else 0.3
                    ),
                    "ema20_slope":              _safe_float(last_row.get("ema20_slope"), 0.0),
                    "tick_weighted_price_dist": _safe_float(last_row.get("tick_weighted_price_dist"), 0.0),
                    "tick_rate_ratio":          _safe_float(last_row.get("tick_rate_ratio"), 1.0),
                    "rsi":                      _safe_float(last_row.get("rsi"), 50.0),
                    # B6 parity: ScanMarketUseCase passes closed_5m_ts.hour
                    "utc_hour":                 closed_5m_ts.hour if hasattr(closed_5m_ts, "hour") else minute_ts.hour,
                    "rsi_price_divergence":     0.0,
                }
                if scorer_mode_norm != "legacy":
                    atr_v = _safe_float(last_row.get("atr"), 1.0)
                    close_v = _safe_float(last_row.get("close"), 0.0)
                    ema20_v = _safe_float(last_row.get("ema20"), 0.0)
                    ema20_dist_atr = ((close_v - ema20_v) / max(atr_v, 1e-9)) if ema20_v > 0 else 0.0
                    entry_5m_tr = _safe_float(last_row.get("tick_rate"), 0.0)
                    ent_5m_norm = (
                        DenoiseService.compute_rolling_entropy_5m_norm(close_series.pct_change().dropna())
                        if len(close_series) > 30 else 1.0
                    )
                    kv_1m_atr = (features["kalman_vel"] * (max(entry_5m_tr, 1.0) / 5.0)) / max(atr_v, 0.50)
                    features["atr"] = atr_v
                    features["ema20_dist_atr"] = ema20_dist_atr
                    features["entry_5m_tick_rate"] = entry_5m_tr
                    features["shannon_entropy_5m_norm"] = ent_5m_norm
                    features["kalman_vel_1m_atr"] = kv_1m_atr

                regime_str = regime.value if hasattr(regime, "value") else str(regime)

                scored = self.trade_scorer.filter_and_rank(
                    signals,
                    min_score_threshold=self.profile.min_score_threshold,
                    regime=regime_str,
                    features=features,
                    use_tick_intensity_features=use_tick_intensity_features,
                    scorer_mode=scorer_mode_norm,
                )

                if not scored:
                    for sig in signals:
                        ind = self.trade_scorer.score_signal(
                            sig,
                            regime=regime_str,
                            features=features,
                            use_tick_intensity_features=use_tick_intensity_features,
                            scorer_mode=scorer_mode_norm,
                        )
                        dir_val = sig.direction.value if hasattr(sig.direction, "value") else sig.direction
                        print(
                            f"    SKIP  {sig.strategy:<25s} {dir_val:<5s} "
                            f"score={ind.final_score:.3f}  "
                            f"vel={features['kalman_vel']:.4f}  "
                            f"er={features['kaufman_er']:.3f}  "
                            f"ent={features['shannon_entropy']:.3f}  "
                            f"slope={features['ema20_slope']:.5f}"
                        )
                    _flush_remaining_kalman()
                    continue

                best = scored[0]
                dir_val = best.direction.value if hasattr(best.direction, "value") else best.direction

                if (dir_val == "BUY" and htf_trend != 1) or (dir_val == "SELL" and htf_trend != -1):
                    print(f"    SKIP  {best.strategy:<25s} {dir_val:<5s} H1 Trend Gate: trend={htf_trend}")
                    _flush_remaining_kalman()
                    continue

                if not passes_regime_filters(
                    dir_val,
                    regime_str,
                    block_high_volatility=getattr(self.profile, "block_high_volatility", False),
                    block_buy_sideways=getattr(self.profile, "block_buy_sideways", False),
                ):
                    print(f"    SKIP  {best.strategy:<25s} {dir_val:<5s} Regime Gate: {regime_str}")
                    _flush_remaining_kalman()
                    continue

                if max_score_threshold is not None and float(best.final_score) >= float(max_score_threshold):
                    print(
                        f"    SKIP  {best.strategy:<25s} {dir_val:<5s} "
                        f"Score Cap Gate: {best.final_score:.4f} >= {float(max_score_threshold):.4f}"
                    )
                    _flush_remaining_kalman()
                    continue

                if max_kaufman_er is not None and float(features["kaufman_er"]) >= float(max_kaufman_er):
                    print(
                        f"    SKIP  {best.strategy:<25s} {dir_val:<5s} "
                        f"Kaufman ER Cap Gate: {features['kaufman_er']:.4f} >= {float(max_kaufman_er):.4f}"
                    )
                    _flush_remaining_kalman()
                    continue

                if self.expected_pnl_model is not None:
                    # Legacy 11-feature pickled Ridge observation schema maps quote-rate & TWP proxies cleanly
                    pred_pnl = self.expected_pnl_model.predict_expected_pnl({
                        "rsi": features["rsi"],
                        "volume_ratio": features["tick_rate_ratio"],
                        "ema20_slope": features["ema20_slope"],
                        "ema_spread": float((last_row["ema20"] - last_row["ema50"]) / last_row["ema50"]) if last_row.get("ema50", 0) else 0.0,
                        "vwap_dist": features["tick_weighted_price_dist"],
                        "kalman_velocity": features["kalman_vel"],
                        "quant_score": float(best.final_score),
                    })
                    if pred_pnl is not None and self.min_expected_pnl is not None and pred_pnl <= self.min_expected_pnl:
                        print(f"    SKIP  {best.strategy:<25s} {dir_val:<5s} Ridge PnL=${pred_pnl:+.2f} <= ${self.min_expected_pnl:+.2f}")
                        _flush_remaining_kalman()
                        continue

                key = (best.strategy, dir_val)
                if bar_idx - last_strat_dir_bar.get(key, -9999) < COOLDOWN_BARS:
                    print(f"    COOL  {best.strategy}  {dir_val}  cooldown ({COOLDOWN_BARS} bars)")
                    _flush_remaining_kalman()
                    continue

                if hwm_ks is not None:
                    allowed_hwm, halt_msg = hwm_ks.can_open_new_position(symbol=symbol)
                    if not allowed_hwm:
                        trades_prevented_by_kill_switch += 1
                        print(f"    HALT  {best.strategy}  {dir_val}  {halt_msg}")
                        _flush_remaining_kalman()
                        continue

                latest_atr = _safe_float(
                    df_ind["atr"].iloc[-1] if "atr" in df_ind.columns else None, 1.0
                )
                raw_entry_price = self.sizing_service.resolve_executable_entry(
                    dir_val, float(bids[en_i]), float(asks[en_i])
                )
                entry_price = (raw_entry_price + slip_offset) if dir_val == "BUY" else (raw_entry_price - slip_offset)
                comm_for_sizing = (
                    active_cost_service.estimate_round_trip_per_lot(entry_price)
                    if active_cost_service is not None
                    else float(commission_per_lot)
                )
                lot_size, sl_price, tp_price, reject_reason = self.sizing_service.calculate_size(
                    account_balance=equity,
                    risk_profile=self.profile,
                    entry_price=entry_price,
                    atr_value=latest_atr,
                    direction=best.direction,
                    symbol=symbol,
                    commission_per_lot=comm_for_sizing,
                )

                if reject_reason:
                    print(f"    SKIP  {best.strategy}  {dir_val}  Sizing: {reject_reason}")
                    _flush_remaining_kalman()
                    continue

                if active_cost_service is not None:
                    entry_comm_usd = active_cost_service.calculate_leg_commission(lot_size, entry_price)
                    est_rt_comm_usd = entry_comm_usd + active_cost_service.calculate_leg_commission(lot_size, entry_price)
                    comm_usd_init = entry_comm_usd
                else:
                    entry_comm_usd = round(0.5 * float(commission_per_lot) * lot_size, 6)
                    est_rt_comm_usd = float(commission_per_lot) * lot_size
                    comm_usd_init = est_rt_comm_usd

                entry_slip_usd = slip_offset * lot_size * _CONTRACT_SIZE
                planned_risk_usd = abs(entry_price - sl_price) * lot_size * _CONTRACT_SIZE + est_rt_comm_usd
                planned_risk_pct = (planned_risk_usd / equity * 100.0) if equity > 0 else 0.0
                entry_tick_ts = tick_times[en_i]

                open_pos = _OpenPosition(
                    ticket=bar_idx,
                    symbol=symbol,
                    direction=dir_val,
                    strategy=best.strategy,
                    entry_price=entry_price,
                    sl=sl_price,
                    tp=tp_price,
                    atr=latest_atr,
                    volume=lot_size,
                    entry_time=entry_tick_ts,
                    entry_bar_idx=bar_idx,
                    final_score=best.final_score,
                    regime=regime_str,
                    planned_risk_usd=planned_risk_usd,
                    planned_risk_pct=planned_risk_pct,
                    commission_usd=comm_usd_init,
                    commission_entry_usd=entry_comm_usd,
                    slippage_usd=entry_slip_usd,
                    rollover_utc_hour=rollover_utc_hour,
                    be_trigger_atr=be_trigger_atr,
                )
                init_unr = -(float(asks[en_i]) - float(bids[en_i])) * lot_size * _CONTRACT_SIZE - est_rt_comm_usd - entry_slip_usd
                mtm_tracker.update(equity + init_unr, equity + init_unr, entry_tick_ts)
                last_strat_dir_bar[key] = bar_idx
                daily_trades += 1
                signals_passed += 1

                print(
                    f"    ENTRY {symbol}  {dir_val}  "
                    f"price=${entry_price:.2f}  SL=${sl_price:.2f}  TGT=${tp_price:.2f}  "
                    f"ATR={latest_atr:.3f}  vol={lot_size:.2f}  score={best.final_score:.3f}  "
                    f"regime={regime_str}  strat={best.strategy}  "
                    f"vel={features['kalman_vel']:.4f}  equity=${equity:,.2f}"
                )

                # B2: Scan remaining post-entry ticks [en_i + 1 .. N-1] immediately after entry
                if en_i + 1 < len(bids):
                    max_hold = getattr(self.profile, "max_hold_bars", 120)
                    entry_hour = open_pos.entry_time.hour if hasattr(open_pos.entry_time, "hour") else 12
                    exited, reason, exit_px, last_idx = open_pos.check_exit_ticks(
                        bids, asks, vols, bar_idx, max_hold, kalman,
                        entry_utc_hour=entry_hour,
                        use_kalman_exit=use_kalman_exit,
                        closed_equity=equity,
                        mtm_tracker=mtm_tracker,
                        minute_ts=minute_ts,
                        early_fail_mins=early_fail_mins,
                        early_fail_min_mfe_atr=early_fail_min_mfe_atr,
                        start_tick_idx=en_i + 1,
                        evaluate_timeout=False,
                        slippage_pips=slippage_pips,
                        tick_times=tick_times,
                        cost_service=active_cost_service,
                        hwm_kill_switch=hwm_ks,
                    )
                    if exited:
                        open_pos.journey.append({
                            "time": minute_ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(minute_ts, "strftime") else str(minute_ts),
                            "price": round(exit_px, 2),
                            "sl": round(open_pos.sl, 2),
                            "bars_held": 0,
                        })
                        for j in range(last_idx + 1, len(bids)):
                            if valid_mask[j]:
                                kalman.update_tick(float(bids[j]), None if np.isnan(vols[j]) else float(vols[j]))
                        _record_exit(open_pos, reason, exit_px, tick_times[last_idx])
                        open_pos = None
                        _emit_progress(minute_ts)

            del ticks_df
            if cancelled:
                break

        max_score_cap_out = float(max_score_threshold) if max_score_threshold is not None else None
        if cancelled:
            print("  Backtest cancelled via stop request.")
            res_cancel = self._empty_result(
                start, initial_capital,
                inverted_quotes_skipped=inverted_quotes_skipped,
                zero_spread_ticks_skipped=zero_spread_ticks_skipped,
            )
            res_cancel["cancelled"] = True
            res_cancel["max_score_threshold"] = max_score_cap_out
            res_cancel["scorer_mode"] = scorer_mode_norm
            return res_cancel

        # B6: Explicitly close any position still open at end of backtest window
        if open_pos is not None and last_valid_tick is not None:
            lb, la, lt = last_valid_tick
            raw_close = lb if open_pos.direction == "BUY" else la
            ex_px = (raw_close - slip_offset) if open_pos.direction == "BUY" else (raw_close + slip_offset)
            if slip_offset > 0.0:
                open_pos.slippage_usd += slip_offset * open_pos.volume * _CONTRACT_SIZE
            _record_exit(open_pos, "END_OF_BACKTEST", ex_px, lt)
            open_pos = None

        # ── Final Summary ─────────────────────────────────────────────────────
        wins   = sum(1 for t in closed_trades if t["pnl"] > 0)
        losses = len(closed_trades) - wins
        total_pnl = sum(t["pnl"] for t in closed_trades)

        print(f"\n  Backtest complete: {len(closed_trades)} trades  |  {wins}W / {losses}L")
        print(f"  Signals seen: {signals_seen}  |  Passed: {signals_passed}  |  Bars: {bars_processed}")
        print(f"  Quotes skipped: inverted={inverted_quotes_skipped}  zero_spread={zero_spread_ticks_skipped}")
        print(f"  Final equity: ${equity:,.2f}  |  Total P&L: ${total_pnl:+,.2f}")

        hwm_summary = None
        if hwm_ks is not None:
            hwm_st = hwm_ks.get_state()
            hwm_summary = {
                **hwm_st.to_dict(),
                "trades_prevented": int(trades_prevented_by_kill_switch),
            }

        if not closed_trades:
            res_empty = self._empty_result(
                start, initial_capital,
                inverted_quotes_skipped=inverted_quotes_skipped,
                zero_spread_ticks_skipped=zero_spread_ticks_skipped,
            )
            res_empty["hwm_kill_switch"] = hwm_summary
            res_empty["max_score_threshold"] = max_score_cap_out
            res_empty["scorer_mode"] = scorer_mode_norm
            return res_empty

        res = self._build_result(
            closed_trades, initial_capital, equity, mtm_tracker=mtm_tracker,
            inverted_quotes_skipped=inverted_quotes_skipped,
            zero_spread_ticks_skipped=zero_spread_ticks_skipped,
            hwm_summary=hwm_summary,
            export_artifacts=export_artifacts,
        )
        res["max_score_threshold"] = max_score_cap_out
        res["scorer_mode"] = scorer_mode_norm
        return res

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_result(
        self,
        closed_trades: list[dict],
        initial_capital: float,
        final_equity: float,
        mtm_tracker: _MtmDrawdownTracker | None = None,
        inverted_quotes_skipped: int = 0,
        zero_spread_ticks_skipped: int = 0,
        hwm_summary: dict | None = None,
        export_artifacts: bool = True,
    ) -> dict:
        df = pd.DataFrame(closed_trades)
        n = len(df)
        # Standardized cent-precision trade outcome classification (no alteration of trade P&L):
        # WIN >= +$0.01, LOSS <= -$0.01, BREAKEVEN == $0.00 (|pnl| < $0.005)
        pnl_cents = df["pnl"].astype(float).round(2)
        win_mask = pnl_cents > 0.0
        loss_mask = pnl_cents < 0.0
        be_mask = pnl_cents == 0.0
        wins = int(win_mask.sum())
        losses = int(loss_mask.sum())
        breakevens = int(be_mask.sum())

        total_pnl  = df["pnl"].sum()
        df["cum_pnl"] = df["pnl"].cumsum()
        max_dd = float((df["cum_pnl"] - df["cum_pnl"].cummax()).min())
        closed_eq_series = initial_capital + df["cum_pnl"]
        closed_hwm_series = np.maximum(initial_capital, closed_eq_series.cummax())
        closed_max_dd_pct = float(((closed_hwm_series - closed_eq_series) / closed_hwm_series).max() * 100.0)
        mtm_max_dd = float(mtm_tracker.max_dd) if mtm_tracker is not None else max_dd
        mtm_max_dd_pct = float(mtm_tracker.max_dd_pct) if mtm_tracker is not None else closed_max_dd_pct
        mtm_max_dd_initial_pct = float(mtm_tracker.max_dd_initial_pct) if mtm_tracker is not None else (abs(max_dd) / max(initial_capital, 1.0) * 100.0)
        mtm_peak_equity = float(mtm_tracker.peak_equity) if mtm_tracker is not None else float(closed_hwm_series.max())
        mtm_trough_equity = float(mtm_tracker.trough_equity) if mtm_tracker is not None else float(closed_eq_series.min())
        raw_tick_mtm_max_dd = float(mtm_tracker.raw_max_dd) if mtm_tracker is not None else mtm_max_dd
        max_planned_risk_pct = float(df["planned_risk_pct"].max()) if "planned_risk_pct" in df.columns else 0.0
        max_realized_loss_pct = float(abs(df["realized_loss_pct"].min())) if "realized_loss_pct" in df.columns else 0.0
        sl_slippage_events = int(mtm_tracker.sl_slippage_events) if mtm_tracker is not None else 0
        total_sl_gap_usd = float(getattr(mtm_tracker, "total_sl_gap_usd", 0.0)) if mtm_tracker is not None else 0.0
        worst_sl_gap_usd = float(mtm_tracker.worst_sl_gap_usd) if mtm_tracker is not None else 0.0
        total_commission_usd = float(df["commission_usd"].sum()) if "commission_usd" in df.columns else 0.0
        total_slippage_usd = float(df["slippage_usd"].sum()) if "slippage_usd" in df.columns else 0.0
        total_swap_usd = float(df["swap_usd"].sum()) if "swap_usd" in df.columns else 0.0

        avg_win  = float(df[win_mask]["pnl"].mean()) if wins > 0 else 0.0
        avg_loss = float(df[loss_mask]["pnl"].mean()) if losses > 0 else 0.0
        total_win_sum  = float(df[win_mask]["pnl"].sum())
        total_loss_sum = float(df[loss_mask]["pnl"].sum())
        pf = (
            abs(total_win_sum / total_loss_sum) if total_loss_sum != 0
            else (999.0 if total_win_sum > 0 else 0.0)
        )
        rr = float(abs(avg_win / avg_loss)) if avg_loss != 0 else 0.0

        print(f"\n{'='*60}  FINAL REPORT  {'='*60}")
        print(f"  Total trades:      {n}")
        # Note: We group by date to find unique days traded
        df["day"] = pd.to_datetime(df["entry_time"]).dt.date
        days_traded = df["day"].nunique()
        print(f"  Days traded:       {days_traded}")
        print(f"  Trades / day:      {n / max(1, days_traded):.1f}")
        
        print(f"\n  Total P&L:         ${total_pnl:+,.2f}")
        print(f"  Avg P&L / trade:   ${df['pnl'].mean():+,.2f}")
        print(f"  Avg P&L / day:     ${total_pnl / max(1, days_traded):+,.2f}")
        
        print(f"\n  Wins:              {wins} ({wins/n*100:.1f}%)")
        print(f"  Losses:            {losses} ({losses/n*100:.1f}%)  |  Breakevens: {breakevens}")
        print(f"  Avg winner:        ${avg_win:+,.2f}")
        print(f"  Avg loser:         ${avg_loss:+,.2f}")
        print(f"  Risk-Reward:       {rr:.2f}")
        print(f"  Profit Factor:     {pf:.2f}")
        
        max_win = float(df["pnl"].max())
        max_loss = float(df["pnl"].min())
        print(f"  Max win:           ${max_win:+,.2f}")
        print(f"  Max loss:          ${max_loss:+,.2f}")

        # True required capital (Assuming 1:100 leverage on 100oz contract = ~1% margin)
        # Margin per trade ≈ Entry Price
        df["margin_used"] = df["entry_price"] * df["qty"]
        max_margin = float(df["margin_used"].max())
        required_capital = max_margin + abs(max_dd)
        roi = (total_pnl / required_capital * 100) if required_capital > 0 else 0.0

        print(f"\n  Max margin used:      ${max_margin:,.2f}  (Est. 1:100 Lev)")
        print(f"  Closed-trade MaxDD:   ${max_dd:+,.2f} ({closed_max_dd_pct:.2f}% HWM)")
        print(f"  Tick-MTM MaxDD:       ${mtm_max_dd:+,.2f} ({mtm_max_dd_pct:.2f}% HWM | {mtm_max_dd_initial_pct:.2f}% of initial)")
        print(f"  Planned risk max:     {max_planned_risk_pct:.3f}% | Realized single loss max: {max_realized_loss_pct:.3f}%")
        print(f"  SL gap events:        {sl_slippage_events} (worst extra gap: ${worst_sl_gap_usd:+,.2f})")
        print(f"  Required Capital:     ${required_capital:,.2f}")
        print(f"  Peak equity:       ${df['cum_pnl'].max():+,.2f}")
        print(f"  Total ROI:         {roi:,.1f}%")

        # By strategy
        print(f"\n  {'Strategy':<30s} {'Trades':>6s} {'WR':>5s} {'Total P&L':>10s} {'Avg':>8s}")
        print(f"  {'-'*30} {'-'*6} {'-'*5} {'-'*10} {'-'*8}")
        for strat, g in df.groupby("strategy"):
            wr = (g["pnl"].astype(float).round(2) > 0.0).mean() * 100
            print(f"  {strat:<30s} {len(g):>6d} {wr:>4.0f}% {g['pnl'].sum():>+10,.2f} {g['pnl'].mean():>+8,.2f}")

        # By direction
        print(f"\n  {'Direction':<10s} {'Trades':>6s} {'WR':>5s} {'Total P&L':>10s}")
        print(f"  {'-'*10} {'-'*6} {'-'*5} {'-'*10}")
        for d, g in df.groupby("direction"):
            wr = (g["pnl"].astype(float).round(2) > 0.0).mean() * 100
            print(f"  {d:<10s} {len(g):>6d} {wr:>4.0f}% {g['pnl'].sum():>+10,.2f}")

        # By result
        print(f"\n  {'Result':<15s} {'Count':>6s} {'Profitable':>10s} {'Total P&L':>10s}")
        print(f"  {'-'*15} {'-'*6} {'-'*10} {'-'*10}")
        for r, g in df.groupby("exit_reason"):
            prof = (g["pnl"].astype(float).round(2) > 0.0).mean() * 100
            print(f"  {r:<15s} {len(g):>6d} {prof:>9.0f}% {g['pnl'].sum():>+10,.2f}")

        # Per-day breakdown
        print(f"\n  {'Date':<12s} {'Trades':>6s} {'W':>3s} {'L':>3s} {'P&L':>10s}")
        print(f"  {'-'*12} {'-'*6} {'-'*3} {'-'*3} {'-'*10}")
        for day, g in df.groupby("day"):
            gp_c = g["pnl"].astype(float).round(2)
            w = int((gp_c > 0.0).sum())
            l = int((gp_c < 0.0).sum())
            print(f"  {str(day):<12s} {len(g):>6d} {w:>3d} {l:>3d} {g['pnl'].sum():>+10,.2f}")

        # Export CSV and Journeys (only when export_artifacts=True)
        if export_artifacts:
            import json
            out_dir = Path("backtest_results")
            out_dir.mkdir(exist_ok=True)
            
            # Save journeys
            journeys = {i: t.get("journey", []) for i, t in enumerate(closed_trades)}
            journey_path = out_dir / f"journeys_{self.profile.name.lower()}.json"
            with open(journey_path, "w") as f:
                json.dump(journeys, f)
            print(f"\n  Trade journeys:    {journey_path} ({len(journeys)} trades)")

            # Save CSV (without journey lists to avoid breaking Excel/Pandas)
            clean_df = df.drop(columns=["journey"], errors="ignore")
            csv_path = out_dir / "tick_replay_trades.csv"
            clean_df.to_csv(csv_path, index=False)
            print(f"  Trades exported to {csv_path}")

        # Equity curve (timestamped at realized trade exit_time, not entry_time)
        eq = initial_capital
        equity_curve = []
        for _, row in df.iterrows():
            eq += row["pnl"]
            t = row.get("exit_time") if row.get("exit_time") is not None else row["entry_time"]
            equity_curve.append({
                "time":   t.strftime("%Y-%m-%d %H:%M:%S") if hasattr(t, "strftime") else str(t),
                "equity": round(eq, 2),
            })

        # Trade list for frontend
        trade_list = []
        for t in closed_trades:
            def _fmt(v):
                return v.strftime("%Y-%m-%d %H:%M:%S") if hasattr(v, "strftime") else str(v)
            trade_list.append({
                "entry_time":             _fmt(t["entry_time"]),
                "exit_time":              _fmt(t["exit_time"]),
                "symbol":                 t["symbol"],
                "direction":              t["direction"],
                "strategy":               t["strategy"],
                "entry_price":            float(t["entry_price"]),
                "exit_price":             float(t["exit_price"]),
                "sl":                     float(t["sl"]),
                "theoretical_stop_price": float(t.get("theoretical_stop_price", t["sl"])),
                "target":                 float(t["tp"]),
                "gross_pnl":              float(t.get("gross_pnl", t["pnl"])),
                "pnl":                    float(t["pnl"]),
                "commission_usd":         float(t.get("commission_usd", 0.0)),
                "slippage_usd":           float(t.get("slippage_usd", 0.0)),
                "swap_usd":               float(t.get("swap_usd", 0.0)),
                "qty":                    float(t["qty"]),
                "exit_reason":            t["exit_reason"],
                "score":                  float(t["score"]),
                "regime":                 t.get("regime", "UNKNOWN"),
                "planned_risk_usd":       float(t.get("planned_risk_usd", 0.0)),
                "planned_risk_pct":       float(t.get("planned_risk_pct", 0.0)),
                "realized_loss_pct":      float(t.get("realized_loss_pct", 0.0)),
                "sl_gap_usd":             float(t.get("sl_gap_usd", 0.0)),
            })

        return {
            "trades":                  int(n),
            "wins":                    int(wins),
            "losses":                  int(losses),
            "breakevens":              int(breakevens),
            "win_rate":                float(wins / n) if n > 0 else 0.0,
            "pnl":                     float(total_pnl),
            "initial_capital":         float(initial_capital),
            "final_equity":            float(final_equity),
            "max_margin":              float(max_margin),
            "required_capital":        float(required_capital),
            "roi":                     float(roi),
            "max_dd":                  float(max_dd),
            "closed_max_dd":           float(max_dd),
            "closed_max_dd_pct":       float(closed_max_dd_pct),
            "mtm_max_dd":              float(mtm_max_dd),
            "mtm_max_dd_pct":          float(mtm_max_dd_pct),
            "mtm_max_dd_initial_pct":  float(mtm_max_dd_initial_pct),
            "mtm_peak_equity":         float(mtm_peak_equity),
            "mtm_trough_equity":       float(mtm_trough_equity),
            "mtm_peak_time":           str(mtm_tracker.peak_time) if mtm_tracker else None,
            "mtm_trough_time":         str(mtm_tracker.trough_time) if mtm_tracker else None,
            "raw_tick_mtm_max_dd":     float(raw_tick_mtm_max_dd),
            "max_planned_risk_pct":    float(max_planned_risk_pct),
            "max_realized_loss_pct":   float(max_realized_loss_pct),
            "sl_slippage_events":      sl_slippage_events,
            "total_sl_gap_usd":        float(total_sl_gap_usd),
            "worst_sl_gap_usd":        float(worst_sl_gap_usd),
            "total_commission_usd":    float(total_commission_usd),
            "total_slippage_usd":      float(total_slippage_usd),
            "total_swap_usd":          float(total_swap_usd),
            "inverted_quotes_skipped": int(inverted_quotes_skipped),
            "zero_spread_ticks_skipped": int(zero_spread_ticks_skipped),
            "pf":                      float(pf),
            "avg_win":                 float(avg_win),
            "avg_loss":                float(avg_loss),
            "rr":                      float(rr),
            "equity_curve":            equity_curve,
            "trade_list":              trade_list,
            "hwm_kill_switch":         hwm_summary,
        }

    def _empty_result(
        self,
        start: datetime,
        initial_capital: float,
        inverted_quotes_skipped: int = 0,
        zero_spread_ticks_skipped: int = 0,
    ) -> dict:
        return {
            "trades":                  0,
            "win_rate":                0.0,
            "pnl":                     0.0,
            "initial_capital":         float(initial_capital),
            "final_equity":            float(initial_capital),
            "max_margin":              0.0,
            "required_capital":        float(initial_capital),
            "roi":                     0.0,
            "max_dd":                  0.0,
            "closed_max_dd":           0.0,
            "closed_max_dd_pct":       0.0,
            "mtm_max_dd":              0.0,
            "mtm_max_dd_pct":          0.0,
            "mtm_max_dd_initial_pct":  0.0,
            "mtm_peak_equity":         float(initial_capital),
            "mtm_trough_equity":       float(initial_capital),
            "mtm_peak_time":           None,
            "mtm_trough_time":         None,
            "raw_tick_mtm_max_dd":     0.0,
            "max_planned_risk_pct":    0.0,
            "max_realized_loss_pct":   0.0,
            "sl_slippage_events":      0,
            "total_sl_gap_usd":        0.0,
            "worst_sl_gap_usd":        0.0,
            "total_commission_usd":    0.0,
            "total_slippage_usd":      0.0,
            "total_swap_usd":          0.0,
            "inverted_quotes_skipped": int(inverted_quotes_skipped),
            "zero_spread_ticks_skipped": int(zero_spread_ticks_skipped),
            "pf":                      0.0,
            "avg_win":                 0.0,
            "avg_loss":                0.0,
            "rr":                      0.0,
            "equity_curve":            [{"time": start.strftime("%Y-%m-%d %H:%M:%S"), "equity": initial_capital}],
            "trade_list":              [],
            "max_score_threshold":     None,
            "scorer_mode":             "legacy",
        }
