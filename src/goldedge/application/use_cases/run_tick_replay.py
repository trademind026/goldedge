import logging
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone
import pandas as pd
import numpy as np

from ...domain.models.forex_open_trade import (
    ForexOpenTrade,
    BacktestEngineState,
    HALF_SPREAD_PCT,
    check_exit_tick as check_subbar_exit_tick
)
from ...domain.models.pip_calculator import PipCalculator
from ...domain.models.signal import Signal, SignalDirection
from ...domain.services.forex_adaptive_sizing import (
    forex_adaptive_position_size,
    calculate_forex_position_size
)
from ...domain.services.indicator_service import IndicatorService
from ...domain.services.regime_detector import RegimeDetectorService
from ...domain.services.signal_generator import SignalGeneratorService
from ...domain.services.trade_scorer import TradeScorer
from ...adapters.secondary.features.forex_kalman_filter import StreamingForexKalmanFilter
from ...adapters.secondary.features.microstructure_feature_repo import compute_forex_micro_features

logger = logging.getLogger("tick_replay_engine")


def pre_calibrate_kalman(kalman_filter: Any, warmup_candles: Any) -> Any:
    """
    Step 2: Warm-Up Kalman Pre-Calibration (Phase 1).
    Runs pre-session candles through Kalman filter to converge state [x_t, v_t]
    and covariance matrix P to steady-state equilibrium before Bar 1.
    """
    if warmup_candles is None:
        return kalman_filter

    if hasattr(warmup_candles, "iterrows"):
        for _, candle in warmup_candles.iterrows():
            high = float(candle.get("high", candle.get("close", 0.0)))
            low = float(candle.get("low", candle.get("close", 0.0)))
            mid_price = (high + low) / 2.0 if (high > 0 and low > 0) else float(candle.get("close", 0.0))
            vol = float(candle.get("volume", candle.get("tick_volume", 10.0)))
            kalman_filter.update(mid_price, volume=vol)
    elif isinstance(warmup_candles, list):
        for candle in warmup_candles:
            high = float(candle.get("high", candle.get("close", 0.0)))
            low = float(candle.get("low", candle.get("close", 0.0)))
            mid_price = (high + low) / 2.0 if (high > 0 and low > 0) else float(candle.get("close", 0.0))
            vol = float(candle.get("volume", 10.0))
            kalman_filter.update(mid_price, volume=vol)

    return kalman_filter


def compute_rolling_shannon_entropy(closes: np.ndarray, window: int = 60, num_bins: int = 10) -> float:
    """
    Step 4: Computes normalized Shannon entropy over rolling log-returns.
    Range: [0.0 = Pure Trend, 1.0 = Pure Random Walk / Chop].
    Gating threshold: H_crit = 0.65 -> If entropy > 0.65, block trend entries.
    """
    if len(closes) < window + 1:
        return 0.50  # Default neutral entropy

    recent_closes = closes[-window - 1:]
    # Protect against non-positive prices
    if np.any(recent_closes <= 0):
        return 0.50

    log_returns = np.diff(np.log(recent_closes))
    if len(log_returns) < 5 or np.all(log_returns == 0):
        return 0.0

    # Calibrated static bin edges for 1m bar returns (10 bins)
    # Distinguishes structured directional movement (low entropy) from random chop (high entropy)
    bins = np.array([-np.inf, -0.00050, -0.00020, -0.00010, -0.00005, 0.0, 0.00005, 0.00010, 0.00020, 0.00050, np.inf])
    counts, _ = np.histogram(log_returns, bins=bins)
    total_counts = np.sum(counts)
    if total_counts <= 0:
        return 0.50

    probs = counts / total_counts
    probs = probs[probs > 0]
    n_bins = len(bins) - 1
    entropy = -np.sum(probs * np.log2(probs)) / np.log2(n_bins)
    return float(np.clip(entropy, 0.0, 1.0))


