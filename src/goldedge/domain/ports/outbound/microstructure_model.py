from typing import Protocol, Dict, Any, Tuple, Optional


class IMicrostructureModel(Protocol):
    """
    Protocol for Microstructure ML Model inference predicting instantaneous
    explosive breakout momentum.
    """
    def predict_probability(self, features: Dict[str, Any]) -> float:
        """
        Runs inference on sub-second microstructure features and returns
        Platt-scaled breakout probability in [0.0, 1.0].
        """
        ...


class IMicrostructureModelTrainer(Protocol):
    """
    Protocol for training and calibrating the Microstructure ML Model
    using Session Group Time-Series Split and Platt scaling.
    """
    def train(self, X: Any, y: Any, groups: Optional[Any] = None) -> Tuple[Any, Dict[str, Any]]:
        """
        Trains High-Speed LightGBM with Session Group CV and Platt scaling.
        """
        ...
