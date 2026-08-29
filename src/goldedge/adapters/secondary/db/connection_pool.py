"""
Database engine factory for GoldEdge.
Uses SQLAlchemy Core (NOT the ORM) — no models, no magic, just clean connection pooling
and type-safe query execution on top of psycopg2.

All repositories receive an `Engine` instance via dependency injection.
Only this file and the repositories may import sqlalchemy.
"""
import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, text, Engine
from sqlalchemy.pool import QueuePool

logger = logging.getLogger(__name__)


def build_engine(db_url: str, pool_size: int = 5, max_overflow: int = 10) -> Engine:
    """
    Creates and returns a SQLAlchemy Engine with a QueuePool.
    Called once at application startup in the DI Container.

    Args:
        db_url:       PostgreSQL DSN e.g. 'postgresql+psycopg2://user:pass@host/db'
        pool_size:    Number of permanent connections in the pool.
        max_overflow: Extra connections allowed beyond pool_size under burst load.
    """
    # Ensure driver prefix is correct
    if db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)

    engine = create_engine(
        db_url,
        poolclass=QueuePool,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,   # verify connection is alive before using it
        echo=False            # set True to log all SQL (useful for debugging)
    )
    logger.info(f"SQLAlchemy engine created (pool_size={pool_size}, max_overflow={max_overflow})")
    return engine


@contextmanager
def get_connection(engine: Engine) -> Generator:
    """
    Context manager that checks out a connection from the SQLAlchemy pool,
    commits on success, rolls back on exception, and always returns it.

    Usage:
        with get_connection(engine) as conn:
            conn.execute(text("SELECT 1"))
    """
    with engine.connect() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
