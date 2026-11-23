import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from ....domain.ports.outbound.shadow_journal import IShadowJournal

logger = logging.getLogger(__name__)


class JsonlShadowJournal(IShadowJournal):
    """Thread-safe, restart-idempotent JSONL adapter for Phase 8 Shadow Live Validation telemetry."""

    def __init__(self, log_path: str = "backtest_results/shadow_live_rollovers.jsonl"):
        self.path = Path(log_path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seen_keys: set[tuple] = set()
        self._load_seen_keys()

    @staticmethod
    def _event_key(payload: Dict[str, Any]) -> Optional[tuple]:
        kind = payload.get("event")
        sym = payload.get("symbol")
        if kind == "SHADOW_ROLLOVER" and sym and payload.get("rollover_utc"):
            return ("SHADOW_ROLLOVER", str(sym), str(payload["rollover_utc"]))
        if kind == "SHADOW_OPEN" and sym and payload.get("direction") and payload.get("opened_at"):
            return ("SHADOW_OPEN", str(sym), str(payload["direction"]), str(payload["opened_at"]))
        if kind == "SHADOW_EXIT" and sym and payload.get("direction") and payload.get("opened_at"):
            return ("SHADOW_EXIT", str(sym), str(payload["direction"]), str(payload["opened_at"]))
        if kind == "SHADOW_PORTFOLIO_EXIT" and sym and payload.get("portfolio") and payload.get("opened_at"):
            return ("SHADOW_PORTFOLIO_EXIT", str(sym), str(payload["portfolio"]), str(payload["opened_at"]))
        return None

    def _load_seen_keys(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                        key = self._event_key(ev)
                        if key is not None:
                            self._seen_keys.add(key)
                    except Exception:
                        continue
        except Exception as exc:
            logger.error(f"Failed indexing shadow telemetry keys from {self.path}: {exc}")

    def has_rollover(self, symbol: str, rollover_utc: str) -> bool:
        with self._lock:
            return ("SHADOW_ROLLOVER", str(symbol), str(rollover_utc)) in self._seen_keys

    def record_event(self, payload: Dict[str, Any]) -> bool:
        key = self._event_key(payload)
        line = json.dumps(payload, default=str, ensure_ascii=False)
        try:
            with self._lock:
                if key is not None and key in self._seen_keys:
                    logger.debug(f"Skipping duplicate shadow journal event {key}")
                    return False
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                if key is not None:
                    self._seen_keys.add(key)
                return True
        except Exception as exc:
            logger.error(f"Failed to append shadow telemetry to {self.path}: {exc}")
            return False

    def read_all(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            with self._lock:
                with self.path.open("r", encoding="utf-8") as fh:
                    lines = fh.readlines()
            out = []
            for line in lines:
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
            return out
        except Exception as exc:
            logger.error(f"Failed to read shadow telemetry from {self.path}: {exc}")
            return []

    def read_recent(self, limit: int = 100) -> List[Dict[str, Any]]:
        all_events = self.read_all()
        return all_events[-max(1, int(limit)):]
