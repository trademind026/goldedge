import numpy as np
from sklearn.model_selection import TimeSeriesSplit, KFold
from typing import Generator, Tuple, Optional


class PurgedTimeSeriesSplit:
    """
    Advances in Financial Machine Learning (Marcos López de Prado, Ch. 7)
    - Purging: Drops training observations whose forward-looking labels overlap the test fold.
    - Embargo: Adds a time buffer post-test fold to remove auto-correlation leakage.
    """

    def __init__(self, n_splits: int = 5, purge_bars: int = 30, embargo_bars: int = 5):
        self.n_splits = n_splits
        self.purge_bars = purge_bars
        self.embargo_bars = embargo_bars

    def get_n_splits(self, X=None, y=None, groups=None) -> int:
        return self.n_splits

    def split(self, X, y=None, groups=None) -> Generator[Tuple[np.ndarray, np.ndarray], None, None]:
        tscv = TimeSeriesSplit(n_splits=self.n_splits)
        for train_raw, val_idx in tscv.split(X):
            val_start = val_idx[0]
            # Drop training rows that reach into the test window
            purge_cutoff = val_start - self.purge_bars
            train_purged = train_raw[train_raw < purge_cutoff]
            yield train_purged, val_idx


class PurgedKFold:
    """
    Purged K-Fold Cross-Validation with 5-Bar Embargoes.
    Splits time-series into K sequential folds.
    For each test fold [val_start, val_end]:
    - Purging: excludes training samples within [val_start - purge_bars, val_start]
    - Embargo: excludes training samples within [val_end, val_end + embargo_bars]
    """

    def __init__(self, n_splits: int = 5, purge_bars: int = 30, embargo_bars: int = 5):
        self.n_splits = n_splits
        self.purge_bars = purge_bars
        self.embargo_bars = embargo_bars

    def get_n_splits(self, X=None, y=None, groups=None) -> int:
        return self.n_splits

    def split(self, X, y=None, groups=None) -> Generator[Tuple[np.ndarray, np.ndarray], None, None]:
        n_samples = len(X)
        indices = np.arange(n_samples)
        fold_size = n_samples // self.n_splits

        for i in range(self.n_splits):
            val_start = i * fold_size
            val_end = (i + 1) * fold_size if i < self.n_splits - 1 else n_samples
            val_idx = indices[val_start:val_end]

            # Purge window before test fold
            purge_start = max(0, val_start - self.purge_bars)
            # Embargo window after test fold
            embargo_end = min(n_samples, val_end + self.embargo_bars)

            # Valid training indices are those strictly before purge_start or strictly after embargo_end
            train_mask = (indices < purge_start) | (indices >= embargo_end)
            train_idx = indices[train_mask]

            yield train_idx, val_idx
