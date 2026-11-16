import pytest
import numpy as np
import pandas as pd
from src.goldedge.infrastructure.ml.forex_meta_model import (
    train_forex_meta_model,
    ForexMetaLabeler
)


def test_train_forex_meta_model():
    np.random.seed(42)
    n = 120
    # Generate synthetic features
    data = {
        "kalman_vel": np.random.randn(n) * 0.01,
        "kalman_vel_z": np.random.randn(n),
        "shannon_entropy": np.random.uniform(2.0, 3.5, n),
        "kaufman_er": np.random.uniform(0.1, 0.8, n),
        "rsi_14": np.random.uniform(30, 70, n),
        "atr_14": np.random.uniform(0.001, 0.005, n),
        "spread_pips": np.random.uniform(0.5, 2.5, n),
        "tick_momentum": np.random.randint(-10, 10, n),
        "burst_ratio": np.random.uniform(0.5, 2.0, n),
        "sin_time": np.sin(np.linspace(0, 2 * np.pi, n)),
        "cos_time": np.cos(np.linspace(0, 2 * np.pi, n)),
    }
    X = pd.DataFrame(data)
    # Target: 1 if positive velocity & low entropy
    y = ((X["kalman_vel"] > 0) & (X["shannon_entropy"] < 3.0)).astype(int)

    # Train with 3 splits and 2 quick trials for speed in unit test
    calibrated_model, best_params = train_forex_meta_model(
        X, y, n_splits=3, n_trials=2, timeout=30
    )

    assert calibrated_model is not None
    assert "num_leaves" in best_params

    # Test prediction
    preds = calibrated_model.predict_proba(X)
    assert preds.shape == (n, 2)
    assert np.all(preds >= 0.0) and np.all(preds <= 1.0)


def test_forex_meta_labeler_inference():
    labeler = ForexMetaLabeler(model_path="models/test_meta_model.joblib")
    features = {
        "kalman_vel": 0.005,
        "kalman_vel_z": 1.2,
        "shannon_entropy": 2.5,
        "kaufman_er": 0.6,
        "rsi_14": 55.0,
        "atr_14": 0.002,
        "spread_pips": 1.1,
        "tick_momentum": 4,
        "burst_ratio": 1.4,
        "sin_time": 0.5,
        "cos_time": 0.86,
    }
    prob = labeler.predict_probability(features)
    assert 0.0 <= prob <= 1.0
