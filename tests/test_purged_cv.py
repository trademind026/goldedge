import pytest
import numpy as np
from src.goldedge.infrastructure.ml.purged_cv import PurgedTimeSeriesSplit


def test_purged_cv_split_shapes():
    n_samples = 100
    X = np.random.randn(n_samples, 4)
    y = np.random.randint(0, 2, n_samples)

    cv = PurgedTimeSeriesSplit(n_splits=5, purge_bars=10, embargo_bars=2)
    assert cv.get_n_splits() == 5

    splits = list(cv.split(X, y))
    assert len(splits) == 5

    for train_idx, val_idx in splits:
        val_start = val_idx[0]
        # Purge verification: no train index should be within [val_start - purge_bars, val_start)
        assert np.all(train_idx < (val_start - 10))
        assert len(val_idx) > 0
