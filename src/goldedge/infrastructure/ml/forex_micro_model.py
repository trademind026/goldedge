import os
import joblib
import logging
from typing import Dict, Any, Tuple, Optional, List
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.calibration import CalibratedClassifierCV
from sklearn.model_selection import TimeSeriesSplit

from .microstructure_feature_engine import MicrostructureFeatureEngine
from ...domain.ports.outbound.microstructure_model import IMicrostructureModel, IMicrostructureModelTrainer

logger = logging.getLogger("forex_micro_model")


class SessionGroupTimeSeriesSplit:
    """Group Time-Series Split preserving temporal order of trading sessions."""
    def __init__(self, n_splits: int = 3):
        self.n_splits = n_splits

    def get_n_splits(self, X=None, y=None, groups=None) -> int:
        return self.n_splits

    def split(self, X, y=None, groups=None):
        if groups is None:
            ts = TimeSeriesSplit(n_splits=self.n_splits)
            yield from ts.split(X, y)
            return

        grp_series = pd.Series(groups)
        unique_groups = grp_series.unique()
        n_groups = len(unique_groups)

        if n_groups <= self.n_splits:
            ts = TimeSeriesSplit(n_splits=min(self.n_splits, max(2, len(X) // 5)))
            yield from ts.split(X, y)
            return

        ts = TimeSeriesSplit(n_splits=self.n_splits)
        for tr_grp_idx, val_grp_idx in ts.split(unique_groups):
            train_grps = set(unique_groups[tr_grp_idx])
            val_grps = set(unique_groups[val_grp_idx])

            train_idx = np.where(grp_series.isin(train_grps))[0]
            val_idx = np.where(grp_series.isin(val_grps))[0]
            if len(train_idx) > 0 and len(val_idx) > 0:
                yield train_idx, val_idx


def train_forex_micro_model(
    X: pd.DataFrame,
    y: pd.Series,
    groups: Optional[pd.Series] = None,
    n_splits: int = 3,
) -> Tuple[CalibratedClassifierCV, Dict[str, Any]]:
    """
    2.3 Microstructure LightGBM Hyperparameters & 2.4 Platt Scaling (Sigmoid)
      - Objective: binary
      - Metric: binary_logloss, precision
      - Boosting Type: gbdt
      - Number of Estimators: 100
      - Learning Rate: 0.05
      - Max Depth: 3 (Ultra-shallow trees for sub-millisecond scoring)
      - Num Leaves: 8
      - Min Child Samples: 100 (or adaptive for smaller sample sizes)
      - Subsample (Bagging): 0.70
      - Feature Fraction: 0.80
      - Reg Alpha (L1): 1.00 (Aggressive sparsity)
      - Reg Lambda (L2): 2.00
      - Scale Pos Weight: 0.50 (Heavy penalty on False Positives / Fakeouts)
      - Calibration: Platt Scaling (Sigmoid Logistic)
    """
    params = {
        "objective": "binary",
        "metric": ["binary_logloss", "precision"],
        "boosting_type": "gbdt",
        "n_estimators": 100,
        "learning_rate": 0.05,
        "max_depth": 3,
        "num_leaves": 8,
        "min_child_samples": 100 if len(X) >= 300 else max(5, len(X) // 10),
        "subsample": 0.70,
        "colsample_bytree": 0.80,
        "reg_alpha": 1.00,
        "reg_lambda": 2.00,
        "scale_pos_weight": 0.50,
        "verbose": -1,
        "random_state": 42,
    }

    base_clf = lgb.LGBMClassifier(**params)

    # Cross-Validation: Group Time-Series Split by Trading Session if groups provided
    cv = SessionGroupTimeSeriesSplit(n_splits=n_splits)

    # Platt Scaling (Sigmoid Logistic)
    # CalibratedClassifierCV in scikit-learn accepts cv as a cv splitter generator or object
    splits = list(cv.split(X, y, groups=groups))
    calibrated_model = CalibratedClassifierCV(estimator=base_clf, method="sigmoid", cv=splits)
    calibrated_model.fit(X, y)

    return calibrated_model, params


class ForexMicroModel(IMicrostructureModel, IMicrostructureModelTrainer):
    """
    High-Frequency Microstructure ML Model living in the sub-second order flow realm.
    Predicts probability of an instantaneous explosive breakout momentum.
    Acts as the Hard Gatekeeper Kill-Switch (Veto <= 0.40).
    """

    DEFAULT_FEATURES = MicrostructureFeatureEngine.FEATURE_NAMES

    def __init__(
        self,
        model_path: str = "models/micro_model.pkl",
        feature_names: Optional[List[str]] = None,
    ):
        self.model_path = model_path
        self.feature_names = feature_names or self.DEFAULT_FEATURES
        self.model: Optional[CalibratedClassifierCV] = None
        self.load_model()

    def train(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        groups: Optional[pd.Series] = None,
        n_splits: int = 3,
    ) -> Tuple[Any, Dict[str, Any]]:
        """Trains and persists the Platt-calibrated Micro LightGBM model."""
        calibrated_model, best_params = train_forex_micro_model(
            X[self.feature_names], y, groups=groups, n_splits=n_splits
        )
        self.model = calibrated_model
        self.save_model()
        return calibrated_model, best_params

    def save_model(self):
        """Serializes model to disk."""
        os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
        joblib.dump({"model": self.model, "features": self.feature_names}, self.model_path)
        logger.info(f"Saved Calibrated Microstructure Model to {self.model_path}")

    def load_model(self):
        """Loads serialized model from disk."""
        if os.path.exists(self.model_path):
            try:
                data = joblib.load(self.model_path)
                if isinstance(data, dict) and "model" in data:
                    self.model = data["model"]
                    self.feature_names = data.get("features", self.feature_names)
                else:
                    self.model = data
                logger.info(f"Loaded Calibrated Microstructure Model from {self.model_path}")
            except Exception as e:
                logger.error(f"Failed to load Microstructure Model from {self.model_path}: {e}")
                self.model = None
        else:
            self.model = None

    def predict_probability(self, features: Dict[str, Any]) -> float:
        """
        Runs sub-millisecond inference and returns calibrated breakout probability in [0.0, 1.0].
        If no model is loaded, defaults to 0.70.
        """
        if self.model is None:
            return 0.70

        try:
            row = [features.get(f, 0.0) for f in self.feature_names]
            X = pd.DataFrame([row], columns=self.feature_names)
            probs = self.model.predict_proba(X)
            return float(probs[0, 1])
        except Exception as e:
            logger.warning(f"Error during Micro Model inference: {e}. Defaulting to 0.70")
            return 0.70
