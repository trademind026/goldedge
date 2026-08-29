import json
import logging
from typing import List, Optional
from datetime import datetime

from sqlalchemy import Engine, text
from ....domain.models.trade import Position, TradeStatus
from ....domain.ports.outbound.trade_repo import ITradeRepository
from .connection_pool import get_connection

logger = logging.getLogger(__name__)


class TradeRepository(ITradeRepository):
    """
    SQLAlchemy Core implementation for Trade Persistence.
    Tracks orders and executions.
    """

    def __init__(self, engine: Engine):
        self.engine = engine

    # ──────────────────────────────────────────────────────────────
    # WRITE OPS
    # ──────────────────────────────────────────────────────────────

    def save_position(self, position: Position) -> None:
        """
        Upserts a position into the market_trades table.
        Used by the VirtualBroker to sync executed trades, and the JourneyLogger 
        to update the JSONB log when trades close.
        """
        sql = text("""
            INSERT INTO market_trades (
                ticket, time_execution, time_closing, symbol, type,
                volume, price_open, price_sl, price_tp, price_close,
                commission, swap, profit, status, ml_score, risk_profile, journey_log
            ) VALUES (
                :ticket, :time_ex, :time_close, :symbol, :type,
                :vol, :p_open, :p_sl, :p_tp, :p_close,
                :comm, :swap, :profit, :status, :ml, :risk, :j_log
            )
            ON CONFLICT (ticket) DO UPDATE SET
                time_closing = EXCLUDED.time_closing,
                price_sl = EXCLUDED.price_sl,
                price_tp = EXCLUDED.price_tp,
                price_close = EXCLUDED.price_close,
                profit = EXCLUDED.profit,
                status = EXCLUDED.status,
                journey_log = EXCLUDED.journey_log
        """)

        j_log_str = json.dumps(position.journey_log) if position.journey_log else None

        params = {
            "ticket": position.ticket,
            "time_ex": position.time_execution,
            "time_close": position.time_closing,
            "symbol": position.symbol,
            "type": position.type,
            "vol": position.volume,
            "p_open": position.price_open,
            "p_sl": position.price_sl,
            "p_tp": position.price_tp,
            "p_close": position.price_close,
            "comm": position.commission,
            "swap": position.swap,
            "profit": position.profit,
            "status": position.status.value,
            "ml": position.ml_score,
            "risk": position.risk_profile,
            "j_log": j_log_str
        }

        with self.engine.begin() as conn:
            conn.execute(sql, params)

    def save_positions(self, positions: List[Position]) -> None:
        if not positions:
            return
        # A full production implementation would use bulk executemany here
        # For simplicity in Phase 1, we map to save_position.
        for p in positions:
            self.save_position(p)

    # ──────────────────────────────────────────────────────────────
    # READ OPS
    # ──────────────────────────────────────────────────────────────

    def get_position(self, ticket: int) -> Optional[Position]:
        sql = text("SELECT * FROM market_trades WHERE ticket = :ticket")
        with get_connection(self.engine) as conn:
            row = conn.execute(sql, {"ticket": ticket}).fetchone()
        return self._row_to_pos(row) if row else None

    def get_positions_by_status(self, status: TradeStatus) -> List[Position]:
        sql = text("SELECT * FROM market_trades WHERE status = :status ORDER BY time_execution ASC")
        with get_connection(self.engine) as conn:
            rows = conn.execute(sql, {"status": status.value}).fetchall()
        return [self._row_to_pos(r) for r in rows]

    def get_positions_by_symbol(self, symbol: str) -> List[Position]:
        sql = text("SELECT * FROM market_trades WHERE symbol = :symbol ORDER BY time_execution ASC")
        with get_connection(self.engine) as conn:
            rows = conn.execute(sql, {"symbol": symbol}).fetchall()
        return [self._row_to_pos(r) for r in rows]

    # ──────────────────────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────────────────────

    def _row_to_pos(self, row) -> Position:
        return Position(
            ticket=row.ticket,
            symbol=row.symbol,
            type=row.type,
            volume=row.volume,
            price_open=row.price_open,
            price_sl=row.price_sl,
            price_tp=row.price_tp,
            price_close=row.price_close,
            time_execution=row.time_execution,
            time_closing=row.time_closing,
            status=TradeStatus(row.status),
            profit=row.profit,
            commission=row.commission,
            swap=row.swap,
            ml_score=row.ml_score,
            risk_profile=row.risk_profile,
            journey_log=row.journey_log if hasattr(row, 'journey_log') else None
        )
