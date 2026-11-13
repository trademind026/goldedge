"""
In-memory Event Bus for Backtesting.
Implements IEventBus — collects events in a list for logging/analysis.
No Redis or external infrastructure required.
"""
from typing import Callable, Any, List
from ...domain.events.base import DomainEvent


class InMemoryEventBus:
    """IEventBus implementation that stores events in-memory."""

    def __init__(self):
        self._events: List[DomainEvent] = []
        self._handlers: dict[str, list[Callable]] = {}

    def publish(self, event: DomainEvent) -> None:
        self._events.append(event)
        # Also dispatch to registered handlers
        topic = event.event_type
        for handler in self._handlers.get(topic, []):
            handler(event)

    def consume(self, topic: str, handler: Callable[[DomainEvent], Any]) -> None:
        self._handlers.setdefault(topic, []).append(handler)

    def ack(self, message_id: str) -> None:
        pass  # No-op in memory

    @property
    def events(self) -> List[DomainEvent]:
        return self._events

    def clear(self) -> None:
        self._events.clear()
