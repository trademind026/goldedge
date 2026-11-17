import time
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ....config.settings import settings
from ....application.use_cases.scan_market import ScanMarketUseCase
from ....domain.models.bar import Bar, Tick
from ....domain.ports.outbound.candle_repo import ICandleRepository
from ....domain.ports.outbound.event_bus import IEventBus
from ....domain.ports.outbound.tick_repo import ITickRepository
from ....domain.services.denoise_service import VolumeAdaptiveKalmanFilter
from ....domain.events.base import DomainEvent

logger = logging.getLogger(__name__)


def _parse_utc(raw: str) -> datetime:
    s = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class ScanScheduler:
    """
    Event-Driven Trading Engine Orchestrator.
    Warms up Kalman state from historical ticks on startup, consumes live ticks
    from Redis, builds/persists 1m bars, tracks virtual shadow exits on live ticks,
    and triggers ScanMarketUseCase on completed 5m rollovers.
    """
    def __init__(
        self,
        scan_market_uc: ScanMarketUseCase,
        event_bus: IEventBus,
        tick_repo: Optional[ITickRepository] = None,
        candle_repo: Optional[ICandleRepository] = None,
    ):
        self.scan_market_uc = scan_market_uc
        self.event_bus = event_bus
        self.tick_repo = tick_repo
        self.candle_repo = candle_repo
        self._running = False
        self._thread = None

        self.kalman_filters: Dict[str, VolumeAdaptiveKalmanFilter] = {
            symbol: VolumeAdaptiveKalmanFilter() for symbol in settings.ACTIVE_SYMBOLS
        }
        self._last_1m_bucket: Dict[str, int] = {symbol: -1 for symbol in settings.ACTIVE_SYMBOLS}
        self._last_tick_ts: Dict[str, Optional[datetime]] = {symbol: None for symbol in settings.ACTIVE_SYMBOLS}
        self._current_1m: Dict[str, Optional[Dict[str, Any]]] = {symbol: None for symbol in settings.ACTIVE_SYMBOLS}
        self._tick_buffer: Dict[str, List[Tick]] = {symbol: [] for symbol in settings.ACTIVE_SYMBOLS}
        self._lock = threading.Lock()

    def warmup_symbol(self, symbol: str) -> Dict[str, Any]:
        """
        Deterministically initialize Kalman Filter & recent 1m bars from DB ticks
        plus any newer buffered Redis stream ticks so restarts recover identical state.
        """
        t0 = time.perf_counter()
        kf = VolumeAdaptiveKalmanFilter()
        warmed_ticks = 0
        first_ts: Optional[datetime] = None
        last_ts: Optional[datetime] = None
        cur_1m: Optional[Dict[str, Any]] = None
        closed_bars_to_save: List[Bar] = []

        shadow_mode = getattr(self.scan_market_uc, "shadow_mode", False)
        tracker = getattr(self.scan_market_uc, "shadow_tracker", None) if shadow_mode else None
        journal = getattr(self.scan_market_uc, "shadow_journal", None) if shadow_mode else None
        if tracker is not None and journal is not None and not getattr(tracker, "restored", True):
            restored_n = tracker.restore_from_events(journal.read_all())
            logger.info(f"[SHADOW_RESTORE] Reconstructed {restored_n} open virtual position(s) from journal.")

        def _ingest_warmup_tick(dt: datetime, bid: float, ask: float, vol: float):
            nonlocal warmed_ticks, first_ts, last_ts, cur_1m
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            if last_ts is not None and dt <= last_ts:
                return
            kf.update_tick(bid, vol)
            warmed_ticks += 1
            if first_ts is None:
                first_ts = dt
            last_ts = dt

            if tracker is not None and (tracker.open_positions or tracker.open_portfolio_positions):
                for dir_key in ("BUY", "SELL"):
                    slot = (symbol, dir_key)
                    pos = tracker.open_positions.get(slot)
                    if pos is not None and dt > pos.opened_at:
                        exit_ev = tracker._step_position(slot, pos, bid, ask, dt)
                        if exit_ev is not None and journal is not None:
                            journal.record_event(exit_ev)
                            logger.info(
                                f"[SHADOW_EXIT_WARMUP] {symbol} dir={exit_ev['direction']} reason={exit_ev['exit_reason']} "
                                f"pnl=${exit_ev['net_pnl']:+.2f}"
                            )
                for p_name in tracker.PORTFOLIO_NAMES:
                    p_slot = (symbol, p_name)
                    p_pos = tracker.open_portfolio_positions.get(p_slot)
                    if p_pos is not None and dt > p_pos.opened_at:
                        p_ev = tracker._step_portfolio_position(p_slot, p_pos, bid, ask, dt)
                        if p_ev is not None and journal is not None:
                            journal.record_event(p_ev)

            bucket_1m = int(dt.timestamp()) // 60
            spread_pts = max(0, int(round((ask - bid) / 0.01)))
            if cur_1m is None or cur_1m["bucket"] != bucket_1m:
                if cur_1m is not None:
                    closed_bars_to_save.append(
                        Bar(
                            symbol=symbol,
                            time=datetime.fromtimestamp(cur_1m["bucket"] * 60, tz=timezone.utc),
                            open=cur_1m["open"],
                            high=cur_1m["high"],
                            low=cur_1m["low"],
                            close=cur_1m["close"],
                            tick_volume=cur_1m["tick_volume"],
                            real_volume=0,
                            spread=cur_1m["spread"],
                        )
                    )
                cur_1m = {
                    "bucket": bucket_1m,
                    "open": bid,
                    "high": bid,
                    "low": bid,
                    "close": bid,
                    "tick_volume": 1,
                    "spread": spread_pts,
                }
            else:
                if bid > cur_1m["high"]:
                    cur_1m["high"] = bid
                if bid < cur_1m["low"]:
                    cur_1m["low"] = bid
                cur_1m["close"] = bid
                cur_1m["tick_volume"] += 1
                cur_1m["spread"] = spread_pts

        # 1. Seed missing 1m bars & replay DB ticks over lookback window
        latest_db_ts: Optional[datetime] = None
        if self.tick_repo is not None:
            try:
                latest_db_ts = self.tick_repo.get_latest_tick_time(symbol)
                if latest_db_ts is not None:
                    if latest_db_ts.tzinfo is None:
                        latest_db_ts = latest_db_ts.replace(tzinfo=timezone.utc)
                    else:
                        latest_db_ts = latest_db_ts.astimezone(timezone.utc)

                    if self.candle_repo is not None:
                        seed_start = latest_db_ts - timedelta(days=7)
                        seed_end = latest_db_ts.replace(second=0, microsecond=0)
                        try:
                            inserted = self.candle_repo.upsert_1m_bars_from_ticks(symbol, seed_start, seed_end)
                            if inserted:
                                logger.info(f"[WARMUP] Seeded {inserted} missing 1m bars for {symbol} from market_ticks.")
                        except Exception as exc:
                            logger.warning(f"[WARMUP] Could not seed 1m bars for {symbol}: {exc}")

                    warmup_start = latest_db_ts - timedelta(hours=settings.KALMAN_WARMUP_HOURS)
                    for t in self.tick_repo.stream_ticks(symbol, warmup_start, latest_db_ts):
                        bid = float(t.bid or t.last or 0.0)
                        ask = float(t.ask or bid)
                        if bid <= 0:
                            continue
                        vol = float(t.volume if t.volume is not None else 1.0)
                        _ingest_warmup_tick(t.time, bid, ask, vol)
            except Exception as exc:
                logger.warning(f"[WARMUP] DB tick warmup failed for {symbol}: {exc}")

        # 2. Catch up from any newer ticks currently sitting in Redis stream
        redis_client = getattr(self.event_bus, "client", None)
        redis_ticks_to_save: List[Tick] = []
        if redis_client is not None:
            try:
                min_id = f"{max(0, int(last_ts.timestamp() * 1000) - 5000)}-0" if last_ts is not None else "-"
                entries = redis_client.xrange("goldedge:events:TickReceived", min=min_id, max="+")
                for _msg_id, fields in entries:
                    raw_payload = fields.get("payload")
                    if not raw_payload:
                        continue
                    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
                    td = payload.get("tick", {})
                    if td.get("symbol") != symbol:
                        continue
                    t_str = td.get("time")
                    bid = float(td.get("bid") or td.get("last") or 0.0)
                    ask = float(td.get("ask") or bid)
                    if not t_str or bid <= 0:
                        continue
                    dt = _parse_utc(t_str)
                    if last_ts is not None and dt <= last_ts:
                        continue
                    vol = float(td.get("volume") or 1.0)
                    _ingest_warmup_tick(dt, bid, ask, vol)
                    redis_ticks_to_save.append(
                        Tick(
                            symbol=symbol,
                            time=dt,
                            bid=bid,
                            ask=ask,
                            last=round((bid + ask) / 2.0, 5),
                            volume=int(vol) if td.get("volume") is not None else None,
                        )
                    )
            except Exception as exc:
                logger.warning(f"[WARMUP] Redis tail catch-up failed for {symbol}: {exc}")

        if redis_ticks_to_save and self.tick_repo is not None:
            try:
                self.tick_repo.save_ticks(redis_ticks_to_save)
            except Exception as exc:
                logger.warning(f"[WARMUP] Saving Redis tail ticks failed: {exc}")

        if closed_bars_to_save and self.candle_repo is not None:
            try:
                # Only save bars missed after latest_db_ts
                tail_bars = (
                    [b for b in closed_bars_to_save if latest_db_ts is None or b.time >= latest_db_ts.replace(second=0, microsecond=0)]
                )
                if tail_bars:
                    self.candle_repo.save_bars(tail_bars)
            except Exception as exc:
                logger.warning(f"[WARMUP] Saving warmed 1m bars failed: {exc}")

        with self._lock:
            self.kalman_filters[symbol] = kf
            self._last_tick_ts[symbol] = last_ts
            self._current_1m[symbol] = cur_1m
            self._last_1m_bucket[symbol] = cur_1m["bucket"] if cur_1m is not None else -1

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        summary = {
            "symbol": symbol,
            "warmed_ticks": warmed_ticks,
            "first_ts": first_ts.isoformat() if first_ts else None,
            "last_ts": last_ts.isoformat() if last_ts else None,
            "kalman_price": round(float(kf._x0), 5),
            "kalman_vel": round(float(kf.get_velocity()), 6),
            "kalman_accel": round(float(kf.get_acceleration()), 6),
            "elapsed_ms": round(elapsed_ms, 1),
        }
        logger.info(
            f"[KALMAN_WARMUP] {symbol} ticks={warmed_ticks:,} range=[{summary['first_ts']} .. {summary['last_ts']}] "
            f"k_p={summary['kalman_price']} k_v={summary['kalman_vel']} k_a={summary['kalman_accel']} "
            f"in {summary['elapsed_ms']}ms"
        )
        return summary

    def _flush_ticks(self, symbol: str) -> None:
        buf = self._tick_buffer.get(symbol)
        if not buf or self.tick_repo is None:
            return
        batch = list(buf)
        buf.clear()
        try:
            self.tick_repo.save_ticks(batch)
        except Exception as exc:
            logger.error(f"Failed persisting {len(batch)} live ticks for {symbol}: {exc}")

    def start(self):
        if self._running:
            return

        for symbol in settings.ACTIVE_SYMBOLS:
            self.warmup_symbol(symbol)

        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ScanScheduler")
        self._thread.start()
        logger.info(f"Event-Driven ScanScheduler started for symbols: {settings.ACTIVE_SYMBOLS}")

    def stop(self):
        if not self._running:
            return

        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        for symbol in settings.ACTIVE_SYMBOLS:
            self._flush_ticks(symbol)
        logger.info("ScanScheduler stopped.")

    def _handle_tick(self, event: DomainEvent):
        """Callback for when a tick is pulled from Redis."""
        try:
            payload = event.payload
            if isinstance(payload, str):
                payload = json.loads(payload)

            tick_data = payload.get("tick", {})
            symbol = tick_data.get("symbol")

            if symbol not in settings.ACTIVE_SYMBOLS:
                return

            bid_raw = tick_data.get("bid") or tick_data.get("last")
            if not bid_raw:
                return
            bid = float(bid_raw)
            ask = float(tick_data.get("ask") or bid)
            raw_vol = tick_data.get("volume")
            volume = float(raw_vol) if raw_vol is not None else 1.0

            tick_time_str = tick_data.get("time")
            if not tick_time_str:
                return
            dt = _parse_utc(tick_time_str)

            # Skip ticks already consumed during warmup (zero double-counting on restart)
            prev_ts = self._last_tick_ts.get(symbol)
            if prev_ts is not None and dt <= prev_ts:
                return

            # 1. Update Kalman Filter continuously on every tick
            kf = self.kalman_filters[symbol]
            kf.update_tick(bid, volume)
            self._last_tick_ts[symbol] = dt

            # 2. Buffer raw live tick for durable restart recovery
            self._tick_buffer[symbol].append(
                Tick(
                    symbol=symbol,
                    time=dt,
                    bid=bid,
                    ask=ask,
                    last=round((bid + ask) / 2.0, 5),
                    volume=int(raw_vol) if raw_vol is not None else None,
                )
            )
            if len(self._tick_buffer[symbol]) >= 25:
                self._flush_ticks(symbol)

            # 3. Check live virtual exits in shadow mode (BUY & SELL tracked independently)
            if getattr(self.scan_market_uc, "shadow_mode", False):
                tracker = getattr(self.scan_market_uc, "shadow_tracker", None)
                if tracker is not None:
                    exit_events = tracker.on_tick_all(symbol, bid, ask, dt) if hasattr(tracker, "on_tick_all") else (
                        [ev] if (ev := tracker.on_tick(symbol, bid, ask, dt)) is not None else []
                    )
                    for exit_ev in exit_events:
                        logger.info(
                            f"[SHADOW_EXIT] {symbol} dir={exit_ev['direction']} reason={exit_ev['reason']} "
                            f"entry={exit_ev['entry_price']} exit={exit_ev['exit_price']} pnl=${exit_ev['pnl']:+.2f} "
                            f"dir_equity=${exit_ev.get('dir_equity', exit_ev['equity']):.2f} "
                            f"in_15utc={exit_ev.get('in_15utc_window')} e1_15m={exit_ev.get('e1_15m_would_exit')}"
                        )
                        journal = getattr(self.scan_market_uc, "shadow_journal", None)
                        if journal is not None:
                            journal.record_event(exit_ev)
                    if hasattr(tracker, "drain_portfolio_exits"):
                        journal = getattr(self.scan_market_uc, "shadow_journal", None)
                        for p_ev in tracker.drain_portfolio_exits():
                            logger.info(
                                f"[SHADOW_PORTFOLIO_EXIT] {symbol} portfolio={p_ev['portfolio']} dir={p_ev['direction']} "
                                f"reason={p_ev['exit_reason']} pnl=${p_ev['net_pnl']:+.2f} "
                                f"eq=${p_ev['portfolio_equity']:.2f} mtm_dd={p_ev['mtm_max_drawdown_pct']:.2f}% risk={p_ev['risk_status']}"
                            )
                            if journal is not None:
                                journal.record_event(p_ev)

            # 4. Aggregate 1m candle & detect 1m / 5m bar rollovers
            bucket_1m = int(dt.timestamp()) // 60
            spread_pts = max(0, int(round((ask - bid) / 0.01)))
            cur_1m = self._current_1m.get(symbol)

            rollover_5m = False
            prev_1m_bucket = self._last_1m_bucket.get(symbol, -1)

            if cur_1m is None:
                self._current_1m[symbol] = {
                    "bucket": bucket_1m,
                    "open": bid,
                    "high": bid,
                    "low": bid,
                    "close": bid,
                    "tick_volume": 1,
                    "spread": spread_pts,
                }
            elif cur_1m["bucket"] != bucket_1m:
                closed_bar = Bar(
                    symbol=symbol,
                    time=datetime.fromtimestamp(cur_1m["bucket"] * 60, tz=timezone.utc),
                    open=cur_1m["open"],
                    high=cur_1m["high"],
                    low=cur_1m["low"],
                    close=cur_1m["close"],
                    tick_volume=cur_1m["tick_volume"],
                    real_volume=0,
                    spread=cur_1m["spread"],
                )
                self._flush_ticks(symbol)
                if self.candle_repo is not None:
                    try:
                        self.candle_repo.save_bar(closed_bar)
                    except Exception as exc:
                        logger.error(f"Failed saving live 1m bar for {symbol}: {exc}")

                if (bucket_1m // 5) != (cur_1m["bucket"] // 5):
                    rollover_5m = True

                self._current_1m[symbol] = {
                    "bucket": bucket_1m,
                    "open": bid,
                    "high": bid,
                    "low": bid,
                    "close": bid,
                    "tick_volume": 1,
                    "spread": spread_pts,
                }
            else:
                if bid > cur_1m["high"]:
                    cur_1m["high"] = bid
                if bid < cur_1m["low"]:
                    cur_1m["low"] = bid
                cur_1m["close"] = bid
                cur_1m["tick_volume"] += 1
                cur_1m["spread"] = spread_pts

            if not rollover_5m and prev_1m_bucket != -1 and (bucket_1m // 5) != (prev_1m_bucket // 5):
                rollover_5m = True

            self._last_1m_bucket[symbol] = bucket_1m

            if rollover_5m:
                logger.info(
                    f"5m Bar Rollover detected for {symbol} "
                    f"(bucket {prev_1m_bucket // 5} -> {bucket_1m // 5}, utc={dt.strftime('%Y-%m-%dT%H:%M:%SZ')}). "
                    f"Triggering ScanMarketUseCase."
                )
                kalman_features = {
                    "kalman_price": float(kf._x0),
                    "kalman_vel": kf.get_velocity(),
                    "kalman_accel": kf.get_acceleration(),
                }
                live_quote = {"bid": bid, "ask": ask, "time": dt}
                self.scan_market_uc.execute(
                    symbol,
                    kalman_features=kalman_features,
                    live_quote=live_quote,
                    as_of_time=dt,
                )

        except Exception as e:
            logger.error(f"Error processing tick in ScanScheduler: {e}")

    def _loop(self):
        while self._running:
            try:
                # This will block and pull new ticks from Redis
                self.event_bus.consume("TickReceived", self._handle_tick)
            except Exception as e:
                logger.error(f"Error in Redis consume loop: {e}")
                time.sleep(1) # Backoff on failure
