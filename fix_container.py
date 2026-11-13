import re

path = "src/goldedge/config/container.py"
with open(path, "r") as f:
    c = f.read()

c = c.replace(
    'token="mock", redis_url="mock"',
    'access_token="mock", refresh_token="mock", token_expires_at=0.0, redis_url="mock"'
)

with open(path, "w") as f:
    f.write(c)
