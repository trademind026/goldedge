from typing import Protocol, Dict, Any, Tuple


class IMetaLabeler(Protocol):
    """
    Protocol for Meta-Model inference producing calibrated win probabilities.
    """
    def predict_probability(self, features: Dict[str, Any]) -> float:
        """
        Runs inference on features and returns calibrated win probability (0.0 to 1.0).
        """
        ...


class IMetaModelTrainer(Protocol):
    """
    Protocol for training and calibrating meta-models using Purged CV.
    """
    def train(self, X: Any, y: Any, n_splits: int = 5) -> Tuple[Any, Dict[str, Any]]:
        """
        Trains model with Purged CV, Optuna hyperparameter optimization, and probability calibration.
        """
        ...
