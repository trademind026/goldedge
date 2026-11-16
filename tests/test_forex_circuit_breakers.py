import pytest
from datetime import datetime, time, timezone
from src.goldedge.domain.services.circuit_breaker_service import (
    ForexCircuitBreakerService,
    CircuitBreakerConfig,
    CircuitBreakerStatus
)


def test_daily_drawdown_hard_breaker():
    service = ForexCircuitBreakerService(CircuitBreakerConfig(max_daily_drawdown_pct=0.03))
    t0 = datetime(2026, 11, 10, 14, 0, 0, tzinfo=timezone.utc)

    # 1. 2.5% loss - should allow
    status = service.check_preflight(
        symbol="EURUSD",
        strategy_id="trend_pullback",
        direction="BUY",
        current_bar_idx=100,
        current_utc_time=t0,
        starting_session_equity=10000.0,
        current_closed_equity=9750.0,  # 2.5% DD
        current_spread_pips=1.2,
        median_24h_spread_pips=1.0,
        active_open_symbols=[]
    )
    assert status.allowed
    assert not status.is_locked_out_for_day

    # 2. 3.2% loss - should trip hard breaker
    status_trip = service.check_preflight(
        symbol="EURUSD",
        strategy_id="trend_pullback",
        direction="BUY",
        current_bar_idx=101,
        current_utc_time=t0,
        starting_session_equity=10000.0,
        current_closed_equity=9680.0,  # 3.2% DD
        current_spread_pips=1.2,
        median_24h_spread_pips=1.0,
        active_open_symbols=[]
    )
    assert not status_trip.allowed
    assert status_trip.is_locked_out_for_day
    assert any("Daily Drawdown" in r for r in status_trip.rejection_reasons)


def test_consecutive_loss_cooldown():
    service = ForexCircuitBreakerService(CircuitBreakerConfig(consecutive_loss_cooldown_bars=45))
    t0 = datetime(2026, 11, 10, 14, 0, 0, tzinfo=timezone.utc)

    # Record 2 consecutive SL hits at bar 50
    service.record_trade_close("STOP_LOSS", bar_idx=48)
    service.record_trade_close("STOP_LOSS", bar_idx=50)

    # Check at bar 70 (20 bars elapsed < 45) -> rejected
    status = service.check_preflight(
        symbol="EURUSD",
        strategy_id="trend_pullback",
        direction="BUY",
        current_bar_idx=70,
        current_utc_time=t0,
        starting_session_equity=10000.0,
        current_closed_equity=10000.0,
        current_spread_pips=1.0,
        median_24h_spread_pips=1.0,
        active_open_symbols=[]
    )
    assert not status.allowed
    assert status.cooldown_bars_remaining == 25
    assert any("Consecutive Loss" in r for r in status.rejection_reasons)

    # Check at bar 96 (46 bars elapsed > 45) -> allowed
    status_ok = service.check_preflight(
        symbol="EURUSD",
        strategy_id="trend_pullback",
        direction="BUY",
        current_bar_idx=96,
        current_utc_time=t0,
        starting_session_equity=10000.0,
        current_closed_equity=10000.0,
        current_spread_pips=1.0,
        median_24h_spread_pips=1.0,
        active_open_symbols=[]
    )
    assert status_ok.allowed


def test_ladder_reentry_spacing_guard():
    service = ForexCircuitBreakerService(CircuitBreakerConfig(ladder_cooldown_bars=15))
    t0 = datetime(2026, 11, 10, 14, 0, 0, tzinfo=timezone.utc)

    # Record entry for (EURUSD, trend_pullback, BUY) at bar 100
    service.record_entry("EURUSD", "trend_pullback", "BUY", bar_idx=100)

    # Try same setup at bar 110 (10 bars < 15) -> rejected
    status = service.check_preflight(
        symbol="EURUSD",
        strategy_id="trend_pullback",
        direction="BUY",
        current_bar_idx=110,
        current_utc_time=t0,
        starting_session_equity=10000.0,
        current_closed_equity=10000.0,
        current_spread_pips=1.0,
        median_24h_spread_pips=1.0,
        active_open_symbols=[]
    )
    assert not status.allowed
    assert any("Ladder Re-entry" in r for r in status.rejection_reasons)

    # Different strategy or direction at bar 110 -> allowed
    status_diff = service.check_preflight(
        symbol="EURUSD",
        strategy_id="mean_reversion",
        direction="BUY",
        current_bar_idx=110,
        current_utc_time=t0,
        starting_session_equity=10000.0,
        current_closed_equity=10000.0,
        current_spread_pips=1.0,
        median_24h_spread_pips=1.0,
        active_open_symbols=[]
    )
    assert status_diff.allowed


def test_rollover_blackout_and_spread_and_basket_cap():
    service = ForexCircuitBreakerService()

    # 1. Rollover blackout at 21:30 UTC
    t_rollover = datetime(2026, 11, 10, 21, 30, 0, tzinfo=timezone.utc)
    status_roll = service.check_preflight(
        symbol="EURUSD",
        strategy_id="trend_pullback",
        direction="BUY",
        current_bar_idx=100,
        current_utc_time=t_rollover,
        starting_session_equity=10000.0,
        current_closed_equity=10000.0,
        current_spread_pips=1.0,
        median_24h_spread_pips=1.0,
        active_open_symbols=[]
    )
    assert not status_roll.allowed
    assert any("Rollover Blackout" in r for r in status_roll.rejection_reasons)

    # 2. Spread spike (3.0 pips vs 1.0 median > 2.5x)
    t_normal = datetime(2026, 11, 10, 14, 0, 0, tzinfo=timezone.utc)
    status_spread = service.check_preflight(
        symbol="EURUSD",
        strategy_id="trend_pullback",
        direction="BUY",
        current_bar_idx=100,
        current_utc_time=t_normal,
        starting_session_equity=10000.0,
        current_closed_equity=10000.0,
        current_spread_pips=3.0,
        median_24h_spread_pips=1.0,
        active_open_symbols=[]
    )
    assert not status_spread.allowed
    assert any("Spread Spike" in r for r in status_spread.rejection_reasons)

    # 3. Currency Basket Concentration Cap (2 USD pairs already open)
    status_basket = service.check_preflight(
        symbol="GBPUSD",
        strategy_id="trend_pullback",
        direction="BUY",
        current_bar_idx=100,
        current_utc_time=t_normal,
        starting_session_equity=10000.0,
        current_closed_equity=10000.0,
        current_spread_pips=1.0,
        median_24h_spread_pips=1.0,
        active_open_symbols=["EURUSD", "USDJPY"]
    )
    assert not status_basket.allowed
    assert any("Currency Basket Concentration" in r for r in status_basket.rejection_reasons)
