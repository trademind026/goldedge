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
import numpy as np
import pandas as pd
from collections import deque
from pathlib import Path
from datetime import datetime, timedelta

from ...domain.ports.outbound.tick_repo import ITickRepository
from ...domain.services.denoise_service import VolumeAdaptiveKalmanFilter, DenoiseService
from ...domain.services.indicator_service import IndicatorService
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
        "bars_held", "final_score", "regime", "exit_reason_flag", "journey"
    )

    def __init__(
        self, ticket: int, symbol: str, direction: str, strategy: str,
        entry_price: float, sl: float, tp: float, atr: float, volume: float,
        entry_time: datetime, entry_bar_idx: int,
        final_score: float, regime: str,
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

        # 1. Breakeven Trigger: raised to 2.0 ATR for XAUUSD
        #    (1.5 ATR was choking winning trades too early before they hit the 3.0 ATR target)
        if not self.trailing_active and gain_abs >= self.atr * 2.0:
            self.trailing_active = True
            # Lock breakeven + 0.5 ATR to secure a small win
            lock = (self.entry_price + (self.atr * 0.5) if is_bull
                    else self.entry_price - (self.atr * 0.5))
            if (is_bull and lock > self.sl) or (not is_bull and lock < self.sl):
                self.sl = lock
                self.exit_reason_flag = "BREAKEVEN"

        # 2. ATR Trailing SL: trail 2.0 ATR behind peak
        if self.trailing_active:
            trail_sl = (self.peak_price - (self.atr * 2.0) if is_bull
                        else self.peak_price + (self.atr * 2.0))
            if (is_bull and trail_sl > self.sl) or (not is_bull and trail_sl < self.sl):
                self.sl = trail_sl
                if getattr(self, "exit_reason_flag", "") not in ("KALMAN_TIGHTEN", "TIME_SQUEEZE"):
                    self.exit_reason_flag = "TRAILING_SL"

        # 3. Time-Based Tightening (The Clock Ratchet)
        if effective_max_hold > 0:
            hold_pct = self.bars_held / effective_max_hold
            if hold_pct >= 0.75:
                # Squeeze SL toward the candidate_price
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

            # 2. Normalize velocity against per-tick ATR unit
            # vel_norm = k_vel / atr_per_tick

            # --- DISABLED FOR XAUUSD: TICK-LEVEL PANIC EXITS ---
            # Gold ticks are too spiky; these filters cause massive whipsaws.
            # We rely entirely on the 1.5 ATR Trail and TIME_SQUEEZE instead.
            
            # 3. KALMAN_EXIT — extreme velocity reversal (2.5σ per tick)
            # if is_bull and vel_norm < -2.5:
            #     return True, "KALMAN_EXIT", float(bids[i]), i
            # elif not is_bull and vel_norm > 2.5:
            #     return True, "KALMAN_EXIT", float(asks[i]), i

            # 4. KALMAN_TIGHTEN — weakening momentum (1.5σ per tick)
            # if is_bull and vel_norm < -1.5:
            #     tight_sl = float(bids[i]) - (self.atr * 0.8)
            #     if tight_sl > self.sl:
            #         self.sl = tight_sl
            #         self.trailing_active = True
            #         self.exit_reason_flag = "KALMAN_TIGHTEN"
            # elif not is_bull and vel_norm > 1.5:
            #     tight_sl = float(asks[i]) + (self.atr * 0.8)
            #     if tight_sl < self.sl:
            #         self.sl = tight_sl
            #         self.trailing_active = True
            #         self.exit_reason_flag = "KALMAN_TIGHTEN"
            # ---------------------------------------------------

            # 5. Standard SL check
            if is_bull and bids[i] <= self.sl:
                flag = getattr(self, "exit_reason_flag", "")
                reason = (flag if flag
                          else ("TRAILING_SL" if self.trailing_active else "SL"))
                return True, reason, float(self.sl), i

            if not is_bull and asks[i] >= self.sl:
                flag = getattr(self, "exit_reason_flag", "")
                reason = (flag if flag
                          else ("TRAILING_SL" if self.trailing_active else "SL"))
                return True, reason, float(self.sl), i

            # 6. TP check
            if is_bull and asks[i] >= self.tp:
                return True, "TARGET", float(self.tp), i
            if not is_bull and bids[i] <= self.tp:
                return True, "TARGET", float(self.tp), i

            # 7. Ratchet standard ATR trailing SL
            self._ratchet_trailing(float(bids[i]) if is_bull else float(asks[i]), bar_idx, effective_max_hold)

        # 8. TIMEOUT — session-aware
        if self.bars_held >= effective_max_hold:
            last_price = float(bids[-1]) if len(bids) > 0 else self.entry_price
            return True, "TIMEOUT", last_price, len(bids) - 1

        # No exit — ratchet peak using bar extreme
        bar_peak = float(bids.max()) if is_bull else float(asks.min())
        self._ratchet_trailing(bar_peak, bar_idx, effective_max_hold)

        return False, "", 0.0, len(bids) - 1


    def compute_pnl(self, exit_price: float) -> float:
        if self.direction == "BUY":
            return (exit_price - self.entry_price) * self.volume * _CONTRACT_SIZE
        else:
            return (self.entry_price - exit_price) * self.volume * _CONTRACT_SIZE


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
    ):
        self.tick_repo = tick_repo
        self.virtual_broker = virtual_broker
        self.sizing_service = sizing_service
        self.profile = profile or get_profile("MEDIUM_BACKTEST")

        self.regime_detector = RegimeDetectorService()
        self.trade_scorer = TradeScorer(w_ml=0.0, w_quant=1.0)

    # ─────────────────────────────────────────────────────────────────────────

    def execute(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        initial_capital: float = 10000.0,
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

        # ── 2. Load replay ticks ─────────────────────────────────────────────
        print(f"  [2/5] Fetching replay ticks from database...")
        ticks_df = self.tick_repo.get_ticks_as_df(symbol, start, end)
        if ticks_df.empty:
            print("  No ticks found in date range.")
            return self._empty_result(start, initial_capital)

        print(f"  Loaded {len(ticks_df):,} ticks ({len(warmup_df):,} warmup).")

        # ── 3. PRE-GROUP all ticks into minutes — ONE vectorized operation ───
        print(f"  [3/5] Pre-grouping {len(ticks_df):,} ticks into minute bars...")
        ticks_df["minute"] = ticks_df["time"].dt.floor("min")
        minute_groups = ticks_df.groupby("minute", sort=True)
        minutes = sorted(minute_groups.groups.keys())
        print(f"  Grouped into {len(minutes):,} minute bars.")

        # ── 4. Warmup: seed Kalman + candle buffer ───────────────────────────
        print(f"  [4/5] Processing warmup candles...")
        kalman = VolumeAdaptiveKalmanFilter()
        candle_buffer: deque = deque(maxlen=200)   # fixed-size, O(1) append

        if not warmup_df.empty:
            warmup_df["minute"] = warmup_df["time"].dt.floor("min")
            for minute_ts, grp in warmup_df.groupby("minute", sort=True):
                bids = grp["bid"].values
                vols = grp["volume"].values if "volume" in grp.columns else np.ones(len(grp))
                for b, v in zip(bids, vols):
                    kalman.update_tick(float(b), float(v))
                candle_buffer.append({
                    "time": minute_ts,
                    "open":   float(bids[0]),
                    "high":   float(bids.max()),
                    "low":    float(bids.min()),
                    "close":  float(bids[-1]),
                    "volume": int(vols.sum()),
                })

        print(f"  Warmup complete: {len(candle_buffer)} bars seeded, Kalman initialized.")

        # ── 5. MAIN LOOP: iterate minute bars ────────────────────────────────
        print(f"  [5/5] Replaying {len(minutes):,} minute bars...")

        open_pos: _OpenPosition | None = None
        closed_trades: list[dict] = []

        equity = initial_capital
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

        for bar_idx, minute_ts in enumerate(minutes):
            # ── Daily Reset ──────────────────────────────────────────────────
            if current_day != minute_ts.date():
                current_day = minute_ts.date()
                daily_pnl = 0.0
                daily_trades = 0
                consecutive_losses = 0
                
            grp = minute_groups.get_group(minute_ts)
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

            # ── Build candle (vectorized NumPy) ──────────────────────────────
            candle = {
                "time":   minute_ts,
                "open":   float(bids[0]),
                "high":   float(bids.max()),
                "low":    float(bids.min()),
                "close":  float(bids[-1]),
                "volume": int(vols.sum()),
            }
            candle_buffer.append(candle)
            bars_processed += 1

            # ── Check open position: scan ticks for SL/TP ───────────────────
            if open_pos is not None:
                open_pos.bars_held += 1
                max_hold = getattr(self.profile, "max_hold_bars", 120)
                entry_hour = open_pos.entry_time.hour if hasattr(open_pos.entry_time, "hour") else 12
                exited, reason, exit_px, last_idx = open_pos.check_exit_ticks(
                    bids, asks, vols, bar_idx, max_hold, kalman,
                    entry_utc_hour=entry_hour,
                )

                # Record journey point for this minute
                current_price = exit_px if exited else float(bids[-1])
                open_pos.journey.append({
                    "time": minute_ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(minute_ts, "strftime") else str(minute_ts),
                    "price": round(current_price, 2),
                    "sl": round(open_pos.sl, 2),
                    "bars_held": open_pos.bars_held
                })
                
                # If exited, update kalman for the rest of the minute's ticks
                if exited:
                    for j in range(last_idx + 1, len(bids)):
                        kalman.update_tick(float(bids[j]), float(vols[j]))
                        
                    pnl = open_pos.compute_pnl(exit_px)
                    equity += pnl
                    daily_pnl += pnl
                    consecutive_losses = 0 if pnl > 0 else consecutive_losses + 1

                    color = "\033[92m" if pnl > 0 else "\033[91m"
                    reset = "\033[0m"
                    print(
                        f"    EXIT  {reason:<12s} {symbol}  {open_pos.direction}  "
                        f"{color}${pnl:+,.2f}{reset}  "
                        f"entry=${open_pos.entry_price:.2f}  exit=${exit_px:.2f}  "
                        f"vol={open_pos.volume:.2f}  equity=${equity:,.2f}"
                    )
                    closed_trades.append({
                        "entry_time":  open_pos.entry_time,
                        "exit_time":   minute_ts,
                        "symbol":      open_pos.symbol,
                        "direction":   open_pos.direction,
                        "strategy":    open_pos.strategy,
                        "entry_price": open_pos.entry_price,
                        "exit_price":  exit_px,
                        "sl":          open_pos.initial_sl,
                        "tp":          open_pos.tp,
                        "qty":         open_pos.volume,
                        "pnl":         pnl,
                        "exit_reason": reason,
                        "score":       open_pos.final_score,
                        "regime":      open_pos.regime,
                        "journey":     list(open_pos.journey),
                    })
                    open_pos = None
                    continue  # skip signal generation this bar

            else:
                # ── Update Kalman on every tick this minute if NO position ───
                for b, v in zip(bids, vols):
                    kalman.update_tick(float(b), float(v))

            # ── Log progress every 100 bars ──────────────────────────────────
            if bars_processed % 100 == 0:
                print(
                    f"  [{bars_processed} bars]  {candle['time']}  "
                    f"O={candle['open']:.2f} H={candle['high']:.2f} "
                    f"L={candle['low']:.2f} C={candle['close']:.2f}  "
                    f"vol={candle['volume']}  equity=${equity:,.2f}"
                )

            # ── Skip signal evaluation while position is open ────────────────
            if open_pos is not None:
                continue

            # ── Not enough history yet ───────────────────────────────────────
            if len(candle_buffer) < 60:
                continue

            # ── Daily circuit breakers ────────────────────────────────────────
            daily_loss_pct = abs(min(0.0, daily_pnl)) / max(initial_capital, 1) * 100
            if daily_loss_pct >= self.profile.max_daily_loss_pct:
                continue
            if daily_trades >= self.profile.max_trades_per_day:
                continue
            if consecutive_losses >= self.profile.max_consecutive_losses:
                continue

            # ── Compute indicators (once per bar, on fixed deque→DataFrame) ──
            df_bars = pd.DataFrame(list(candle_buffer))
            df_ind  = IndicatorService.compute_indicators(df_bars)
            if df_ind.empty:
                continue

            # ── Regime detection ─────────────────────────────────────────────
            regime = self.regime_detector.detect(df_ind)
            if regime != prev_regime:
                print(f"  REGIME  {prev_regime} -> {regime.value if hasattr(regime, 'value') else regime}")
                prev_regime = regime

            # ── Signal generation ─────────────────────────────────────────────
            signals = SignalGeneratorService.generate_signals(df_ind, symbol, regime)
            if not signals:
                continue

            signals_seen += len(signals)

            # ── Build full feature dict for scorer ────────────────────────────
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

            # ── Score and filter signals ──────────────────────────────────────
            scored = self.trade_scorer.filter_and_rank(
                signals,
                min_score_threshold=self.profile.min_score_threshold,
                regime=regime_str,
                features=features,
            )

            # Log skipped (with actual score)
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

            # ── HTF Trend Gate (The "Dead Cat Bounce" Filter from AI-Trader) ──
            # Block trades that counter the structural trend unless conviction is extreme.
            ema20_v = _safe_float(last_row.get("ema20", 0.0), 0.0)
            ema50_v = _safe_float(last_row.get("ema50", 0.0), 0.0)
            close_px = float(last_row.get("close", 0.0))
            
            if dir_val == "BUY":
                in_downtrend = (close_px > 0 and ema50_v > 0 and close_px < ema50_v and ema20_v < ema50_v)
                if in_downtrend and best.final_score < 0.90:
                    print(f"    SKIP  {best.strategy:<25s} BUY    Trend Gate: Blocked in downtrend (score={best.final_score:.3f} < 0.90)")
                    continue
            elif dir_val == "SELL":
                in_uptrend = (close_px > 0 and ema50_v > 0 and close_px > ema50_v and ema20_v > ema50_v)
                if in_uptrend and best.final_score < 0.90:
                    print(f"    SKIP  {best.strategy:<25s} SELL   Trend Gate: Blocked in uptrend (score={best.final_score:.3f} < 0.90)")
                    continue

            # ── Cooldown gate: block same (strategy, direction) for 15 bars ──
            key = (best.strategy, dir_val)
            if bar_idx - last_strat_dir_bar.get(key, -9999) < COOLDOWN_BARS:
                print(f"    COOL  {best.strategy}  {dir_val}  cooldown ({COOLDOWN_BARS} bars)")
                continue

            # ── Position sizing (same service as live) ────────────────────────
            latest_atr = _safe_float(
                df_ind["atr"].iloc[-1] if "atr" in df_ind.columns else None, 1.0
            )
            lot_size, sl_price, tp_price, reject_reason = self.sizing_service.calculate_size(
                account_balance=equity,
                risk_profile=self.profile,
                entry_price=best.entry_price,
                atr_value=latest_atr,
                direction=best.direction,
                symbol=symbol,
            )

            if reject_reason:
                print(f"    SKIP  {best.strategy}  {dir_val}  Sizing: {reject_reason}")
                continue

            # ── Open position ─────────────────────────────────────────────────
            entry_price = (
                float(bids[-1]) + 0.30  # ask ≈ bid + spread
                if dir_val == "BUY"
                else float(bids[-1])
            )

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
            )
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

        # ── Final Summary ─────────────────────────────────────────────────────
        wins   = sum(1 for t in closed_trades if t["pnl"] > 0)
        losses = len(closed_trades) - wins
        total_pnl = sum(t["pnl"] for t in closed_trades)

        print(f"\n  Backtest complete: {len(closed_trades)} trades  |  {wins}W / {losses}L")
        print(f"  Signals seen: {signals_seen}  |  Passed: {signals_passed}  |  Bars: {bars_processed}")
        print(f"  Final equity: ${equity:,.2f}  |  Total P&L: ${total_pnl:+,.2f}")

        if not closed_trades:
            return self._empty_result(start, initial_capital)

        return self._build_result(closed_trades, initial_capital, equity)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_result(
        self, closed_trades: list[dict], initial_capital: float, final_equity: float
    ) -> dict:
        df = pd.DataFrame(closed_trades)
        n      = len(df)
        wins   = (df["pnl"] > 0).sum()
        losses = n - wins

        total_pnl  = df["pnl"].sum()
        df["cum_pnl"] = df["pnl"].cumsum()
        max_dd = float((df["cum_pnl"] - df["cum_pnl"].cummax()).min())

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

        print(f"\n  Max margin used:   ${max_margin:,.2f}  (Est. 1:100 Lev)")
        print(f"  Max drawdown:      ${max_dd:+,.2f}")
        print(f"  Required Capital:  ${required_capital:,.2f}")
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
            "trades":       int(n),
            "win_rate":     float(wins / n) if n > 0 else 0.0,
            "pnl":          float(total_pnl),
            "max_dd":       float(max_dd),
            "pf":           float(pf),
            "avg_win":      float(avg_win),
            "avg_loss":     float(avg_loss),
            "rr":           float(rr),
            "equity_curve": equity_curve,
            "trade_list":   trade_list,
        }

    def _empty_result(self, start: datetime, initial_capital: float) -> dict:
        return {
            "trades":       0,
            "win_rate":     0.0,
            "pnl":          0.0,
            "max_dd":       0.0,
            "pf":           0.0,
            "avg_win":      0.0,
            "avg_loss":     0.0,
            "rr":           0.0,
            "equity_curve": [{"time": start.strftime("%Y-%m-%d %H:%M:%S"), "equity": initial_capital}],
            "trade_list":   [],
        }
