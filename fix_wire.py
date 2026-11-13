import re

path = "src/goldedge/adapters/primary/rest_api/factory.py"
with open(path, "r") as f:
    c = f.read()

c = c.replace('"goldedge.adapters.primary.rest_api.routes.stream"', '"goldedge.adapters.primary.rest_api.routes.stream",\n        "goldedge.adapters.primary.rest_api.routes.backtest"')

with open(path, "w") as f:
    f.write(c)
