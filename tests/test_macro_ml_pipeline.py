import os
import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta

from goldedge.infrastructure.ml.macro_feature_engine import MacroFeatureEngine
from goldedge.infrastructure.ml.triple_barrier_labeler import DynamicATRTripleBarrierLabeler
from goldedge.infrastructure.ml.purged_cv import PurgedKFold
from goldedge.infrastructure.ml.forex_meta_model import ForexMetaLabeler, train_forex_macro_model


def test_macro_feature_engine_computes_32_features():
    engine = MacroFeatureEngine()
    assert len(engine.FEATURE_NAMES) == 32

    # Create dummy pair bars
    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)
    pair_rows = []
    dxy_rows = []
    yield_rows = []

    for i in range(100):
        t = t0 + timedelta(minutes=i)
        p = 1.0800 + np.sin(i / 10.0) * 0.0020
        pair_rows.append({
            "time": t,
            "open": p - 0.0002,
            "high": p + 0.0005,
            "low": p - 0.0005,
            "close": p,
            "tick_volume": 150 + int(np.random.randint(10, 50)),
            "kalman_price": p - 0.0001,
            "kalman_velocity_z": 1.2,
        })
        dxy_p = 104.50 + np.cos(i / 12.0) * 0.30
        dxy_rows.append({
            "time": t,
            "open": dxy_p,
            "high": dxy_p + 0.1,
            "low": dxy_p - 0.1,
            "close": dxy_p,
            "kalman_vel": 0.05,
        })
        y_p = 4.35 + (i * 0.001)
        yield_rows.append({
            "time": t,
            "close": y_p,
            "yield_spread": 1.65,
            "yield_spread_vel": 0.01,
            "risk_sentiment": 0.25,
        })

    df_pair = pd.DataFrame(pair_rows)
    df_dxy = pd.DataFrame(dxy_rows)
    df_yields = pd.DataFrame(yield_rows)

    g8_dfs = {
        "EUR": df_pair,
        "USD": df_dxy,
        "GBP": df_pair,
        "JPY": df_pair,
    }

    feats = engine.compute_macro_features(
        df_pair=df_pair,
        symbol="EURUSD",
        df_dxy=df_dxy,
        df_yields=df_yields,
        g8_dfs=g8_dfs,
        current_time=t0 + timedelta(minutes=99),
        news_proximity_minutes=45.0,
    )

    assert len(feats) == 32
    for name in engine.FEATURE_NAMES:
        assert name in feats
        val = feats[name]
        assert not np.isnan(val), f"Feature {name} is NaN"
        assert not np.isinf(val), f"Feature {name} is Inf"

    # Verify specific feature calculations
    assert feats["Currency_Carry"] == pytest.approx(3.75 - 5.25, abs=1e-2)  # EUR (3.75) - USD (5.25)
    assert 1.0 <= feats["Fractal_Dim"] <= 2.0
    assert feats["News_Proximity"] == 45.0


