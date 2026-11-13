import requests
import json

resp = requests.get("http://localhost:5000/api/backtest/results")
print("Status:", resp.status_code)
d = resp.json()
print("Keys:", list(d.keys()))
if "medium" in d:
    print("Trades:", d["medium"].get("trades"))
