"""
PostgreSQL SystemConfigRepository using SQLAlchemy Core.
Manages the system_config key-value JSONB table.
"""
import json
import logging
from typing import Any, Optional

from sqlalchemy import Engine, text

from .connection_pool import get_connection

logger = logging.getLogger(__name__)


class SystemConfigRepository:

    def __init__(self, engine: Engine):
        self.engine = engine

    def get(self, key: str) -> Optional[Any]:
        sql = text("SELECT value FROM system_config WHERE key = :key LIMIT 1")
        with get_connection(self.engine) as conn:
            row = conn.execute(sql, {"key": key}).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: Any) -> None:
        sql = text("""
            INSERT INTO system_config (key, value, updated_at)
            VALUES (:key, CAST(:value AS jsonb), NOW())
            ON CONFLICT (key) DO UPDATE
                SET value      = EXCLUDED.value,
                    updated_at = NOW()
        """)
        with get_connection(self.engine) as conn:
            conn.execute(sql, {"key": key, "value": json.dumps(value)})

    def delete(self, key: str) -> None:
        sql = text("DELETE FROM system_config WHERE key = :key")
        with get_connection(self.engine) as conn:
            conn.execute(sql, {"key": key})
