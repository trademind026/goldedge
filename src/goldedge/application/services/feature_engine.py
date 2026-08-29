import logging
from typing import Optional
from datetime import datetime

from src.goldedge.domain.ports.outbound.feature_repo import IFeatureRepository

logger = logging.getLogger("feature_engine")

class FeatureEngineService:
    """
    Application Service that orchestrates the generation of Machine Learning features.
    Relies on Dependency Injection (IFeatureRepository) to keep Hexagonal Architecture clean.
    """
    def __init__(self, feature_repo: IFeatureRepository):
        self.repo = feature_repo

    def generate_features(self, symbol: str, start: Optional[datetime] = None, end: Optional[datetime] = None):
        logger.info(f"Triggering Feature Generation for {symbol}...")
        
        rows_saved = self.repo.generate_features(symbol, start, end)
        
        if rows_saved > 0:
            logger.info(f"Orchestration complete: Computed and saved {rows_saved} feature rows for {symbol}!")
        else:
            logger.warning(f"Orchestration complete: No features generated for {symbol}.")
