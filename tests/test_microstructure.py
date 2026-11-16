import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from src.goldedge.adapters.secondary.features.microstructure_feature_repo import (
    compute_forex_micro_features,
    MicrostructureFeatureRepository
)


def test_compute_forex_micro_features():
    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)
    records = []
    for i in range(25):
        # 1-second ticks
        t = t0 + timedelta(milliseconds=i * 500)
        bid = 1.0850 + (i * 0.00001)
        ask = bid + 0.00012
        # Alternate buyer/seller aggressor
        price = ask if i % 2 == 0 else bid
        records.append({
            "timestamp": t,
            "bid": bid,
            "ask": ask,
            "price": price,
            "volume": 5
        })

    tick_df = pd.DataFrame(records)
    agg = compute_forex_micro_features(tick_df, window_seconds=10)

    assert not agg.empty
    assert "spread" in agg.columns
    assert "buy_vol" in agg.columns
    assert "sell_vol" in agg.columns
    assert "tick_momentum" in agg.columns
    assert "burst_ratio" in agg.columns
    assert len(agg) > 0


def test_microstructure_feature_repository():
    repo = MicrostructureFeatureRepository()
    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)
    data = [
        {"timestamp": t0, "bid": 1.0850, "ask": 1.0851, "price": 1.0851, "volume": 10},
        {"timestamp": t0 + timedelta(seconds=1), "bid": 1.0851, "ask": 1.0852, "price": 1.0851, "volume": 5},
    ]
    df = repo.compute_micro_features(data)
    assert not df.empty
    assert "tick_momentum" in df.columns
