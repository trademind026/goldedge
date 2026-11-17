import logging
from typing import List, Optional
from datetime import datetime

from sqlalchemy import Engine, text
from ....domain.models.bar import Bar
from ....domain.ports.outbound.candle_repo import ICandleRepository
from .connection_pool import get_connection

logger = logging.getLogger(__name__)


class CandleRepository(ICandleRepository):
    """
    SQLAlchemy Core implementation for M1 Bars.
    """

    def __init__(self, engine: Engine):
        self.engine = engine

    # ──────────────────────────────────────────────────────────────
    # WRITE OPS
    # ──────────────────────────────────────────────────────────────

    def save_bar(self, bar: Bar) -> None:
        self.save_bars([bar])

    def save_bars(self, bars: List[Bar]) -> None:
        if not bars:
            return

        sql = text("""
            INSERT INTO market_bars_1m
                (time, symbol, open, high, low, close, tick_volume, real_volume, spread)
            VALUES
                (:time, :symbol, :open, :high, :low, :close,
                 :tick_volume, :real_volume, :spread)
            ON CONFLICT (symbol, time) DO NOTHING
        """)

        rows = [
            {
                "time": b.time,
                "symbol": b.symbol,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "tick_volume": b.tick_volume,
                "real_volume": b.real_volume,
                "spread": b.spread,
            }
            for b in bars
        ]

        with self.engine.begin() as conn:
            conn.execute(sql, rows)

    # ──────────────────────────────────────────────────────────────
    # READ OPS
    # ──────────────────────────────────────────────────────────────

    def get_bars(self, symbol: str, start_time: datetime, end_time: datetime) -> List[Bar]:
        sql = text("""
            SELECT time, symbol, open, high, low, close,
                   tick_volume, real_volume, spread
            FROM market_bars_1m
            WHERE symbol = :symbol
              AND time >= :start
              AND time <  :end
            ORDER BY time ASC
        """)
        with get_connection(self.engine) as conn:
            rows = conn.execute(sql, {"symbol": symbol, "start": start_time, "end": end_time}).fetchall()
        return [self._row_to_bar(r) for r in rows]

    def get_latest_bars(self, symbol: str, count: int) -> List[Bar]:
        sql = text("""
            SELECT time, symbol, open, high, low, close,
                   tick_volume, real_volume, spread
            FROM market_bars_1m
            WHERE symbol = :symbol
            ORDER BY time DESC
            LIMIT :count
        """)
        with get_connection(self.engine) as conn:
            rows = conn.execute(sql, {"symbol": symbol, "count": count}).fetchall()
        # Return oldest-first
        return [self._row_to_bar(r) for r in reversed(rows)]

    def upsert_1m_bars_from_ticks(self, symbol: str, start_time: datetime, end_exclusive: datetime) -> int:
        """Aggregate completed 1m bid OHLCV bars from market_ticks into market_bars_1m."""
        sql = text("""
            INSERT INTO market_bars_1m (
                time, symbol, open, high, low, close, tick_volume, real_volume, spread
            )
            SELECT
                time_bucket('1 minute', time) AS bucket_time,
                symbol,
                first(bid, time) AS open,
                max(bid) AS high,
                min(bid) AS low,
                last(bid, time) AS close,
                count(*) AS tick_volume,
                0 AS real_volume,
                GREATEST(0, ROUND((last(ask, time) - last(bid, time)) / 0.01))::int AS spread
            FROM market_ticks
            WHERE symbol = :symbol
              AND time >= :start
              AND time < :end_exclusive
              AND bid > 0
            GROUP BY bucket_time, symbol
            ON CONFLICT (symbol, time) DO NOTHING
        """)
        with self.engine.begin() as conn:
            res = conn.execute(
                sql,
                {"symbol": symbol, "start": start_time, "end_exclusive": end_exclusive},
            )
            return int(res.rowcount or 0)

    # ──────────────────────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────────────────────

    def _row_to_bar(self, row) -> Bar:
        return Bar(
            time=row.time,
            symbol=row.symbol,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            tick_volume=row.tick_volume,
            real_volume=row.real_volume,
            spread=row.spread
        )
