import os
import sys
from pathlib import Path

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "src"))

from goldedge.adapters.primary.rest_api.factory import create_app

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5055"))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"[*] Starting GoldEdge Flask API on http://{host}:{port} (XAUUSD Institutional Engine)...")
    app = create_app()
    app.run(host=host, port=port, debug=False, threaded=True)
