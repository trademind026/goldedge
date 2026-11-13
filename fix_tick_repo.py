import re

path = "src/goldedge/adapters/secondary/db/tick_repository.py"
with open(path, "r") as f:
    c = f.read()

new_method = """
    def get_ticks_as_df(self, symbol: str, start_time, end_time):
        import pandas as pd
        sql = "SELECT time as timestamp, symbol, bid, ask, last as price FROM market_ticks WHERE symbol = %(symbol)s AND time >= %(start)s AND time <= %(end)s ORDER BY time ASC"
        params = {"symbol": symbol, "start": start_time, "end": end_time}
        return pd.read_sql(sql, self.engine, params=params)

    def get_ticks(self"""

c = c.replace("    def get_ticks(self", new_method)

with open(path, "w") as f:
    f.write(c)
