import pytest
import numpy as np
import pandas as pd
from src.goldedge.adapters.secondary.features.forex_kalman_filter import (
    apply_forex_adaptive_kalman,
    StreamingForexKalmanFilter
)


def test_apply_forex_adaptive_kalman_basic():
    # Generate 50 bars of synthetic trend
    np.random.seed(42)
    prices = 1.0800 + np.cumsum(np.random.normal(0.0001, 0.0002, 50))
    price_series = pd.Series(prices)
    volume_series = pd.Series(np.random.randint(100, 500, 50))
    spread_series = pd.Series(np.random.uniform(0.0001, 0.0002, 50))

    result_df = apply_forex_adaptive_kalman(
        price_series=price_series,
        volume_series=volume_series,
        spread_series=spread_series
    )

    assert not result_df.empty
    assert "kalman_price" in result_df.columns
    assert "kalman_velocity" in result_df.columns
    assert len(result_df) == 50
    # Kalman price should track price series closely
    assert np.all(np.isfinite(result_df["kalman_price"]))
    assert np.all(np.isfinite(result_df["kalman_velocity"]))


def test_apply_forex_adaptive_kalman_empty():
    empty_series = pd.Series([], dtype=float)
    df = apply_forex_adaptive_kalman(empty_series)
    assert df.empty
    assert "kalman_price" in df.columns
    assert "kalman_velocity" in df.columns


def test_streaming_forex_kalman():
    skf = StreamingForexKalmanFilter()
    prices = [1.0800, 1.0805, 1.0810, 1.0816, 1.0825, 1.0835, 1.0846]

    for p in prices:
        k_price, k_vel, z_score = skf.update(p, volume=200, spread=1.2)
        assert isinstance(k_price, float)
        assert isinstance(k_vel, float)
        assert isinstance(z_score, float)

    # In a rising price series, velocity must be positive
    assert k_vel > 0
