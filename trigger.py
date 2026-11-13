import requests
import json

resp = requests.post(
    "http://localhost:5000/api/backtest/run",
    json={"start_date": "2026-08-01", "end_date": "2026-08-31", "risk": "medium"}
)
print("Status:", resp.status_code)
print("Response:", resp.text)
