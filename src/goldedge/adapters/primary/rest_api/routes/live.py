import json
import time
from flask import Blueprint, Response, jsonify, request
from dependency_injector.wiring import inject, Provide
from ..middleware.auth import require_api_key
from .....config.container import Container
from .....gold.application.live_execution_engine import ForexLiveExecutionEngine

bp = Blueprint("live", __name__, url_prefix="/api/live")


@bp.route("/stream")
@inject
def api_live_stream(engine: ForexLiveExecutionEngine = Provide[Container.live_execution_engine]):
    """
    Streams live Forex / Gold telemetry over Server-Sent Events (SSE) every 1s.
    Outputs the unified LiveEnginePayload schema.
    """
    def event_stream():
        while True:
            try:
                payload = engine.get_telemetry_snapshot()
                data = json.dumps(payload)
                yield f"data: {data}\n\n"
                time.sleep(1.0)
            except Exception as e:
                yield f"event: error\ndata: {str(e)}\n\n"
                time.sleep(2.0)

    return Response(event_stream(), mimetype="text/event-stream")


@bp.route("/snapshot", methods=["GET"])
@require_api_key
@inject
def api_live_snapshot(engine: ForexLiveExecutionEngine = Provide[Container.live_execution_engine]):
    """Returns instant JSON telemetry snapshot for fast initial page render."""
    return jsonify(engine.get_telemetry_snapshot())


@bp.route("/zero_loss", methods=["POST"])
@require_api_key
@inject
def api_zero_loss(engine: ForexLiveExecutionEngine = Provide[Container.live_execution_engine]):
    """
    Moves Stop-Loss to Breakeven (+ spread buffer) as soon as position is in profit.
    """
    data = request.json or {}
    ticket = data.get("ticket") or data.get("id")
    if not ticket:
        return jsonify({"error": "Ticket is required"}), 400

    success, new_sl, msg = engine.zero_loss(int(ticket))
    if success:
        return jsonify({"status": "zero_loss_set", "ticket": ticket, "new_sl": new_sl, "message": msg})
    return jsonify({"error": msg, "current_sl": new_sl}), 400


@bp.route("/lock_profit", methods=["POST"])
@require_api_key
@inject
def api_lock_profit(engine: ForexLiveExecutionEngine = Provide[Container.live_execution_engine]):
    """
    Ratchets Stop-Loss to lock 60% of current floating profit.
    """
    data = request.json or {}
    ticket = data.get("ticket") or data.get("id")
    lock_pct = float(data.get("lock_pct", 0.60))
    if not ticket:
        return jsonify({"error": "Ticket is required"}), 400

    success, new_sl, msg = engine.lock_profit(int(ticket), lock_pct=lock_pct)
    if success:
        return jsonify({"status": "profit_locked", "ticket": ticket, "new_sl": new_sl, "message": msg})
    return jsonify({"error": msg, "current_sl": new_sl}), 400


@bp.route("/exit", methods=["POST"])
@require_api_key
@inject
def api_exit_position(engine: ForexLiveExecutionEngine = Provide[Container.live_execution_engine]):
    """Instant market close on a specific ticket."""
    data = request.json or {}
    ticket = data.get("ticket") or data.get("id")
    if not ticket:
        return jsonify({"error": "Ticket is required"}), 400

    success, msg = engine.exit_position(int(ticket))
    if success:
        return jsonify({"status": "closed", "ticket": ticket, "message": msg})
    return jsonify({"error": msg}), 400


@bp.route("/kill_switch", methods=["POST"])
@require_api_key
@inject
def api_kill_switch(engine: ForexLiveExecutionEngine = Provide[Container.live_execution_engine]):
    """EMERGENCY: Closes ALL open positions and disables trading engine."""
    closed_tickets = engine.kill_switch()
    return jsonify({"status": "emergency_halted", "closed_positions": closed_tickets})


@bp.route("/enter", methods=["POST"])
@require_api_key
@inject
def api_enter_position(engine: ForexLiveExecutionEngine = Provide[Container.live_execution_engine]):
    """Executes a trade suggestion or manual entry."""
    data = request.json or {}
    suggestion_id = data.get("suggestion_id", "manual")
    lots = float(data.get("lots", data.get("lot_size", 0.50)))

    ticket = engine.execute_suggestion(suggestion_id, lots=lots)
    if ticket:
        return jsonify({"status": "executed", "ticket": ticket, "lots": lots})
    return jsonify({"error": "Execution rejected (Rollover freeze or halted)"}), 400


@bp.route("/mode", methods=["GET", "POST"])
@require_api_key
@inject
def api_trading_mode(engine: ForexLiveExecutionEngine = Provide[Container.live_execution_engine]):
    """Gets or sets the live trading mode (paper vs live)."""
    if request.method == "GET":
        return jsonify({"mode": engine.mode})

    data = request.json or {}
    new_mode = data.get("mode", "paper").lower()
    try:
        engine.set_mode(new_mode)
        return jsonify({"status": "success", "mode": engine.mode})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
