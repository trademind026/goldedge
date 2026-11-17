import datetime
import io
import json
import sys
import threading
import time
import uuid

from dependency_injector.wiring import Provide, inject
from flask import Blueprint, Response, jsonify, request

from .....application.use_cases.run_backtest import RunBacktestUseCase
from .....config.container import Container
from .....config.risk_profiles import PROFILES, get_profile
from .....domain.ports.outbound.backtest_repo import IBacktestRepository
from ..middleware.auth import require_api_key

bp = Blueprint("backtest", __name__, url_prefix="/api")

# In-memory caches for current session + live progress
_backtest_results_cache = {}
_equity_curve_cache = {}
_kalman_comparison_cache = None
_backtest_status = {
    "running": False,
    "status": "idle",
    "last_risk": None,
    "run_id": None,
    "symbol": "XAUUSD",
    "pct_complete": 0.0,
    "current_day": 0,
    "total_days": 0,
    "current_date": "",
    "live_trades": 0,
    "live_wins": 0,
    "live_pnl": 0.0,
    "live_equity": 10000.0,
    "elapsed_secs": 0,
    "eta_secs": 0,
    "compare_kalman": False,
}


@bp.route("/backtest/results", methods=["GET"])
@require_api_key
def get_backtest_results():
    return jsonify(_backtest_results_cache)


@bp.route("/backtest/status", methods=["GET"])
@require_api_key
def get_backtest_status():
    return jsonify(_backtest_status)


@bp.route("/backtest/compare", methods=["GET"])
@require_api_key
def get_backtest_compare():
    if _kalman_comparison_cache is None:
        return jsonify({"available": False}), 404
    return jsonify(_kalman_comparison_cache)


@bp.route("/equity/curve", methods=["GET"])
@require_api_key
def get_equity_curve():
    return jsonify(_equity_curve_cache)


@bp.route("/risk/profiles", methods=["GET"])
@require_api_key
def get_risk_profiles():
    from dataclasses import asdict

    profiles = {
        "low": asdict(PROFILES["LOW_BACKTEST"]),
        "medium": asdict(PROFILES["MEDIUM_BACKTEST"]),
        "high": asdict(PROFILES["LOW_RR20_BACKTEST"]),
    }
    profiles["low"]["name"] = "Low Risk (1:1)"
    profiles["medium"]["name"] = "Medium Risk (1:2)"
    profiles["high"]["name"] = "High Reward (1:2)"

    for p in profiles.values():
        if hasattr(p["context"], "value"):
            p["context"] = p["context"].value

    return jsonify(profiles)


