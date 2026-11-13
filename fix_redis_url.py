import re

path = "src/goldedge/config/container.py"
with open(path, "r") as f:
    c = f.read()

c = c.replace('redis_url="mock"', 'redis_url="redis://mock"')

with open(path, "w") as f:
    f.write(c)
