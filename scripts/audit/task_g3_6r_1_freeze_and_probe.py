"""
TASK G3.6R.1 — Phase 0 & Phase 1:
1. Freeze & SHA-256 fingerprint all four evaluation configurations (H0, H3b, Change A, Change C),
   application source files, and existing May-Nov 2026 verified dataset BEFORE any backfill or testing.
2. Execute a controlled, read-only cTrader Open API historical tick availability probe across
   January 2024 through May 2026 (1-minute weekday 13:00-13:01 UTC BID + ASK windows, zero orders,
   zero DB writes) to establish exact broker/account tick history boundaries.

Outputs in backtest_results/audit/task_g3_6r_1_extended_history/:
  - g3_6r_1_frozen_config_manifest.json
  - g3_6r_1_source_availability_report.json
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import psycopg2
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

OUT_DIR = Path("backtest_results/audit/task_g3_6r_1_extended_history")
SYMBOL_ID = 41  # XAUUSD on Fusion Markets cTrader demo
PRICE_SCALE = 100000.0
RATE_SLEEP_S = 0.30

# Monthly weekday 13:00-13:01 UTC probe windows from Jan 2024 through May 2026
PROBE_DATES: List[Tuple[str, datetime]] = [
    ("2026-05-15_control", datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)),
    ("2026-04-15", datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)),
    ("2026-03-16", datetime(2026, 3, 16, 13, 0, tzinfo=timezone.utc)),
    ("2026-02-17", datetime(2026, 2, 17, 13, 0, tzinfo=timezone.utc)),
    ("2026-01-15", datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc)),
    ("2025-12-15", datetime(2025, 12, 15, 13, 0, tzinfo=timezone.utc)),
    ("2025-11-17", datetime(2025, 11, 17, 13, 0, tzinfo=timezone.utc)),
    ("2025-10-15", datetime(2025, 10, 15, 13, 0, tzinfo=timezone.utc)),
    ("2025-09-15", datetime(2025, 9, 15, 13, 0, tzinfo=timezone.utc)),
    ("2025-08-15", datetime(2025, 8, 15, 13, 0, tzinfo=timezone.utc)),
    ("2025-07-15", datetime(2025, 7, 15, 13, 0, tzinfo=timezone.utc)),
    ("2025-06-16", datetime(2025, 6, 16, 13, 0, tzinfo=timezone.utc)),
    ("2025-05-15", datetime(2025, 5, 15, 13, 0, tzinfo=timezone.utc)),
    ("2025-04-15", datetime(2025, 4, 15, 13, 0, tzinfo=timezone.utc)),
    ("2025-03-17", datetime(2025, 3, 17, 13, 0, tzinfo=timezone.utc)),
    ("2025-02-17", datetime(2025, 2, 17, 13, 0, tzinfo=timezone.utc)),
    ("2025-01-15", datetime(2025, 1, 15, 13, 0, tzinfo=timezone.utc)),
    ("2024-12-16", datetime(2024, 12, 16, 13, 0, tzinfo=timezone.utc)),
    ("2024-11-15", datetime(2024, 11, 15, 13, 0, tzinfo=timezone.utc)),
    ("2024-10-15", datetime(2024, 10, 15, 13, 0, tzinfo=timezone.utc)),
    ("2024-09-16", datetime(2024, 9, 16, 13, 0, tzinfo=timezone.utc)),
    ("2024-08-15", datetime(2024, 8, 15, 13, 0, tzinfo=timezone.utc)),
    ("2024-07-15", datetime(2024, 7, 15, 13, 0, tzinfo=timezone.utc)),
    ("2024-06-17", datetime(2024, 6, 17, 13, 0, tzinfo=timezone.utc)),
    ("2024-05-15", datetime(2024, 5, 15, 13, 0, tzinfo=timezone.utc)),
    ("2024-04-15", datetime(2024, 4, 15, 13, 0, tzinfo=timezone.utc)),
    ("2024-03-15", datetime(2024, 3, 15, 13, 0, tzinfo=timezone.utc)),
    ("2024-02-15", datetime(2024, 2, 15, 13, 0, tzinfo=timezone.utc)),
    ("2024-01-15", datetime(2024, 1, 15, 13, 0, tzinfo=timezone.utc)),
]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _sha256_obj(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest().upper()


def _write_frozen_manifest() -> Dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("DB_URL") or "postgresql://postgres:postgres@db:5432/goldedge"
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM market_orders;")
    n_orders = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM market_trades;")
    n_trades = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*), MIN(time), MAX(time) FROM market_ticks;")
    mt_cnt, mt_min, mt_max = cur.fetchone()
    cur.execute("SELECT COUNT(*), MIN(time), MAX(time) FROM market_ticks_staged_expansion;")
    st_cnt, st_min, st_max = cur.fetchone()
    conn.close()

    common_exec = {
        "symbol": "XAUUSD",
        "starting_capital_usd": 10000.0,
        "risk_per_trade_pct": 0.5,
        "commission_per_million_usd_per_side": 12.0,
        "slippage_pips_per_side": 0.0533,
        "hwm_max_drawdown_pct": 8.0,
        "use_kalman_exit": False,
        "be_trigger_atr": 1.0,
        "sl_atr_mult": 1.5,
        "tp_atr_mult": 3.0,
        "max_concurrent_positions": 1,
        "swap_per_lot_night": 0.0,
    }

    configs = {
        "H0_ORIGINAL": {
            **common_exec,
            "session_close_utc": 17,
            "early_fail_mins": None,
            "early_fail_min_mfe_atr": None,
            "min_score_threshold": 0.60,
            "max_kaufman_er": None,
        },
        "H3B_FROZEN": {
            **common_exec,
            "session_close_utc": 15,
            "early_fail_mins": 15,
            "early_fail_min_mfe_atr": 0.4,
            "min_score_threshold": 0.60,
            "max_kaufman_er": None,
        },
        "H3B_CHANGE_A_ER50": {
            **common_exec,
            "session_close_utc": 15,
            "early_fail_mins": 15,
            "early_fail_min_mfe_atr": 0.4,
            "min_score_threshold": 0.60,
            "max_kaufman_er": 0.50,
        },
        "H3B_CHANGE_C_SCORE68": {
            **common_exec,
            "session_close_utc": 15,
            "early_fail_mins": 15,
            "early_fail_min_mfe_atr": 0.4,
            "min_score_threshold": 0.68,
            "max_kaufman_er": None,
        },
    }

    code_files = [
        "src/goldedge/application/use_cases/run_backtest.py",
        "src/goldedge/domain/services/signal_generator.py",
        "src/goldedge/domain/services/sizing_service.py",
        "src/goldedge/domain/services/denoise_service.py",
        "src/goldedge/config/settings.py",
    ]
    frozen_artifacts = [
        "backtest_results/frozen_111_baseline_trades.csv",
        "backtest_results/audit/task_g1_backtest_accuracy/g1_original_70_baseline_trades.csv",
        "backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_controlled_experiments.json",
    ]

    manifest = {
        "task": "G3.6R.1_FROZEN_CONFIG_MANIFEST",
        "created_before_backfill_utc": datetime.now(timezone.utc).isoformat(),
        "safety_locks": {
            "SHADOW_MODE": os.environ.get("SHADOW_MODE", "True"),
            "TRADING_MODE": os.environ.get("TRADING_MODE", "demo"),
            "market_orders": n_orders,
            "market_trades": n_trades,
            "HWM_MAX_DRAWDOWN_PCT": 8.0,
        },
        "frozen_configurations": {
            k: {"params": v, "config_sha256": _sha256_obj(v)} for k, v in configs.items()
        },
        "prespecified_candidates_for_fdr": ["H3B_CHANGE_A_ER50", "H3B_CHANGE_C_SCORE68"],
        "code_sha256": {p: _sha256_file(Path(p)) for p in code_files if Path(p).exists()},
        "prior_frozen_artifacts_sha256": {p: _sha256_file(Path(p)) for p in frozen_artifacts if Path(p).exists()},
        "existing_verified_may_nov_dataset_snapshot": {
            "market_ticks_rows": int(mt_cnt),
            "market_ticks_min_utc": mt_min.isoformat() if mt_min else None,
            "market_ticks_max_utc": mt_max.isoformat() if mt_max else None,
            "market_ticks_staged_expansion_rows": int(st_cnt),
            "market_ticks_staged_expansion_min_utc": st_min.isoformat() if st_min else None,
            "market_ticks_staged_expansion_max_utc": st_max.isoformat() if st_max else None,
            "preservation_rule": "READ-ONLY; new extended history MUST be stored in isolated table `market_ticks_extended_pre202605`.",
        },
    }
    (OUT_DIR / "g3_6r_1_frozen_config_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("[PHASE 0] Frozen configuration manifest written BEFORE backfill/probe.", flush=True)
    return manifest


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


class ExtendedHistoryProbe:
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
        reactor.callLater(90.0, self._timeout)
        self.client.startService()
        reactor.run()

    def _timeout(self) -> None:
        self.errors.append("PROBE_TIMEOUT_90S")
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
                avail = len(self.cur_bids) > 0 and len(self.cur_asks) > 0
                spread = None
                if avail:
                    spread = round(self.cur_asks[0][1] - self.cur_bids[0][1], 5)
                rec = {
                    "probe_label": lbl,
                    "window_utc": dt.isoformat(),
                    "bid_ticks": len(self.cur_bids),
                    "ask_ticks": len(self.cur_asks),
                    "has_more_flag": bool(payload.hasMore),
                    "available": avail,
                    "sample_bid_px": self.cur_bids[0][1] if self.cur_bids else None,
                    "sample_ask_px": self.cur_asks[0][1] if self.cur_asks else None,
                    "sample_spread_usd": spread,
                }
                self.results.append(rec)
                print(
                    f"[PROBE] {lbl:18s}: bids={rec['bid_ticks']:4d} asks={rec['ask_ticks']:4d} "
                    f"avail={str(avail):5s} bid={rec['sample_bid_px']} ask={rec['sample_ask_px']}",
                    flush=True,
                )
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
    _write_frozen_manifest()
    probe = ExtendedHistoryProbe()
    probe.start()

    pre_may_avail = [r for r in probe.results if r["probe_label"] != "2026-05-15_control" and r["available"]]
    pre_may_unavail = [r for r in probe.results if r["probe_label"] != "2026-05-15_control" and not r["available"]]
    earliest_utc = min((r["window_utc"] for r in pre_may_avail), default=None)
    latest_pre_may_utc = max((r["window_utc"] for r in pre_may_avail), default=None)

    report = {
        "task": "G3.6R.1_SOURCE_AVAILABILITY_REPORT",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "broker": f"Fusion Markets cTrader {probe.mode.upper()} (account {probe.account_id}, symbolId={SYMBOL_ID} XAUUSD)",
        "api_method": "ProtoOAGetTickDataReq (read-only, 1-min weekday 13:00-13:01 UTC BID+ASK probes)",
        "auth_succeeded": probe.auth_ok,
        "total_months_probed_pre_may_2026": len(PROBE_DATES) - 1,
        "months_available_pre_may_2026": len(pre_may_avail),
        "months_unavailable_pre_may_2026": len(pre_may_unavail),
        "earliest_available_probe_utc": earliest_utc,
        "latest_pre_may_probe_utc": latest_pre_may_utc,
        "independence_statement": (
            "All periods strictly prior to 2026-05-13T00:00:00Z were NEVER downloaded, viewed, or used "
            "during G1, G2, G3.1-G3.5R, or G3.6R selection of Change A (max_kaufman_er=0.50) or Change C (min_score=0.68)."
        ),
        "probe_results": probe.results,
        "errors": probe.errors,
        "elapsed_sec": round(time.time() - probe.t0, 2),
    }
    (OUT_DIR / "g3_6r_1_source_availability_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        f"[PROBE COMPLETE] auth={probe.auth_ok} avail_pre_may={len(pre_may_avail)}/{len(PROBE_DATES)-1} "
        f"earliest={earliest_utc} errors={probe.errors}",
        flush=True,
    )


if __name__ == "__main__":
    main()
