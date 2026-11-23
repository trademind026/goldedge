"""
TASK G3.3 — Phase A: Controlled Read-Only cTrader Historical Tick Availability Probe.
Tests 1-minute BID + ASK windows backward from 2026-08-20 to 2025-11-17 via
ProtoOAGetTickDataReq (read-only, zero orders, zero DB writes, <= 3 req/s)
to determine the exact earliest recoverable broker-consistent XAUUSD history.
Outputs:
  - backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_historical_expansion_report.json
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from twisted.internet import reactor
from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoErrorRes
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAAccountAuthRes,
    ProtoOAApplicationAuthReq,
    ProtoOAApplicationAuthRes,
    ProtoOAErrorRes,
    ProtoOAGetTickDataReq,
    ProtoOAGetTickDataRes,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAQuoteType

OUT_DIR = Path("backtest_results/audit/task_g3_3_leakage_proof_pipeline")
SYMBOL_ID = 41  # XAUUSD on Fusion Markets cTrader demo
PRICE_SCALE = 100000.0
RATE_SLEEP_S = 0.35

# Controlled 1-minute weekday 13:00-13:01 UTC probe windows (London/NY overlap)
PROBE_DATES = [
    ("2026-08-20_control", datetime(2026, 8, 20, 13, 0, tzinfo=timezone.utc)),
    ("2026-08-10", datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc)),
    ("2026-07-15", datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)),
    ("2026-06-15", datetime(2026, 6, 15, 13, 0, tzinfo=timezone.utc)),
    ("2026-05-15", datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)),
    ("2026-04-15", datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)),
    ("2026-02-17", datetime(2026, 2, 17, 13, 0, tzinfo=timezone.utc)),
    ("2025-11-17", datetime(2025, 11, 17, 13, 0, tzinfo=timezone.utc)),
]


def _decode(tick_data) -> List[Tuple[int, float]]:
    out: List[Tuple[int, float]] = []
    cur_ts = 0
    cur_px = 0
    for idx, td in enumerate(tick_data):
        if idx == 0:
            cur_ts = int(td.timestamp)
            cur_px = int(td.tick)
        else:
            cur_ts += int(td.timestamp)
            cur_px += int(td.tick)
        out.append((cur_ts, round(cur_px / PRICE_SCALE, 5)))
    out.sort(key=lambda x: x[0])
    return out


class ReadOnlyHistoryProbe:
    def __init__(self) -> None:
        self.client_id = os.environ["CTRADER_CLIENT_ID"]
        self.client_secret = os.environ["CTRADER_CLIENT_SECRET"]
        self.access_token = os.environ["CTRADER_ACCESS_TOKEN"]
        self.account_id = int(os.environ["CTRADER_ACCOUNT_ID"])
        self.mode = os.environ.get("TRADING_MODE", "demo").lower()
        host = EndPoints.PROTOBUF_LIVE_HOST if self.mode == "live" else EndPoints.PROTOBUF_DEMO_HOST
        self.client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)

        self.idx = 0
        self.side = ProtoOAQuoteType.BID
        self.cur_bids: List[Tuple[int, float]] = []
        self.cur_asks: List[Tuple[int, float]] = []
        self.results: List[Dict[str, Any]] = []
        self.errors: List[str] = []
        self.auth_ok = False
        self.t0 = time.time()

    def start(self) -> None:
        self.client.setConnectedCallback(self._on_connected)
        self.client.setDisconnectedCallback(self._on_disconnected)
        self.client.setMessageReceivedCallback(self._on_message)
        reactor.callLater(35.0, self._timeout)
        self.client.startService()
        reactor.run()

    def _timeout(self) -> None:
        self.errors.append("PROBE_TIMEOUT_35S")
        self._finish()

    def _finish(self) -> None:
        try:
            self.client.stopService()
        except Exception:
            pass
        if reactor.running:
            reactor.stop()

    def _on_connected(self, client) -> None:
        req = ProtoOAApplicationAuthReq()
        req.clientId = self.client_id
        req.clientSecret = self.client_secret
        client.send(req)

    def _on_disconnected(self, _client, reason) -> None:
        if self.idx < len(PROBE_DATES):
            self.errors.append(f"DISCONNECTED: {reason}")
        if reactor.running:
            reactor.stop()

    def _send_probe(self) -> None:
        if self.idx >= len(PROBE_DATES):
            self._finish()
            return
        _lbl, dt = PROBE_DATES[self.idx]
        s_ms = int(dt.timestamp() * 1000)
        e_ms = s_ms + 60_000
        req = ProtoOAGetTickDataReq()
        req.ctidTraderAccountId = self.account_id
        req.symbolId = SYMBOL_ID
        req.type = self.side
        req.fromTimestamp = s_ms
        req.toTimestamp = e_ms
        self.client.send(req)

    def _on_message(self, _client, msg) -> None:
        payload = Protobuf.extract(msg)
        if msg.payloadType == ProtoOAApplicationAuthRes().payloadType:
            req = ProtoOAAccountAuthReq()
            req.ctidTraderAccountId = self.account_id
            req.accessToken = self.access_token
            self.client.send(req)
        elif msg.payloadType == ProtoOAAccountAuthRes().payloadType:
            self.auth_ok = True
            reactor.callLater(RATE_SLEEP_S, self._send_probe)
        elif msg.payloadType == ProtoOAGetTickDataRes().payloadType:
            decoded = _decode(payload.tickData)
            if self.side == ProtoOAQuoteType.BID:
                self.cur_bids = decoded
                self.side = ProtoOAQuoteType.ASK
                reactor.callLater(RATE_SLEEP_S, self._send_probe)
            else:
                self.cur_asks = decoded
                lbl, dt = PROBE_DATES[self.idx]
                rec = {
                    "probe_label": lbl,
                    "window_utc": dt.isoformat(),
                    "bid_ticks": len(self.cur_bids),
                    "ask_ticks": len(self.cur_asks),
                    "has_more_flag": bool(payload.hasMore),
                    "available": len(self.cur_bids) > 0 and len(self.cur_asks) > 0,
                    "sample_bid_px": self.cur_bids[0][1] if self.cur_bids else None,
                    "sample_ask_px": self.cur_asks[0][1] if self.cur_asks else None,
                }
                self.results.append(rec)
                print(f"[PROBE] {lbl}: bids={rec['bid_ticks']} asks={rec['ask_ticks']} avail={rec['available']}", flush=True)
                self.idx += 1
                self.side = ProtoOAQuoteType.BID
                self.cur_bids = []
                self.cur_asks = []
                reactor.callLater(RATE_SLEEP_S, self._send_probe)
        elif msg.payloadType in (ProtoOAErrorRes().payloadType, ProtoErrorRes().payloadType):
            err = f"code={getattr(payload, 'errorCode', None)} desc={getattr(payload, 'description', None)}"
            self.errors.append(err)
            print(f"[PROBE ERROR] {err}", flush=True)
            self._finish()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    probe = ReadOnlyHistoryProbe()
    probe.start()

    earlier_avail = [r for r in probe.results if r["probe_label"] != "2026-08-20_control" and r["available"]]
    control_ok = any(r["probe_label"] == "2026-08-20_control" and r["available"] for r in probe.results)

    if len(earlier_avail) >= 3:
        status = "HISTORY_EXPANSION_PARTIAL"  # Probe confirmed server availability; full backfill staged separately
    elif len(earlier_avail) >= 1:
        status = "HISTORY_EXPANSION_PARTIAL"
    else:
        status = "HISTORY_EXPANSION_UNAVAILABLE"

    report = {
        "task": "G3.3_PHASE_A_HISTORICAL_EXPANSION_REPORT",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "broker": "Fusion Markets cTrader Demo (account 48439552, symbolId=41 XAUUSD)",
        "api_method": "ProtoOAGetTickDataReq (read-only, <= 1 week per request, paginated via hasMore)",
        "auth_succeeded": probe.auth_ok,
        "control_2026_08_20_available": control_ok,
        "earlier_months_probes_available": len(earlier_avail),
        "earliest_available_probe_utc": min((r["window_utc"] for r in earlier_avail), default=None),
        "current_verified_db_coverage": {
            "ml_eligible_min_utc": "2026-08-13T00:00:00.153000+00:00",
            "ml_eligible_max_utc": "2026-11-19T11:45:39.744483+00:00",
            "ml_eligible_1m_bars": 96862,
            "clean_weekday_trading_days": 68,
            "approx_months": 3.2,
        },
        "local_backup_archives_checked": {
            "csv_goldedge_20261117_143146.zip": "Snapshot of same Aug 13 - Nov 17 DB; zero earlier months.",
            "db_data.zip": "Bundle of csv_goldedge_20261117_143146.zip + shadow_artifacts_20261117.zip.",
        },
        "probe_results": probe.results,
        "errors": probe.errors,
        "governance_note": (
            "Read-only probe only; zero broker orders placed, zero raw market_ticks overwritten, "
            "and frozen H0/H3b research windows remain strictly untouched."
        ),
        "elapsed_sec": round(time.time() - probe.t0, 2),
    }
    (OUT_DIR / "g3_3_historical_expansion_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[PHASE A PROBE DONE] status={status} auth={probe.auth_ok} earlier_avail={len(earlier_avail)} errors={probe.errors}")


if __name__ == "__main__":
    main()
