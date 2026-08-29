from flask import Flask
from .routes import system, trading, positions, stream
from .....application.container import Container

def create_app() -> Flask:
    app = Flask(__name__)
    
    # Simple CORS
    @app.after_request
    def after_request(response):
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-API-Key')
        response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
        return response

    container = Container()
    
    # Wire the container to the route modules
    container.wire(modules=[
        "goldedge.adapters.primary.rest_api.routes.system",
        "goldedge.adapters.primary.rest_api.routes.trading",
        "goldedge.adapters.primary.rest_api.routes.positions",
        "goldedge.adapters.primary.rest_api.routes.stream"
    ])
    
    app.container = container
    
    # Register blueprints
    app.register_blueprint(system.bp)
    app.register_blueprint(trading.bp)
    app.register_blueprint(positions.bp)
    app.register_blueprint(stream.bp)
    
    # Start Runtime Services (Scan Scheduler) in the background
    import os
    if os.getenv("GOLDEDGE_AUTOSTART", "1").lower() not in {"0", "false", "no"}:
        scheduler = container.scan_scheduler()
        scheduler.start()
        app.logger.info("Background ScanScheduler (Redis Tick Consumer) started.")
    
    return app
