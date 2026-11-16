import time
import math
from datetime import datetime, timezone
from dataclasses import replace
from typing import Optional, Union

from ...domain.models.trade import TradeStatus, ExitReason, Position
from ...domain.models.pip_calculator import PipCalculator
from ...domain.services.forex_kalman_exit_agent import ForexKalmanEntropyExitAgent
from ...domain.ports.outbound.broker import IBrokerAdapter
from ...domain.ports.outbound.trade_repo import ITradeRepository
from ...domain.ports.outbound.event_bus import IEventBus
from ...domain.events.trading import TradeClosed, SLMoved, BreakevenSet, JourneySnapshot
from ...domain.ports.outbound.market_data import IMarketDataProvider


class ManagePositionUseCase:
    """
    Tick-driven position management for Forex / CFD trading.
    Incorporates ForexKalmanEntropyExitAgent for real-time velocity exhaustion,
    Shannon entropy collapse detection, and dynamic trailing stop adjustment.
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
        self.modify_min_points = cfg.get("modify_min_points", 10)
        self.modify_min_atr_fraction = cfg.get("modify_min_atr_fraction", 0.05)
        self.modify_min_interval_seconds = cfg.get("modify_min_interval_seconds", 2.0)

        # Breakeven config
        be_cfg = cfg.get("breakeven", {})
        self.be_enabled = be_cfg.get("enabled", True)
        self.be_trigger_r = be_cfg.get("trigger_r", 1.5)
        self.be_offset_ticks = be_cfg.get("offset_ticks", 20)

        # Timeout config
        self.max_hold_bars = cfg.get("max_hold_bars", 60)

        # Internal state per ticket
        self.last_modification_time: dict[int, float] = {}
        self.breakeven_applied: set[int] = set()
        self.bars_held_counter: dict[int, int] = {}
        self.exit_agents: dict[int, ForexKalmanEntropyExitAgent] = {}

    def get_or_create_exit_agent(self, pos: Position) -> ForexKalmanEntropyExitAgent:
        """Retrieves or creates a dedicated ForexKalmanEntropyExitAgent for a position."""
        if pos.ticket not in self.exit_agents:
            pip_unit = PipCalculator.get_pip_unit(pos.symbol)
            agent = ForexKalmanEntropyExitAgent(pip_unit=pip_unit)
            agent.reset(pos.price_open)
            self.exit_agents[pos.ticket] = agent
        return self.exit_agents[pos.ticket]

    def execute(self, arg1: Union[str, float], arg2: Optional[str] = None) -> None:
        """
        Called on every live tick. Handles both execute(symbol) and execute(price, symbol).
        """
        if isinstance(arg1, str):
            symbol = arg1
        elif isinstance(arg2, str):
            symbol = arg2
        else:
            return

        open_positions = self.trade_repo.get_positions_by_status(TradeStatus.OPEN)
        if not open_positions:
            return

        current_tick = self.market_data.get_current_tick(symbol)
        if not current_tick:
            return

        # Fetch broker symbol specification
        symbol_info = {}
        if hasattr(self.broker, "get_symbol_info"):
            symbol_info = self.broker.get_symbol_info(symbol)
        symbol_point = symbol_info.get("point", PipCalculator.get_pip_unit(symbol) * 0.1)
        symbol_digits = symbol_info.get("digits", 5 if "JPY" not in symbol else 3)

        current_price = current_tick.bid
        current_atr = current_price * 0.0005

        for pos in open_positions:
            if pos.symbol != symbol:
                continue

            bars_held = self.bars_held_counter.get(pos.ticket, 0)

            # ── Timeout Gate ───────────────────────────────────────────
            if self.max_hold_bars > 0 and bars_held >= self.max_hold_bars:
                self._process_exit(pos, current_tick, ExitReason.TIMEOUT)
                continue

            executable_price = current_tick.bid if pos.type == "BUY" else current_tick.ask

            # ── Local SL/TP check ─────────────────────────────────────
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

            # ── Kalman-Entropy Exit & Trailing Agent Decision ─────────
            exit_agent = self.get_or_create_exit_agent(pos)
            exit_agent.update(executable_price, current_tick.bid, current_tick.ask, direction=pos.type)
            action, suggested_sl = exit_agent.decide(
                entry_price=pos.price_open,
                current_price=executable_price,
                current_sl=pos.price_sl,
                tick_time=current_tick.time,
                direction=pos.type
            )

            if action == "EARLY_EXIT":
                reason_str = getattr(exit_agent, "exit_reason", "")
                if reason_str == "KALMAN_VELOCITY_REVERSAL":
                    exit_reason = ExitReason.KALMAN_VELOCITY_REVERSAL
                elif reason_str == "KALMAN_TIGHTEN":
                    exit_reason = ExitReason.KALMAN_TIGHTEN
                else:
                    exit_reason = ExitReason.SYSTEM_CLOSE
                self._process_exit(pos, current_tick, exit_reason)
                continue

            # ── Breakeven (applied once per position) ─────────────────
            if self.be_enabled and pos.ticket not in self.breakeven_applied:
                initial_risk = abs(pos.price_open - pos.price_sl)
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

            # ── Dynamic Trailing Stop (incorporating agent suggested_sl)
            trail_distance = current_atr * 1.5

            if pos.type == "BUY":
                candidate_sl = max(suggested_sl, executable_price - trail_distance)
                if self.max_hold_bars > 0:
                    hold_pct = bars_held / self.max_hold_bars
                    if hold_pct >= 0.75:
                        progress = min((hold_pct - 0.75) / 0.25, 1.0)
                        candidate_sl = max(candidate_sl, candidate_sl + progress * (executable_price - candidate_sl))

                is_monotonic = candidate_sl > pos.price_sl
                distance_moved = candidate_sl - pos.price_sl
                new_sl = candidate_sl
            else:
                candidate_sl = min(suggested_sl, executable_price + trail_distance)
                if self.max_hold_bars > 0:
                    hold_pct = bars_held / self.max_hold_bars
                    if hold_pct >= 0.75:
                        progress = min((hold_pct - 0.75) / 0.25, 1.0)
                        candidate_sl = min(candidate_sl, candidate_sl - progress * (candidate_sl - executable_price))

                is_monotonic = candidate_sl < pos.price_sl
                distance_moved = pos.price_sl - candidate_sl
                new_sl = candidate_sl

            if not is_monotonic:
                continue

            # ── Throttle ──────────────────────────────────────────────
            effective_threshold = max(
                self.modify_min_points * symbol_point,
                current_atr * self.modify_min_atr_fraction
            )
            now_ts = time.time()
            time_since_last = now_ts - self.last_modification_time.get(pos.ticket, 0)

            if distance_moved < effective_threshold:
                continue
            if time_since_last < self.modify_min_interval_seconds:
                continue

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
        """Called once per closed bar."""
        open_positions = self.trade_repo.get_positions_by_status(TradeStatus.OPEN)
        current_tick = self.market_data.get_current_tick(symbol)

        for pos in open_positions:
            if pos.symbol == symbol:
                self.bars_held_counter[pos.ticket] = (
                    self.bars_held_counter.get(pos.ticket, 0) + 1
                )
                if pos.ticket in self.exit_agents:
                    self.exit_agents[pos.ticket].on_bar_closed()

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
        factor = 10 ** digits
        return math.floor(price * factor) / factor

    def _process_exit(self, pos: Position, tick, reason: ExitReason) -> None:
        exit_price = tick.bid if pos.type == "BUY" else tick.ask
        success = self.broker.close_position(pos.ticket)
        if success:
            self.breakeven_applied.discard(pos.ticket)
            self.last_modification_time.pop(pos.ticket, None)
            self.bars_held_counter.pop(pos.ticket, None)
            self.exit_agents.pop(pos.ticket, None)
            updated_pos = replace(
                pos,
                status=TradeStatus.CLOSED,
                price_close=exit_price,
                time_closing=datetime.now(timezone.utc),
                exit_reason=reason
            )
            self.trade_repo.save_position(updated_pos)
            self.event_bus.publish(
                TradeClosed(position=updated_pos, exit_reason=reason)
            )
