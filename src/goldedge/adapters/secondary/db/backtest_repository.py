import json
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ....domain.ports.outbound.backtest_repo import IBacktestRepository

logger = logging.getLogger(__name__)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


class BacktestRepository(IBacktestRepository):
    """SQLAlchemy Core repository for persisting backtest runs in TimescaleDB."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def save_run(self, run_data: Dict[str, Any]) -> None:
        sql = text("""
            INSERT INTO backtest_runs (
                run_id, symbol, risk, exit_agent, start_date, end_date,
                trades, win_rate, pnl, max_dd, pf, avg_win, avg_loss, rr,
                equity_curve, trade_list
            ) VALUES (
                :run_id, :symbol, :risk, :exit_agent, :start_date, :end_date,
                :trades, :win_rate, :pnl, :max_dd, :pf, :avg_win, :avg_loss, :rr,
                CAST(:equity_curve AS jsonb), CAST(:trade_list AS jsonb)
            )
            ON CONFLICT (run_id) DO UPDATE SET
                trades = EXCLUDED.trades,
                win_rate = EXCLUDED.win_rate,
                pnl = EXCLUDED.pnl,
                max_dd = EXCLUDED.max_dd,
                pf = EXCLUDED.pf,
                avg_win = EXCLUDED.avg_win,
                avg_loss = EXCLUDED.avg_loss,
                rr = EXCLUDED.rr,
                equity_curve = EXCLUDED.equity_curve,
                trade_list = EXCLUDED.trade_list
        """)
        params = {
            "run_id": str(run_data["run_id"]),
            "symbol": str(run_data.get("symbol", "XAUUSD")),
            "risk": str(run_data.get("risk", "medium")),
            "exit_agent": str(run_data.get("exit_agent", "Kalman")),
            "start_date": run_data["start_date"],
            "end_date": run_data["end_date"],
            "trades": int(run_data.get("trades", 0)),
            "win_rate": float(run_data.get("win_rate", 0.0)),
            "pnl": float(run_data.get("pnl", 0.0)),
            "max_dd": float(run_data.get("max_dd", 0.0)),
            "pf": float(run_data.get("pf", 0.0)),
            "avg_win": float(run_data.get("avg_win", 0.0)),
            "avg_loss": float(run_data.get("avg_loss", 0.0)),
            "rr": float(run_data.get("rr", 0.0)),
            "equity_curve": json.dumps(run_data.get("equity_curve", []), default=_json_default),
            "trade_list": json.dumps(run_data.get("trade_list", []), default=_json_default),
        }
        with self.engine.begin() as conn:
            conn.execute(sql, params)

    def list_runs(
        self,
        limit: int = 25,
        offset: int = 0,
        risk: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        clauses: List[str] = []
        params: Dict[str, Any] = {"limit": int(limit), "offset": int(offset)}

        if risk:
            clauses.append("risk = :risk")
            params["risk"] = risk.lower()
        if date_from:
            clauses.append("created_at >= CAST(:date_from AS timestamptz)")
            params["date_from"] = date_from
        if date_to:
            clauses.append("created_at <= CAST(:date_to AS timestamptz)")
            params["date_to"] = date_to

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        count_sql = text(f"SELECT COUNT(*) FROM backtest_runs {where_sql}")
        rows_sql = text(f"""
            SELECT run_id, symbol, risk, exit_agent, start_date, end_date,
                   trades, win_rate, pnl, max_dd, pf, avg_win, avg_loss, rr, created_at
            FROM backtest_runs
            {where_sql}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """)

        with self.engine.connect() as conn:
            total = int(conn.execute(count_sql, params).scalar() or 0)
            rows = conn.execute(rows_sql, params).fetchall()

        runs = [
            {
                "run_id": r.run_id,
                "symbol": r.symbol,
                "risk": r.risk,
                "exit_agent": r.exit_agent,
                "start_date": str(r.start_date),
                "end_date": str(r.end_date),
                "trades": int(r.trades),
                "win_rate": float(r.win_rate),
                "pnl": float(r.pnl),
                "max_dd": float(r.max_dd),
                "pf": float(r.pf),
                "avg_win": float(r.avg_win),
                "avg_loss": float(r.avg_loss),
                "rr": float(r.rr),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
        return {"runs": runs, "total": total}

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        sql = text("""
            SELECT run_id, symbol, risk, exit_agent, start_date, end_date,
                   trades, win_rate, pnl, max_dd, pf, avg_win, avg_loss, rr,
                   equity_curve, trade_list, created_at
            FROM backtest_runs
            WHERE run_id = :run_id
        """)
        with self.engine.connect() as conn:
            r = conn.execute(sql, {"run_id": run_id}).fetchone()
        if not r:
            return None
        return {
            "run_id": r.run_id,
            "symbol": r.symbol,
            "risk": r.risk,
            "exit_agent": r.exit_agent,
            "start_date": str(r.start_date),
            "end_date": str(r.end_date),
            "trades": int(r.trades),
            "win_rate": float(r.win_rate),
            "pnl": float(r.pnl),
            "max_dd": float(r.max_dd),
            "pf": float(r.pf),
            "avg_win": float(r.avg_win),
            "avg_loss": float(r.avg_loss),
            "rr": float(r.rr),
            "equity_curve": r.equity_curve or [],
            "trade_list": r.trade_list or [],
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
