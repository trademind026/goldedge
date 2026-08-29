import json
import logging
from typing import Callable, Any
from datetime import datetime
import redis
from ....domain.ports.outbound.event_bus import IEventBus
from ....domain.events.base import DomainEvent

from dataclasses import asdict

logger = logging.getLogger(__name__)

# Custom JSON Encoder for dataclasses and datetimes
class DomainEncoder(json.JSONEncoder):
    def default(self, obj):
        if hasattr(obj, "__dataclass_fields__"):
            return asdict(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

IDEMPOTENCY_SCRIPT = """
if redis.call("EXISTS", KEYS[1]) == 1 then
    return 0
else
    redis.call("SET", KEYS[1], "1", "EX", ARGV[1])
    return 1
end
"""

class RedisStreamEventBus(IEventBus):
    """
    Production Event Bus using Redis Streams with Consumer Groups.
    Implements reliable two-phase startup consumption, idempotency, and DLQ.
    """
    def __init__(self, redis_url: str, consumer_group: str, consumer_name: str, max_stream_length: int = 10000):
        self.client = redis.from_url(redis_url, decode_responses=True)
        self.group = consumer_group
        self.name = consumer_name
        self.max_len = max_stream_length
        self._idempotency_sha = self.client.script_load(IDEMPOTENCY_SCRIPT)
        
    def _create_group_if_not_exists(self, stream_name: str):
        try:
            self.client.xgroup_create(stream_name, self.group, id="0", mkstream=True)
        except redis.exceptions.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def publish(self, event: DomainEvent) -> None:
        stream_name = f"goldedge:events:{event.event_type}"
        payload = {
            "event_id": event.event_id,
            "correlation_id": event.correlation_id,
            "timestamp": event.timestamp.isoformat(),
            "payload": json.dumps(getattr(event, "payload", event), cls=DomainEncoder)
        }
        # Approximate MAXLEN uses ~ for high performance
        self.client.xadd(stream_name, payload, maxlen=self.max_len, approximate=True)

    def consume(self, topic: str, handler: Callable[[DomainEvent], Any]) -> None:
        """
        Sets up the stream and performs a single consumption loop (Two-phase pattern).
        In production, this would be wrapped in an asyncio or Thread loop.
        """
        stream_name = f"goldedge:events:{topic}"
        self._create_group_if_not_exists(stream_name)
        
        # Phase 1: Drain PEL (Pending Entries List) for recovering crashed messages
        while True:
            pending = self.client.xreadgroup(self.group, self.name, {stream_name: "0"}, count=10)
            if not pending or not pending[0][1]:
                break
                
            for msg_id, msg_data in pending[0][1]:
                self._process_message(stream_name, msg_id, msg_data, handler)
                
        # Phase 2: Read new messages blocking
        new_msgs = self.client.xreadgroup(self.group, self.name, {stream_name: ">"}, count=10, block=1000)
        if new_msgs and new_msgs[0][1]:
            for msg_id, msg_data in new_msgs[0][1]:
                self._process_message(stream_name, msg_id, msg_data, handler)

    def _process_message(self, stream_name: str, msg_id: str, msg_data: dict, handler: Callable):
        event_id = msg_data.get("event_id")
        
        # Idempotency check: 24 hour TTL
        idemp_key = f"idempotency:{self.group}:{event_id}"
        if not self.client.evalsha(self._idempotency_sha, 1, idemp_key, 86400):
            self._ack_internal(stream_name, msg_id)
            return
            
        try:
            # Reconstruct domain event dynamically
            class DynamicEvent(DomainEvent):
                pass
            
            event = DynamicEvent(event_id=event_id)
            event.__dict__.update(msg_data)
            
            handler(event)
            self._ack_internal(stream_name, msg_id)
            
        except Exception as e:
            logger.error(f"Error processing msg {msg_id}: {e}")
            self.move_to_dlq(stream_name, msg_id, msg_data, str(e))
            self._ack_internal(stream_name, msg_id) 

    def _ack_internal(self, stream_name: str, message_id: str) -> None:
        self.client.xack(stream_name, self.group, message_id)

    def ack(self, message_id: str) -> None:
        """IEventBus interface compliance (Expects stream_name:msg_id format)"""
        if ":" in message_id:
            stream, msg = message_id.rsplit(":", 1)
            self._ack_internal(stream, msg)
        
    def move_to_dlq(self, original_stream: str, msg_id: str, msg_data: dict, error_reason: str):
        dlq_stream = f"{original_stream}:dlq"
        msg_data["dlq_reason"] = error_reason
        msg_data["original_msg_id"] = msg_id
        self.client.xadd(dlq_stream, msg_data, maxlen=self.max_len, approximate=True)
