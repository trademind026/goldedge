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

@bp.route("/config", methods=["POST"])
@require_api_key
def update_config():
    return jsonify({"status": "success"})
