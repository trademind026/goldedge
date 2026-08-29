from flask import Blueprint, Response
import redis
import json
import time
import os
from datetime import datetime

bp = Blueprint("stream", __name__, url_prefix="/api")

@bp.route("/stream")
def sse_stream():
    """
    SSE Endpoint for frontend dashboard.
    Outputs the massive StreamPayload exactly matching AI-trader UI.
    """
    def generate():
        while True:
            try:
                # Build the complete State JSON the UI expects
                payload = {
                    "state": {
                        "last_price": 2400.50, # Mock price
                        "regime": "TREND_UP",
                        "status": "Running",
                        "last_scan": datetime.utcnow().isoformat(),
                        "scan_count": 42,
                        "trade_suggestions": [],
                        "auto_trade_enabled": True,
                        "alerts": []
                    },
                    "positions_by_mode": {
                        "test": [],
                        "live": []
                    },
                    "tick_cache": {
                        "XAUUSD": {"price": 2400.50, "ts": datetime.utcnow().isoformat()}
                    },
                    "tick_cache_age": 0,
                    
                    # PnL Aggregations
                    "total_open_pnl": 0.0,
                    "total_closed_pnl": 1500.0,
                    "total_pnl": 1500.0,
                    "total_open_pnl_test": 0.0,
                    "total_closed_pnl_test": 1500.0,
                    "total_pnl_test": 1500.0,
                    "total_open_pnl_live": 0.0,
                    "total_closed_pnl_live": 0.0,
                    "total_pnl_live": 0.0
                }
                
                # Format exactly as Server-Sent Events requires
                yield f"data: {json.dumps(payload)}\n\n"
                
                time.sleep(1.0)
            except Exception as e:
                yield f"event: error\ndata: {str(e)}\n\n"
                time.sleep(2.0)
                
    return Response(generate(), mimetype="text/event-stream")
