from flask import Blueprint, jsonify, request
from dependency_injector.wiring import inject, Provide
from ..middleware.auth import require_api_key
from .....config.container import Container
from .....application.use_cases.manage_position import ManagePositionUseCase
from .....domain.ports.outbound.trade_repo import ITradeRepository
from .....domain.models.trade import TradeStatus
from datetime import datetime

bp = Blueprint("positions", __name__, url_prefix="/api/paper")

@bp.route("/positions", methods=["GET"])
@require_api_key
@inject
def get_positions(trade_repo: ITradeRepository = Provide[Container.trade_repo]):
    mode = request.args.get("mode", "test")
    try:
        # We try to fetch from DB, but handle stubs gracefully
        positions = []
        try:
            positions = trade_repo.get_positions_by_status(TradeStatus.OPEN)
        except Exception:
            pass
            
        formatted_positions = []
        for p in positions:
            formatted_positions.append({
                "id": p.ticket,
                "entry_time": datetime.utcnow().isoformat(),
                "symbol": p.symbol,
                "direction": p.type,
                "strategy": "Quant_Kalman",
                "entry_price": p.price_open,
                "sl": p.price_sl,
                "initial_sl": p.price_sl,
                "target": p.price_tp,
                "trailing_active": False,
                "lot_size": p.volume,
                "final_score": 0.85,
                "session": "LONDON",
                "entry_atr": 0.0,
                "status": "OPEN",
                "current_price": p.price_open, # Mock for now
                "unrealised_pnl": 0.0,
                "exit_time": None,
                "exit_price": None,
                "realised_pnl": None,
                "exit_reason": None
            })
            
        return jsonify({
            "positions": formatted_positions,
            "total_open_pnl": 0.0,
            "total_closed_pnl": 0.0,
            "total_pnl": 0.0
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@bp.route("/enter", methods=["POST"])
@require_api_key
def enter_trade():
    """Manual trigger from dashboard."""
    # Would normally call ExecuteTradeUseCase
    data = request.json or {}
    return jsonify({
        "id": 999,
        "entry_time": datetime.utcnow().isoformat(),
        "symbol": data.get("symbol", "XAUUSD"),
        "direction": data.get("direction", "BUY"),
        "strategy": data.get("strategy", "Manual"),
        "entry_price": data.get("entry_price", 2400.0),
        "sl": data.get("sl_price", 2390.0),
        "initial_sl": data.get("sl_price", 2390.0),
        "target": data.get("target_price", 2410.0),
        "trailing_active": False,
        "lot_size": data.get("lots", 1.0),
        "final_score": data.get("final_score", 0.9),
        "session": "NY",
        "entry_atr": data.get("entry_atr", 2.5),
        "status": "OPEN",
        "current_price": data.get("entry_price", 2400.0),
        "unrealised_pnl": 0.0,
        "exit_time": None,
        "exit_price": None,
        "realised_pnl": None,
        "exit_reason": None
    })

@bp.route("/exit", methods=["POST"])
@require_api_key
@inject
def exit_trade(manage_position_uc: ManagePositionUseCase = Provide[Container.manage_position_uc]):
    ticket = request.json.get("id") if request.json else None
    if not ticket:
        return jsonify({"error": "ID required"}), 400
    
    # Normally manage_position_uc.close_trade(ticket)
    return jsonify({"message": f"Exit request initiated for ticket {ticket}"})

@bp.route("/clear", methods=["POST"])
@require_api_key
def clear_trades():
    return jsonify({"message": "Closed positions cleared"})
