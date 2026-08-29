import time
import math
from datetime import datetime, timezone
from dataclasses import replace
from ...domain.models.trade import TradeStatus, ExitReason, Position
from ...domain.ports.outbound.broker import IBrokerAdapter
from ...domain.ports.outbound.trade_repo import ITradeRepository
from ...domain.ports.outbound.event_bus import IEventBus
from ...domain.events.trading import TradeClosed, SLMoved, BreakevenSet, JourneySnapshot
from ...domain.ports.outbound.market_data import IMarketDataProvider


class ManagePositionUseCase:
    """
    Tick-driven position management for GoldEdge XAU/USD.

    Design principles:
      - Hard SL/TP sits at the MT5 broker (survives disconnects).
      - Live Bid/Ask ticks are used for dynamic management only.
      - BUY  positions monitor executable Bid.
      - SELL positions monitor executable Ask.
      - SL can only tighten — never widen (monotonic enforcement).
      - MT5 modifications are throttled (distance + time + digit normalization).
      - Breakeven is applied once per trade (configurable).
      - Trades are closed after max_hold_bars to prevent rollover swap charges.
    """

    def __init__(
        self,
        broker: IBrokerAdapter,
        trade_repo: ITradeRepository,
        event_bus: IEventBus,
        market_data: IMarketDataProvider,
        position_management_config: dict = None
    ):
        self.broker = broker
        self.trade_repo = trade_repo
        self.event_bus = event_bus
        self.market_data = market_data

        cfg = position_management_config or {}

        # Trailing SL throttle config
        self.modify_min_points           = cfg.get("modify_min_points", 10)
        self.modify_min_atr_fraction     = cfg.get("modify_min_atr_fraction", 0.05)
        self.modify_min_interval_seconds = cfg.get("modify_min_interval_seconds", 2.0)

        # Breakeven config
        be_cfg = cfg.get("breakeven", {})
        self.be_enabled      = be_cfg.get("enabled", True)
        self.be_trigger_r    = be_cfg.get("trigger_r", 1.0)
        self.be_offset_ticks = be_cfg.get("offset_ticks", 1)

        # Timeout config
        # max_hold_bars: close position after N M1 bars to prevent rollover swap.
        # Rollover time: typically 22:00–23:00 NY (00:00 server time MT5).
        # With 60 bars (1 hour), a position opened at 21:00 UTC closes by 22:00.
        # Set to 0 to disable.
        self.max_hold_bars               = cfg.get("max_hold_bars", 60)

        # Internal state per ticket
        self.last_modification_time: dict[int, float] = {}
        self.breakeven_applied: set[int] = set()
        self.bars_held_counter: dict[int, int] = {}

    def execute(self, symbol: str) -> None:
        """
        Called on every live tick. Manages all open positions for the symbol.
        """
        open_positions = self.trade_repo.get_positions_by_status(TradeStatus.OPEN)
        if not open_positions:
            return

        current_tick = self.market_data.get_current_tick(symbol)
        if not current_tick:
            return

        # Fetch broker symbol specification
        symbol_info = self.broker.get_symbol_info(symbol)
        symbol_point  = symbol_info.get("point", 0.01)
        symbol_digits = symbol_info.get("digits", 2)

        # ATR should come from a shared indicator cache in production
        current_atr = 2.5  # Placeholder — wire from IndicatorService M1 window

        for pos in open_positions:
            if pos.symbol != symbol:
                continue

            # Increment bars-held counter (1 per tick is approximate;
            # in production this should be incremented once per closed M1 bar)
            bars_held = self.bars_held_counter.get(pos.ticket, 0)

            # ── Timeout Gate (rollover protection) ───────────────────────────
            if self.max_hold_bars > 0 and bars_held >= self.max_hold_bars:
                self._process_exit(pos, current_tick, ExitReason.TIMEOUT)
                continue

            # ── Correct executable exit price ─────────────────────────────────
            executable_price = current_tick.bid if pos.type == "BUY" else current_tick.ask

            # ── Local SL/TP redundancy check (broker holds the real ones) ─────
            if pos.type == "BUY":
                if executable_price <= pos.price_sl:
                    self._process_exit(pos, current_tick, ExitReason.SL)
                    continue
                if executable_price >= pos.price_tp:
                    self._process_exit(pos, current_tick, ExitReason.TARGET)
                    continue
            else:
                if executable_price >= pos.price_sl:
                    self._process_exit(pos, current_tick, ExitReason.SL)
                    continue
                if executable_price <= pos.price_tp:
                    self._process_exit(pos, current_tick, ExitReason.TARGET)
                    continue

            # ── Breakeven (applied once per position, after +trigger_r move) ─
            if self.be_enabled and pos.ticket not in self.breakeven_applied:
                initial_risk    = abs(pos.price_open - pos.price_sl)
                trigger_distance = initial_risk * self.be_trigger_r

                if pos.type == "BUY":
                    be_triggered = executable_price >= (pos.price_open + trigger_distance)
                    be_sl = pos.price_open + (self.be_offset_ticks * symbol_point)
                    is_improvement = be_sl > pos.price_sl
                else:
                    be_triggered = executable_price <= (pos.price_open - trigger_distance)
                    be_sl = pos.price_open - (self.be_offset_ticks * symbol_point)
                    is_improvement = be_sl < pos.price_sl

                if be_triggered and is_improvement:
                    be_sl = self._round_to_digits(be_sl, symbol_digits)
                    success = self.broker.modify_position(pos.ticket, sl=be_sl, tp=pos.price_tp)
                    if success:
                        self.breakeven_applied.add(pos.ticket)
                        pos = replace(pos, price_sl=be_sl)
                        self.trade_repo.save_position(pos)
                        self.event_bus.publish(BreakevenSet(ticket=pos.ticket, be_sl=be_sl))

            # ── ATR Trailing SL ───────────────────────────────────────────────
            trail_distance = current_atr * 1.5

            if pos.type == "BUY":
                new_sl         = executable_price - trail_distance
                is_monotonic   = new_sl > pos.price_sl
                distance_moved = new_sl - pos.price_sl
            else:
                new_sl         = executable_price + trail_distance
                is_monotonic   = new_sl < pos.price_sl
                distance_moved = pos.price_sl - new_sl

            if not is_monotonic:
                continue

            # ── Three-Way Throttle ────────────────────────────────────────────
            effective_threshold = max(
                self.modify_min_points * symbol_point,
                current_atr * self.modify_min_atr_fraction
            )
            now_ts          = time.time()
            time_since_last = now_ts - self.last_modification_time.get(pos.ticket, 0)

            if distance_moved < effective_threshold:
                continue
            if time_since_last < self.modify_min_interval_seconds:
                continue

            # Normalize to valid broker digits
            new_sl = self._round_to_digits(new_sl, symbol_digits)

            success = self.broker.modify_position(pos.ticket, sl=new_sl, tp=pos.price_tp)
            if success:
                self.last_modification_time[pos.ticket] = now_ts
                updated_pos = replace(pos, price_sl=new_sl)
                self.trade_repo.save_position(updated_pos)
                self.event_bus.publish(
                    SLMoved(ticket=pos.ticket, old_sl=pos.price_sl, new_sl=new_sl)
                )

    def on_bar_closed(self, symbol: str) -> None:
        """
        Called once per closed M1 bar.
        Increments the bars-held counter for all open positions and publishes a Journey Snapshot.
        """
        open_positions = self.trade_repo.get_positions_by_status(TradeStatus.OPEN)
        current_tick = self.market_data.get_current_tick(symbol)
        
        for pos in open_positions:
            if pos.symbol == symbol:
                self.bars_held_counter[pos.ticket] = (
                    self.bars_held_counter.get(pos.ticket, 0) + 1
                )
                
                # Publish Journey Snapshot
                if current_tick:
                    price = current_tick.bid if pos.type == "BUY" else current_tick.ask
                    self.event_bus.publish(JourneySnapshot(
                        ticket=pos.ticket,
                        time=current_tick.time.isoformat(),
                        price=price,
                        sl=pos.price_sl,
                        bars_held=self.bars_held_counter[pos.ticket]
                    ))

    def _round_to_digits(self, price: float, digits: int) -> float:
        """Round a price to the broker's valid symbol digits (always floor)."""
        factor = 10 ** digits
        return math.floor(price * factor) / factor

    def _process_exit(
        self, pos: Position, tick, reason: ExitReason
    ) -> None:
        """Close position at broker and persist the final state."""
        exit_price = tick.bid if pos.type == "BUY" else tick.ask
        success = self.broker.close_position(pos.ticket)
        if success:
            self.breakeven_applied.discard(pos.ticket)
            self.last_modification_time.pop(pos.ticket, None)
            self.bars_held_counter.pop(pos.ticket, None)
            updated_pos = replace(
                pos,
                status=TradeStatus.CLOSED,
                price_close=exit_price,
                time_closing=datetime.now(timezone.utc)
            )
            self.trade_repo.save_position(updated_pos)
            self.event_bus.publish(
                TradeClosed(position=updated_pos, exit_reason=reason)
            )
