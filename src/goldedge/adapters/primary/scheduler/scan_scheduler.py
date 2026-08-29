import time
import json
import logging
import threading
from datetime import datetime
from typing import Dict

from ....config.settings import settings
from ....application.use_cases.scan_market import ScanMarketUseCase
from ....domain.ports.outbound.event_bus import IEventBus
from ....domain.services.denoise_service import VolumeAdaptiveKalmanFilter
from ....domain.events.base import DomainEvent

logger = logging.getLogger(__name__)

class ScanScheduler:
    """
    Event-Driven Trading Engine Orchestrator.
    Consumes live ticks from Redis, maintains the Kalman Filter state,
    and triggers the ScanMarketUseCase on minute rollovers.
    """
    def __init__(self, scan_market_uc: ScanMarketUseCase, event_bus: IEventBus):
        self.scan_market_uc = scan_market_uc
        self.event_bus = event_bus
        self._running = False
        self._thread = None
        
        # Maintain a Kalman filter per active symbol
        self.kalman_filters: Dict[str, VolumeAdaptiveKalmanFilter] = {
            symbol: VolumeAdaptiveKalmanFilter() for symbol in settings.ACTIVE_SYMBOLS
        }
        self.last_minute: Dict[str, int] = {symbol: -1 for symbol in settings.ACTIVE_SYMBOLS}

    def start(self):
        if self._running:
            return
            
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ScanScheduler")
        self._thread.start()
        logger.info(f"Event-Driven ScanScheduler started for symbols: {settings.ACTIVE_SYMBOLS}")

    def stop(self):
        if not self._running:
            return
            
        self._running = False
        if self._thread:
            self._thread.join()
        logger.info("ScanScheduler stopped.")

    def _handle_tick(self, event: DomainEvent):
        """Callback for when a tick is pulled from Redis."""
        try:
            # Parse the tick payload
            payload = event.payload
            if isinstance(payload, str):
                payload = json.loads(payload)
                
            tick_data = payload.get("tick", {})
            symbol = tick_data.get("symbol")
            
            if symbol not in settings.ACTIVE_SYMBOLS:
                return
                
            price = tick_data.get("last") or tick_data.get("bid")
            volume = tick_data.get("volume") or 1.0
            
            if not price:
                return
                
            # 1. Update the Kalman Filter (Micro-Feature)
            kf = self.kalman_filters[symbol]
            kf.update_tick(price, volume)
            
            # 2. Check for Minute Rollover (Macro-Feature Trigger)
            # Parse timestamp to detect minute change
            tick_time_str = tick_data.get("time")
            if not tick_time_str:
                return
                
            # Handle ISO format
            if tick_time_str.endswith('Z'):
                tick_time_str = tick_time_str[:-1] + '+00:00'
            dt = datetime.fromisoformat(tick_time_str)
            
            current_minute = dt.minute
            last_min = self.last_minute[symbol]
            
            if last_min != -1 and current_minute != last_min:
                # MINUTE ROLLOVER DETECTED! Trigger the Trading Logic
                logger.info(f"Minute Rollover detected for {symbol} ({last_min} -> {current_minute}). Triggering ScanMarketUseCase.")
                
                kalman_features = {
                    "kalman_vel": kf.get_velocity(),
                    "kalman_accel": kf.get_acceleration()
                }
                
                # Execute the full trading logic (Indicators, Regimes, Scorer)
                self.scan_market_uc.execute(symbol, kalman_features=kalman_features)
                
            self.last_minute[symbol] = current_minute
            
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
