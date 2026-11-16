import os
import joblib
import logging
from typing import Dict, Any, Tuple, Optional, List
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import roc_auc_score

from .purged_cv import PurgedKFold, PurgedTimeSeriesSplit
from .macro_feature_engine import MacroFeatureEngine
from ...domain.ports.outbound.meta_model import IMetaLabeler, IMetaModelTrainer

logger = logging.getLogger("forex_meta_model")


def train_forex_macro_model(
    X: pd.DataFrame,
    y: pd.Series,
    n_splits: int = 5,
    purge_bars: int = 30,
    embargo_bars: int = 5,
    **kwargs
) -> Tuple[CalibratedClassifierCV, Dict[str, Any]]:
    """
    1.4 LightGBM Model Hyperparameters (Tuned for Low Overfitting)
    and 1.5 Isotonic Probability Calibration:
      - Objective: binary
      - Metric: binary_logloss, auc
      - Boosting Type: gbdt
      - Number of Estimators: 150
      - Learning Rate: 0.03
      - Max Depth: 4 (Shallow trees prevent overfitting)
      - Num Leaves: 15 (Restricted branching)
      - Min Child Samples: 50 (Requires at least 50 samples per leaf)
      - Subsample (Bagging): 0.80
      - Subsample Frequency: 1
      - Feature Fraction: 0.70
      - Reg Alpha (L1): 0.50
      - Reg Lambda (L2): 1.00
      - Class Weight: balanced
      - Calibration: Isotonic Regression Calibrator (CalibratedClassifierCV)
    """
    params = {
        "objective": "binary",
        "metric": ["binary_logloss", "auc"],
        "boosting_type": "gbdt",
        "n_estimators": 150,
        "learning_rate": 0.03,
        "max_depth": 4,
        "num_leaves": 15,
        "min_child_samples": 50 if len(X) >= 150 else max(5, len(X) // 10),
        "subsample": 0.80,
        "subsample_freq": 1,
        "colsample_bytree": 0.70,
        "reg_alpha": 0.50,
        "reg_lambda": 1.00,
        "class_weight": "balanced",
        "verbose": -1,
        "random_state": 42,
    }

    base_clf = lgb.LGBMClassifier(**params)

    # 1.3 Validation Defense: Purged K-Fold Cross-Validation with 5-Bar Embargoes
    cv = PurgedKFold(n_splits=n_splits, purge_bars=purge_bars, embargo_bars=embargo_bars)

    # 1.5 Isotonic Probability Calibration
    calibrated_model = CalibratedClassifierCV(estimator=base_clf, method="isotonic", cv=cv)
    calibrated_model.fit(X, y)

    return calibrated_model, params


train_forex_meta_model = train_forex_macro_model


class ForexMetaLabeler(IMetaLabeler, IMetaModelTrainer):
    """
    Forex Meta-Model inference and training service adhering to Hexagonal ports.
    Evaluates candidate trade under the current global macroeconomic regime.
    Role: 50% Advisory Weight in Composite Score.
    """

    DEFAULT_FEATURES = MacroFeatureEngine.FEATURE_NAMES

    def __init__(
        self,
        model_path: str = "models/macro_model.pkl",
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
        n_splits: int = 5,
    ) -> Tuple[Any, Dict[str, Any]]:
        """Trains and persists the calibrated LightGBM model with Purged K-Fold and 5-bar embargoes."""
        calibrated_model, best_params = train_forex_macro_model(
            X[self.feature_names], y, n_splits=n_splits
        )
        self.model = calibrated_model
        self.save_model()
        return calibrated_model, best_params

    def save_model(self):
        """Serializes model to disk."""
        os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
        joblib.dump({"model": self.model, "features": self.feature_names}, self.model_path)
        logger.info(f"Saved Calibrated Macro Meta-Model to {self.model_path}")

    def load_model(self):
        """Loads serialized model from disk."""
        paths_to_try = [self.model_path, "models/forex_meta_model.joblib"]
        for p in paths_to_try:
            if os.path.exists(p):
                try:
                    data = joblib.load(p)
                    if isinstance(data, dict) and "model" in data:
                        self.model = data["model"]
                        self.feature_names = data.get("features", self.feature_names)
                    else:
                        self.model = data
                    logger.info(f"Loaded Calibrated Macro Meta-Model from {p}")
                    return
                except Exception as e:
                    logger.error(f"Failed to load Meta-Model from {p}: {e}")
        self.model = None

    def predict_probability(self, features: Dict[str, Any]) -> float:
        """
        Runs inference on 32-feature vector and returns calibrated win probability.
        If no model is loaded, defaults to 0.65.
        """
        if self.model is None:
            return 0.65

        try:
            row = [features.get(f, 0.0) for f in self.feature_names]
            X = pd.DataFrame([row], columns=self.feature_names)
            probs = self.model.predict_proba(X)
            return float(probs[0, 1])
        except Exception as e:
            logger.warning(f"Error during Macro Meta-Model inference: {e}. Defaulting to 0.65")
            return 0.65