@bp.route("/days", methods=["GET"])
@require_api_key
@inject
def get_available_days(tick_repo=Provide[Container.tick_repo]):
    try:
        from sqlalchemy import text

        with tick_repo.engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT time::date as day, COUNT(*) as ticks
                FROM market_ticks
                GROUP BY 1
                HAVING COUNT(*) > 100
                ORDER BY 1
            """)).fetchall()
        days = [{"day": str(row.day), "ticks": int(row.ticks)} for row in rows]
        return jsonify(days)
    except Exception:
        return jsonify([])


@bp.route("/backtest/history", methods=["GET"])
@require_api_key
@inject
def get_backtest_history(backtest_repo: IBacktestRepository = Provide[Container.backtest_repo]):
    limit = min(100, max(1, int(request.args.get("limit", 25))))
    offset = max(0, int(request.args.get("offset", 0)))
    risk = request.args.get("risk") or None
    date_from = request.args.get("from") or None
    date_to = request.args.get("to") or None
    data = backtest_repo.list_runs(
        limit=limit,
        offset=offset,
        risk=risk,
        date_from=date_from,
        date_to=date_to,
    )
    return jsonify(data)


@bp.route("/backtest/history/<run_id>", methods=["GET"])
@require_api_key
@inject
def get_backtest_history_detail(
    run_id: str,
    backtest_repo: IBacktestRepository = Provide[Container.backtest_repo],
):
    run = backtest_repo.get_run(run_id)
    if not run:
        return jsonify({"error": "Backtest run not found"}), 404
    return jsonify(run)


@bp.route("/backtest/run", methods=["POST"])
@require_api_key
@inject
def run_backtest(
    run_backtest_uc: RunBacktestUseCase = Provide[Container.run_backtest_uc],
    backtest_repo: IBacktestRepository = Provide[Container.backtest_repo],
):
    data = request.json or {}
    start_str = data.get("start_date")
    end_str = data.get("end_date")
    risk_level = str(data.get("risk", "medium")).lower()
    symbol = data.get("symbol", "XAUUSD")
    exit_agent = data.get("exit_agent", "Kalman")
    compare_kalman = bool(data.get("compare_kalman", False))
    use_kalman_exit = exit_agent != "None"

    if not start_str or not end_str:
        return jsonify({"error": "start_date and end_date required"}), 400

    start_date = datetime.datetime.strptime(start_str, "%Y-%m-%d")
    end_date = datetime.datetime.strptime(end_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

    profile_key = {
        "low": "LOW_BACKTEST",
        "medium": "MEDIUM_BACKTEST",
        "high": "LOW_RR20_BACKTEST",
    }.get(risk_level, "MEDIUM_BACKTEST")

    run_id = f"bt_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    def _on_progress(p: dict):
        _backtest_status.update(p)

    def _summarize(stats: dict) -> dict:
        trades_list = stats.get("trade_list", [])
        kalman_exits = sum(
            1 for t in trades_list
            if "KALMAN" in str(t.get("exit_reason", "")).upper()
        )
        return {
            "trades": int(stats.get("trades", 0)),
            "win_rate": round(float(stats.get("win_rate", 0.0)) * 100.0, 1),
            "total_pnl": round(float(stats.get("pnl", 0.0)), 2),
            "avg_win": round(float(stats.get("avg_win", 0.0)), 2),
            "avg_loss": round(float(stats.get("avg_loss", 0.0)), 2),
            "max_drawdown": round(float(stats.get("max_dd", 0.0)), 2),
            "closed_max_drawdown": round(float(stats.get("closed_max_dd", stats.get("max_dd", 0.0))), 2),
            "closed_max_dd_pct": round(float(stats.get("closed_max_dd_pct", 0.0)), 2),
            "mtm_max_drawdown": round(float(stats.get("mtm_max_dd", stats.get("max_dd", 0.0))), 2),
            "mtm_max_dd_pct": round(float(stats.get("mtm_max_dd_pct", 0.0)), 2),
            "max_planned_risk_pct": round(float(stats.get("max_planned_risk_pct", 0.0)), 3),
            "pf": round(float(stats.get("pf", 0.0)), 2),
            "rr": round(float(stats.get("rr", 0.0)), 2),
            "kalman_exits": kalman_exits,
        }

    def background_run():
        global _backtest_results_cache, _equity_curve_cache, _kalman_comparison_cache
        _backtest_status.update({
            "running": True,
            "status": "running",
            "last_risk": risk_level,
            "run_id": run_id,
            "symbol": symbol,
            "pct_complete": 0.0,
            "current_day": 0,
            "total_days": 0,
            "current_date": start_str,
            "live_trades": 0,
            "live_wins": 0,
            "live_pnl": 0.0,
            "live_equity": 10000.0,
            "elapsed_secs": 0,
            "eta_secs": 0,
            "compare_kalman": compare_kalman,
        })
        try:
            try:
                run_backtest_uc.profile = get_profile(profile_key)
            except Exception:
                pass

            if compare_kalman:
                print(f"[COMPARE] Running Baseline (No Kalman Exit) for {symbol}...")
                baseline_stats = run_backtest_uc.execute(
                    symbol, start_date, end_date, use_kalman_exit=False, progress_callback=_on_progress
                )
                print(f"[COMPARE] Running With Kalman Exit for {symbol}...")
                stats = run_backtest_uc.execute(
                    symbol, start_date, end_date, use_kalman_exit=True, progress_callback=_on_progress
                )
                wo = _summarize(baseline_stats or {})
                w = _summarize(stats or {})
                _kalman_comparison_cache = {
                    "available": True,
                    "symbol": symbol,
                    "risk": risk_level,
                    "start_date": start_str,
                    "end_date": end_str,
                    "without_kalman": wo,
                    "with_kalman": w,
                    "net_kalman_difference": round(w["total_pnl"] - wo["total_pnl"], 2),
                }
            else:
                stats = run_backtest_uc.execute(
                    symbol,
                    start_date,
                    end_date,
                    use_kalman_exit=use_kalman_exit,
                    progress_callback=_on_progress,
                )

            if stats:
                _backtest_results_cache[risk_level] = {
                    "trades": stats["trades"],
                    "win_rate": stats["win_rate"],
                    "pnl": stats["pnl"],
                    "max_dd": stats["max_dd"],
                    "closed_max_dd": stats.get("closed_max_dd", stats["max_dd"]),
                    "closed_max_dd_pct": stats.get("closed_max_dd_pct", 0.0),
                    "mtm_max_dd": stats.get("mtm_max_dd", stats["max_dd"]),
                    "mtm_max_dd_pct": stats.get("mtm_max_dd_pct", 0.0),
                    "max_planned_risk_pct": stats.get("max_planned_risk_pct", 0.0),
                    "pf": stats["pf"],
                    "avg_win": stats["avg_win"],
                    "avg_loss": stats["avg_loss"],
                    "rr": stats["rr"],
                    "trade_list": stats["trade_list"],
                }
                _equity_curve_cache[risk_level] = stats["equity_curve"]

                backtest_repo.save_run({
                    "run_id": run_id,
                    "symbol": symbol,
                    "risk": risk_level,
                    "exit_agent": "Compare" if compare_kalman else exit_agent,
                    "start_date": start_str,
                    "end_date": end_str,
                    "trades": stats["trades"],
                    "win_rate": stats["win_rate"],
                    "pnl": stats["pnl"],
                    "max_dd": stats["max_dd"],
                    "pf": stats["pf"],
                    "avg_win": stats["avg_win"],
                    "avg_loss": stats["avg_loss"],
                    "rr": stats["rr"],
                    "equity_curve": stats["equity_curve"],
                    "trade_list": stats["trade_list"],
                })
            else:
                _backtest_results_cache[risk_level] = {"trades": 0, "pnl": 0}
                _equity_curve_cache[risk_level] = []

            _backtest_status.update({"status": "done", "pct_complete": 100.0, "eta_secs": 0})
        except Exception:
            import traceback

            traceback.print_exc()
            _backtest_status.update({"status": "error"})
        finally:
            _backtest_status["running"] = False

    thread = threading.Thread(target=background_run, daemon=True)
    thread.start()

    return jsonify({
        "status": "started",
        "run_id": run_id,
        "message": f"Backtest running in background for {symbol} ({risk_level})",
    })


class TerminalBuffer(io.StringIO):
    def __init__(self):
        super().__init__()
        self.logs = []

    def write(self, s):
        if not s:
            return
        sys.__stdout__.write(s)
        sys.__stdout__.flush()

        if "\r" in s:
            parts = s.split("\r")
            s = parts[-1]
            if self.logs and (self.logs[-1].startswith(" 10%") or self.logs[-1].startswith(" 20%") or "%|" in self.logs[-1]):
                self.logs[-1] = s
            else:
                self.logs.append(s)
        else:
            for line in s.split("\n"):
                if line.strip():
                    self.logs.append(line.strip())

        if len(self.logs) > 400:
            self.logs = self.logs[-400:]


terminal_buffer = TerminalBuffer()
sys.stdout = terminal_buffer


@bp.route("/backtest/progress", methods=["GET"])
def backtest_progress():
    def generate():
        last_idx = len(terminal_buffer.logs)
        was_running = _backtest_status["running"]
        last_progress_sent = 0.0
        while True:
            if last_idx < len(terminal_buffer.logs):
                new_logs = terminal_buffer.logs[last_idx:]
                last_idx = len(terminal_buffer.logs)
                for log in new_logs:
                    yield f"data: {json.dumps({'log': log})}\n\n"
            is_running = _backtest_status["running"]
            now_ts = time.time()
            if is_running and (now_ts - last_progress_sent >= 0.5):
                last_progress_sent = now_ts
                yield f"data: {json.dumps({'progress': _backtest_status})}\n\n"
            if was_running and not is_running:
                yield f"data: {json.dumps({'status': _backtest_status['status'], 'risk': _backtest_status['last_risk'], 'run_id': _backtest_status['run_id'], 'progress': _backtest_status})}\n\n"
            was_running = is_running
            time.sleep(0.25)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
