import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from src.goldedge.domain.models.signal import SignalDirection
from src.goldedge.domain.models.regime import MarketRegime
from src.goldedge.domain.services.signal_generator import SignalGeneratorService


def test_kinematic_kalman_trend_bullish():
    # Construct 60 bars with Close > EMA50 > EMA200, positive velocity z-score >= 1.5,
    # acceleration >= 0, and a lower rejection wick >= 40%
    rows = []
    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(55):
        p = 1.0800 + (i * 0.0001)
        rows.append({
            "time": t0 + timedelta(minutes=i),
            "open": p,
            "high": p + 0.0003,
            "low": p - 0.0001,
            "close": p + 0.0002,
            "ema20": p - 0.0002,
            "ema50": p - 0.0005,
            "ema200": p - 0.0015,
            "atr": 0.0008,
            "kalman_price": p - 0.0001,
            "kalman_velocity_z": 1.8,
            "kalman_accel": 0.0002,
        })

    # The last candle pulls back to EMA20 and prints a >40% lower wick:
    # Range = 1.0860 - 1.0850 = 10 pips. Open = 1.0857, Close = 1.0859.
    # Lower wick = min(1.0857, 1.0859) - 1.0850 = 7 pips (70% > 40%)
    p_last = 1.0855
    rows[-1] = {
        "time": t0 + timedelta(minutes=54),
        "open": 1.0857,
        "high": 1.0860,
        "low": 1.0850,
        "close": 1.0859,
        "ema20": 1.0852,
        "ema50": 1.0848,
        "ema200": 1.0830,
        "atr": 0.0008,
        "kalman_price": 1.0851,
        "kalman_velocity_z": 1.8,
        "kalman_accel": 0.0001,
    }

    df = pd.DataFrame(rows)
    sig = SignalGeneratorService._kinematic_kalman_trend(df, "EURUSD")

    assert sig is not None
    assert sig.direction == SignalDirection.BUY
    assert sig.strategy == "kinematic_kalman_trend"
    assert sig.initial_sl_price < sig.entry_price
    assert sig.target_price > sig.entry_price
    # Check 1:2 RR geometry
    assert sig.base_reward_pips == pytest.approx(sig.base_risk_pips * 2.0, rel=1e-2)


def test_statistical_mean_reversion_bullish():
    # 30 bars in sideways range. Previous bar breaches below 3.0 std Bollinger,
    # current bar closes back inside, RSI < 28, Kalman velocity curls upward.
    rows = []
    t0 = datetime(2026, 11, 10, 14, 0, 0, tzinfo=timezone.utc)
    base = 1.0850
    for i in range(25):
        rows.append({
            "time": t0 + timedelta(minutes=i),
            "open": base,
            "high": base + 0.0005,
            "low": base - 0.0005,
            "close": base,
            "rsi": 45.0,
            "kalman_velocity": -0.0001,
        })

    # Bar 23 breaches lower band
    rows[-2] = {
        "time": t0 + timedelta(minutes=23),
        "open": base,
        "high": base,
        "low": base - 0.0030,  # extreme breach below 3 std
        "close": base - 0.0028,
        "rsi": 22.0,
        "kalman_velocity": -0.0005,
    }

    # Bar 24 closes back inside and velocity curls upward
    rows[-1] = {
        "time": t0 + timedelta(minutes=24),
        "open": base - 0.0028,
        "high": base - 0.0002,
        "low": base - 0.0029,
        "close": base - 0.0005,  # back inside
        "rsi": 26.0,
        "kalman_velocity": 0.0001,  # upward curl
    }

    df = pd.DataFrame(rows)
    sig = SignalGeneratorService._statistical_mean_reversion(df, "EURUSD")

    assert sig is not None
    assert sig.direction == SignalDirection.BUY
    assert sig.strategy == "statistical_mean_reversion"
    assert sig.initial_sl_price < sig.entry_price


def test_session_orb_breakout():
    # 25 bars during London Open (07:15 UTC).
    # 15m opening range is tight (< 0.75 ADR), breakout bar has volume > 2.0x avg
    rows = []
    t0 = datetime(2026, 11, 10, 7, 0, 0, tzinfo=timezone.utc)
    for i in range(20):
        rows.append({
            "time": t0 + timedelta(minutes=i),
            "open": 1.0850,
            "high": 1.0854,
            "low": 1.0848,
            "close": 1.0852,
            "tick_volume": 100,
            "atr": 0.0010,
        })

    # Breakout bar at 07:20 UTC
    rows[-1] = {
        "time": t0 + timedelta(minutes=20),
        "open": 1.0852,
        "high": 1.0862,
        "low": 1.0851,
        "close": 1.0860,  # cleanly above 1.0854 OR high
        "tick_volume": 350,  # 3.5x volume
        "atr": 0.0010,
    }

    df = pd.DataFrame(rows)
    sig = SignalGeneratorService._session_orb(df, "EURUSD")

    assert sig is not None
    assert sig.direction == SignalDirection.BUY
    assert sig.strategy == "session_orb"
    assert sig.initial_sl_price == pytest.approx((1.0854 + 1.0848) / 2.0, rel=1e-3)
