from typing import Callable, Dict, List, Any
from queue import Queue
import threading
from ....domain.ports.outbound.event_bus import IEventBus
from ....domain.events.base import DomainEvent

class InMemoryEventBus(IEventBus):
    """
    A simple thread-safe in-memory event bus for local testing and backtesting.
    """
    def __init__(self):
        self.handlers: Dict[str, List[Callable[[DomainEvent], Any]]] = {}
        self.queue: Queue = Queue()
        self.running = False
        self.thread = None

    def publish(self, event: DomainEvent) -> None:
        self.queue.put(event)

    def consume(self, topic: str, handler: Callable[[DomainEvent], Any]) -> None:
        if topic not in self.handlers:
            self.handlers[topic] = []
        self.handlers[topic].append(handler)
        
    def ack(self, message_id: str) -> None:
        pass # No-op for in-memory
        
    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._process_events, daemon=True)
        self.thread.start()
        
    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)

    def _process_events(self):
        while self.running:
            try:
                event = self.queue.get(timeout=0.1)
                topic = event.event_type
                if topic in self.handlers:
                    for handler in self.handlers[topic]:
                        try:
                            handler(event)
                        except Exception as e:
                            print(f"Error handling event {event}: {e}")
                self.queue.task_done()
            except Exception:
                pass
