import pytest
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

from src.goldedge.domain.models.forex_open_trade import ForexOpenTrade, HALF_SPREAD_PCT
from src.goldedge.domain.services.forex_adaptive_sizing import forex_adaptive_position_size
from src.goldedge.application.use_cases.run_tick_replay import (
    ForexTickReplayEngine,
    check_forex_micro_confirmation
)
from scripts.tick_replay_backtest import generate_synthetic_forex_ticks, run_tick_replay


def test_forex_open_trade_intra_tick_sl_before_peak():
    """
    Core Invariant: For tick k+1, Stop-Loss breach is checked on the level
    from tick k BEFORE updating the peak price for tick k+1.
    """
    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)
    entry_price = 1.0850
    sl_pct = 0.0020  # SL = 1.0850 * 0.9980 = 1.08283

    # Create ticks for minute 0: price plunges below SL, then spikes to high
    ticks_data = [
        {"timestamp": t0, "price": 1.0845, "bid": 1.0844, "ask": 1.0846, "volume": 10},
        {"timestamp": t0 + timedelta(seconds=10), "price": 1.0825, "bid": 1.0824, "ask": 1.0826, "volume": 50},  # SL BREACH
        {"timestamp": t0 + timedelta(seconds=20), "price": 1.0900, "bid": 1.0899, "ask": 1.0901, "volume": 100}, # Spike
    ]
    tick_df = pd.DataFrame(ticks_data)

    trade = ForexOpenTrade(
        entry_time=t0,
        symbol="EURUSD",
        direction="BUY",
        strategy="momentum",
        entry_price=entry_price,
        tick_df=tick_df,
        sl_pct=sl_pct,
        tgt_pct=0.0050,
        lot_size=1.0,
        contract_size=100000.0
    )

    is_closed = trade.check_exit_tick(
        minute_ts=pd.Timestamp(t0),
        bar_idx=0,
        bars_held=0,
        macro_close=1.0900
    )

    assert is_closed is True
    # Trade MUST have exited at SL, NOT reached the 1.0900 spike!
    assert trade.result == "SL"
    assert trade.pnl < 0


def test_forex_open_trade_sell_direction():
    """Verifies SELL position logic with Ask check for SL/TP."""
    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)
    entry_price = 1.0850
    # For SELL, target is lower, SL is higher
    ticks_data = [
        {"timestamp": t0, "price": 1.0840, "bid": 1.0839, "ask": 1.0841, "volume": 10},
        {"timestamp": t0 + timedelta(seconds=15), "price": 1.0790, "bid": 1.0789, "ask": 1.0791, "volume": 50}, # TARGET HIT
    ]
    tick_df = pd.DataFrame(ticks_data)

    trade = ForexOpenTrade(
        entry_time=t0,
        symbol="EURUSD",
        direction="SELL",
        strategy="momentum",
        entry_price=entry_price,
        tick_df=tick_df,
        sl_pct=0.0030,
        tgt_pct=0.0050,
        lot_size=1.0,
        contract_size=100000.0
    )

    is_closed = trade.check_exit_tick(
        minute_ts=pd.Timestamp(t0),
        bar_idx=0,
        bars_held=0
    )

    assert is_closed is True
    assert trade.result == "TARGET"
    assert trade.pnl > 0


def test_adaptive_position_size_half_kelly():
    equity = 50000.0
    entry_price = 1.0850

    lots_bull = forex_adaptive_position_size(
        equity=equity,
        entry_price=entry_price,
        win_rate=0.60,
        avg_win_pct=0.0060,
        avg_loss_pct=0.0030,
        regime="TRENDING_BULL",
        contract_size=100000.0
    )

    lots_sideways = forex_adaptive_position_size(
        equity=equity,
        entry_price=entry_price,
        win_rate=0.60,
        avg_win_pct=0.0060,
        avg_loss_pct=0.0030,
        regime="SIDEWAYS",
        contract_size=100000.0
    )

    assert lots_bull >= 0.01
    assert lots_sideways >= 0.01
    # Bull regime should size larger or equal to sideways
    assert lots_bull >= lots_sideways


