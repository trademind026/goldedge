import re
import pandas as pd

path = "src/goldedge/adapters/secondary/db/tick_repository.py"
with open(path, "r") as f:
    c = f.read()

c = c.replace(
    "SELECT time as timestamp, symbol, bid, ask, last as price",
    "SELECT time, symbol, bid, ask, last as price, 1 as volume"
)

with open(path, "w") as f:
    f.write(c)
