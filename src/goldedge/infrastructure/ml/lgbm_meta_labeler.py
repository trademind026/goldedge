import lightgbm as lgb
import numpy as np
import os
import logging
from typing import Dict, Any

logger = logging.getLogger("lgbm_meta")

class LightGBMMetaLabeler:
    """
    ML Inference Engine for GoldEdge Phase 4.
    Loads the serialized LightGBM model and predicts the probability 
    of a trade reaching Take Profit before hitting the Stop Loss.
    """
    def __init__(self, model_path: str = "models/lgbm_meta.txt"):
        self.model_path = model_path
        self.model = None
        self.feature_names = [
            "kalman_vel", "kalman_accel", "shannon_entropy", 
            "kaufman_er", "rsi_14", "atr_14", "volume_ratio"
        ]

    def load_model(self):
        """Loads the trained model from disk."""
        if os.path.exists(self.model_path):
            try:
                self.model = lgb.Booster(model_file=self.model_path)
                logger.info(f"Loaded LightGBM model from {self.model_path}")
            except Exception as e:
                logger.error(f"Failed to load LightGBM model: {e}")
                self.model = None
        else:
            logger.warning(f"LightGBM model not found at {self.model_path}. Will return 1.0 probabilities.")

    def predict_probability(self, features: Dict[str, Any]) -> float:
        """
        Runs inference on the denoised features.
        Returns a float between 0.0 and 1.0 representing the win probability.
        Missing features are handled gracefully with np.nan.
        """
        if not self.model:
            return 1.0 # Default passthrough if no model is loaded

        # Construct feature array strictly in the order the model was trained on
        feature_vector = []
        for name in self.feature_names:
            val = features.get(name)
            # LightGBM handles np.nan natively for missing data splits
            feature_vector.append(val if val is not None else np.nan)

        try:
            # Predict returns an array of probabilities, take the first one
            prob = self.model.predict([feature_vector])[0]
            return float(prob)
        except Exception as e:
            logger.error(f"Inference error: {e}")
            return 1.0