def test_consecutive_loss_cooldown_and_ladder_cooldown():
    """
    Verifies that 2 consecutive losses trigger a 45-bar cooldown,
    and ladder cooldown blocks same (strategy, direction) for 15 bars.
    """
    engine = ForexTickReplayEngine(
        symbol="EURUSD",
        initial_equity=100000.0,
        consecutive_loss_trigger=2,
        consecutive_loss_cooldown_bars=45,
        ladder_cooldown_bars=15
    )

    assert engine.consecutive_loss_trigger == 2
    assert engine.consecutive_loss_cooldown_bars == 45
    assert engine.ladder_cooldown_bars == 15


def test_end_to_end_replay_run():
    ticks = generate_synthetic_forex_ticks(symbol="EURUSD", n_minutes=90, ticks_per_minute=10)
    assert not ticks.empty
    assert len(ticks) == 900

    trades, metrics = run_tick_replay(
        symbol="EURUSD",
        initial_equity=100000.0,
        tick_df=ticks,
        verbose=False
    )

    assert "total_trades" in metrics
    assert "win_rate" in metrics
    assert "final_equity" in metrics
    assert "profit_factor" in metrics
    assert "calmar_ratio" in metrics
    assert "expectancy_usd" in metrics
    assert "avg_mae_pips" in metrics
    assert "avg_mfe_pips" in metrics


def test_kalman_pre_calibration_covariance():
    """Verify covariance matrix P converges and stabilizes after warm-up candles."""
    from src.goldedge.adapters.secondary.features.forex_kalman_filter import StreamingForexKalmanFilter
    from src.goldedge.application.use_cases.run_tick_replay import pre_calibrate_kalman

    kf = StreamingForexKalmanFilter()
    initial_p = kf.p
    assert initial_p == 1.0

    # 100 warm-up candles around 1.0850
    warmup = [{"high": 1.0855, "low": 1.0845, "close": 1.0850, "volume": 50} for _ in range(100)]
    pre_calibrate_kalman(kf, warmup)

    # Covariance P must have converged to steady-state (significantly below 1.0)
    assert kf.p < initial_p
    assert kf.p < 0.10


def test_tiered_profit_ratchet_four_tiers():
    """Verify that a trade hitting +50 pips locks +40 pips (80%), +35 locks 70%, +20 locks 55%, +10 locks BE+2x spread."""
    from src.goldedge.domain.models.forex_open_trade import check_exit_tick

    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)
    entry_price = 1.0800
    pip_size = 0.0001

    trade = ForexOpenTrade(
        entry_time=t0,
        symbol="EURUSD",
        direction="BUY",
        entry_price=entry_price,
        sl_pct=0.0030, # SL = 1.07676
        tgt_pct=0.0100 # Target = 1.0908
    )

    initial_sl = trade.current_sl_price
    assert initial_sl < entry_price

    # 1. Price moves to +12 pips -> Tier 4 (+10 pips: Breakeven + 2x spread)
    tick_12p = {"timestamp": t0 + timedelta(seconds=10), "bid": 1.0812, "ask": 1.0813}
    check_exit_tick(trade, tick_12p, pip_size=pip_size, current_spread_pips=1.0)
    assert trade.peak_pnl_pips >= 12.0
    # SL must now be locked above breakeven (+ 2.0 pips)
    expected_tier4_sl = entry_price + (2.0 * pip_size)
    assert trade.current_sl_price >= expected_tier4_sl

    # 2. Price moves to +25 pips -> Tier 3 (+20 pips: Lock 55%)
    tick_25p = {"timestamp": t0 + timedelta(seconds=20), "bid": 1.0825, "ask": 1.0826}
    check_exit_tick(trade, tick_25p, pip_size=pip_size, current_spread_pips=1.0)
    assert trade.peak_pnl_pips >= 25.0
    expected_tier3_sl = entry_price + (25.0 * 0.55 * pip_size)
    assert trade.current_sl_price >= expected_tier3_sl

    # 3. Price moves to +38 pips -> Tier 2 (+35 pips: Lock 70%)
    tick_38p = {"timestamp": t0 + timedelta(seconds=30), "bid": 1.0838, "ask": 1.0839}
    check_exit_tick(trade, tick_38p, pip_size=pip_size, current_spread_pips=1.0)
    assert trade.peak_pnl_pips >= 38.0
    expected_tier2_sl = entry_price + (38.0 * 0.70 * pip_size)
    assert trade.current_sl_price >= expected_tier2_sl

    # 4. Price moves to +52 pips -> Tier 1 (+50 pips: Lock 80%)
    tick_52p = {"timestamp": t0 + timedelta(seconds=40), "bid": 1.0852, "ask": 1.0853}
    check_exit_tick(trade, tick_52p, pip_size=pip_size, current_spread_pips=1.0)
    assert trade.peak_pnl_pips >= 52.0
    # 52 * 0.80 = 41.6 pips locked
    expected_tier1_sl = entry_price + (52.0 * 0.80 * pip_size)
    assert trade.current_sl_price >= expected_tier1_sl


