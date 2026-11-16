from flask import Flask
from .routes import system, trading, positions, stream
from ....config.container import Container

def create_app() -> Flask:
    app = Flask(__name__)
    
    # Simple CORS
    @app.after_request
    def after_request(response):
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-API-Key')
        response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
        return response

    from ....config.settings import settings
    from ....adapters.secondary.event_bus.redis_event_bus import RedisStreamEventBus
    
    try:
        event_bus = RedisStreamEventBus(
            redis_url=settings.REDIS_URL,
            consumer_group="live_ticker_group",
            consumer_name="backend_1"
        )
    except Exception as e:
        app.logger.warning(f"Redis connection failed ({e}). Falling back to InMemoryEventBus.")
        from ....adapters.secondary.in_memory_event_bus import InMemoryEventBus
        event_bus = InMemoryEventBus()
    
    container = Container(
        db_url=settings.DB_URL,
        event_bus=event_bus,
        config={"BROKER_TYPE": settings.BROKER_TYPE}
    )
    
    # Wire the container to the route modules
    container.wire(modules=[
        "goldedge.adapters.primary.rest_api.routes.system",
        "goldedge.adapters.primary.rest_api.routes.trading",
        "goldedge.adapters.primary.rest_api.routes.positions",
        "goldedge.adapters.primary.rest_api.routes.stream",
        "goldedge.adapters.primary.rest_api.routes.backtest",
        "goldedge.adapters.primary.rest_api.routes.live",
        "goldedge.adapters.primary.rest_api.routes.charts"
    ])
    
    app.container = container
    
    from .routes import system, trading, positions, stream, backtest, live, charts
    
    # Register blueprints
    
    from flask import jsonify, request

    _FOREX_SYSTEM_CONFIG = {
        "trading_mode": "paper",
        "order_confirmation": "manual",
        "risk_level": "medium",
        "initial_capital": 100000.0,
        "default_lot_size": 1.0,
        "max_trades_per_day": 5,
        "max_daily_loss": 0.03,
        "risk_per_trade": 0.01,
        "max_concurrent_positions": 2,
        "consecutive_losses": 2,
        "ladder_cooldown": 15,
        "max_usd_pairs_open": 2,
        "drawdown_scaler": True,
        "sandbox_enabled": False,
        "max_risk_sandbox_trade": 5.0,
        "forced_micro_lots": 0.01,
        "max_allowed_open_sandbox": 1,
        "rollover_blackout": True,
        "wednesday_triple_swap": True,
        "kalman_velocity_veto": 1.8,
        "shannon_entropy_chop": 0.65,
        "consecutive_loss_cooldown_bars": 45,
        "advanced_ladder_cooldown_bars": 15,
    }

    @app.route("/api/config", methods=["GET"])
    def get_global_config():
        mode = container.gold_order_manager().get_mode()
        _FOREX_SYSTEM_CONFIG["trading_mode"] = mode
        return jsonify(_FOREX_SYSTEM_CONFIG)

    @app.route("/api/config", methods=["POST"])
    def update_global_config():
        data = request.json or {}
        for key, val in data.items():
            _FOREX_SYSTEM_CONFIG[key] = val

        if "trading_mode" in data:
            new_mode = str(data["trading_mode"]).lower()
            try:
                container.gold_order_manager().set_mode(new_mode)
                container.live_execution_engine().set_mode(new_mode)
            except Exception as e:
                return jsonify({"error": str(e)}), 400

        _FOREX_SYSTEM_CONFIG["trading_mode"] = container.gold_order_manager().get_mode()
        return jsonify({"status": "success", "config": _FOREX_SYSTEM_CONFIG, "trading_mode": _FOREX_SYSTEM_CONFIG["trading_mode"]})


    app.register_blueprint(system.bp)
    app.register_blueprint(trading.bp)
    app.register_blueprint(positions.bp)
    app.register_blueprint(stream.bp)
    app.register_blueprint(backtest.bp)
    app.register_blueprint(live.bp)
    app.register_blueprint(charts.bp)
    
    # Start Runtime Services (Scan Scheduler) in the background
    import os
    if os.getenv("GOLDEDGE_AUTOSTART", "1").lower() not in {"0", "false", "no"}:
        try:
            scheduler = container.scan_scheduler()
            scheduler.start()
            app.logger.info("Background ScanScheduler (Redis Tick Consumer) started.")
        except Exception as e:
            app.logger.warning(f"Background ScanScheduler could not start: {e}")
    
    return app
