from flask import Blueprint, jsonify, request
from dependency_injector.wiring import inject, Provide
from ..middleware.auth import require_api_key
from .....config.container import Container
from .....application.use_cases.scan_market import ScanMarketUseCase

bp = Blueprint("trading", __name__, url_prefix="/api")

@bp.route("/scan", methods=["POST"])
@require_api_key
@inject
def trigger_scan(scan_market_uc: ScanMarketUseCase = Provide[Container.scan_market_uc]):
    symbol = request.json.get("symbol", "XAUUSD") if request.json else "XAUUSD"
    scan_market_uc.execute(symbol)
    return jsonify({"message": f"Scan triggered for {symbol}"})

@bp.route("/auto_trade", methods=["GET", "POST"])
@require_api_key
def toggle_auto_trade():
    if request.method == "GET":
        return jsonify({"auto_trade": True})
    
    data = request.json or {}
    enabled = data.get("enabled", True)
    return jsonify({"message": f"Auto trade set to {enabled}", "auto_trade": enabled})
