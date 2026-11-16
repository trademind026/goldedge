from flask import Blueprint, Response
import redis
import json
import time
import os
from datetime import datetime

from dependency_injector.wiring import inject, Provide
from .....config.container import Container
from .....gold.adapters.broker.gold_order_manager import GoldOrderManager
from .....gold.application.live_execution_engine import ForexLiveExecutionEngine

bp = Blueprint("stream", __name__, url_prefix="/api")

@bp.route("/stream")
@inject
def sse_stream(
    gold_order_mgr: GoldOrderManager = Provide[Container.gold_order_manager],
    live_engine: ForexLiveExecutionEngine = Provide[Container.live_execution_engine]
):
    """
    SSE Endpoint for frontend dashboard.
    Outputs the massive StreamPayload backed strictly by real ticks and engine state.
    """
    def generate():
        while True:
            try:
                positions_by_mode = gold_order_mgr.get_positions_by_mode()
                summary = gold_order_mgr.get_account_summary()
                
                # Fetch real price from engine or last recorded tick
                last_price = live_engine.last_tick_price
                if last_price is None and "XAUUSD" in gold_order_mgr.last_ticks:
                    last_price = gold_order_mgr.last_ticks["XAUUSD"]["price"]

                payload = {
                    "state": {
                        "last_price": last_price,
                        "regime": "SCANNING" if last_price is None else "ACTIVE",
                        "status": live_engine.status,
                        "last_scan": datetime.utcnow().isoformat(),
                        "scan_count": len(live_engine.suggestions),
                        "trade_suggestions": live_engine.suggestions,
                        "auto_trade_enabled": not live_engine.halted,
                        "alerts": live_engine.system_alerts
                    },
                    "positions_by_mode": positions_by_mode,
                    "tick_cache": {
                        "XAUUSD": {"price": last_price, "ts": datetime.utcnow().isoformat()}
                    } if last_price is not None else {},
                    "tick_cache_age": 0 if last_price is not None else None,
                    
                    # PnL Aggregations
                    "total_open_pnl": summary["total_open_pnl_test"] + summary["total_open_pnl_live"],
                    "total_closed_pnl": summary["total_closed_pnl_test"] + summary["total_closed_pnl_live"],
                    "total_pnl": summary["total_pnl_test"] + summary["total_pnl_live"],
                    "total_open_pnl_test": summary["total_open_pnl_test"],
                    "total_closed_pnl_test": summary["total_closed_pnl_test"],
                    "total_pnl_test": summary["total_pnl_test"],
                    "total_open_pnl_live": summary["total_open_pnl_live"],
                    "total_closed_pnl_live": summary["total_closed_pnl_live"],
                    "total_pnl_live": summary["total_pnl_live"]
                }
                
                # Format exactly as Server-Sent Events requires
                yield f"data: {json.dumps(payload)}\n\n"
                time.sleep(1.0)
            except Exception as e:
                yield f"event: error\ndata: {str(e)}\n\n"
                time.sleep(2.0)
                
    return Response(generate(), mimetype="text/event-stream")
