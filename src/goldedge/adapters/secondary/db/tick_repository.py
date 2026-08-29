import logging
from typing import List, Generator
from datetime import datetime

from sqlalchemy import Engine, text
from ....domain.models.bar import Tick
from ....domain.ports.outbound.tick_repo import ITickRepository
from .connection_pool import get_connection

logger = logging.getLogger(__name__)


class TickRepository(ITickRepository):
    """
    SQLAlchemy Core implementation for Ticks.
    Supports server-side cursor streaming (yield_per) to prevent OOM
    when backtesting millions of ticks.
    """

    def __init__(self, engine: Engine):
        self.engine = engine

    # ──────────────────────────────────────────────────────────────
    # WRITE OPS
    # ──────────────────────────────────────────────────────────────

    def save_ticks(self, ticks: List[Tick]) -> None:
        if not ticks:
            return

        sql = text("""
            INSERT INTO market_ticks
                (time, symbol, bid, ask, last, volume, flags)
            VALUES
                (:time, :symbol, :bid, :ask, :last, :volume, :flags)
            ON CONFLICT (symbol, time) DO NOTHING
        """)

        rows = [
            {
                "time": t.time,
                "symbol": t.symbol,
                "bid": t.bid,
                "ask": t.ask,
                "last": t.last,
                "volume": t.volume,
                "flags": t.flags
            }
            for t in ticks
        ]

        with self.engine.begin() as conn:
            conn.execute(sql, rows)

    # ──────────────────────────────────────────────────────────────
    # READ OPS
    # ──────────────────────────────────────────────────────────────

    def get_ticks(self, symbol: str, start_time: datetime, end_time: datetime) -> List[Tick]:
        """Loads ticks entirely into memory (use only for small ranges)."""
        sql = text("""
            SELECT time, symbol, bid, ask, last, volume, flags
            FROM market_ticks
            WHERE symbol = :symbol
              AND time >= :start
              AND time <= :end
            ORDER BY time ASC
        """)
        with get_connection(self.engine) as conn:
            rows = conn.execute(sql, {"symbol": symbol, "start": start_time, "end": end_time}).fetchall()
        return [self._row_to_tick(r) for r in rows]

    def stream_ticks(self, symbol: str, start_time: datetime, end_time: datetime, chunk_size: int = 50000) -> Generator[Tick, None, None]:
        """
        Streams ticks from the DB using a server-side cursor to prevent memory overload.
        (SQLAlchemy Core execution_options yield_per)
        """
        sql = text("""
            SELECT time, symbol, bid, ask, last, volume, flags
            FROM market_ticks
            WHERE symbol = :symbol
              AND time >= :start
              AND time <= :end
            ORDER BY time ASC
        """)
        
        # Must run inside a connection block
        with self.engine.connect() as conn:
            # yield_per enables server-side cursor behind the scenes in pg8000/psycopg
            result = conn.execution_options(yield_per=chunk_size).execute(
                sql, {"symbol": symbol, "start": start_time, "end": end_time}
            )
            for row in result:
                yield self._row_to_tick(row)

    # ──────────────────────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────────────────────

    def _row_to_tick(self, row) -> Tick:
        return Tick(
            time=row.time,
            symbol=row.symbol,
            bid=row.bid,
            ask=row.ask,
            last=row.last,
            volume=row.volume,
            flags=row.flags
        )
