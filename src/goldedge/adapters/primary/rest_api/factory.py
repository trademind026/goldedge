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
    
    event_bus = RedisStreamEventBus(
        redis_url=settings.REDIS_URL,
        consumer_group="live_ticker_group",
        consumer_name="backend_1"
    )
    
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
        "goldedge.adapters.primary.rest_api.routes.backtest"
    ])
    
    app.container = container
    
    from .routes import system, trading, positions, stream, backtest
    
    # Register blueprints
    
    from flask import jsonify
    @app.route("/api/config", methods=["GET"])
    def get_global_config():
        return jsonify({"trading_mode": "demo", "order_confirmation": "true", "risk_level": "medium"})

    @app.route("/api/config", methods=["POST"])
    def update_global_config():
        return jsonify({"status": "success"})

    app.register_blueprint(system.bp)

    app.register_blueprint(trading.bp)
    app.register_blueprint(positions.bp)
    app.register_blueprint(stream.bp)
    app.register_blueprint(backtest.bp)
    
    # Start Runtime Services (Scan Scheduler) in the background
    import os
    if os.getenv("GOLDEDGE_AUTOSTART", "1").lower() not in {"0", "false", "no"}:
        scheduler = container.scan_scheduler()
        scheduler.start()
        app.logger.info("Background ScanScheduler (Redis Tick Consumer) started.")
    
    return app
