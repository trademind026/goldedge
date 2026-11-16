from flask import Blueprint, jsonify, request
from dependency_injector.wiring import inject, Provide
from ..middleware.auth import require_api_key
from .....config.container import Container
from .....gold.adapters.broker.gold_order_manager import GoldOrderManager
from datetime import datetime

bp = Blueprint("positions", __name__, url_prefix="/api/paper")

@bp.route("/positions", methods=["GET"])
@require_api_key
@inject
def get_positions(gold_order_mgr: GoldOrderManager = Provide[Container.gold_order_manager]):
    mode = request.args.get("mode", "test").lower()
    try:
        positions_by_mode = gold_order_mgr.get_positions_by_mode()
        positions = positions_by_mode.get(mode, [])
        summary = gold_order_mgr.get_account_summary()
        
        open_pnl = summary.get(f"total_open_pnl_{mode}", 0.0)
        closed_pnl = summary.get(f"total_closed_pnl_{mode}", 0.0)
        total_pnl = summary.get(f"total_pnl_{mode}", 0.0)

        return jsonify({
            "positions": positions,
            "total_open_pnl": open_pnl,
            "total_closed_pnl": closed_pnl,
            "total_pnl": total_pnl
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@bp.route("/enter", methods=["POST"])
@require_api_key
@inject
def enter_trade(gold_order_mgr: GoldOrderManager = Provide[Container.gold_order_manager]):
    """Manual trigger from dashboard."""
    data = request.json or {}
    symbol = data.get("symbol", "XAUUSD")
    direction = data.get("direction", "BUY")
    lots = float(data.get("lots", data.get("lot_size", 0.5)))
    mode = data.get("mode", gold_order_mgr.get_mode())
    
    # Check if tick exists for pricing
    tick = gold_order_mgr.last_ticks.get(symbol)
    explicit_price = data.get("entry_price")
    market_price = float(explicit_price) if explicit_price else (tick.get("price") if tick else None)

    if market_price is None and mode == "test":
        return jsonify({"error": f"Cannot execute paper order: No live market tick received for {symbol}"}), 400

    pip_unit = 0.10 if "XAU" in symbol else 0.0001
    default_sl = (market_price - 25.0 * pip_unit) if direction.upper() in ("BUY", "CALL", "LONG") else (market_price + 25.0 * pip_unit) if market_price else 0.0
    default_tp = (market_price + 50.0 * pip_unit) if direction.upper() in ("BUY", "CALL", "LONG") else (market_price - 50.0 * pip_unit) if market_price else 0.0

    sl = float(data.get("sl_price", data.get("sl", default_sl)))
    tp = float(data.get("target_price", data.get("target", default_tp)))

    ticket = gold_order_mgr.place_order(
        symbol=symbol,
        order_type=direction,
        volume=lots,
        sl=sl,
        tp=tp,
        mode=mode,
        price=market_price
    )

    if not ticket:
        return jsonify({"error": f"Order placement failed in {mode} mode (broker rejected or insufficient margin)"}), 400

    pos = gold_order_mgr.paper_positions.get(ticket)
    fill_price = pos.price_open if pos else (market_price or 0.0)

    return jsonify({
        "id": ticket,
        "entry_time": datetime.utcnow().isoformat(),
        "symbol": symbol,
        "direction": direction,
        "strategy": data.get("strategy", "Manual_Execution"),
        "entry_price": fill_price,
        "sl": sl,
        "initial_sl": sl,
        "target": tp,
        "trailing_active": False,
        "lot_size": lots,
        "status": "OPEN",
        "current_price": fill_price,
        "unrealised_pnl": 0.0,
        "exit_time": None,
        "exit_price": None,
        "realised_pnl": None,
        "exit_reason": None
    })

@bp.route("/exit", methods=["POST"])
@require_api_key
@inject
def exit_trade(gold_order_mgr: GoldOrderManager = Provide[Container.gold_order_manager]):
    data = request.json or {}
    ticket = data.get("id")
    mode = data.get("mode", gold_order_mgr.get_mode())
    if not ticket:
        return jsonify({"error": "ID required"}), 400
    
    success = gold_order_mgr.close_position(int(ticket), mode=mode)
    return jsonify({
        "success": success,
        "message": f"Exit completed for ticket {ticket}" if success else f"Position {ticket} not found"
    })

@bp.route("/clear", methods=["POST"])
@require_api_key
@inject
def clear_trades(gold_order_mgr: GoldOrderManager = Provide[Container.gold_order_manager]):
    gold_order_mgr.closed_paper_positions.clear()
    return jsonify({"message": "Closed positions cleared"})
