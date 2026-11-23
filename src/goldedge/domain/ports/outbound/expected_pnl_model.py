from typing import Dict, Optional, Protocol


class IExpectedPnlModel(Protocol):
    """Port for predicting expected trade PnL ($) from pre-entry features."""

    def predict_expected_pnl(self, features: Dict[str, float]) -> Optional[float]:
        """Return predicted trade PnL in USD, or None if no model is loaded."""
        ...