def is_forex_rollover_window(dt: datetime) -> bool:
    """
    Step 5: Forex Rollover Session Filter (Phase 5).
    Blocks new trade generation during 21:00 - 22:15 UTC (17:00 - 18:15 EST).
    """
    if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
        utc_dt = dt.astimezone(timezone.utc)
    else:
        utc_dt = dt
    hour = utc_dt.hour
    minute = utc_dt.minute

    if hour == 21 or (hour == 22 and minute <= 15):
        return True
    return False


def generate_advanced_tearsheet(state: BacktestEngineState, output_csv_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Step 7: Advanced Tearsheet & Full Audit Trail (Phase 8).
    Computes Calmar ratio, Expectancy, Win Rate, Profit Factor, MFE/MAE distribution,
    and exports the complete tick audit trail CSV.
    """
    trades = state.closed_trades
    if not trades:
        metrics = {
            "total_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "expectancy_usd": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "net_pnl": 0.0,
            "final_equity": state.equity,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
            "max_drawdown_bars": 0,
            "calmar_ratio": 0.0,
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "avg_mae_pips": 0.0,
            "avg_mfe_pips": 0.0,
        }
        if output_csv_path:
            pd.DataFrame(columns=[
                "trade_id", "symbol", "direction", "open_time", "exit_time",
                "entry_price", "exit_price", "lots", "units", "pnl_usd",
                "pnl_pips", "mae_pips", "mfe_pips", "peak_pnl_pips",
                "ticks_held", "bars_held", "bars_since_peak", "exit_reason",
                "strategy_name", "regime_at_entry"
            ]).to_csv(output_csv_path, index=False)
            logger.info(f"[+] Empty trade audit log saved to {output_csv_path}")
        return metrics

    df = pd.DataFrame(trades)

    # 1. Core Metrics
    wins = df[df['pnl_usd'] > 0]
    losses = df[df['pnl_usd'] <= 0]
    win_rate = len(wins) / len(df) if len(df) > 0 else 0.0

    gross_profit = float(wins['pnl_usd'].sum()) if len(wins) > 0 else 0.0
    gross_loss = float(abs(losses['pnl_usd'].sum())) if len(losses) > 0 else 0.0
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (float('inf') if gross_profit > 0 else 0.0)

    avg_win = float(wins['pnl_usd'].mean()) if len(wins) > 0 else 0.0
    avg_loss = float(abs(losses['pnl_usd'].mean())) if len(losses) > 0 else 0.0
    expectancy_usd = (win_rate * avg_win) - ((1.0 - win_rate) * avg_loss)

    # 2. Calmar Ratio
    total_net_return_pct = (state.equity - state.initial_balance) / state.initial_balance
    calmar_ratio = round(total_net_return_pct / state.max_drawdown_pct, 2) if state.max_drawdown_pct > 0 else 0.0

    # 3. Sharpe & Sortino Ratios
    pnls = df['pnl_usd'].to_numpy(dtype=float)
    returns = pnls / state.initial_balance
    mean_ret = float(np.mean(returns)) if len(returns) > 0 else 0.0
    std_ret = float(np.std(returns)) if len(returns) > 1 else 1e-6
    downside_returns = returns[returns < 0]
    downside_std = float(np.sqrt(np.mean(downside_returns ** 2))) if len(downside_returns) > 0 else 1e-6
    annual_factor = np.sqrt(500)
    sharpe = round((mean_ret / std_ret) * annual_factor, 2) if std_ret > 0 else 0.0
    sortino = round((mean_ret / downside_std) * annual_factor, 2) if downside_std > 0 else 0.0

    avg_mae = float(df['mae_pips'].mean()) if 'mae_pips' in df.columns else 0.0
    avg_mfe = float(df['mfe_pips'].mean()) if 'mfe_pips' in df.columns else 0.0

    metrics = {
        "total_trades": len(df),
        "win_rate": round(win_rate * 100.0, 2),
        "profit_factor": profit_factor,
        "expectancy_usd": round(expectancy_usd, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl": round(float(np.sum(pnls)), 2),
        "final_equity": round(state.equity, 2),
        "max_drawdown": round(state.current_drawdown_usd, 2),
        "max_drawdown_pct": round(state.max_drawdown_pct * 100.0, 2),
        "max_drawdown_bars": 0,
        "calmar_ratio": calmar_ratio,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "avg_mae_pips": round(avg_mae, 2),
        "avg_mfe_pips": round(avg_mfe, 2),
    }

    if output_csv_path:
        df.to_csv(output_csv_path, index=False)
        logger.info(f"[+] Full trade audit log saved to {output_csv_path}")

    return metrics



def check_forex_micro_confirmation(
    minute_ticks: pd.DataFrame,
    direction: str,
    predictor: Any = None
) -> Tuple[bool, float]:
    """
    Verifies order book imbalance and tick momentum on the current minute's ticks
    before entering a trade.
    Returns: (is_confirmed, flow_score)
    """
    if len(minute_ticks) < 5:
        return True, 0.60  # Insufficient ticks; neutral flow

    try:
        micro = compute_forex_micro_features(minute_ticks, window_seconds=10)
        if micro.empty:
            return True, 0.60

        last = micro.iloc[-1]

        # 1. Micro ML Prediction (if available)
        if predictor is not None and getattr(predictor, "is_loaded", False):
            prob = predictor.predict_micro(last.to_dict())
            if prob is not None:
                return prob > 0.40, float(prob)

        # 2. Raw Tick Momentum
        momentum = float(last.get("tick_momentum", 0.0))
        burst_ratio = float(last.get("burst_ratio", 1.0))

        flow_score = 0.50 + np.clip(momentum * 0.05, -0.40, 0.40)

        if direction.upper() in ("CALL", "BUY"):
            confirmed = momentum > -0.10  # Not strongly selling
        else:
            confirmed = momentum < 0.10   # Not strongly buying

        return confirmed, float(flow_score)
    except Exception:
        return True, 0.60


class ForexTickReplayEngine:
    """
    Institutional Tick-Level Replay Backtest Engine.
    Executes historical simulations with zero-lookahead, sub-bar intra-tick stepping,
    half-spread bid/ask slippage modeling, multi-tier circuit breakers, and comprehensive tearsheets.
    """

    def __init__(
        self,
        symbol: str = "EURUSD",
        initial_equity: float = 100000.0,
        daily_loss_limit: float = -1500.0,
        consecutive_loss_trigger: int = 2,
        consecutive_loss_cooldown_bars: int = 45,
        ladder_cooldown_bars: int = 15,
        half_spread_pct: float = HALF_SPREAD_PCT,
        contract_size: float = 100000.0,
        commission_per_lot: float = 5.0,
        sl_pct: float = 0.0030,
        tgt_pct: float = 0.0060,
        max_hold_bars: int = 40,
        quant_score_threshold: float = 0.80,
        final_score_threshold: float = 0.75
    ):
        self.symbol = symbol.upper()
        self.initial_equity = initial_equity
        self.daily_loss_limit = daily_loss_limit
        self.consecutive_loss_trigger = consecutive_loss_trigger
        self.consecutive_loss_cooldown_bars = consecutive_loss_cooldown_bars
        self.ladder_cooldown_bars = ladder_cooldown_bars
        self.half_spread_pct = half_spread_pct
        self.contract_size = contract_size
        self.commission_per_lot = commission_per_lot
        self.sl_pct = sl_pct
        self.tgt_pct = tgt_pct
        self.max_hold_bars = max_hold_bars
        self.quant_score_threshold = quant_score_threshold
        self.final_score_threshold = final_score_threshold

        self.regime_detector = RegimeDetectorService()
        self.trade_scorer = TradeScorer(w_ml=0.5, w_quant=0.5)

    def run(
        self,
        tick_df: pd.DataFrame,
        warmup_candles: Optional[pd.DataFrame] = None,
        meta_labeler: Any = None,
        verbose: bool = False,
        output_csv_path: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Executes the tick replay simulation across all 7 institutional phases.
        Returns (completed_trades, tearsheet_metrics).
        """
        if tick_df.empty:
            return [], {}

        # Phase 1: Pre-Grouping into 1-minute blocks
        df = tick_df.copy()
        time_col = "timestamp" if "timestamp" in df.columns else ("time" if "time" in df.columns else df.columns[0])
        df[time_col] = pd.to_datetime(df[time_col])
        df = df.sort_values(time_col)

        price_col = "price" if "price" in df.columns else ("close" if "close" in df.columns else "premium")
        if "volume" not in df.columns:
            df["volume"] = 1

        df["minute"] = df[time_col].dt.floor("1min")
        minute_groups = df.groupby("minute")
        minutes = sorted(minute_groups.groups.keys())

        # State initialization via BacktestEngineState
        state = BacktestEngineState(
            initial_balance=self.initial_equity,
            equity=self.initial_equity,
            cash=self.initial_equity,
            peak_equity=self.initial_equity
        )

        daily_pnl = 0.0
        consecutive_sl_hits = 0
        sl_pause_until_bar = -1
        last_entry_bar_by_strat_dir: Dict[Tuple[str, str], int] = {}

        open_trade: Optional[ForexOpenTrade] = None
        completed_trades: List[Dict[str, Any]] = []

        # Warm-up buffer: initialize with up to 250 historical candles if provided
        if warmup_candles is not None and not warmup_candles.empty:
            candle_buffer = warmup_candles.copy().tail(250)
        else:
            candle_buffer = pd.DataFrame()

        # Step 2: Kalman Pre-Calibration (converge covariance matrix P before Bar 1)
        kalman_filter = pre_calibrate_kalman(StreamingForexKalmanFilter(), candle_buffer)
        current_day: Optional[datetime.date] = None

        for bar_idx, minute_ts in enumerate(minutes):
            bar_date = minute_ts.date()
            if current_day != bar_date:
                current_day = bar_date
                daily_pnl = 0.0  # Reset daily circuit breaker at start of each trading session
                state.daily_pnl = 0.0

            minute_ticks = minute_groups.get_group(minute_ts)
            macro_close_now = float(minute_ticks[price_col].iloc[-1])

            # ── Phase 2: Active Trade Execution (Sub-Bar Intra-Tick Stepping) ──
            if open_trade is not None:
                bars_held = bar_idx - open_trade.entry_bar_idx
                k_vel = getattr(kalman_filter, "last_velocity", 0.0)
                k_sigma = getattr(kalman_filter, "sigma_v", 0.0001)

                is_closed = open_trade.check_exit_tick(
                    minute_ts=minute_ts,
                    bar_idx=bar_idx,
                    bars_held=bars_held,
                    macro_close=macro_close_now,
                    kalman_velocity=k_vel,
                    kalman_sigma_v=k_sigma
                )
                if is_closed:
                    trade_summary = open_trade.to_dict()
                    completed_trades.append(trade_summary)
                    state.closed_trades.append(trade_summary)
                    trade_pnl = float(open_trade.pnl)
                    daily_pnl += trade_pnl
                    state.equity += trade_pnl
                    state.daily_pnl = daily_pnl

                    if state.equity > state.peak_equity:
                        state.peak_equity = state.equity
                    state.current_drawdown_usd = max(0.0, state.peak_equity - state.equity)
                    state.max_drawdown_pct = max(state.max_drawdown_pct, state.current_drawdown_usd / state.peak_equity if state.peak_equity > 0 else 0.0)

                    # Phase 4 Circuit Breaker: Consecutive Loss Cooling-Off
                    if trade_pnl < 0:
                        consecutive_sl_hits += 1
                        state.consecutive_losses = consecutive_sl_hits
                        if consecutive_sl_hits >= self.consecutive_loss_trigger:
                            sl_pause_until_bar = bar_idx + self.consecutive_loss_cooldown_bars
                            state.cooling_off_until_bar = sl_pause_until_bar
                            if verbose:
                                logger.info(
                                    f"[{minute_ts}] {consecutive_sl_hits} consecutive losses: "
                                    f"Paused for {self.consecutive_loss_cooldown_bars} bars."
                                )
                    else:
                        consecutive_sl_hits = 0
                        state.consecutive_losses = 0

                    dir_key = open_trade.direction if isinstance(open_trade.direction, str) else ("BUY" if open_trade.direction_int == 1 else "SELL")
                    last_entry_bar_by_strat_dir[(open_trade.strategy, dir_key)] = bar_idx
                    state.last_trade_bar[open_trade.strategy] = bar_idx
                    open_trade = None
                    state.active_positions = []

            # ── Phase 3: Candle Synthesis & Feature Pipeline ─────────────────
            candle = {
                "time": minute_ts,
                "symbol": self.symbol,
                "open": float(minute_ticks[price_col].iloc[0]),
                "high": float(minute_ticks[price_col].max()),
                "low": float(minute_ticks[price_col].min()),
                "close": macro_close_now,
                "volume": int(minute_ticks["volume"].sum()),
            }
            candle_buffer = pd.concat([candle_buffer, pd.DataFrame([candle])], ignore_index=True).tail(300)

            # Denoising via 2D Kinematic Kalman Filter
            k_price, k_vel, k_vel_z = kalman_filter.update(macro_close_now, volume=float(candle["volume"]))

            # ── Phase 4: Risk Gates & Safety Circuit Breakers ─────────────────
            if open_trade is not None:
                continue
            if daily_pnl <= self.daily_loss_limit:
                continue  # Intraday Drawdown Breaker tripped
            if bar_idx <= sl_pause_until_bar:
                continue  # Cooling off after consecutive losses
            if len(candle_buffer) < 50:
                continue  # Warmup buffer threshold

            # Step 5: Forex Rollover Session Filter (21:00-22:15 UTC lockout)
            if is_forex_rollover_window(minute_ts):
                continue

            # Compute macro indicators and detect market regime
            featured = IndicatorService.compute_indicators(candle_buffer.copy())
            if featured.empty:
                continue

            regime = self.regime_detector.detect(featured)
            latest_row = featured.iloc[-1].to_dict()

            # Step 4: Dynamic Rolling Shannon Entropy (over last 60 closed bars)
            closes_arr = candle_buffer["close"].to_numpy(dtype=float)
            rolling_entropy = compute_rolling_shannon_entropy(closes_arr, window=60, num_bins=10)

            # Gating: If entropy > 0.65, block all trend-following entries in chop
            regime_str = regime.value if hasattr(regime, "value") else str(regime)
            if rolling_entropy > 0.65 and ("TRENDING" in regime_str or "BREAKOUT" in regime_str):
                continue

            # ── Phase 5: Multi-Tiered Signal Gating ───────────────────────────
            signals = SignalGeneratorService.generate_signals(featured, self.symbol, regime)
            if not signals:
                continue

            for sig in signals:
                direction_str = sig.direction.value if hasattr(sig.direction, "value") else str(sig.direction)

                # Ladder cooldown check (15 bars)
                last_bar = last_entry_bar_by_strat_dir.get((sig.strategy, direction_str), -999)
                if (bar_idx - last_bar) < self.ladder_cooldown_bars:
                    continue

                # Microstructure Order Flow Confirmation
                micro_ok, flow_score = check_forex_micro_confirmation(minute_ticks, direction_str)
                if not micro_ok:
                    continue

                # Quant Hard Veto (< 0.80 discarded)
                kalman_features = {
                    "kalman_vel": k_vel,
                    "kalman_accel": kalman_filter.last_velocity,
                    "shannon_entropy": rolling_entropy,
                    "kaufman_er": 0.45,
                    "ema20_slope": float(latest_row.get("ema20_slope", 0.0)),
                    "vwap_dist": float(latest_row.get("vwap_dist", 0.0)),
                    "volume_ratio": float(latest_row.get("volume_ratio", 1.0)),
                    "utc_hour": minute_ts.hour,
                }

                quant_score = self.trade_scorer.compute_quant_score(
                    signal=sig,
                    regime=regime_str,
                    features=kalman_features
                )

                if quant_score < self.quant_score_threshold:
                    continue

                # LightGBM Meta-Probability & Composite Final Score
                ml_prob = 0.65
                if meta_labeler is not None and hasattr(meta_labeler, "predict_probability"):
                    ml_features = {
                        "kalman_vel": k_vel,
                        "kalman_vel_z": k_vel_z,
                        "shannon_entropy": rolling_entropy,
                        "kaufman_er": 0.45,
                        "rsi_14": float(latest_row.get("rsi", 50.0)),
                        "atr_14": float(latest_row.get("atr", macro_close_now * 0.0005)),
                        "spread_pips": 1.2,
                        "tick_momentum": 1.0 if direction_str in ("BUY", "CALL") else -1.0,
                        "burst_ratio": 1.1,
                        "sin_time": 0.5,
                        "cos_time": 0.86,
                    }
                    ml_prob = meta_labeler.predict_probability(ml_features)

                regime_bonus = 0.05 if "TRENDING" in regime_str else 0.0
                technical_strength = float(sig.technical_strength)

                # Final Composite Multi-Tier Formula
                final_score = (
                    0.35 * quant_score +
                    0.35 * ml_prob +
                    0.15 * flow_score +
                    0.10 * technical_strength +
                    regime_bonus
                )

                if final_score < self.final_score_threshold:
                    continue

                # ── Phase 6: Sizing & Entry Fill ─────────────────────────────
                is_buy = direction_str in ("BUY", "CALL")
                fill_price = macro_close_now * (1.0 + self.half_spread_pct) if is_buy else macro_close_now * (1.0 - self.half_spread_pct)

                # Step 6: Drawdown Position Sizing Scaler
                pip_size = PipCalculator.get_pip_unit(self.symbol)
                stop_dist_pips = max(5.0, (fill_price * self.sl_pct) / pip_size)
                pip_val_usd = (self.contract_size * pip_size)
                kelly_fraction = max(0.01, min(0.02, (ml_prob * 2.0 - 1.0) * 0.5 if ml_prob > 0.5 else 0.01))

                lot_size = calculate_forex_position_size(
                    state=state,
                    kelly_fraction=kelly_fraction,
                    stop_distance_pips=stop_dist_pips,
                    pip_value_usd=pip_val_usd,
                    max_allowed_dd_pct=0.10
                )

                open_trade = ForexOpenTrade(
                    entry_time=minute_ts,
                    symbol=self.symbol,
                    direction=direction_str,
                    strategy=sig.strategy,
                    entry_price=fill_price,
                    tick_df=minute_ticks,
                    ml_prob=round(ml_prob, 4),
                    strat_prob=round(quant_score, 4),
                    flow_score=round(flow_score, 4),
                    final_score=round(final_score, 4),
                    regime=regime_str,
                    entry_bar_idx=bar_idx,
                    sl_pct=self.sl_pct,
                    tgt_pct=self.tgt_pct,
                    lot_size=lot_size,
                    contract_size=self.contract_size,
                    commission_per_lot=self.commission_per_lot,
                    rl_agent="kalman",
                    half_spread_pct=self.half_spread_pct,
                    max_hold_bars=self.max_hold_bars
                )
                state.active_positions = [open_trade]

                if verbose:
                    logger.info(
                        f"[{minute_ts}] {direction_str} Order Filled @ {fill_price:.5f} | "
                        f"Lots: {lot_size} | Quant: {quant_score:.2f} | Final: {final_score:.2f} | Entropy: {rolling_entropy:.2f}"
                    )
                break

        # ── Phase 7: EOD Settlement & Metric Generation ──────────────────────
        if open_trade is not None:
            last_minute = minutes[-1]
            last_ticks = minute_groups.get_group(last_minute)
            terminal_price = float(last_ticks[price_col].iloc[-1])
            terminal_exit = terminal_price * (1.0 - self.half_spread_pct) if open_trade.is_buy else terminal_price * (1.0 + self.half_spread_pct)
            open_trade._finalize_exit(current_minute=last_minute, exit_price=terminal_exit, result="EOD_FORCE_CLOSE")
            trade_summary = open_trade.to_dict()
            completed_trades.append(trade_summary)
            state.closed_trades.append(trade_summary)
            state.equity += float(open_trade.pnl)
            if state.equity > state.peak_equity:
                state.peak_equity = state.equity
            state.current_drawdown_usd = max(0.0, state.peak_equity - state.equity)
            state.max_drawdown_pct = max(state.max_drawdown_pct, state.current_drawdown_usd / state.peak_equity if state.peak_equity > 0 else 0.0)

        # Step 7: Advanced Tearsheet & Full Audit Trail
        metrics = generate_advanced_tearsheet(state, output_csv_path=output_csv_path)
        return completed_trades, metrics


    def _calculate_tearsheet(self, trades: List[Dict[str, Any]], final_equity: float) -> Dict[str, Any]:
        """Compiles institutional tearsheet with Sharpe, Sortino, Drawdown and duration."""
        if not trades:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
                "net_pnl": 0.0,
                "final_equity": final_equity,
                "max_drawdown": 0.0,
                "max_drawdown_pct": 0.0,
                "max_drawdown_bars": 0,
                "sharpe_ratio": 0.0,
                "sortino_ratio": 0.0,
            }

        pnls = np.array([t["pnl"] for t in trades], dtype=float)
        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]

        total_trades = len(pnls)
        win_rate = (len(wins) / total_trades) * 100.0 if total_trades > 0 else 0.0
        gross_profit = float(np.sum(wins)) if len(wins) > 0 else 0.0
        gross_loss = float(abs(np.sum(losses))) if len(losses) > 0 else 0.0
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")
        net_pnl = round(float(np.sum(pnls)), 2)

        # Equity curve & Drawdowns
        equity_curve = [self.initial_equity]
        for p in pnls:
            equity_curve.append(equity_curve[-1] + p)
        equity_curve = np.array(equity_curve)

        peak = np.maximum.accumulate(equity_curve)
        drawdowns = peak - equity_curve
        drawdown_pcts = np.where(peak > 0, drawdowns / peak, 0.0)

        max_dd = float(np.max(drawdowns))
        max_dd_pct = float(np.max(drawdown_pcts)) * 100.0

        # Drawdown duration (in trade intervals)
        dd_duration = 0
        current_dd_dur = 0
        for i in range(1, len(equity_curve)):
            if equity_curve[i] < peak[i]:
                current_dd_dur += 1
                if current_dd_dur > dd_duration:
                    dd_duration = current_dd_dur
            else:
                current_dd_dur = 0

        # Sharpe & Sortino Ratios (per-trade return basis annualized assuming 250 days, 2 trades/day)
        returns = pnls / self.initial_equity
        mean_ret = float(np.mean(returns)) if len(returns) > 0 else 0.0
        std_ret = float(np.std(returns)) if len(returns) > 1 else 1e-6

        downside_returns = returns[returns < 0]
        downside_std = float(np.sqrt(np.mean(downside_returns ** 2))) if len(downside_returns) > 0 else 1e-6

        annual_factor = np.sqrt(500)  # ~500 trades per year
        sharpe = round((mean_ret / std_ret) * annual_factor, 2) if std_ret > 0 else 0.0
        sortino = round((mean_ret / downside_std) * annual_factor, 2) if downside_std > 0 else 0.0

        return {
            "total_trades": total_trades,
            "win_rate": round(win_rate, 2),
            "profit_factor": profit_factor,
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "net_pnl": net_pnl,
            "final_equity": round(final_equity, 2),
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
            "max_drawdown_bars": dd_duration,
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
        }
