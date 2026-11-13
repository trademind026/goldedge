import threading
import datetime
from flask import Blueprint, jsonify, request, Response
from dependency_injector.wiring import inject, Provide
from ..middleware.auth import require_api_key
from .....config.container import Container
from .....application.use_cases.run_backtest import RunBacktestUseCase
from .....config.risk_profiles import get_profile

bp = Blueprint("backtest", __name__, url_prefix="/api")

# Global variables to act as an in-memory cache for the UI since it hits these endpoints on load
_backtest_results_cache = {}
_equity_curve_cache = {}

@bp.route("/backtest/results", methods=["GET"])
@require_api_key
def get_backtest_results():
    return jsonify(_backtest_results_cache)

@bp.route("/equity/curve", methods=["GET"])
@require_api_key
def get_equity_curve():
    return jsonify(_equity_curve_cache)

from .....config.risk_profiles import get_profile, PROFILES

@bp.route("/risk/profiles", methods=["GET"])
@require_api_key
def get_risk_profiles():
    # Return the actual Python Forex profiles configured in risk_profiles.py
    # We map them to low/medium/high keys for the UI dropdown
    
    # Using dataclass to dict conversion
    from dataclasses import asdict
    
    profiles = {
        "low": asdict(PROFILES["LOW_BACKTEST"]),
        "medium": asdict(PROFILES["MEDIUM_BACKTEST"]),
        "high": asdict(PROFILES["LOW_RR20_BACKTEST"]) # Example for high reward
    }
    
    # Overwrite the name so the UI renders it nicely
    profiles["low"]["name"] = "Low Risk (1:1)"
    profiles["medium"]["name"] = "Medium Risk (1:2)"
    profiles["high"]["name"] = "High Reward (1:2)"
    
    # Serialize enums
    for p in profiles.values():
        if hasattr(p["context"], "value"):
            p["context"] = p["context"].value
            
    return jsonify(profiles)

@bp.route("/days", methods=["GET"])
@require_api_key
@inject
def get_available_days(tick_repo = Provide[Container.tick_repo]):
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
    except Exception as e:
        return jsonify([])

@bp.route("/backtest/run", methods=["POST"])
@require_api_key
@inject
def run_backtest(run_backtest_uc: RunBacktestUseCase = Provide[Container.run_backtest_uc]):
    data = request.json or {}
    start_str = data.get("start_date")
    end_str = data.get("end_date")
    risk_level = data.get("risk", "medium")
    
    if not start_str or not end_str:
        return jsonify({"error": "start_date and end_date required"}), 400
        
    start_date = datetime.datetime.strptime(start_str, "%Y-%m-%d")
    end_date = datetime.datetime.strptime(end_str, "%Y-%m-%d")
    end_date = end_date.replace(hour=23, minute=59, second=59)
    
    def background_run():
        global _backtest_results_cache, _equity_curve_cache
        try:
            stats = run_backtest_uc.execute("XAUUSD", start_date, end_date)
            
            if stats:
                _backtest_results_cache[risk_level] = {
                    "trades": stats["trades"],
                    "win_rate": stats["win_rate"],
                    "pnl": stats["pnl"],
                    "max_dd": stats["max_dd"],
                    "pf": stats["pf"],
                    "avg_win": stats["avg_win"],
                    "avg_loss": stats["avg_loss"],
                    "rr": stats["rr"],
                    "trade_list": stats["trade_list"]
                }
                _equity_curve_cache[risk_level] = stats["equity_curve"]
            else:
                _backtest_results_cache[risk_level] = {"trades": 0, "pnl": 0}
                _equity_curve_cache[risk_level] = []
                
        except Exception as e:
            import traceback
            traceback.print_exc()

    thread = threading.Thread(target=background_run)
    thread.start()
    
    return jsonify({"status": "started", "message": "Backtest running in background"})
    
import sys
import io

class TerminalBuffer(io.StringIO):
    def __init__(self):
        super().__init__()
        self.logs = []
    
    def write(self, s):
        if not s: return
        sys.__stdout__.write(s)
        sys.__stdout__.flush()
        
        # Handle tqdm \r carriage returns
        if '\r' in s:
            parts = s.split('\r')
            s = parts[-1]
            if self.logs and self.logs[-1].startswith(" 10%") or self.logs[-1].startswith(" 20%") or "%|" in self.logs[-1]:
                self.logs[-1] = s
            else:
                self.logs.append(s)
        else:
            for line in s.split('\n'):
                if line.strip():
                    self.logs.append(line.strip())
        
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]

terminal_buffer = TerminalBuffer()
sys.stdout = terminal_buffer

@bp.route("/backtest/progress", methods=["GET"])
def backtest_progress():
    def generate():
        import time
        import json
        last_idx = 0
        while True:
            if last_idx < len(terminal_buffer.logs):
                new_logs = terminal_buffer.logs[last_idx:]
                last_idx = len(terminal_buffer.logs)
                for log in new_logs:
                    yield f"data: {json.dumps({'log': log})}\n\n"
            time.sleep(0.5)
    return Response(generate(), mimetype="text/event-stream")
