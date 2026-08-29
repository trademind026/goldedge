from typing import Protocol, Optional
from datetime import datetime

class IFeatureRepository(Protocol):
    def generate_features(self, symbol: str, start: Optional[datetime] = None, end: Optional[datetime] = None) -> int:
        """
        Reads OHLCV bars, computes ML features (Kalman, Technicals, Sessions), 
        and saves them to the feature store. Returns number of rows saved.
        """
        ...
