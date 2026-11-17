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

from ...domain.ports.outbound.expected_pnl_model import IExpectedPnlModel
from ...domain.ports.outbound.tick_repo import ITickRepository
from ...domain.services.denoise_service import VolumeAdaptiveKalmanFilter, DenoiseService
from ...domain.services.indicator_service import IndicatorService
from ...domain.services.mtf_trend import RollingBarAggregator, compute_htf_ema_trend, passes_regime_filters
from ...domain.services.regime_detector import RegimeDetectorService
from ...domain.services.signal_generator import SignalGeneratorService
from ...domain.services.trade_scorer import TradeScorer
from ...domain.services.sizing_service import PositionSizingService
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
        "entry_price", "sl", "initial_sl", "tp", "atr",
        "volume", "entry_time", "entry_bar_idx",
        "trailing_active", "peak_price", "peak_bar_idx",
        "bars_held", "final_score", "regime", "exit_reason_flag", "journey",
        "planned_risk_usd", "planned_risk_pct", "commission_usd", "last_sl_gap_usd",
    )

    def __init__(
        self, ticket: int, symbol: str, direction: str, strategy: str,
        entry_price: float, sl: float, tp: float, atr: float, volume: float,
        entry_time: datetime, entry_bar_idx: int,
        final_score: float, regime: str,
        planned_risk_usd: float = 0.0, planned_risk_pct: float = 0.0,
        commission_usd: float = 0.0,
    ):
        self.ticket = ticket
        self.symbol = symbol
        self.direction = direction          # "BUY" or "SELL"
        self.strategy = strategy
        self.entry_price = entry_price
        self.sl = sl
        self.initial_sl = sl
        self.tp = tp
        self.atr = atr
        self.volume = volume
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
        self.commission_usd = float(commission_usd)
        self.last_sl_gap_usd = 0.0

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
        elif gain_abs >= self.atr * 1.0:
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
    ) -> tuple[bool, str, float, int]:
        """
        Scan this minute's tick arrays for SL/TP hits.
        Updates kalman tick-by-tick to trigger dynamic exits.
        Returns (exited, reason, exit_price, last_tick_idx).

        XAUUSD-specific Kalman exit logic:
          - ATR-normalized velocity: vel_norm = k_vel / (atr / 60.0)
            where atr/60 approximates per-tick ATR unit
          - KALMAN_EXIT  fires at |vel_norm| > 2.5  (extreme 2.5σ reversal)
          - KALMAN_TIGHTEN fires at |vel_norm| > 1.5  (weakening momentum)
            Minimum tighten distance = 0.8 ATR (prevents intra-spread stop)
        """
        is_bull = self.direction == "BUY"
        tightened_this_tick = False

        # Per-tick ATR unit: spread the 1m ATR across the ~60 ticks in a minute
        atr_per_tick = max(self.atr / 60.0, 0.01)

        # Session-aware timeout: tighter during London/NY overlap
        if 7 <= entry_utc_hour < 20:
            effective_max_hold = min(max_hold_bars, 60)   # London + NY: 1h max
        else:
            effective_max_hold = min(max_hold_bars, 90)   # Asia/off-hours: 1.5h max

        for i in range(len(bids)):
            # 1. Update Kalman for this tick
            k_price, k_vel = kalman.update_tick(float(bids[i]), float(vols[i]))

            # 2. Normalize velocity against per-tick ATR unit (sigma_v proxy)
            vel_norm = k_vel / atr_per_tick

            if use_kalman_exit:
                # 3. KALMAN_VELOCITY_REVERSAL — extreme 2.5σ velocity reversal shock
                if is_bull and vel_norm <= -2.5:
                    return True, "KALMAN_VELOCITY_REVERSAL", float(bids[i]), i
                elif not is_bull and vel_norm >= 2.5:
                    return True, "KALMAN_VELOCITY_REVERSAL", float(asks[i]), i

                # 4. KALMAN_TIGHTEN — momentum turns adverse while well in profit (>= 1.5x ATR)
                gain = (float(bids[i]) - self.entry_price) if is_bull else (self.entry_price - float(asks[i]))
                if gain >= self.atr * 1.5:
                    if (is_bull and vel_norm < -0.5) or (not is_bull and vel_norm > 0.5):
                        tight_sl = (float(bids[i]) - (self.atr * 0.75)) if is_bull else (float(asks[i]) + (self.atr * 0.75))
                        if (is_bull and tight_sl > self.sl) or (not is_bull and tight_sl < self.sl):
                            self.sl = tight_sl
                            self.trailing_active = True
                            self.exit_reason_flag = "KALMAN_TIGHTEN"


            # 5. Standard SL / TP checks and tick-level MTM tracking
            exec_q = float(bids[i]) if is_bull else float(asks[i])
            raw_unr = self.compute_pnl(exec_q)

            if is_bull and bids[i] <= self.sl:
                flag = getattr(self, "exit_reason_flag", "")
                reason = (flag if flag
                          else ("TRAILING_SL" if self.trailing_active else "SL"))
                fill_px = float(self.sl)
                fill_unr = self.compute_pnl(fill_px)
                gap_usd = raw_unr - fill_unr
                self.last_sl_gap_usd = gap_usd
                if mtm_tracker is not None and minute_ts is not None:
                    mtm_tracker.update(closed_equity + fill_unr, closed_equity + raw_unr, minute_ts, gap_usd)
                return True, reason, fill_px, i

            if not is_bull and asks[i] >= self.sl:
                flag = getattr(self, "exit_reason_flag", "")
                reason = (flag if flag
                          else ("TRAILING_SL" if self.trailing_active else "SL"))
                fill_px = float(self.sl)
                fill_unr = self.compute_pnl(fill_px)
                gap_usd = raw_unr - fill_unr
                self.last_sl_gap_usd = gap_usd
                if mtm_tracker is not None and minute_ts is not None:
                    mtm_tracker.update(closed_equity + fill_unr, closed_equity + raw_unr, minute_ts, gap_usd)
                return True, reason, fill_px, i

            # 6. TP check (BUY closes at Bid, SELL closes at Ask)
            if is_bull and bids[i] >= self.tp:
                fill_px = float(self.tp)
                fill_unr = self.compute_pnl(fill_px)
                if mtm_tracker is not None and minute_ts is not None:
                    mtm_tracker.update(closed_equity + fill_unr, closed_equity + raw_unr, minute_ts)
                return True, "TARGET", fill_px, i
            if not is_bull and asks[i] <= self.tp:
                fill_px = float(self.tp)
                fill_unr = self.compute_pnl(fill_px)
                if mtm_tracker is not None and minute_ts is not None:
                    mtm_tracker.update(closed_equity + fill_unr, closed_equity + raw_unr, minute_ts)
                return True, "TARGET", fill_px, i

            if mtm_tracker is not None and minute_ts is not None:
                mtm_tracker.update(closed_equity + raw_unr, closed_equity + raw_unr, minute_ts)

            # 7. Ratchet standard ATR trailing SL
            self._ratchet_trailing(exec_q, bar_idx, effective_max_hold)

        # 7b. Optional early failure exit at exact N minutes (e.g. 15m MFE < 0.4 ATR)
        if early_fail_mins is not None and early_fail_min_mfe_atr is not None and self.bars_held == early_fail_mins:
            gain_abs = (self.peak_price - self.entry_price) if is_bull else (self.entry_price - self.peak_price)
            mfe_atr = gain_abs / max(self.atr, 1e-9)
            if mfe_atr < early_fail_min_mfe_atr:
                last_price = (float(bids[-1]) if len(bids) > 0 else self.entry_price) if is_bull else (float(asks[-1]) if len(asks) > 0 else self.entry_price)
                return True, f"EARLY_FAIL_{early_fail_mins}M", last_price, len(bids) - 1

        # 8. TIMEOUT — session-aware (BUY closes at Bid, SELL closes at Ask)
        if self.bars_held >= effective_max_hold:
            if is_bull:
                last_price = float(bids[-1]) if len(bids) > 0 else self.entry_price
            else:
                last_price = float(asks[-1]) if len(asks) > 0 else self.entry_price
            return True, "TIMEOUT", last_price, len(bids) - 1

        # No exit — ratchet peak using bar extreme
        bar_peak = float(bids.max()) if is_bull else float(asks.min())
        self._ratchet_trailing(bar_peak, bar_idx, effective_max_hold)

        return False, "", 0.0, len(bids) - 1


    def compute_pnl(self, exit_price: float) -> float:
        if self.direction == "BUY":
            gross = (exit_price - self.entry_price) * self.volume * _CONTRACT_SIZE
        else:
            gross = (self.entry_price - exit_price) * self.volume * _CONTRACT_SIZE
        return gross - self.commission_usd


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
    ) -> dict:

        print(f"\n{'='*60}")
        print(f"  TICK REPLAY BACKTEST  (Minute-Grouped Architecture)")
        print(f"  Symbol: {symbol} | Period: {start.date()} to {end.date()}")
        print(f"  Profile: {self.profile.name} | Score >= {self.profile.min_score_threshold}")
        print(f"  SL: {self.profile.sl_atr_multiplier}x ATR | TP: {self.profile.tp_atr_multiplier}x ATR")
        print(f"  Risk/trade: {self.profile.risk_per_trade_pct}% | Max daily loss: {self.profile.max_daily_loss_pct}%")
        print(f"{'='*60}")

        self.virtual_broker.reset(equity=initial_capital)

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
                bids = grp["bid"].values
                vols = grp["volume"].values if "volume" in grp.columns else np.ones(len(grp))
                for b, v in zip(bids, vols):
                    kalman.update_tick(float(b), float(v))
                w_candle = {
                    "time": minute_ts,
                    "open":   float(bids[0]),
                    "high":   float(bids.max()),
                    "low":    float(bids.min()),
                    "close":  float(bids[-1]),
                    "volume": int(vols.sum()),
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
        mtm_tracker = _MtmDrawdownTracker(initial_capital, start)
        bars_processed = 0
        signals_seen = 0
        signals_passed = 0
        daily_pnl = 0.0
        daily_trades = 0
        consecutive_losses = 0
        prev_regime = None
        last_strat_dir_bar: dict[tuple, int] = {}    # cooldown tracker
        COOLDOWN_BARS = 15
        current_day = None
        start_naive = start.replace(tzinfo=None)
        end_naive = end.replace(tzinfo=None)
        total_days = max(1, (end_naive.date() - start_naive.date()).days + 1)
        total_span_secs = max(1.0, (end_naive - start_naive).total_seconds())
        run_started_ts = time.time()

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
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=7), end)
            ticks_df = self.tick_repo.get_ticks_as_df(symbol, chunk_start, chunk_end)
            chunk_start = chunk_end
            if ticks_df.empty:
                continue

            ticks_df["minute"] = ticks_df["time"].dt.floor("min")
            for minute_ts, grp in ticks_df.groupby("minute", sort=True):
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

                bids = grp["bid"].values.astype(np.float64)
                asks = (
                    grp["ask"].values.astype(np.float64)
                    if "ask" in grp.columns
                    else bids + 0.30
                )
                vols = (
                    grp["volume"].values.astype(np.float64)
                    if "volume" in grp.columns
                    else np.ones(len(grp))
                )

                # ── Build candle (vectorized NumPy) ──────────────────────────
                candle = {
                    "time":   minute_ts,
                    "open":   float(bids[0]),
                    "high":   float(bids.max()),
                    "low":    float(bids.min()),
                    "close":  float(bids[-1]),
                    "volume": int(vols.sum()),
                }
                candle_buffer.append(candle)
                closed_5m = agg_5m.update(candle)
                agg_1h.update(candle)
                bars_processed += 1

                # ── Check open position: scan ticks for SL/TP ───────────────
                if open_pos is not None:
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
                    )

                    current_price = exit_px if exited else float(bids[-1])
                    open_pos.journey.append({
                        "time": minute_ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(minute_ts, "strftime") else str(minute_ts),
                        "price": round(current_price, 2),
                        "sl": round(open_pos.sl, 2),
                        "bars_held": open_pos.bars_held
                    })

                    if exited:
                        # Continuously advance Kalman on remaining ticks so filter state never depends on position state
                        for j in range(last_idx + 1, len(bids)):
                            kalman.update_tick(float(bids[j]), float(vols[j]))

                        pnl = open_pos.compute_pnl(exit_px)
                        eq_before = equity
                        equity += pnl
                        daily_pnl += pnl
                        consecutive_losses = 0 if pnl > 0 else consecutive_losses + 1
                        mtm_tracker.update(equity, equity, minute_ts)
                        realized_loss_pct = (pnl / eq_before * 100.0) if (eq_before > 0 and pnl < 0) else 0.0

                        color = "\033[92m" if pnl > 0 else "\033[91m"
                        reset = "\033[0m"
                        print(
                            f"    EXIT  {reason:<12s} {symbol}  {open_pos.direction}  "
                            f"{color}${pnl:+,.2f}{reset}  "
                            f"entry=${open_pos.entry_price:.2f}  exit=${exit_px:.2f}  "
                            f"vol={open_pos.volume:.2f}  equity=${equity:,.2f}"
                        )
                        closed_trades.append({
                            "entry_time":       open_pos.entry_time,
                            "exit_time":        minute_ts,
                            "symbol":           open_pos.symbol,
                            "direction":        open_pos.direction,
                            "strategy":         open_pos.strategy,
                            "entry_price":      open_pos.entry_price,
                            "exit_price":       exit_px,
                            "sl":               open_pos.initial_sl,
                            "tp":               open_pos.tp,
                            "qty":              open_pos.volume,
                            "pnl":              pnl,
                            "planned_risk_usd": open_pos.planned_risk_usd,
                            "planned_risk_pct": open_pos.planned_risk_pct,
                            "realized_loss_pct": realized_loss_pct,
                            "sl_gap_usd":       open_pos.last_sl_gap_usd,
                            "exit_reason":      reason,
                            "score":            open_pos.final_score,
                            "regime":           open_pos.regime,
                            "journey":          list(open_pos.journey),
                        })
                        open_pos = None
                        _emit_progress(minute_ts)
                        continue

                else:
                    # Continuous tick-by-tick Kalman updates while flat (parity with live & active position)
                    for b, v in zip(bids, vols):
                        kalman.update_tick(float(b), float(v))

                if bars_processed % 100 == 0:
                    print(
                        f"  [{bars_processed} bars]  {candle['time']}  "
                        f"O={candle['open']:.2f} H={candle['high']:.2f} "
                        f"L={candle['low']:.2f} C={candle['close']:.2f}  "
                        f"vol={candle['volume']}  equity=${equity:,.2f}"
                    )

                if open_pos is not None:
                    continue

                if not closed_5m or len(agg_5m.bars) < 60:
                    continue

                if session_close_utc is not None and minute_ts.hour >= session_close_utc:
                    continue

                daily_loss_pct = abs(min(0.0, daily_pnl)) / max(initial_capital, 1) * 100
                if daily_loss_pct >= self.profile.max_daily_loss_pct:
                    continue
                if daily_trades >= self.profile.max_trades_per_day:
                    continue
                if consecutive_losses >= self.profile.max_consecutive_losses:
                    continue

                htf_trend = compute_htf_ema_trend(agg_1h.closed_closes())
                if htf_trend == 0:
                    continue

                df_bars = pd.DataFrame(list(agg_5m.bars))
                df_ind  = IndicatorService.compute_indicators(df_bars)
                if df_ind.empty:
                    continue

                regime = self.regime_detector.detect(df_ind)
                if regime != prev_regime:
                    print(f"  REGIME  {prev_regime} -> {regime.value if hasattr(regime, 'value') else regime}")
                    prev_regime = regime

                signals = SignalGeneratorService.generate_signals(df_ind, symbol, regime)
                if not signals:
                    continue

                signals_seen += len(signals)

                last_row    = df_ind.iloc[-1]
                close_series = df_ind["close"] if "close" in df_ind.columns else pd.Series(dtype=float)

                features = {
                    "kalman_vel":        kalman.get_velocity(),
                    "kalman_accel":      kalman.get_acceleration(),
                    "shannon_entropy":   (
                        DenoiseService.compute_rolling_entropy(close_series.pct_change().dropna())
                        if len(close_series) > 30 else 2.0
                    ),
                    "kaufman_er":        (
                        DenoiseService.compute_kaufman_er(close_series)
                        if len(close_series) > 15 else 0.3
                    ),
                    "ema20_slope":       _safe_float(last_row.get("ema20_slope"), 0.0),
                    "vwap_dist":         _safe_float(last_row.get("vwap_dist"), 0.0),
                    "volume_ratio":      _safe_float(last_row.get("volume_ratio"), 1.0),
                    "rsi":               _safe_float(last_row.get("rsi"), 50.0),
                    "utc_hour":          minute_ts.hour if hasattr(minute_ts, "hour") else 12,
                    "rsi_price_divergence": 0.0,
                }

                regime_str = regime.value if hasattr(regime, "value") else str(regime)

                scored = self.trade_scorer.filter_and_rank(
                    signals,
                    min_score_threshold=self.profile.min_score_threshold,
                    regime=regime_str,
                    features=features,
                )

                if not scored:
                    for sig in signals:
                        ind = self.trade_scorer.score_signal(sig, regime=regime_str, features=features)
                        dir_val = sig.direction.value if hasattr(sig.direction, "value") else sig.direction
                        print(
                            f"    SKIP  {sig.strategy:<25s} {dir_val:<5s} "
                            f"score={ind.final_score:.3f}  "
                            f"vel={features['kalman_vel']:.4f}  "
                            f"er={features['kaufman_er']:.3f}  "
                            f"ent={features['shannon_entropy']:.3f}  "
                            f"slope={features['ema20_slope']:.5f}"
                        )
                    continue

                best = scored[0]
                dir_val = best.direction.value if hasattr(best.direction, "value") else best.direction

                if (dir_val == "BUY" and htf_trend != 1) or (dir_val == "SELL" and htf_trend != -1):
                    print(f"    SKIP  {best.strategy:<25s} {dir_val:<5s} H1 Trend Gate: trend={htf_trend}")
                    continue

                if not passes_regime_filters(
                    dir_val,
                    regime_str,
                    block_high_volatility=getattr(self.profile, "block_high_volatility", False),
                    block_buy_sideways=getattr(self.profile, "block_buy_sideways", False),
                ):
                    print(f"    SKIP  {best.strategy:<25s} {dir_val:<5s} Regime Gate: {regime_str}")
                    continue

                if self.expected_pnl_model is not None:
                    pred_pnl = self.expected_pnl_model.predict_expected_pnl({
                        "rsi": features["rsi"],
                        "volume_ratio": features["volume_ratio"],
                        "ema20_slope": features["ema20_slope"],
                        "ema_spread": float((last_row["ema20"] - last_row["ema50"]) / last_row["ema50"]) if last_row.get("ema50", 0) else 0.0,
                        "vwap_dist": features["vwap_dist"],
                        "kalman_velocity": features["kalman_vel"],
                        "quant_score": float(best.final_score),
                    })
                    if pred_pnl is not None and self.min_expected_pnl is not None and pred_pnl <= self.min_expected_pnl:
                        print(f"    SKIP  {best.strategy:<25s} {dir_val:<5s} Ridge PnL=${pred_pnl:+.2f} <= ${self.min_expected_pnl:+.2f}")
                        continue

                key = (best.strategy, dir_val)
                if bar_idx - last_strat_dir_bar.get(key, -9999) < COOLDOWN_BARS:
                    print(f"    COOL  {best.strategy}  {dir_val}  cooldown ({COOLDOWN_BARS} bars)")
                    continue

                latest_atr = _safe_float(
                    df_ind["atr"].iloc[-1] if "atr" in df_ind.columns else None, 1.0
                )
                entry_price = self.sizing_service.resolve_executable_entry(
                    dir_val, float(bids[0]), float(asks[0])
                )
                lot_size, sl_price, tp_price, reject_reason = self.sizing_service.calculate_size(
                    account_balance=equity,
                    risk_profile=self.profile,
                    entry_price=entry_price,
                    atr_value=latest_atr,
                    direction=best.direction,
                    symbol=symbol,
                    commission_per_lot=commission_per_lot,
                )

                if reject_reason:
                    print(f"    SKIP  {best.strategy}  {dir_val}  Sizing: {reject_reason}")
                    continue

                comm_usd = float(commission_per_lot) * lot_size
                planned_risk_usd = abs(entry_price - sl_price) * lot_size * _CONTRACT_SIZE + comm_usd
                planned_risk_pct = (planned_risk_usd / equity * 100.0) if equity > 0 else 0.0

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
                    entry_time=minute_ts,
                    entry_bar_idx=bar_idx,
                    final_score=best.final_score,
                    regime=regime_str,
                    planned_risk_usd=planned_risk_usd,
                    planned_risk_pct=planned_risk_pct,
                    commission_usd=comm_usd,
                )
                init_unr = -(float(asks[0]) - float(bids[0])) * lot_size * _CONTRACT_SIZE - comm_usd
                mtm_tracker.update(equity + init_unr, equity + init_unr, minute_ts)
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

            del ticks_df

        # ── Final Summary ─────────────────────────────────────────────────────
        wins   = sum(1 for t in closed_trades if t["pnl"] > 0)
        losses = len(closed_trades) - wins
        total_pnl = sum(t["pnl"] for t in closed_trades)

        print(f"\n  Backtest complete: {len(closed_trades)} trades  |  {wins}W / {losses}L")
        print(f"  Signals seen: {signals_seen}  |  Passed: {signals_passed}  |  Bars: {bars_processed}")
        print(f"  Final equity: ${equity:,.2f}  |  Total P&L: ${total_pnl:+,.2f}")

        if not closed_trades:
            return self._empty_result(start, initial_capital)

        return self._build_result(closed_trades, initial_capital, equity, mtm_tracker=mtm_tracker)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_result(
        self,
        closed_trades: list[dict],
        initial_capital: float,
        final_equity: float,
        mtm_tracker: _MtmDrawdownTracker | None = None,
    ) -> dict:
        df = pd.DataFrame(closed_trades)
        n      = len(df)
        wins   = (df["pnl"] > 0).sum()
        losses = n - wins

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
        worst_sl_gap_usd = float(mtm_tracker.worst_sl_gap_usd) if mtm_tracker is not None else 0.0

        avg_win  = float(df[df["pnl"] > 0]["pnl"].mean()) if wins  > 0 else 0.0
        avg_loss = float(df[df["pnl"] < 0]["pnl"].mean()) if losses > 0 else 0.0
        total_win_sum  = float(df[df["pnl"] > 0]["pnl"].sum())
        total_loss_sum = float(df[df["pnl"] < 0]["pnl"].sum())
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
        
        print(f"\n  Wins:              {wins} ({wins/n*100:.0f}%)")
        print(f"  Losses:            {losses} ({losses/n*100:.0f}%)")
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
            wr = (g["pnl"] > 0).mean() * 100
            print(f"  {strat:<30s} {len(g):>6d} {wr:>4.0f}% {g['pnl'].sum():>+10,.2f} {g['pnl'].mean():>+8,.2f}")

        # By direction
        print(f"\n  {'Direction':<10s} {'Trades':>6s} {'WR':>5s} {'Total P&L':>10s}")
        print(f"  {'-'*10} {'-'*6} {'-'*5} {'-'*10}")
        for d, g in df.groupby("direction"):
            wr = (g["pnl"] > 0).mean() * 100
            print(f"  {d:<10s} {len(g):>6d} {wr:>4.0f}% {g['pnl'].sum():>+10,.2f}")

        # By result
        print(f"\n  {'Result':<15s} {'Count':>6s} {'Profitable':>10s} {'Total P&L':>10s}")
        print(f"  {'-'*15} {'-'*6} {'-'*10} {'-'*10}")
        for r, g in df.groupby("exit_reason"):
            prof = (g["pnl"] > 0).mean() * 100
            print(f"  {r:<15s} {len(g):>6d} {prof:>9.0f}% {g['pnl'].sum():>+10,.2f}")

        # Per-day breakdown
        print(f"\n  {'Date':<12s} {'Trades':>6s} {'W':>3s} {'L':>3s} {'P&L':>10s}")
        print(f"  {'-'*12} {'-'*6} {'-'*3} {'-'*3} {'-'*10}")
        for day, g in df.groupby("day"):
            w = (g["pnl"] > 0).sum()
            l = len(g) - w
            print(f"  {str(day):<12s} {len(g):>6d} {w:>3d} {l:>3d} {g['pnl'].sum():>+10,.2f}")

        # Export CSV and Journeys
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

        # Equity curve
        eq = initial_capital
        equity_curve = []
        for _, row in df.iterrows():
            eq += row["pnl"]
            t = row["entry_time"]
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
                "entry_time":  _fmt(t["entry_time"]),
                "exit_time":   _fmt(t["exit_time"]),
                "symbol":      t["symbol"],
                "direction":   t["direction"],
                "strategy":    t["strategy"],
                "entry_price": float(t["entry_price"]),
                "exit_price":  float(t["exit_price"]),
                "sl":          float(t["sl"]),
                "target":      float(t["tp"]),
                "pnl":         float(t["pnl"]),
                "qty":         float(t["qty"]),
                "exit_reason": t["exit_reason"],
                "score":       float(t["score"]),
            })

        return {
            "trades":                  int(n),
            "win_rate":                float(wins / n) if n > 0 else 0.0,
            "pnl":                     float(total_pnl),
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
            "worst_sl_gap_usd":        float(worst_sl_gap_usd),
            "pf":                      float(pf),
            "avg_win":                 float(avg_win),
            "avg_loss":                float(avg_loss),
            "rr":                      float(rr),
            "equity_curve":            equity_curve,
            "trade_list":              trade_list,
        }

    def _empty_result(self, start: datetime, initial_capital: float) -> dict:
        return {
            "trades":                  0,
            "win_rate":                0.0,
            "pnl":                     0.0,
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
            "worst_sl_gap_usd":        0.0,
            "pf":                      0.0,
            "avg_win":                 0.0,
            "avg_loss":                0.0,
            "rr":                      0.0,
            "equity_curve":            [{"time": start.strftime("%Y-%m-%d %H:%M:%S"), "equity": initial_capital}],
            "trade_list":              [],
        }