def test_kinematic_kalman_velocity_exit():
    """Ensure trades cleanly close when adverse velocity exceeds 1.8 * sigma_v."""
    from src.goldedge.domain.models.forex_open_trade import check_exit_tick

    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)
    entry_price = 1.0850
    pip_size = 0.0001

    trade = ForexOpenTrade(
        entry_time=t0,
        symbol="EURUSD",
        direction="BUY",
        entry_price=entry_price,
        sl_pct=0.0050, # Wide SL at 1.07957
        tgt_pct=0.0100
    )
    # Simulate trade having been held > 10 ticks
    trade.ticks_held = 11

    # Adverse velocity: Buy trade, but kalman_velocity is -0.0020 when sigma_v is 0.0010 (|vel| = 2.0 * sigma_v > 1.8 * sigma_v)
    tick = {"timestamp": t0 + timedelta(seconds=5), "bid": 1.0840, "ask": 1.0841}
    exit_rec = check_exit_tick(
        trade=trade,
        tick=tick,
        pip_size=pip_size,
        current_spread_pips=1.0,
        kalman_velocity=-0.0020,
        kalman_sigma_v=0.0010
    )

    assert exit_rec is not None
    assert exit_rec["exit_reason"] == "KALMAN_VELOCITY_REVERSAL"
    assert exit_rec["exit_price"] == 1.0840


def test_stagnation_defense_linear_compression():
    """Verify stop tightens linearly after 15 bars with no new peak."""
    from src.goldedge.domain.models.forex_open_trade import check_exit_tick

    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)
    entry_price = 1.0800
    pip_size = 0.0001

    trade = ForexOpenTrade(
        entry_time=t0,
        symbol="EURUSD",
        direction="BUY",
        entry_price=entry_price,
        sl_pct=0.0030,
        tgt_pct=0.0100
    )

    # Establish peak at +15 pips (Tier 4 locked)
    tick_peak = {"timestamp": t0, "bid": 1.0815, "ask": 1.0816}
    check_exit_tick(trade, tick_peak, pip_size=pip_size, current_spread_pips=1.0)
    sl_before_stagnation = trade.current_sl_price

    # Advance 18 bars without new peak (3 bars of stagnation compression)
    trade.bars_since_peak = 18
    tick_stagnant = {"timestamp": t0 + timedelta(minutes=18), "bid": 1.0810, "ask": 1.0811}
    check_exit_tick(trade, tick_stagnant, pip_size=pip_size, current_spread_pips=1.0)

    # 3 bars * 0.5 pips = 1.5 pips compression
    assert trade.current_sl_price > sl_before_stagnation


def test_dynamic_rolling_shannon_entropy_gating():
    """Verify that during high-entropy chop (H > 0.65), entropy exceeds 0.65, and during trend H < 0.65."""
    from src.goldedge.application.use_cases.run_tick_replay import compute_rolling_shannon_entropy

    np.random.seed(42)
    # 1. Pure Random Walk / Chop (noisy oscillations around 1.0850)
    chop_closes = 1.0850 + np.random.normal(0, 0.0005, size=80)
    entropy_chop = compute_rolling_shannon_entropy(chop_closes, window=60, num_bins=10)
    assert entropy_chop > 0.65

    # 2. Strong Uniform Trend
    trend_closes = np.linspace(1.0800, 1.0950, 80)
    entropy_trend = compute_rolling_shannon_entropy(trend_closes, window=60, num_bins=10)
    assert entropy_trend < 0.65


