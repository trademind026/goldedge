from dataclasses import dataclass
from enum import Enum

class TradingSession(str, Enum):
    ASIA = "ASIA"
    LONDON = "LONDON"
    NY = "NY"
    OVERLAP = "OVERLAP"
    CLOSED = "CLOSED"

@dataclass(frozen=True)
class SessionState:
    current_session: TradingSession
    is_active: bool
    time_to_next_session: float
