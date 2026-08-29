import redis
import json
from typing import Protocol, Any

class INotificationService(Protocol):
    def send_alert(self, level: str, message: str, details: Any = None) -> None:
        ...

class RedisSSENotifier(INotificationService):
    """
    Publishes system alerts and updates to a Redis Stream for the
    Flask SSE endpoint to fan-out to the frontend dashboard.
    """
    def __init__(self, redis_url: str):
        self.client = redis.from_url(redis_url, decode_responses=True)
        self.stream_name = "goldedge:system:alerts"
        self.max_len = 1000

    def send_alert(self, level: str, message: str, details: Any = None) -> None:
        payload = {
            "level": level,
            "message": message,
            "details": json.dumps(details) if details else ""
        }
        self.client.xadd(self.stream_name, payload, maxlen=self.max_len, approximate=True)
