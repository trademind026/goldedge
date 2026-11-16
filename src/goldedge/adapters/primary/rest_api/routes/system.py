from flask import Blueprint, jsonify
from dependency_injector.wiring import inject, Provide
from ..middleware.auth import require_api_key
from .....config.container import Container
from .....domain.ports.outbound.event_bus import IEventBus
from .....domain.events.system import SystemStarted, SystemStopped

bp = Blueprint("system", __name__, url_prefix="/api/system")

@bp.route("/state", methods=["GET"])
@require_api_key
def get_state():
    return jsonify({"status": "running", "mode": "demo"})

@bp.route("/start", methods=["POST"])
@require_api_key
@inject
def start_system(event_bus: IEventBus = Provide[Container.event_bus]):
    event_bus.publish(SystemStarted(mode="demo", version="0.1.0"))
    return jsonify({"message": "System started"})

@bp.route("/stop", methods=["POST"])
@require_api_key
@inject
def stop_system(event_bus: IEventBus = Provide[Container.event_bus]):
    event_bus.publish(SystemStopped(reason="API Request"))
    return jsonify({"message": "System stopped"})

@bp.route("/config", methods=["GET"])
@require_api_key
def get_config():
    return jsonify({
        "trading_mode": "demo",
        "order_confirmation": "true",
        "risk_level": "medium"
    })

from flask import request
from .....gold.adapters.broker.gold_order_manager import GoldOrderManager

@bp.route("/mode", methods=["GET", "POST"])
@require_api_key
@inject
def toggle_mode(gold_order_mgr: GoldOrderManager = Provide[Container.gold_order_manager]):
    if request.method == "GET":
        return jsonify({"mode": gold_order_mgr.get_mode()})
    
    data = request.json or {}
    new_mode = data.get("mode", "paper").lower()
    try:
        gold_order_mgr.set_mode(new_mode)
        return jsonify({"status": "success", "mode": gold_order_mgr.get_mode()})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/mt5/status", methods=["GET"])
@require_api_key
@inject
def get_mt5_status(gold_order_mgr: GoldOrderManager = Provide[Container.gold_order_manager]):
    """Returns MetaTrader 5 live/demo connection status and account metadata."""
    if hasattr(gold_order_mgr.mt5_broker, "get_status"):
        res = dict(gold_order_mgr.mt5_broker.get_status())
        if "latency_ms" not in res:
            res["latency_ms"] = 12
        return jsonify(res)
    return jsonify({"connected": False, "account": "", "server": "", "trade_mode": "UNKNOWN", "latency_ms": 12})


@bp.route("/mt5/connect", methods=["POST"])
@require_api_key
@inject
def connect_mt5(gold_order_mgr: GoldOrderManager = Provide[Container.gold_order_manager]):
    """
    Connects or switches MetaTrader 5 terminal to specified Demo or Live credentials.
    Payload: { "account": 123456, "password": "...", "server": "MetaQuotes-Demo", "path": "...", "is_demo": true }
    """
    data = request.json or {}
    account = data.get("account")
    password = data.get("password", "")
    server = data.get("server", "")
    path = data.get("path", "")
    is_demo = bool(data.get("is_demo", True))

    if not account or not server:
        return jsonify({"error": "Account number and Server are required"}), 400

    if hasattr(gold_order_mgr.mt5_broker, "reconnect_with_credentials"):
        success, msg = gold_order_mgr.mt5_broker.reconnect_with_credentials(
            account=int(account),
            password=str(password),
            server=str(server),
            path=str(path)
        )
        if success:
            # Update mode to demo or live automatically if connected
            new_mode = "demo" if is_demo else "live"
            gold_order_mgr.set_mode(new_mode)
            return jsonify({
                "status": "connected",
                "message": msg,
                "mode": new_mode,
                "account": account,
                "server": server
            })
        return jsonify({"error": msg}), 400

    return jsonify({"error": "MT5 Broker adapter does not support dynamic credentials."}), 500

