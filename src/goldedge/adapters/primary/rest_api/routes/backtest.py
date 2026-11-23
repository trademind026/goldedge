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
from .....domain.services.trade_scorer import VALID_SCORER_MODES
from ....secondary.db.backtest_repository import compute_trade_breakdowns
from ..middleware.auth import require_api_key

bp = Blueprint("backtest", __name__, url_prefix="/api")

# In-memory caches for current session + live progress
_backtest_results_cache = {}
_equity_curve_cache = {}
_kalman_comparison_cache = None
_stop_requested = False
_backtest_lock = threading.Lock()
_backtest_status = {
    "running": False,
    "status": "idle",
    "last_risk": None,
    "run_id": None,
    "symbol": "XAUUSD",
    "strategy_mode": "candidate_15_e1",
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


def _extract_run_metrics(
    stats: dict,
    strategy_mode: str,
    commission_per_lot: float,
    kalman_comp: dict | None = None,
    commission_profile: str | None = None,
    slippage_pips: float = 0.0,
    apply_swap: bool = False,
    max_score_threshold: float | None = None,
    scorer_mode: str = "legacy",
) -> dict:
    trade_list = stats.get("trade_list", []) or []
    initial_capital = float(stats.get("initial_capital", 10000.0) or 10000.0)
    pnl = float(stats.get("pnl", 0.0) or 0.0)
    max_dd = float(stats.get("max_dd", 0.0) or 0.0)
    derived = compute_trade_breakdowns(
        trade_list,
        initial_capital=initial_capital,
        total_pnl=pnl,
        max_dd=max_dd,
    )
    return {
        **derived,
        "strategy_mode": strategy_mode,
        "commission_per_lot": float(commission_per_lot),
        "commission_profile": commission_profile,
        "slippage_pips": float(slippage_pips),
        "apply_swap": bool(apply_swap),
        "final_equity": round(float(stats.get("final_equity", initial_capital + pnl)), 2),
        "max_margin": round(float(stats.get("max_margin", derived["max_margin"])), 2),
        "required_capital": round(float(stats.get("required_capital", derived["required_capital"])), 2),
        "roi": round(float(stats.get("roi", derived["roi"])), 2),
        "closed_max_dd": round(float(stats.get("closed_max_dd", max_dd)), 2),
        "closed_max_dd_pct": round(float(stats.get("closed_max_dd_pct", 0.0)), 2),
        "mtm_max_dd": round(float(stats.get("mtm_max_dd", max_dd)), 2),
        "mtm_max_dd_pct": round(float(stats.get("mtm_max_dd_pct", 0.0)), 2),
        "mtm_max_dd_initial_pct": round(float(stats.get("mtm_max_dd_initial_pct", 0.0)), 2),
        "mtm_peak_equity": round(float(stats.get("mtm_peak_equity", initial_capital)), 2),
        "mtm_trough_equity": round(float(stats.get("mtm_trough_equity", initial_capital)), 2),
        "mtm_peak_time": stats.get("mtm_peak_time"),
        "mtm_trough_time": stats.get("mtm_trough_time"),
        "raw_tick_mtm_max_dd": round(float(stats.get("raw_tick_mtm_max_dd", max_dd)), 2),
        "max_planned_risk_pct": round(float(stats.get("max_planned_risk_pct", derived["max_planned_risk_pct"])), 3),
        "max_realized_loss_pct": round(float(stats.get("max_realized_loss_pct", derived["max_realized_loss_pct"])), 3),
        "sl_slippage_events": int(stats.get("sl_slippage_events", 0)),
        "total_sl_gap_usd": round(float(stats.get("total_sl_gap_usd", 0.0)), 2),
        "worst_sl_gap_usd": round(float(stats.get("worst_sl_gap_usd", 0.0)), 2),
        "total_commission_usd": round(float(stats.get("total_commission_usd", 0.0)), 2),
        "total_slippage_usd": round(float(stats.get("total_slippage_usd", 0.0)), 2),
        "total_swap_usd": round(float(stats.get("total_swap_usd", 0.0)), 2),
        "inverted_quotes_skipped": int(stats.get("inverted_quotes_skipped", 0)),
        "zero_spread_ticks_skipped": int(stats.get("zero_spread_ticks_skipped", 0)),
        "hwm_kill_switch": stats.get("hwm_kill_switch"),
        "max_score_threshold": max_score_threshold,
        "scorer_mode": scorer_mode,
        "kalman_comparison": kalman_comp,
    }


@bp.route("/backtest/results", methods=["GET"])
@require_api_key
def get_backtest_results():
    return jsonify(_backtest_results_cache)


@bp.route("/backtest/status", methods=["GET"])
@require_api_key
def get_backtest_status():
    return jsonify(_backtest_status)


@bp.route("/backtest/stop", methods=["POST"])
@require_api_key
def stop_backtest():
    global _stop_requested
    _stop_requested = True
    if _backtest_status.get("running"):
        _backtest_status.update({"status": "stopping"})
    return jsonify({"status": "stop_requested"})


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
        "high": asdict(PROFILES["HIGH_BACKTEST"]),
    }
    profiles["low"]["name"] = "Low Risk (1:1)"
    profiles["medium"]["name"] = "Medium Risk (1:2)"
    profiles["high"]["name"] = "High Risk (1:2)"

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

        symbol = request.args.get("symbol")
        params = {}
        where_clause = ""
        if symbol:
            where_clause = "WHERE UPPER(symbol) = :symbol"
            params["symbol"] = symbol.upper()

        with tick_repo.engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT time::date as day, COUNT(*) as ticks
                FROM market_ticks
                {where_clause}
                GROUP BY 1
                HAVING COUNT(*) > 100
                ORDER BY 1
            """), params).fetchall()
        days = [
            {
                "day": str(row.day),
                "ticks": int(row.ticks),
                "bars": int(max(1, min(1440, round(int(row.ticks) / 40.0)))),
            }
            for row in rows
        ]
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
    symbol = request.args.get("symbol") or None
    date_from = request.args.get("from") or None
    date_to = request.args.get("to") or None
    data = backtest_repo.list_runs(
        limit=limit,
        offset=offset,
        risk=risk,
        date_from=date_from,
        date_to=date_to,
        symbol=symbol,
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
    global _stop_requested

    data = request.json or {}
    start_str = data.get("start_date")
    end_str = data.get("end_date")
    risk_level = str(data.get("risk", "medium")).lower()
    symbol = data.get("symbol", "XAUUSD")
    exit_agent = str(data.get("exit_agent", "Standard"))
    compare_kalman = bool(data.get("compare_kalman", False))
    strategy_mode = str(data.get("strategy_mode", "candidate_15_e1")).lower()
    use_kalman_exit = exit_agent.lower() == "kalman"

    # E3: Default to verified Fusion Markets cTrader demo costs when omitted, while preserving explicit overrides
    if "commission_profile" in data and data["commission_profile"] is not None:
        commission_profile = str(data["commission_profile"]).strip().lower()
        commission_per_lot = float(data.get("commission_per_lot", 0.0) or 0.0)
        default_slip = 0.0533 if commission_profile != "zero" else 0.0
        slippage_pips = float(data.get("slippage_pips", default_slip) or 0.0)
        apply_swap = bool(data.get("apply_swap", commission_profile != "zero"))
    elif "commission_per_lot" in data and data["commission_per_lot"] is not None:
        commission_profile = None
        commission_per_lot = float(data["commission_per_lot"])
        slippage_pips = float(data.get("slippage_pips", 0.0) or 0.0)
        apply_swap = bool(data.get("apply_swap", False))
    else:
        commission_profile = "fusion_ctrader_demo"
        commission_per_lot = 0.0
        slippage_pips = float(data.get("slippage_pips", 0.0533) if data.get("slippage_pips") is not None else 0.0533)
        apply_swap = bool(data.get("apply_swap", True))

    account_id = (
        int(data["account_id"])
        if data.get("account_id") is not None
        else (48439552 if commission_profile == "fusion_ctrader_demo" else None)
    )

    raw_hwm = data.get("max_hwm_drawdown_pct", 8.0)
    if raw_hwm is None:
        return jsonify({"error": "max_hwm_drawdown_pct cannot be disabled via API"}), 400
    max_hwm_drawdown_pct = float(raw_hwm)
    if max_hwm_drawdown_pct <= 0.0 or max_hwm_drawdown_pct > 8.0:
        return jsonify({"error": "max_hwm_drawdown_pct must be within (0.0, 8.0]"}), 400

    raw_max_score = data.get("max_score_threshold")
    max_score_threshold: float | None = None
    if raw_max_score is not None:
        try:
            max_score_threshold = float(raw_max_score)
        except (TypeError, ValueError):
            return jsonify({"error": "max_score_threshold must be a numeric float within (0.0, 1.0]"}), 400
        if max_score_threshold <= 0.0 or max_score_threshold > 1.0:
            return jsonify({"error": "max_score_threshold must be within (0.0, 1.0]"}), 400

    scorer_mode = str(data.get("scorer_mode", "legacy") or "legacy").strip().lower()
    if scorer_mode not in VALID_SCORER_MODES:
        return jsonify({"error": f"scorer_mode must be one of {list(VALID_SCORER_MODES)}"}), 400

    if not start_str or not end_str:
        return jsonify({"error": "start_date and end_date required"}), 400

    start_date = datetime.datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    end_date = datetime.datetime.strptime(end_str, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=datetime.timezone.utc
    )

    is_candidate = strategy_mode in ("candidate_15_e1", "candidate", "15_e1")
    session_close_utc = 15 if is_candidate else None
    early_fail_mins = 15 if is_candidate else None
    early_fail_min_mfe_atr = 0.4 if is_candidate else None

    profile_key = {
        "low": "LOW_BACKTEST",
        "medium": "MEDIUM_BACKTEST",
        "high": "HIGH_BACKTEST",
    }.get(risk_level, "MEDIUM_BACKTEST")

    run_id = f"bt_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    # E4: Prevent concurrent backtest runs corrupting shared state, virtual broker, or artifacts
    with _backtest_lock:
        if _backtest_status.get("running"):
            return jsonify({"error": "A backtest is already running"}), 409
        _stop_requested = False
        _backtest_status.update({
            "running": True,
            "status": "running",
            "last_risk": risk_level,
            "run_id": run_id,
            "symbol": symbol,
            "strategy_mode": "candidate_15_e1" if is_candidate else "baseline",
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

    def _on_progress(p: dict):
        _backtest_status.update(p)

    def _stop_check() -> bool:
        return bool(_stop_requested)

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
        try:
            try:
                run_backtest_uc.profile = get_profile(profile_key)
            except Exception:
                pass

            exec_kwargs = dict(
                progress_callback=_on_progress,
                session_close_utc=session_close_utc,
                early_fail_mins=early_fail_mins,
                early_fail_min_mfe_atr=early_fail_min_mfe_atr,
                commission_per_lot=commission_per_lot,
                slippage_pips=slippage_pips,
                commission_profile=commission_profile,
                trading_environment="demo",
                account_id=account_id,
                apply_swap=apply_swap,
                max_hwm_drawdown_pct=max_hwm_drawdown_pct,
                stop_check=_stop_check,
                max_score_threshold=max_score_threshold,
                scorer_mode=scorer_mode,
            )

            kalman_comp = None
            if compare_kalman:
                print(f"[COMPARE] Running Standard (No Kalman Exit) for {symbol}...")
                baseline_stats = run_backtest_uc.execute(
                    symbol,
                    start_date,
                    end_date,
                    use_kalman_exit=False,
                    **exec_kwargs,
                )
                if baseline_stats and baseline_stats.get("cancelled"):
                    _backtest_status.update({"status": "stopped", "eta_secs": 0})
                    return
                print(f"[COMPARE] Running With Kalman Exit for {symbol}...")
                stats = run_backtest_uc.execute(
                    symbol,
                    start_date,
                    end_date,
                    use_kalman_exit=True,
                    **exec_kwargs,
                )
                if stats and stats.get("cancelled"):
                    _backtest_status.update({"status": "stopped", "eta_secs": 0})
                    return
                wo = _summarize(baseline_stats or {})
                w = _summarize(stats or {})
                kalman_comp = {
                    "available": True,
                    "symbol": symbol,
                    "risk": risk_level,
                    "start_date": start_str,
                    "end_date": end_str,
                    "without_kalman": wo,
                    "with_kalman": w,
                    "net_kalman_difference": round(w["total_pnl"] - wo["total_pnl"], 2),
                }
                _kalman_comparison_cache = kalman_comp
            else:
                stats = run_backtest_uc.execute(
                    symbol,
                    start_date,
                    end_date,
                    use_kalman_exit=use_kalman_exit,
                    **exec_kwargs,
                )
                if stats and stats.get("cancelled"):
                    _backtest_status.update({"status": "stopped", "eta_secs": 0})
                    return

            mode_label = "candidate_15_e1" if is_candidate else "baseline"
            if stats:
                metrics = _extract_run_metrics(
                    stats,
                    mode_label,
                    commission_per_lot,
                    kalman_comp=kalman_comp,
                    commission_profile=commission_profile,
                    slippage_pips=slippage_pips,
                    apply_swap=apply_swap,
                    max_score_threshold=max_score_threshold,
                    scorer_mode=scorer_mode,
                )
                _backtest_results_cache[risk_level] = {
                    "run_id": run_id,
                    "symbol": symbol,
                    "strategy_mode": mode_label,
                    "commission_profile": commission_profile,
                    "slippage_pips": float(slippage_pips),
                    "apply_swap": bool(apply_swap),
                    "max_score_threshold": max_score_threshold,
                    "scorer_mode": scorer_mode,
                    "trades": stats["trades"],
                    "win_rate": stats["win_rate"],
                    "pnl": stats["pnl"],
                    "max_dd": stats["max_dd"],
                    "closed_max_dd": metrics["closed_max_dd"],
                    "closed_max_dd_pct": metrics["closed_max_dd_pct"],
                    "mtm_max_dd": metrics["mtm_max_dd"],
                    "mtm_max_dd_pct": metrics["mtm_max_dd_pct"],
                    "mtm_max_dd_initial_pct": metrics["mtm_max_dd_initial_pct"],
                    "max_planned_risk_pct": metrics["max_planned_risk_pct"],
                    "max_realized_loss_pct": metrics["max_realized_loss_pct"],
                    "sl_slippage_events": metrics["sl_slippage_events"],
                    "total_sl_gap_usd": metrics["total_sl_gap_usd"],
                    "worst_sl_gap_usd": metrics["worst_sl_gap_usd"],
                    "total_commission_usd": metrics["total_commission_usd"],
                    "total_slippage_usd": metrics["total_slippage_usd"],
                    "total_swap_usd": metrics["total_swap_usd"],
                    "hwm_kill_switch": metrics["hwm_kill_switch"],
                    "required_capital": metrics["required_capital"],
                    "max_margin": metrics["max_margin"],
                    "roi": metrics["roi"],
                    "final_equity": metrics["final_equity"],
                    "days_traded": metrics["days_traded"],
                    "avg_pnl_per_trade": metrics["avg_pnl_per_trade"],
                    "avg_pnl_per_day": metrics["avg_pnl_per_day"],
                    "max_win": metrics["max_win"],
                    "max_loss": metrics["max_loss"],
                    "strategy_breakdown": metrics["strategy_breakdown"],
                    "direction_breakdown": metrics["direction_breakdown"],
                    "exit_reason_breakdown": metrics["exit_reason_breakdown"],
                    "daily_breakdown": metrics["daily_breakdown"],
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
                    "status": "done",
                    "strategy_mode": mode_label,
                    "metrics": metrics,
                })
            else:
                _backtest_results_cache[risk_level] = {"trades": 0, "pnl": 0}
                _equity_curve_cache[risk_level] = []

            _backtest_status.update({"status": "done", "pct_complete": 100.0, "eta_secs": 0})
        except Exception as exc:
            import traceback

            traceback.print_exc()
            _backtest_status.update({"status": "error"})
            try:
                backtest_repo.save_run({
                    "run_id": run_id,
                    "symbol": symbol,
                    "risk": risk_level,
                    "exit_agent": "Compare" if compare_kalman else exit_agent,
                    "start_date": start_str,
                    "end_date": end_str,
                    "trades": 0,
                    "win_rate": 0.0,
                    "pnl": 0.0,
                    "max_dd": 0.0,
                    "pf": 0.0,
                    "avg_win": 0.0,
                    "avg_loss": 0.0,
                    "rr": 0.0,
                    "equity_curve": [],
                    "trade_list": [],
                    "status": "error",
                    "strategy_mode": "candidate_15_e1" if is_candidate else "baseline",
                    "error": str(exc),
                    "metrics": {},
                })
            except Exception:
                pass
        finally:
            with _backtest_lock:
                _backtest_status["running"] = False

    thread = threading.Thread(target=background_run, daemon=True)
    thread.start()

    return jsonify({
        "status": "started",
        "run_id": run_id,
        "message": f"Backtest running in background for {symbol} ({risk_level}, {'Candidate 15:00+E1' if is_candidate else 'Baseline'})",
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
