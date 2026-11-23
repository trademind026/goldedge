from typing import Any, Dict, List, Optional, Protocol


class IBacktestRepository(Protocol):
    """Outbound port for persisting and querying completed backtest runs."""

    def save_run(self, run_data: Dict[str, Any]) -> None:
        ...

    def list_runs(
        self,
        limit: int = 25,
        offset: int = 0,
        risk: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        ...

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        ...
