import logging
from pathlib import Path
from typing import Dict, Optional

import joblib
import numpy as np

from ....domain.ports.outbound.expected_pnl_model import IExpectedPnlModel

logger = logging.getLogger(__name__)


class RidgeExpectedPnlAdapter(IExpectedPnlModel):
    """Sklearn Ridge Regressor adapter predicting expected trade PnL ($)."""

    def __init__(self, model_path: str = "models/gold_ridge_pnl.pkl"):
        self.model_path = Path(model_path)
        self._pipeline = None
        self._features: list[str] = []
        self._load()

    def _load(self) -> None:
        if not self.model_path.exists():
            logger.warning(f"Ridge expected-PnL model not found at {self.model_path}; running without ML filter.")
            return
        try:
            bundle = joblib.load(self.model_path)
            self._pipeline = bundle["pipeline"]
            self._features = list(bundle["features"])
            logger.info(
                f"Loaded Ridge expected-PnL model from {self.model_path} "
                f"({len(self._features)} features, trained_on={bundle.get('trained_on')})."
            )
        except Exception as exc:
            logger.error(f"Failed to load Ridge expected-PnL model from {self.model_path}: {exc}")
            self._pipeline = None
            self._features = []

    def predict_expected_pnl(self, features: Dict[str, float]) -> Optional[float]:
        if self._pipeline is None or not self._features:
            return None
        row = np.array([[float(features.get(col, 0.0) or 0.0) for col in self._features]], dtype=np.float64)
        row = np.nan_to_num(row, nan=0.0, posinf=0.0, neginf=0.0)
        return float(self._pipeline.predict(row)[0])