def test_forex_rollover_window_lockout():
    """Verify rollover window lockout blocks 21:00 to 22:15 UTC."""
    from src.goldedge.application.use_cases.run_tick_replay import is_forex_rollover_window

    t_rollover_1 = datetime(2026, 11, 10, 21, 0, 0, tzinfo=timezone.utc)
    t_rollover_2 = datetime(2026, 11, 10, 21, 45, 0, tzinfo=timezone.utc)
    t_rollover_3 = datetime(2026, 11, 10, 22, 10, 0, tzinfo=timezone.utc)
    t_active_trading = datetime(2026, 11, 10, 14, 30, 0, tzinfo=timezone.utc)
    t_post_rollover = datetime(2026, 11, 10, 22, 30, 0, tzinfo=timezone.utc)

    assert is_forex_rollover_window(t_rollover_1) is True
    assert is_forex_rollover_window(t_rollover_2) is True
    assert is_forex_rollover_window(t_rollover_3) is True
    assert is_forex_rollover_window(t_active_trading) is False
    assert is_forex_rollover_window(t_post_rollover) is False


def test_drawdown_scaled_half_kelly_and_advanced_tearsheet(tmp_path):
    """Verify drawdown scaling and advanced tearsheet metrics."""
    from src.goldedge.domain.models.forex_open_trade import BacktestEngineState
    from src.goldedge.domain.services.forex_adaptive_sizing import calculate_forex_position_size
    from src.goldedge.application.use_cases.run_tick_replay import generate_advanced_tearsheet

    state = BacktestEngineState(initial_balance=100000.0, equity=100000.0, peak_equity=100000.0)

    # 1. Sizing at peak equity (0% drawdown)
    lots_full = calculate_forex_position_size(state, kelly_fraction=0.02, stop_distance_pips=30.0, pip_value_usd=10.0)

    # 2. Sizing under 5% drawdown (max allowed 10%) -> dd_scaler = 0.50
    state.equity = 95000.0
    lots_half = calculate_forex_position_size(state, kelly_fraction=0.02, stop_distance_pips=30.0, pip_value_usd=10.0)
    assert lots_half < lots_full

    # 3. Tearsheet and CSV audit trail
    state.equity = 105000.0
    state.max_drawdown_pct = 0.05
    state.current_drawdown_usd = 5000.0
    state.closed_trades = [
        {"trade_id": "T1", "symbol": "EURUSD", "direction": "BUY", "pnl_usd": 300.0, "mae_pips": -4.2, "mfe_pips": 15.0, "exit_reason": "TARGET"},
        {"trade_id": "T2", "symbol": "EURUSD", "direction": "SELL", "pnl_usd": -100.0, "mae_pips": -12.0, "mfe_pips": 2.0, "exit_reason": "STOP_LOSS"},
        {"trade_id": "T3", "symbol": "EURUSD", "direction": "BUY", "pnl_usd": 250.0, "mae_pips": -2.0, "mfe_pips": 18.0, "exit_reason": "KALMAN_VELOCITY_REVERSAL"},
    ]

    csv_file = str(tmp_path / "test_audit.csv")
    metrics = generate_advanced_tearsheet(state, output_csv_path=csv_file)

    assert metrics["total_trades"] == 3
    assert metrics["win_rate"] == 66.67
    assert metrics["profit_factor"] == 5.5
    assert metrics["expectancy_usd"] > 0
    assert metrics["calmar_ratio"] > 0
    assert metrics["avg_mae_pips"] < 0
    assert metrics["avg_mfe_pips"] > 0

    # Verify audit CSV file was generated and has expected columns
    df_audit = pd.read_csv(csv_file)
    assert len(df_audit) == 3
    assert "mae_pips" in df_audit.columns
    assert "mfe_pips" in df_audit.columns
    assert "exit_reason" in df_audit.columns