def test_dynamic_atr_triple_barrier_labeler():
    labeler = DynamicATRTripleBarrierLabeler(
        atr_upper_mult=2.0,
        atr_lower_mult=1.0,
        max_holding_bars=30,
    )

    # 1. Successful BUY trade: Upper barrier (+2.0 ATR) reached before lower barrier
    rows_win = []
    for i in range(50):
        # Entry at i=10 (close=1.0800, atr=0.0010 -> Upper=1.0820, Lower=1.0790)
        # At i=20, price spikes to 1.0825 (touches Upper)
        p = 1.0800 + (0.0003 * (i - 10) if i > 10 else 0.0)
        rows_win.append({
            "open": p,
            "high": p + 0.0005,
            "low": p - 0.0002,
            "close": p,
            "atr": 0.0010,
        })
    df_win = pd.DataFrame(rows_win)
    label_buy_win = labeler.label_candidate(df_win, entry_idx=10, direction=1)
    assert label_buy_win == 1

    # 2. Failed BUY trade: Lower barrier (-1.0 ATR) hit first
    rows_loss = []
    for i in range(50):
        p = 1.0800 - (0.0003 * (i - 10) if i > 10 else 0.0)
        rows_loss.append({
            "open": p,
            "high": p + 0.0002,
            "low": p - 0.0005,
            "close": p,
            "atr": 0.0010,
        })
    df_loss = pd.DataFrame(rows_loss)
    label_buy_loss = labeler.label_candidate(df_loss, entry_idx=10, direction=1)
    assert label_buy_loss == 0

    # 3. Stagnant trade: Vertical barrier (30 bars) expired without hitting either
    rows_stagnant = []
    for i in range(50):
        p = 1.0800 + (0.00001 * (i - 10) if i > 10 else 0.0)
        rows_stagnant.append({
            "open": p,
            "high": p + 0.0002,
            "low": p - 0.0002,
            "close": p,
            "atr": 0.0010,
        })
    df_stagnant = pd.DataFrame(rows_stagnant)
    label_stagnant = labeler.label_candidate(df_stagnant, entry_idx=10, direction=1)
    assert label_stagnant == 0

    # 4. Successful SELL trade: Upper barrier (drop by 2.0 ATR) touched first
    rows_sell_win = []
    for i in range(50):
        p = 1.0800 - (0.0003 * (i - 10) if i > 10 else 0.0)
        rows_sell_win.append({
            "open": p,
            "high": p + 0.0002,
            "low": p - 0.0005,
            "close": p,
            "atr": 0.0010,
        })
    df_sell_win = pd.DataFrame(rows_sell_win)
    label_sell_win = labeler.label_candidate(df_sell_win, entry_idx=10, direction=-1)
    assert label_sell_win == 1


def test_purged_kfold_with_embargo():
    n_samples = 150
    X = np.arange(n_samples)
    cv = PurgedKFold(n_splits=3, purge_bars=10, embargo_bars=5)

    splits = list(cv.split(X))
    assert len(splits) == 3

    for train_idx, val_idx in splits:
        val_start = val_idx[0]
        val_end = val_idx[-1]

        # Ensure no training samples exist in [val_start - purge_bars, val_start)
        purge_zone = np.arange(max(0, val_start - 10), val_start)
        assert len(np.intersect1d(train_idx, purge_zone)) == 0

        # Ensure no training samples exist in [val_end, val_end + embargo_bars]
        embargo_zone = np.arange(val_end, min(n_samples, val_end + 5))
        assert len(np.intersect1d(train_idx, embargo_zone)) == 0


def test_forex_macro_model_training_and_isotonic_calibration(tmp_path):
    # Synthesize small training dataset
    np.random.seed(42)
    n = 120
    engine = MacroFeatureEngine()
    feature_names = engine.FEATURE_NAMES

    X_data = {f: np.random.randn(n) for f in feature_names}
    X = pd.DataFrame(X_data)
    # Target correlated with trend distance and kalman velocity
    logits = X["Trend_Dist_EMA20"] * 2.0 + X["Kalman_Velocity"] * 1.5
    prob = 1.0 / (1.0 + np.exp(-logits))
    y = pd.Series((prob > 0.5).astype(int))

    model_path = str(tmp_path / "macro_model.pkl")
    labeler = ForexMetaLabeler(model_path=model_path, feature_names=feature_names)
    calibrated_model, params = labeler.train(X, y, n_splits=3)

    assert calibrated_model is not None
    assert os.path.exists(model_path)

    # Test inference
    sample_features = {f: float(X[f].iloc[0]) for f in feature_names}
    pred_prob = labeler.predict_probability(sample_features)
    assert 0.0 <= pred_prob <= 1.0

    # Test loading
    reloaded_labeler = ForexMetaLabeler(model_path=model_path, feature_names=feature_names)
    reloaded_prob = reloaded_labeler.predict_probability(sample_features)
    assert reloaded_prob == pytest.approx(pred_prob, abs=1e-4)
