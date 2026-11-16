from typing import Protocol, Any, Dict, List
from datetime import datetime


class IMicrostructureFeatureRepository(Protocol):
    """
    Protocol for extracting order flow, aggressor classification, and tick momentum features.
    """
    def compute_micro_features(self, tick_data: Any, window_seconds: int = 30) -> Any:
        """
        Computes microstructure features:
        - Spread expansion
        - Order flow tick momentum (Lee-Ready trade classification)
        - Tick intensity / burst ratio
        """
        ...
