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


def compute_trade_breakdowns(
    trade_list: List[Dict[str, Any]],
    initial_capital: float = 10000.0,
    total_pnl: Optional[float] = None,
    max_dd: float = 0.0,
) -> Dict[str, Any]:
    """Compute strategy, direction, exit-reason, daily breakdowns & capital metrics from trades."""
    n = len(trade_list)
    pnls = [float(t.get("pnl", 0.0) or 0.0) for t in trade_list]
    net_pnl = float(total_pnl) if total_pnl is not None else sum(pnls)
    max_win = max(pnls) if pnls else 0.0
    max_loss = min(pnls) if pnls else 0.0
    avg_pnl_per_trade = (net_pnl / n) if n > 0 else 0.0

    # Margin estimate on 1:100 XAUUSD (100oz contract => ~$entry_price * qty margin)
    margins = [
        float(t.get("entry_price", 0.0) or 0.0) * float(t.get("qty", t.get("lot_size", 0.0)) or 0.0)
        for t in trade_list
    ]
    max_margin = max(margins) if margins else 0.0
    required_capital = max_margin + abs(float(max_dd or 0.0))
    if required_capital <= 0:
        required_capital = float(initial_capital)
    roi = (net_pnl / required_capital * 100.0) if required_capital > 0 else 0.0
    final_equity = float(initial_capital) + net_pnl

    strat_map: Dict[str, Dict[str, Any]] = {}
    dir_map: Dict[str, Dict[str, Any]] = {}
    exit_map: Dict[str, Dict[str, Any]] = {}
    daily_map: Dict[str, Dict[str, Any]] = {}

    for t in trade_list:
        pnl = float(t.get("pnl", 0.0) or 0.0)
        pnl_cents = round(pnl, 2)
        is_win = pnl_cents > 0.0
        is_loss = pnl_cents < 0.0
        strat = str(t.get("strategy") or "UNKNOWN")
        direction = str(t.get("direction") or "UNKNOWN").upper()
        reason = str(t.get("exit_reason") or "UNKNOWN")
        entry_time = str(t.get("entry_time") or t.get("exit_time") or "")
        day = entry_time[:10] if len(entry_time) >= 10 else "UNKNOWN"

        for bucket_map, key in (
            (strat_map, strat),
            (dir_map, direction),
            (daily_map, day),
        ):
            b = bucket_map.setdefault(
                key,
                {
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "pnl": 0.0,
                    "gross_win": 0.0,
                    "gross_loss": 0.0,
                },
            )
            b["trades"] += 1
            if is_win:
                b["wins"] += 1
                b["gross_win"] += pnl
            elif is_loss:
                b["losses"] += 1
                b["gross_loss"] += abs(pnl)
            b["pnl"] += pnl

        eb = exit_map.setdefault(reason, {"count": 0, "wins": 0, "pnl": 0.0})
        eb["count"] += 1
        if is_win:
            eb["wins"] += 1
        eb["pnl"] += pnl

    def _finalize_group(g: Dict[str, Any]) -> Dict[str, Any]:
        tr = g["trades"]
        gl = g["gross_loss"]
        gw = g["gross_win"]
        pf = (gw / gl) if gl > 0 else (999.0 if gw > 0 else 0.0)
        return {
            "trades": tr,
            "wins": g["wins"],
            "losses": g["losses"],
            "win_rate": round((g["wins"] / tr * 100.0) if tr > 0 else 0.0, 2),
            "pnl": round(g["pnl"], 2),
            "avg_pnl": round((g["pnl"] / tr) if tr > 0 else 0.0, 2),
            "pf": round(pf, 2),
        }

    strategy_breakdown = {k: _finalize_group(v) for k, v in sorted(strat_map.items())}
    direction_breakdown = {k: _finalize_group(v) for k, v in sorted(dir_map.items())}
    daily_breakdown = {k: _finalize_group(v) for k, v in sorted(daily_map.items())}
    exit_reason_breakdown = {
        k: {
            "count": v["count"],
            "wins": v["wins"],
            "win_rate": round((v["wins"] / v["count"] * 100.0) if v["count"] > 0 else 0.0, 2),
            "pnl": round(v["pnl"], 2),
        }
        for k, v in sorted(exit_map.items())
    }

    days_traded = len([d for d in daily_breakdown if d != "UNKNOWN"])
    avg_pnl_per_day = (net_pnl / max(1, days_traded)) if days_traded > 0 else 0.0

    max_planned_risk = max(
        [float(t.get("planned_risk_pct", 0.0) or 0.0) for t in trade_list] or [0.0]
    )
    realized_losses = [float(t.get("realized_loss_pct", 0.0) or 0.0) for t in trade_list]
    max_realized_loss = abs(min(realized_losses)) if realized_losses else 0.0

    return {
        "initial_capital": round(float(initial_capital), 2),
        "final_equity": round(final_equity, 2),
        "max_margin": round(max_margin, 2),
        "required_capital": round(required_capital, 2),
        "roi": round(roi, 2),
        "days_traded": days_traded,
        "avg_pnl_per_trade": round(avg_pnl_per_trade, 2),
        "avg_pnl_per_day": round(avg_pnl_per_day, 2),
        "max_win": round(max_win, 2),
        "max_loss": round(max_loss, 2),
        "max_planned_risk_pct": round(max_planned_risk, 3),
        "max_realized_loss_pct": round(max_realized_loss, 3),
        "strategy_breakdown": strategy_breakdown,
        "direction_breakdown": direction_breakdown,
        "exit_reason_breakdown": exit_reason_breakdown,
        "daily_breakdown": daily_breakdown,
    }


