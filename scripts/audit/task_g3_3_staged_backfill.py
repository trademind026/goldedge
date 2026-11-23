"""
TASK G3.3 — Phase A2/A3: Resumable Paginated cTrader Staged Historical Backfill.
Retrieves Fusion Markets cTrader Demo XAUUSD bid/ask ticks (symbolId=41) strictly BEFORE
2026-08-13T00:00:00Z into isolated staging table `market_ticks_staged_expansion`
(NEVER modifying raw `market_ticks` or frozen Aug-Nov H0/H3b windows!).

Respects cTrader Open API limits:
  - Read-only ProtoOAGetTickDataReq
  - 6-hour chunks (well under 1-week API cap)
  - Full `hasMore` backward pagination
  - Rate-limited <= ~4.5 req/s (0.22s sleep)
  - Causal outer union of BID & ASK quotes -> ffill BBO -> strict (bid>0 & ask>0 & bid<ask)
  - Excludes weekend off-session hours (Sat UTC, Sun < 21:00 UTC)
  - Checkpoint JSON for lossless resume on disconnect/restart
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

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
RATE_SLEEP_S = 0.22
OUT_DIR = Path("backtest_results/audit/task_g3_3_leakage_proof_pipeline")
CHECKPOINT_FILE = OUT_DIR / "staged_backfill_checkpoint.json"


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
) -> List[Tuple[datetime, str, float, float, float, None, None]]:
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
        # Never stage off-session weekend ticks (Sat UTC or Sun < 21:00 UTC)
        wd = dt.weekday()
        if wd == 5 or (wd == 6 and dt.hour < 21):
            continue
        b_f = round(float(b), 5)
        a_f = round(float(a), 5)
        rows.append((dt, SYMBOL, b_f, a_f, round((b_f + a_f) / 2.0, 5), None, None))
    return rows


def _build_chunks(start_dt: datetime, end_dt: datetime, chunk_hours: int = 6) -> List[Tuple[str, int, int]]:
    chunks: List[Tuple[str, int, int]] = []
    cur = start_dt
    step = timedelta(hours=chunk_hours)
    while cur < end_dt:
        nxt = min(cur + step, end_dt)
        # Skip pure Saturday chunks (market closed all day Saturday UTC)
        if not (cur.weekday() == 5 and nxt.weekday() == 5):
            if not (cur.weekday() == 6 and nxt.hour <= 20 and nxt.weekday() == 6):
                cid = f"{cur.strftime('%Y%m%d_%H')}_{nxt.strftime('%Y%m%d_%H')}"
                chunks.append((cid, int(cur.timestamp() * 1000), int(nxt.timestamp() * 1000)))
        cur = nxt
    return chunks


class StagedCTraderBackfiller:
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

    def _save_checkpoint(self) -> None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        CHECKPOINT_FILE.write_text(
            json.dumps(
                {
                    "updated_at_utc": datetime.now(timezone.utc).isoformat(),
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
            print(f"[STAGED BACKFILL] All {self.total_chunks_initial} chunks already completed!", flush=True)
            self.conn.close()
            return
        print(f"[STAGED BACKFILL] Remaining chunks: {len(self.chunks)} / {self.total_chunks_initial}", flush=True)
        self.client.setConnectedCallback(self._on_connected)
        self.client.setDisconnectedCallback(self._on_disconnected)
        self.client.setMessageReceivedCallback(self._on_message)
        self.client.startService()
        reactor.run()

    def _finish(self) -> None:
        self._save_checkpoint()
        el = round(time.time() - self.t0, 1)
        print(f"[STAGED BACKFILL DONE in {el}s] reqs={self.req_count:,} inserted={self.total_inserted:,} completed={len(self.completed)}/{self.total_chunks_initial}", flush=True)
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
        req = ProtoOAApplicationAuthReq()
        req.clientId = self.client_id
        req.clientSecret = self.client_secret
        client.send(req)

    def _on_disconnected(self, _client, reason) -> None:
        print(f"[STAGED BACKFILL] Disconnected: {reason}", flush=True)
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
        rows = _merge_bid_ask_causal(self.cur_bids, self.cur_asks, s_ms, e_ms)
        ins = 0
        if rows:
            with self.conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO market_ticks_staged_expansion (time, symbol, bid, ask, last, volume, flags)
                    VALUES %s
                    ON CONFLICT (symbol, time) DO NOTHING
                    """,
                    rows,
                    page_size=5000,
                )
                ins = max(0, cur.rowcount or 0)
            self.conn.commit()
            self.total_inserted += ins

        self.completed[cid] = {"bids": len(self.cur_bids), "asks": len(self.cur_asks), "merged": len(rows), "inserted": ins}
        if (self.chunk_idx + 1) % 8 == 0 or self.chunk_idx + 1 == len(self.chunks):
            self._save_checkpoint()
            el = round(time.time() - self.t0, 1)
            print(f"[STAGED] {self.chunk_idx + 1}/{len(self.chunks)} ({cid}) merged={len(rows):,} tot_ins={self.total_inserted:,} elapsed={el}s", flush=True)

        self.chunk_idx += 1
        self._start_next_chunk()

    def _on_message(self, _client, msg) -> None:
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
    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    if "--pilot" in sys.argv:
        # 3-day controlled pilot: 2026-08-10 .. 2026-08-13
        s = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
        e = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)
    elif "--months-3" in sys.argv:
        # 3 additional months: 2026-05-13 .. 2026-08-13 (-> 6.2 months total!)
        s = datetime(2026, 5, 13, 0, 0, tzinfo=timezone.utc)
        e = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)
    else:
        # Default: 3 additional months (6.2 months total coverage)
        s = datetime(2026, 5, 13, 0, 0, tzinfo=timezone.utc)
        e = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)

    chunks = _build_chunks(s, e, chunk_hours=6)
    StagedCTraderBackfiller(chunks, db_url).start()


if __name__ == "__main__":
    main()
