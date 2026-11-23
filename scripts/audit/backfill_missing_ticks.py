"""
Idempotent Historical Tick Backfill for Missing XAUUSD Gaps Only (`QUOTE_ARRIVAL_SEMANTICS_V1`).
Reads cTrader Open API (`ProtoOAGetTickDataReq`) in `goldedge-data-feed-dev`, decodes delta
timestamps/prices causally, validates (bid > 0 & ask > 0 & bid < ask), keeps real_volume = NULL,
and inserts ONLY missing intervals via `ON CONFLICT (symbol, time) DO NOTHING`.

Never touches existing rows inside 2026-08-20T00:00:00Z .. 2026-11-16T20:01:23Z.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
import sys
import time
from typing import List, Tuple

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
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
RATE_SLEEP_S = 0.24  # <= ~4 req/s (under cTrader 5 req/s cap)

MISSING_WINDOWS: List[Tuple[str, datetime, datetime]] = [
    ("aug13", datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 8, 14, 0, 0, 0, tzinfo=timezone.utc)),
    ("aug14", datetime(2026, 8, 14, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 8, 15, 0, 0, 0, tzinfo=timezone.utc)),
    ("aug15_16_sun", datetime(2026, 8, 15, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc)),
    ("aug17", datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 8, 18, 0, 0, 0, tzinfo=timezone.utc)),
    ("aug18", datetime(2026, 8, 18, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 8, 19, 0, 0, 0, tzinfo=timezone.utc)),
    ("aug19", datetime(2026, 8, 19, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 8, 20, 0, 0, 0, tzinfo=timezone.utc)),
    ("nov17_gap1", datetime(2026, 11, 17, 2, 4, 30, tzinfo=timezone.utc), datetime(2026, 11, 17, 4, 21, 10, tzinfo=timezone.utc)),
    ("nov17_gap2", datetime(2026, 11, 17, 7, 23, 40, tzinfo=timezone.utc), datetime(2026, 11, 17, 8, 50, 45, tzinfo=timezone.utc)),
    ("nov17_18_gap3", datetime(2026, 11, 17, 9, 18, 0, tzinfo=timezone.utc), datetime(2026, 11, 18, 12, 19, 5, tzinfo=timezone.utc)),
]


def _decode_tick_batch(tick_data) -> List[Tuple[int, float]]:
    """Decode delta-encoded cTrader ticks (returned newest -> oldest) into ascending (ts_ms, price)."""
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
) -> List[Tuple[datetime, str, float, float, float, None, None]]:
    """Causal outer union of BID & ASK quotes -> ffill BBO -> strict (bid > 0 & ask > 0 & bid < ask)."""
    if not bids or not asks:
        return []
    df_b = pd.DataFrame(bids, columns=["ts_ms", "bid"]).drop_duplicates(subset=["ts_ms"], keep="last").sort_values("ts_ms")
    df_a = pd.DataFrame(asks, columns=["ts_ms", "ask"]).drop_duplicates(subset=["ts_ms"], keep="last").sort_values("ts_ms")
    df = pd.merge(df_b, df_a, on="ts_ms", how="outer").sort_values("ts_ms")
    df["bid"] = df["bid"].ffill()
    df["ask"] = df["ask"].ffill()
    df = df.dropna(subset=["bid", "ask"])
    df = df[(df["ts_ms"] >= start_ms) & (df["ts_ms"] < end_ms)]
    df = df[(df["bid"] > 0.0) & (df["ask"] > 0.0) & (df["bid"] < df["ask"])]
    if df.empty:
        return []

    rows = []
    for ts_ms, b, a in zip(df["ts_ms"].values, df["bid"].values, df["ask"].values):
        dt = datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc)
        b_f = round(float(b), 5)
        a_f = round(float(a), 5)
        mid = round((b_f + a_f) / 2.0, 5)
        rows.append((dt, SYMBOL, b_f, a_f, mid, None, None))
    return rows


def _build_chunks(windows: List[Tuple[str, datetime, datetime]], chunk_hours: int = 2) -> List[Tuple[str, int, int]]:
    chunks: List[Tuple[str, int, int]] = []
    step = timedelta(hours=chunk_hours)
    for name, s, e in windows:
        cur = s
        while cur < e:
            nxt = min(cur + step, e)
            chunks.append((name, int(cur.timestamp() * 1000), int(nxt.timestamp() * 1000)))
            cur = nxt
    return chunks


class CTraderTickBackfiller:
    def __init__(self, chunks: List[Tuple[str, int, int]], db_url: str, dry_run: bool = False):
        self.client_id = os.environ["CTRADER_CLIENT_ID"]
        self.client_secret = os.environ["CTRADER_CLIENT_SECRET"]
        self.access_token = os.environ["CTRADER_ACCESS_TOKEN"]
        self.account_id = int(os.environ["CTRADER_ACCOUNT_ID"])
        self.mode = os.environ.get("TRADING_MODE", "demo").lower()
        host = EndPoints.PROTOBUF_LIVE_HOST if self.mode == "live" else EndPoints.PROTOBUF_DEMO_HOST
        self.client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)

        self.chunks = chunks
        self.dry_run = dry_run
        self.conn = None if dry_run else psycopg2.connect(db_url)

        self.chunk_idx = 0
        self.cur_quote_type = ProtoOAQuoteType.BID
        self.cur_to_ms = 0
        self.cur_bids: List[Tuple[int, float]] = []
        self.cur_asks: List[Tuple[int, float]] = []
        self.total_inserted = 0
        self.total_merged = 0
        self.req_count = 0

    def start(self):
        self.client.setConnectedCallback(self._on_connected)
        self.client.setDisconnectedCallback(self._on_disconnected)
        self.client.setMessageReceivedCallback(self._on_message)
        self.client.startService()
        reactor.run()

    def _finish(self):
        print(f"[BACKFILL] Finished all {len(self.chunks)} chunks! Requests={self.req_count}, Merged={self.total_merged:,}, Inserted={self.total_inserted:,}", flush=True)
        if self.conn is not None:
            self.conn.close()
        self.client.stopService()
        if reactor.running:
            reactor.stop()

    def _on_connected(self, client):
        req = ProtoOAApplicationAuthReq()
        req.clientId = self.client_id
        req.clientSecret = self.client_secret
        client.send(req)

    def _on_disconnected(self, _client, reason):
        print(f"[BACKFILL] Disconnected: {reason}", flush=True)
        if reactor.running:
            reactor.stop()

    def _send_tick_req(self):
        if self.chunk_idx >= len(self.chunks):
            self._finish()
            return
        _lbl, s_ms, _e_ms = self.chunks[self.chunk_idx]
        req = ProtoOAGetTickDataReq()
        req.ctidTraderAccountId = self.account_id
        req.symbolId = SYMBOL_ID
        req.type = self.cur_quote_type
        req.fromTimestamp = max(0, s_ms - 60_000)  # 60s seed buffer for chunk-boundary BBO ffill
        req.toTimestamp = self.cur_to_ms
        self.req_count += 1
        self.client.send(req)

    def _start_next_chunk(self):
        if self.chunk_idx >= len(self.chunks):
            self._finish()
            return
        _lbl, _s_ms, e_ms = self.chunks[self.chunk_idx]
        self.cur_quote_type = ProtoOAQuoteType.BID
        self.cur_to_ms = e_ms
        self.cur_bids = []
        self.cur_asks = []
        reactor.callLater(RATE_SLEEP_S, self._send_tick_req)

    def _flush_chunk_to_db(self):
        lbl, s_ms, e_ms = self.chunks[self.chunk_idx]
        rows = _merge_bid_ask_causal(self.cur_bids, self.cur_asks, s_ms, e_ms)
        self.total_merged += len(rows)
        if self.dry_run:
            s_dt = datetime.fromtimestamp(s_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            print(f"[DRY-RUN] {lbl} ({s_dt}Z): bids={len(self.cur_bids)} asks={len(self.cur_asks)} merged_valid={len(rows)}", flush=True)
            if rows:
                print("  first 3:", rows[:3], flush=True)
                print("  last  2:", rows[-2:], flush=True)
        elif rows:
            with self.conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO market_ticks (time, symbol, bid, ask, last, volume, flags)
                    VALUES %s
                    ON CONFLICT (symbol, time) DO NOTHING
                    """,
                    rows,
                    page_size=5000,
                )
                self.total_inserted += max(0, cur.rowcount or 0)
            self.conn.commit()
            s_dt = datetime.fromtimestamp(s_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            print(f"[SAVED] chunk {self.chunk_idx + 1}/{len(self.chunks)} {lbl} ({s_dt}Z): valid={len(rows):,} cumulative_inserted={self.total_inserted:,}", flush=True)
        else:
            s_dt = datetime.fromtimestamp(s_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            print(f"[EMPTY] chunk {self.chunk_idx + 1}/{len(self.chunks)} {lbl} ({s_dt}Z): 0 valid ticks", flush=True)

        self.chunk_idx += 1
        self._start_next_chunk()

    def _on_message(self, _client, msg):
        payload = Protobuf.extract(msg)
        if msg.payloadType == ProtoOAApplicationAuthRes().payloadType:
            req = ProtoOAAccountAuthReq()
            req.ctidTraderAccountId = self.account_id
            req.accessToken = self.access_token
            self.client.send(req)
        elif msg.payloadType == ProtoOAAccountAuthRes().payloadType:
            print(f"[BACKFILL] Authenticated account {self.account_id} ({self.mode}). Starting {len(self.chunks)} chunks...", flush=True)
            self._start_next_chunk()
        elif msg.payloadType == ProtoOAGetTickDataRes().payloadType:
            _lbl, s_ms, e_ms = self.chunks[self.chunk_idx]
            decoded = _decode_tick_batch(payload.tickData)
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

            # Switch from BID -> ASK, or finalize chunk
            if self.cur_quote_type == ProtoOAQuoteType.BID:
                self.cur_quote_type = ProtoOAQuoteType.ASK
                self.cur_to_ms = e_ms
                reactor.callLater(RATE_SLEEP_S, self._send_tick_req)
            else:
                self._flush_chunk_to_db()
        elif msg.payloadType in (ProtoOAErrorRes().payloadType, ProtoErrorRes().payloadType):
            print(f"[ERROR] cTrader error: code={getattr(payload, 'errorCode', None)} desc={getattr(payload, 'description', None)}", flush=True)
            if self.conn is not None:
                self.conn.close()
            self.client.stopService()
            if reactor.running:
                reactor.stop()


def main():
    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    if "--dry-test" in sys.argv:
        chunks = [("verify_aug20_1m", int(datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc).timestamp() * 1000), int(datetime(2026, 8, 20, 0, 1, tzinfo=timezone.utc).timestamp() * 1000))]
        CTraderTickBackfiller(chunks, db_url, dry_run=True).start()
    elif "--idempotency-check" in sys.argv:
        chunks = _build_chunks([MISSING_WINDOWS[6]], chunk_hours=2)  # nov17_gap1 (~1.6h)
        CTraderTickBackfiller(chunks, db_url, dry_run=False).start()
    else:
        chunks = _build_chunks(MISSING_WINDOWS, chunk_hours=2)
        CTraderTickBackfiller(chunks, db_url, dry_run=False).start()


if __name__ == "__main__":
    main()
