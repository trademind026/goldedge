import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import List, Dict, Any

from ....domain.events.market import TickReceived, BarClosed
from ....domain.models.bar import Tick
from ....domain.ports.outbound.event_bus import IEventBus
from ....adapters.secondary.db.tick_repository import TickRepository
from ....adapters.secondary.db.candle_repository import CandleRepository

logger = logging.getLogger(__name__)

# Fallback spool location
SPOOL_DIR = Path("logs/spool")
SPOOL_FILE = SPOOL_DIR / "ticks_fallback.jsonl"


class MarketDataLoggerService:
    """
    Subscribes to market events and persists them to TimescaleDB.
    Uses a thread-safe bounded queue and background worker to ensure
    database I/O never blocks the main trading thread.
    """

    def __init__(
        self,
        event_bus: IEventBus,
        tick_repo: TickRepository,
        candle_repo: CandleRepository,
        batch_size: int = 500,
        flush_interval_sec: float = 5.0
    ):
        self.event_bus = event_bus
        self.tick_repo = tick_repo
        self.candle_repo = candle_repo
        
        self.batch_size = batch_size
        self.flush_interval_sec = flush_interval_sec
        
        # Bounded queue prevents memory leaks if DB goes down permanently
        self.tick_queue: queue.Queue[Tick] = queue.Queue(maxsize=100_000)
        self._running = False
        self._worker_thread: threading.Thread | None = None
        
        # Observability Metrics
        self.metrics = {
            "ticks_received": 0,
            "ticks_written": 0,
            "ticks_dropped": 0,
            "batch_insert_failures": 0,
        }

    def start(self):
        """Starts the background worker and registers event listeners."""
        if self._running:
            return
            
        SPOOL_DIR.mkdir(parents=True, exist_ok=True)
            
        self._running = True
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="MarketDataLogger")
        self._worker_thread.start()

        self.event_bus.consume("TickReceived", self.handle_tick)
        self.event_bus.consume("BarClosed", self.handle_bar)
        
        logger.info("MarketDataLoggerService started.")

    def stop(self):
        """Gracefully stops the worker, flushing remaining ticks."""
        if not self._running:
            return
            
        logger.info("Stopping MarketDataLoggerService... flushing queue.")
        self._running = False
        
        if self._worker_thread:
            self._worker_thread.join(timeout=10.0)
            
        logger.info(f"MarketDataLoggerService stopped. Metrics: {self.metrics}")

    # ──────────────────────────────────────────────────────────────
    # EVENT HANDLERS (Run on Trading Thread)
    # ──────────────────────────────────────────────────────────────

    def handle_bar(self, event: BarClosed):
        """
        M1 Bars arrive only once per minute. Safe to write synchronously 
        in a background context, but typically even this shouldn't block 
        the exact microsecond of trading. We'll write it directly for simplicity
        as 1 row/minute is practically zero load.
        """
        try:
            self.candle_repo.save_bar(event.bar)
        except Exception as e:
            logger.error(f"Failed to save M1 bar: {e}")

    def handle_tick(self, event: TickReceived):
        """
        Runs on trading thread. NEVER blocks.
        """
        self.metrics["ticks_received"] += 1
        try:
            self.tick_queue.put_nowait(event.tick)
        except queue.Full:
            self.metrics["ticks_dropped"] += 1
            logger.warning("Tick queue is FULL. Dropping live tick to protect trading thread.")

    # ──────────────────────────────────────────────────────────────
    # BACKGROUND WORKER (Runs on dedicated thread)
    # ──────────────────────────────────────────────────────────────

    def _worker_loop(self):
        """Continuously pulls from the queue and batches writes."""
        batch: List[Tick] = []
        last_flush_time = time.time()

        while self._running or not self.tick_queue.empty():
            try:
                # Wait up to 1 second for a new tick
                tick = self.tick_queue.get(timeout=1.0)
                batch.append(tick)
            except queue.Empty:
                pass  # Just loop around to check _running and flush_interval

            now = time.time()
            time_since_flush = now - last_flush_time
            
            # Flush if batch is full, OR if time interval elapsed (and we have data),
            # OR if we are shutting down.
            should_flush = (
                len(batch) >= self.batch_size or 
                (len(batch) > 0 and time_since_flush >= self.flush_interval_sec) or
                (not self._running and len(batch) > 0)
            )

            if should_flush:
                self._flush_batch(batch)
                batch.clear()
                last_flush_time = time.time()

    def _flush_batch(self, batch: List[Tick]):
        """Attempts to write a batch with exponential backoff and spool fallback."""
        max_retries = 3
        base_delay = 1.0

        for attempt in range(1, max_retries + 1):
            try:
                self.tick_repo.save_ticks(batch)
                self.metrics["ticks_written"] += len(batch)
                return  # Success
                
            except Exception as e:
                self.metrics["batch_insert_failures"] += 1
                logger.error(f"Tick bulk insert failed (attempt {attempt}/{max_retries}): {e}")
                
                if attempt < max_retries:
                    time.sleep(base_delay * (2 ** (attempt - 1)))
                else:
                    logger.critical(f"Persistent DB failure. Spooling {len(batch)} ticks to disk.")
                    self._spool_to_disk(batch)

    def _spool_to_disk(self, batch: List[Tick]):
        """Failsafe mechanism to write lost ticks to local disk."""
        try:
            with open(SPOOL_FILE, "a") as f:
                for tick in batch:
                    record = {
                        "time": tick.time.isoformat(),
                        "symbol": tick.symbol,
                        "bid": tick.bid,
                        "ask": tick.ask,
                        "last": tick.last,
                        "volume": tick.volume,
                        "flags": tick.flags
                    }
                    f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.critical(f"FATAL: Could not even spool ticks to disk! Data lost: {e}")
