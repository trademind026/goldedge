from typing import Any, Dict, List, Protocol


class IShadowJournal(Protocol):
    """Outbound port for recording live shadow rollover telemetry without real execution."""

    def record_event(self, payload: Dict[str, Any]) -> bool:
        ...

    def read_recent(self, limit: int = 100) -> List[Dict[str, Any]]:
        ...

    def read_all(self) -> List[Dict[str, Any]]:
        ...

    def has_rollover(self, symbol: str, rollover_utc: str) -> bool:
        ...