class BacktestRepository(IBacktestRepository):
    """SQLAlchemy Core repository for persisting backtest runs in TimescaleDB."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def save_run(self, run_data: Dict[str, Any]) -> None:
        trade_list = run_data.get("trade_list", []) or []
        initial_capital = float(run_data.get("initial_capital", 10000.0) or 10000.0)
        pnl = float(run_data.get("pnl", 0.0) or 0.0)
        max_dd = float(run_data.get("max_dd", 0.0) or 0.0)

        derived = compute_trade_breakdowns(
            trade_list,
            initial_capital=initial_capital,
            total_pnl=pnl,
            max_dd=max_dd,
        )
        merged_metrics: Dict[str, Any] = {
            **derived,
            **(run_data.get("metrics") or {}),
        }

        sql = text("""
            INSERT INTO backtest_runs (
                run_id, symbol, risk, exit_agent, start_date, end_date,
                trades, win_rate, pnl, max_dd, pf, avg_win, avg_loss, rr,
                equity_curve, trade_list, status, strategy_mode, error, metrics
            ) VALUES (
                :run_id, :symbol, :risk, :exit_agent, :start_date, :end_date,
                :trades, :win_rate, :pnl, :max_dd, :pf, :avg_win, :avg_loss, :rr,
                CAST(:equity_curve AS jsonb), CAST(:trade_list AS jsonb),
                :status, :strategy_mode, :error, CAST(:metrics AS jsonb)
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
                trade_list = EXCLUDED.trade_list,
                status = EXCLUDED.status,
                strategy_mode = EXCLUDED.strategy_mode,
                error = EXCLUDED.error,
                metrics = EXCLUDED.metrics
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
            "pnl": pnl,
            "max_dd": max_dd,
            "pf": float(run_data.get("pf", 0.0)),
            "avg_win": float(run_data.get("avg_win", 0.0)),
            "avg_loss": float(run_data.get("avg_loss", 0.0)),
            "rr": float(run_data.get("rr", 0.0)),
            "equity_curve": json.dumps(run_data.get("equity_curve", []), default=_json_default),
            "trade_list": json.dumps(trade_list, default=_json_default),
            "status": str(run_data.get("status", "done")),
            "strategy_mode": str(run_data.get("strategy_mode", "baseline")),
            "error": run_data.get("error"),
            "metrics": json.dumps(merged_metrics, default=_json_default),
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
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        clauses: List[str] = []
        params: Dict[str, Any] = {"limit": int(limit), "offset": int(offset)}

        if risk:
            clauses.append("risk = :risk")
            params["risk"] = risk.lower()
        if symbol:
            clauses.append("UPPER(symbol) = :symbol")
            params["symbol"] = symbol.upper()
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
                   trades, win_rate, pnl, max_dd, pf, avg_win, avg_loss, rr,
                   status, strategy_mode, error, metrics, created_at
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
                "status": getattr(r, "status", "done") or "done",
                "strategy_mode": getattr(r, "strategy_mode", "baseline") or "baseline",
                "error": getattr(r, "error", None),
                "metrics": getattr(r, "metrics", None) or {},
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
        return {"runs": runs, "total": total}

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        sql = text("""
            SELECT run_id, symbol, risk, exit_agent, start_date, end_date,
                   trades, win_rate, pnl, max_dd, pf, avg_win, avg_loss, rr,
                   equity_curve, trade_list, status, strategy_mode, error, metrics, created_at
            FROM backtest_runs
            WHERE run_id = :run_id
        """)
        with self.engine.connect() as conn:
            r = conn.execute(sql, {"run_id": run_id}).fetchone()
        if not r:
            return None

        trade_list = r.trade_list or []
        stored_metrics = dict(getattr(r, "metrics", None) or {})
        if not stored_metrics.get("strategy_breakdown") and trade_list:
            fallback = compute_trade_breakdowns(
                trade_list,
                initial_capital=float(stored_metrics.get("initial_capital", 10000.0)),
                total_pnl=float(r.pnl),
                max_dd=float(r.max_dd),
            )
            stored_metrics = {**fallback, **stored_metrics}

        # Guarantee dual drawdown defaults for older rows
        stored_metrics.setdefault("closed_max_dd", float(r.max_dd))
        stored_metrics.setdefault("mtm_max_dd", float(r.max_dd))

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
            "status": getattr(r, "status", "done") or "done",
            "strategy_mode": getattr(r, "strategy_mode", "baseline") or "baseline",
            "error": getattr(r, "error", None),
            "metrics": stored_metrics,
            "equity_curve": r.equity_curve or [],
            "trade_list": trade_list,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
