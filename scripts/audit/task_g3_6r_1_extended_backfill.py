"""
TASK G3.6R.1 — Isolated Resumable Paginated cTrader Extended Historical Backfill.
Retrieves genuine Fusion Markets cTrader Demo XAUUSD bid/ask ticks (symbolId=41) for
the previously unexamined 12-month period + 10-day warmup:
  2025-04-21T00:00:00Z .. 2026-05-01T00:00:00Z
into isolated table `market_ticks_extended_pre202605`.
NEVER modifies `market_ticks` or `market_ticks_staged_expansion`.

Features:
  - Read-only ProtoOAGetTickDataReq (zero orders)
  - 12-hour chunks, full `hasMore` backward pagination
  - Rate-limited <= ~4.7 req/s (0.21s sleep) + 30s per-request watchdog
  - Causal outer union of BID & ASK quotes -> ffill BBO -> strict (bid>0 & ask>0 & bid<ask)
  - Fast PostgreSQL COPY via TEMP staging table -> ON CONFLICT (symbol, time) DO NOTHING
  - Excludes weekend off-session hours (Sat UTC, Sun < 21:00 UTC)
  - Checkpoint JSON with per-chunk raw/valid/dropped counts for lossless resume
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd
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

SYMBOL = "XAUUSD"
SYMBOL_ID = 41
PRICE_SCALE = 100000.0
RATE_SLEEP_S = 0.21
WATCHDOG_S = 30.0
TARGET_TABLE = "market_ticks_extended_pre202605"
OUT_DIR = Path("backtest_results/audit/task_g3_6r_1_extended_history")
CHECKPOINT_FILE = OUT_DIR / "extended_backfill_checkpoint.json"


def _decode_batch(tick_data) -> List[Tuple[int, float]]:
    if not tick_data:
        return []
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


def _merge_bid_ask_causal(
    bids: List[Tuple[int, float]],
    asks: List[Tuple[int, float]],
    start_ms: int,
    end_ms: int,
) -> Tuple[List[Tuple[str, str, float, float, float]], int]:
    if not bids or not asks:
        return [], 0
    df_b = pd.DataFrame(bids, columns=["ts_ms", "bid"]).drop_duplicates(subset=["ts_ms"], keep="last").sort_values("ts_ms")
    df_a = pd.DataFrame(asks, columns=["ts_ms", "ask"]).drop_duplicates(subset=["ts_ms"], keep="last").sort_values("ts_ms")
    df = pd.merge(df_b, df_a, on="ts_ms", how="outer").sort_values("ts_ms")
    df["bid"] = df["bid"].ffill()
    df["ask"] = df["ask"].ffill()
    df = df.dropna(subset=["bid", "ask"])
    df = df[(df["ts_ms"] >= start_ms) & (df["ts_ms"] < end_ms)]
    before_valid = len(df)
    df = df[(df["bid"] > 0.0) & (df["ask"] > 0.0) & (df["bid"] < df["ask"])]
    invalid_dropped = before_valid - len(df)
    if df.empty:
        return [], invalid_dropped

    # Vectorized weekend filter: Sat (weekday==5) or Sun < 21:00 UTC
    dts = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    wd = dts.dt.day_of_week
    hr = dts.dt.hour
    keep = ~((wd == 5) | ((wd == 6) & (hr < 21)))
    df = df[keep]
    dts = dts[keep]
    if df.empty:
        return [], invalid_dropped

    iso_ts = dts.dt.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00").tolist()
    b_vals = df["bid"].round(5).tolist()
    a_vals = df["ask"].round(5).tolist()
    rows = [
        (t, SYMBOL, b, a, round((b + a) / 2.0, 5))
        for t, b, a in zip(iso_ts, b_vals, a_vals)
    ]
    return rows, invalid_dropped


def _build_chunks(start_dt: datetime, end_dt: datetime, chunk_hours: int = 12) -> List[Tuple[str, int, int]]:
    chunks: List[Tuple[str, int, int]] = []
    cur = start_dt
    step = timedelta(hours=chunk_hours)
    while cur < end_dt:
        nxt = min(cur + step, end_dt)
        # Skip pure Saturday chunks and Sunday morning/afternoon chunks (market closed)
        if not (cur.weekday() == 5 and (nxt.weekday() == 5 or (nxt.weekday() == 6 and nxt.hour == 0))):
            if not (cur.weekday() == 6 and cur.hour < 12 and nxt.weekday() == 6 and nxt.hour <= 20):
                cid = f"{cur.strftime('%Y%m%d_%H')}_{nxt.strftime('%Y%m%d_%H')}"
                chunks.append((cid, int(cur.timestamp() * 1000), int(nxt.timestamp() * 1000)))
        cur = nxt
    return chunks


class ExtendedCTraderBackfiller:
    def __init__(self, chunks: List[Tuple[str, int, int]], db_url: str) -> None:
        self.client_id = os.environ["CTRADER_CLIENT_ID"]
        self.client_secret = os.environ["CTRADER_CLIENT_SECRET"]
        self.access_token = os.environ["CTRADER_ACCESS_TOKEN"]
        self.account_id = int(os.environ["CTRADER_ACCOUNT_ID"])
        self.mode = os.environ.get("TRADING_MODE", "demo").lower()
        host = EndPoints.PROTOBUF_LIVE_HOST if self.mode == "live" else EndPoints.PROTOBUF_DEMO_HOST
        self.client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)

        self.db_url = db_url
        self.conn = psycopg2.connect(db_url)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                CREATE TEMP TABLE _tmp_ext_ticks (
                    time TIMESTAMPTZ NOT NULL,
                    symbol VARCHAR(20) NOT NULL,
                    bid DOUBLE PRECISION NOT NULL,
                    ask DOUBLE PRECISION NOT NULL,
                    last DOUBLE PRECISION
                ) ON COMMIT DELETE ROWS;
                """
            )
        self.conn.commit()

        self.completed: Dict[str, Any] = {}
        if CHECKPOINT_FILE.exists():
            try:
                self.completed = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8")).get("completed_chunks", {})
            except Exception:
                self.completed = {}

        self.chunks = [c for c in chunks if c[0] not in self.completed]
        self.total_chunks_initial = len(chunks)
        self.chunk_idx = 0
        self.cur_quote_type = ProtoOAQuoteType.BID
        self.cur_to_ms = 0
        self.cur_bids: List[Tuple[int, float]] = []
        self.cur_asks: List[Tuple[int, float]] = []
        self.req_count = 0
        self.total_inserted = 0
        self.t0 = time.time()
        self.watchdog = None

    def _reset_watchdog(self) -> None:
        if self.watchdog is not None and self.watchdog.active():
            self.watchdog.cancel()
        self.watchdog = reactor.callLater(WATCHDOG_S, self._on_watchdog)

    def _cancel_watchdog(self) -> None:
        if self.watchdog is not None and self.watchdog.active():
            self.watchdog.cancel()
        self.watchdog = None

    def _on_watchdog(self) -> None:
        print("[EXT BACKFILL] Request watchdog 30s expired; saving checkpoint and exiting for retry.", flush=True)
        self._finish()

    def _save_checkpoint(self) -> None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        CHECKPOINT_FILE.write_text(
            json.dumps(
                {
                    "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                    "target_table": TARGET_TABLE,
                    "completed_count": len(self.completed),
                    "total_chunks": self.total_chunks_initial,
                    "completed_chunks": self.completed,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def start(self) -> None:
        if not self.chunks:
            print(f"[EXT BACKFILL] All {self.total_chunks_initial} chunks already completed!", flush=True)
            self.conn.close()
            return
        print(f"[EXT BACKFILL] Remaining chunks: {len(self.chunks)} / {self.total_chunks_initial}", flush=True)
        self.client.setConnectedCallback(self._on_connected)
        self.client.setDisconnectedCallback(self._on_disconnected)
        self.client.setMessageReceivedCallback(self._on_message)
        self._reset_watchdog()
        self.client.startService()
        reactor.run()

    def _finish(self) -> None:
        self._cancel_watchdog()
        self._save_checkpoint()
        el = round(time.time() - self.t0, 1)
        print(
            f"[EXT BACKFILL DONE in {el}s] reqs={self.req_count:,} inserted={self.total_inserted:,} "
            f"completed={len(self.completed)}/{self.total_chunks_initial}",
            flush=True,
        )
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            self.client.stopService()
        except Exception:
            pass
        if reactor.running:
            reactor.stop()

    def _on_connected(self, client) -> None:
        self._reset_watchdog()
        req = ProtoOAApplicationAuthReq()
        req.clientId = self.client_id
        req.clientSecret = self.client_secret
        client.send(req)

    def _on_disconnected(self, _client, reason) -> None:
        self._cancel_watchdog()
        print(f"[EXT BACKFILL] Disconnected: {reason}", flush=True)
        self._save_checkpoint()
        if reactor.running:
            reactor.stop()

    def _send_tick_req(self) -> None:
        if self.chunk_idx >= len(self.chunks):
            self._finish()
            return
        _cid, s_ms, _e_ms = self.chunks[self.chunk_idx]
        req = ProtoOAGetTickDataReq()
        req.ctidTraderAccountId = self.account_id
        req.symbolId = SYMBOL_ID
        req.type = self.cur_quote_type
        req.fromTimestamp = max(0, s_ms - 60_000)
        req.toTimestamp = self.cur_to_ms
        self.req_count += 1
        self._reset_watchdog()
        self.client.send(req)

    def _start_next_chunk(self) -> None:
        if self.chunk_idx >= len(self.chunks):
            self._finish()
            return
        _cid, _s_ms, e_ms = self.chunks[self.chunk_idx]
        self.cur_quote_type = ProtoOAQuoteType.BID
        self.cur_to_ms = e_ms
        self.cur_bids = []
        self.cur_asks = []
        reactor.callLater(RATE_SLEEP_S, self._send_tick_req)

    def _flush_chunk(self) -> None:
        cid, s_ms, e_ms = self.chunks[self.chunk_idx]
        rows, invalid_dropped = _merge_bid_ask_causal(self.cur_bids, self.cur_asks, s_ms, e_ms)
        ins = 0
        if rows:
            buf = io.StringIO()
            for t, sym, b, a, mid in rows:
                buf.write(f"{t}\t{sym}\t{b}\t{a}\t{mid}\n")
            buf.seek(0)
            with self.conn.cursor() as cur:
                cur.copy_from(buf, "_tmp_ext_ticks", columns=("time", "symbol", "bid", "ask", "last"))
                cur.execute(
                    f"""
                    INSERT INTO {TARGET_TABLE} (time, symbol, bid, ask, last, volume, flags)
                    SELECT time, symbol, bid, ask, last, NULL, NULL
                    FROM _tmp_ext_ticks
                    ON CONFLICT (symbol, time) DO NOTHING;
                    """
                )
                ins = max(0, cur.rowcount or 0)
            self.conn.commit()
            self.total_inserted += ins

        self.completed[cid] = {
            "bids": len(self.cur_bids),
            "asks": len(self.cur_asks),
            "invalid_dropped": invalid_dropped,
            "merged": len(rows),
            "inserted": ins,
        }
        if (self.chunk_idx + 1) % 6 == 0 or self.chunk_idx + 1 == len(self.chunks):
            self._save_checkpoint()
            el = round(time.time() - self.t0, 1)
            done_tot = len(self.completed)
            pct = round(100.0 * done_tot / max(1, self.total_chunks_initial), 1)
            print(
                f"[EXT] {done_tot}/{self.total_chunks_initial} ({pct}%) {cid} "
                f"merged={len(rows):,} tot_ins={self.total_inserted:,} elapsed={el}s",
                flush=True,
            )

        self.chunk_idx += 1
        self._start_next_chunk()

    def _on_message(self, _client, msg) -> None:
        self._reset_watchdog()
        payload = Protobuf.extract(msg)
        if msg.payloadType == ProtoOAApplicationAuthRes().payloadType:
            req = ProtoOAAccountAuthReq()
            req.ctidTraderAccountId = self.account_id
            req.accessToken = self.access_token
            self.client.send(req)
        elif msg.payloadType == ProtoOAAccountAuthRes().payloadType:
            self._start_next_chunk()
        elif msg.payloadType == ProtoOAGetTickDataRes().payloadType:
            _cid, s_ms, e_ms = self.chunks[self.chunk_idx]
            decoded = _decode_batch(payload.tickData)
            if self.cur_quote_type == ProtoOAQuoteType.BID:
                self.cur_bids.extend(decoded)
            else:
                self.cur_asks.extend(decoded)

            oldest_ms = decoded[0][0] if decoded else s_ms
            fetch_floor_ms = max(0, s_ms - 60_000)
            if payload.hasMore and decoded and oldest_ms > fetch_floor_ms:
                next_to = oldest_ms - 1 if oldest_ms - 1 < self.cur_to_ms else self.cur_to_ms - 1
                if next_to > fetch_floor_ms:
                    self.cur_to_ms = next_to
                    reactor.callLater(RATE_SLEEP_S, self._send_tick_req)
                    return

            if self.cur_quote_type == ProtoOAQuoteType.BID:
                self.cur_quote_type = ProtoOAQuoteType.ASK
                self.cur_to_ms = e_ms
                reactor.callLater(RATE_SLEEP_S, self._send_tick_req)
            else:
                self._flush_chunk()
        elif msg.payloadType in (ProtoOAErrorRes().payloadType, ProtoErrorRes().payloadType):
            print(f"[ERROR] cTrader: code={getattr(payload, 'errorCode', None)} desc={getattr(payload, 'description', None)}", flush=True)
            self._finish()


def main() -> None:
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("DB_URL") or "postgresql://postgres:password@timescaledb:5432/goldedge"
    db_url = db_url.replace("+psycopg2", "")
    # 12 full calendar months (2025-05-01 .. 2026-04-30) + 10-day warmup (2025-04-21 .. 2025-04-30)
    s = datetime(2025, 4, 21, 0, 0, tzinfo=timezone.utc)
    e = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    chunks = _build_chunks(s, e, chunk_hours=12)
    ExtendedCTraderBackfiller(chunks, db_url).start()


if __name__ == "__main__":
    main()
