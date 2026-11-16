import os
import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta

from goldedge.infrastructure.ml.microstructure_feature_engine import MicrostructureFeatureEngine
from goldedge.infrastructure.ml.microstructure_labeler import MicrostructureExplosiveBreakoutLabeler
from goldedge.infrastructure.ml.forex_micro_model import ForexMicroModel
from goldedge.domain.services.forex_micro_gatekeeper import ForexMicroGatekeeper
from goldedge.domain.models.signal import SignalDirection


def test_microstructure_feature_engine_computes_16_features():
    engine = MicrostructureFeatureEngine()
    assert len(engine.FEATURE_NAMES) == 16

    t0 = datetime(2026, 11, 10, 14, 0, 0, tzinfo=timezone.utc)
    rows = []
    # Simulate 50 ticks over 35 seconds
    for i in range(50):
        t = t0 + timedelta(milliseconds=i * 700)
        bid = 1.08500 + (i * 0.00002)
        ask = bid + 0.00012  # 1.2 pip spread
        # Alternate aggressor: hitting ask (buy) or hitting bid (sell)
        px = ask if (i % 3 != 0) else bid
        rows.append({
            "time": t,
            "bid": bid,
            "ask": ask,
            "price": px,
            "volume": 2.5 + (i * 0.1),
            "bid_qty": 150.0,
            "ask_qty": 120.0,
        })

    df_ticks = pd.DataFrame(rows)
    feats = engine.compute_features(
        ticks_df=df_ticks,
        symbol="EURUSD",
        current_time=t0 + timedelta(milliseconds=49 * 700),
    )

    assert len(feats) == 16
    for name in engine.FEATURE_NAMES:
        assert name in feats
        val = feats[name]
        assert not np.isnan(val), f"Feature {name} is NaN"
        assert not np.isinf(val), f"Feature {name} is Inf"

    # Verify OFI calculation bounds
    assert -1.0 <= feats["OFI_10s"] <= 1.0
    assert -1.0 <= feats["OFI_30s"] <= 1.0
    assert feats["Spread_Pips"] == pytest.approx(1.2, abs=0.1)
    assert feats["Burst_Intensity"] >= 1.0


def test_microstructure_explosive_breakout_labeling():
    labeler = MicrostructureExplosiveBreakoutLabeler(
        favorable_spread_mult=2.5,
        max_adverse_spread_mult=0.5,
        horizon_seconds=60.0,
    )

    t0 = datetime(2026, 11, 10, 14, 0, 0, tzinfo=timezone.utc)

    # 1. Clean Breakout (Target = 1):
    # Entry at tick 0: bid=1.0850, ask=1.0851 (spread=0.00010 = 1 pip)
    # Target requires +2.5 pips = +0.00025 within 60s, max adverse <= 0.5 pips = 0.00005
    rows_clean = [
        {"time": t0, "bid": 1.08500, "ask": 1.08510, "price": 1.08505},
        {"time": t0 + timedelta(seconds=10), "bid": 1.08510, "ask": 1.08520, "price": 1.08515},
        {"time": t0 + timedelta(seconds=25), "bid": 1.08535, "ask": 1.08545, "price": 1.08540},  # +3.5 pips favorable
    ]
    df_clean = pd.DataFrame(rows_clean)
    label_clean = labeler.label_tick(df_clean, tick_idx=0, direction=1)
    assert label_clean == 1

    # 2. Fakeout / Trap (Target = 0):
    # Adverse excursion reaches 0.00008 (> 0.5 * 0.00010)
    rows_fakeout = [
        {"time": t0, "bid": 1.08500, "ask": 1.08510, "price": 1.08505},
        {"time": t0 + timedelta(seconds=5), "bid": 1.08490, "ask": 1.08500, "price": 1.08495},  # -1.0 pip pullback!
        {"time": t0 + timedelta(seconds=25), "bid": 1.08535, "ask": 1.08545, "price": 1.08540},
    ]
    df_fakeout = pd.DataFrame(rows_fakeout)
    label_fakeout = labeler.label_tick(df_fakeout, tick_idx=0, direction=1)
    assert label_fakeout == 0


def test_forex_micro_model_training_and_platt_scaling(tmp_path):
    np.random.seed(42)
    n = 100
    engine = MicrostructureFeatureEngine()
    feature_names = engine.FEATURE_NAMES

    X_data = {f: np.random.randn(n) for f in feature_names}
    X = pd.DataFrame(X_data)
    logits = X["OFI_30s"] * 2.5 + X["Tick_Momentum"] * 1.5
    prob = 1.0 / (1.0 + np.exp(-logits))
    y = pd.Series((prob > 0.5).astype(int))
    sessions = pd.Series(np.random.choice(["ASIA", "LONDON", "NY"], size=n))

    model_path = str(tmp_path / "micro_model.pkl")
    micro_model = ForexMicroModel(model_path=model_path, feature_names=feature_names)
    calibrated_model, params = micro_model.train(X, y, groups=sessions, n_splits=3)

    assert calibrated_model is not None
    assert os.path.exists(model_path)

    sample_features = {f: float(X[f].iloc[0]) for f in feature_names}
    pred_prob = micro_model.predict_probability(sample_features)
    assert 0.0 <= pred_prob <= 1.0

    reloaded = ForexMicroModel(model_path=model_path, feature_names=feature_names)
    assert reloaded.predict_probability(sample_features) == pytest.approx(pred_prob, abs=1e-4)


def test_forex_micro_gatekeeper_kill_switch():
    gatekeeper = ForexMicroGatekeeper(min_probability_hurdle=0.40, ofi_veto_threshold=0.10)

    # 1. P <= 0.40 -> Hard Veto
    passed, reason = gatekeeper.evaluate(micro_prob=0.35, ofi_30s=0.20, direction=SignalDirection.BUY)
    assert passed is False
    assert "MICRO_MOMENTUM_STALLED" in reason

    # 2. BUY signal vetoed if OFI < -0.10 (Sellers hitting bid)
    passed, reason = gatekeeper.evaluate(micro_prob=0.65, ofi_30s=-0.25, direction=SignalDirection.BUY)
    assert passed is False
    assert "OFI_SELLER_ABSORPTION" in reason

    # 3. SELL signal vetoed if OFI > +0.10 (Buyers hitting ask)
    passed, reason = gatekeeper.evaluate(micro_prob=0.72, ofi_30s=0.30, direction=SignalDirection.SELL)
    assert passed is False
    assert "OFI_BUYER_ABSORPTION" in reason

    # 4. Valid BUY setup passes
    passed, reason = gatekeeper.evaluate(micro_prob=0.68, ofi_30s=0.35, direction=SignalDirection.BUY)
    assert passed is True
    assert reason == "PASSED"

    # 5. Valid SELL setup passes
    passed, reason = gatekeeper.evaluate(micro_prob=0.60, ofi_30s=-0.40, direction="SELL")
    assert passed is True
    assert reason == "PASSED"
