from functools import wraps

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Authentication globally disabled
        return f(*args, **kwargs)
    return decorated

def require_auth(roles=None):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            # Authentication globally disabled
            return f(*args, **kwargs)
        return decorated
    return decorator
