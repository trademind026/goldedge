import re

path = "src/goldedge/adapters/primary/rest_api/factory.py"
with open(path, "r") as f:
    c = f.read()

# Add a global /api/config route
c = c.replace('app.register_blueprint(system.bp)', '''
    from flask import jsonify
    @app.route("/api/config", methods=["GET"])
    def get_global_config():
        return jsonify({"trading_mode": "demo", "order_confirmation": "true", "risk_level": "medium"})

    @app.route("/api/config", methods=["POST"])
    def update_global_config():
        return jsonify({"status": "success"})

    app.register_blueprint(system.bp)
''')

with open(path, "w") as f:
    f.write(c)
