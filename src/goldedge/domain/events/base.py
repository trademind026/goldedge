from dataclasses import dataclass, field
from datetime import datetime, timezone
import uuid

def _generate_id() -> str:
    return str(uuid.uuid4())

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    event_id: str = field(default_factory=_generate_id)
    timestamp: datetime = field(default_factory=_now_utc)
    correlation_id: str = field(default_factory=_generate_id)
    
    @property
    def event_type(self) -> str:
        return self.__class__.__name__
