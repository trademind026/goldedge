from functools import wraps
from flask import request, jsonify
import os

def require_api_key(f):
    """
    Decorator to protect internal endpoints via API Key header.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        expected_key = os.environ.get("API_SECRET", "dev-secret-key")
        
        api_key = request.headers.get("X-API-Key")
        if not api_key or api_key != expected_key:
            return jsonify({"error": "Unauthorized"}), 401
            
        return f(*args, **kwargs)
    return decorated_function
