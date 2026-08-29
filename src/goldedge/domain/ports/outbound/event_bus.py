from typing import Protocol, Callable, Any
from ...events.base import DomainEvent

class IEventBus(Protocol):
    def publish(self, event: DomainEvent) -> None:
        """Publishes an event to the bus."""
        ...
        
    def consume(self, topic: str, handler: Callable[[DomainEvent], Any]) -> None:
        """Registers a handler for a specific topic or event type."""
        ...
        
    def ack(self, message_id: str) -> None:
        """Acknowledges the successful processing of a message."""
        ...
