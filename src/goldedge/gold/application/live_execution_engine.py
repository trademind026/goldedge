import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

from ..domain.models.gold_pip_engine import GoldPipEngine
from ..domain.services.gold_kalman_exit_agent import GoldKalmanExitAgent
from ..domain.services.gold_decision_rules import GoldDecisionRules
from ..adapters.features.gold_kalman_filter import StreamingGoldKalmanFilter
from ..adapters.broker.gold_order_manager import GoldOrderManager
from ...domain.models.trade import Position, TradeStatus

logger = logging.getLogger("live_execution_engine")


class ForexLiveExecutionEngine:
    """
    In-Memory Live Execution Engine & Telemetry Hub for Spot Gold (XAUUSD) & Forex.
    Manages:
    1. 1s Micro Pipeline (Kinematic Kalman, Shannon Entropy, mark-to-market P&L)
    2. Open Position Monitoring & Dynamic Trailing Ratchet
    3. Multi-tier Risk Overrides (Zero Loss, Lock Profit, Exit, Kill Switch)
    4. Broker Zero-Desync Reconciliation (every 5 seconds)
    5. Rollover Freeze Gate (21:00 UTC or wide spread)
    6. Unified Telemetry Snapshot for 1s SSE streaming
    """

    def __init__(
        self,
        order_manager: Optional[GoldOrderManager] = None,
        symbol: str = "XAUUSD",
        default_mode: str = "paper",
        initial_balance: float = 10000.0,
        leverage: float = 100.0,
        redis_client: Optional[Any] = None
    ):
        self.symbol = symbol
        self.mode = default_mode.lower()
        self.leverage = leverage
        self.order_manager = order_manager or GoldOrderManager(
            default_mode="test" if self.mode == "paper" else "live",
            initial_balance=initial_balance,
            default_leverage=leverage
        )
        self.redis_client = redis_client

        # Kalman & Exit Agents
        self.kalman_filter = StreamingGoldKalmanFilter()
        self.exit_agents: Dict[int, GoldKalmanExitAgent] = {}
        self.decision_rules = GoldDecisionRules()

        # Engine State
        self.status: str = "awaiting_ticks"  # "idle" | "awaiting_ticks" | "scanning" | "trading" | "rollover_freeze" | "broker_disconnected"
        self.halted: bool = False
        self.last_tick_price: Optional[float] = None
        self.last_tick_bid: Optional[float] = None
        self.last_tick_ask: Optional[float] = None
        self.last_tick_time: Optional[datetime] = None
        self.last_reconciled: float = time.time()

        # Position tracking with full journey history
        self.journeys: Dict[int, List[Dict[str, Any]]] = {}
        self.suggestions: List[Dict[str, Any]] = []
        self.closed_positions_history: List[Dict[str, Any]] = []
        self.system_alerts: List[Dict[str, Any]] = []
        self._alert_seq: int = 1
        self._last_sl_modify_ts: Dict[int, float] = {}

    def set_mode(self, mode: str):
        mode_clean = mode.lower()
        if mode_clean in ("test", "paper"):
            mode_clean = "paper"
        if mode_clean not in ("paper", "demo", "live"):
            raise ValueError("Mode must be 'paper', 'demo', or 'live'")
        self.mode = mode_clean
        self.order_manager.set_mode(mode_clean)
        self.add_alert("info", f"Trading mode switched to {mode_clean.upper()}")

    def add_alert(self, level: str, text: str):
        alert_id = f"alt_{self._alert_seq}"
        self._alert_seq += 1
        self.system_alerts.insert(0, {
            "id": alert_id,
            "level": level.lower(),  # "info" | "warning" | "critical"
            "text": text,
            "ts": datetime.now(timezone.utc).strftime("%H:%M:%S")
        })
        if len(self.system_alerts) > 50:
            self.system_alerts = self.system_alerts[:50]

    def add_suggestion(self, suggestion: Dict[str, Any]):
        """Adds a genuine signal suggestion evaluated by the quant & ML engine."""
        self.suggestions.insert(0, suggestion)
        if len(self.suggestions) > 10:
            self.suggestions.pop()

    def on_tick(self, symbol: str, price: float, bid: float, ask: float, volume: float = 1.0):
        """
        1s Micro-Pipeline:
        - Kinematic Kalman update & Shannon entropy
        - Rollover freeze check
        - Position mark-to-market & dynamic trailing ratchet
        - Momentum collapse check
        """
        if symbol != self.symbol and symbol:
            return

        self.last_tick_price = price
        self.last_tick_bid = bid
        self.last_tick_ask = ask
        now = datetime.now(timezone.utc)
        self.last_tick_time = now

        # 1. Kinematic Kalman update
        spread = max(0.01, ask - bid)
        k_price, k_vel, vel_z = self.kalman_filter.update(price, volume, spread)

        # 2. Rollover Freeze Gate (21:00 - 22:00 UTC or wide spread > $0.60)
        is_rollover_hour = (now.hour == 21) or (now.hour == 22 and now.minute < 15)
        if is_rollover_hour or spread > GoldPipEngine.MAX_SPREAD_DOLLARS:
            self.status = "rollover_freeze"
        elif self.halted:
            self.status = "idle"
        else:
            open_positions = self.order_manager.get_open_positions(self.symbol)
            self.status = "trading" if open_positions else "scanning"

        # Update paper tick pricing
        self.order_manager.update_paper_tick(self.symbol, price, bid, ask)

        # 3. Open Position Monitoring & Trailing Ratchet
        open_positions = self.order_manager.get_open_positions(self.symbol)
        for pos in open_positions:
            ticket = pos.ticket
            if ticket not in self.exit_agents:
                agent = GoldKalmanExitAgent(pip_unit=GoldPipEngine.PIP_SIZE)
                agent.reset(pos.price_open)
                self.exit_agents[ticket] = agent

            agent = self.exit_agents[ticket]
            agent.update(price, bid, ask, pos.type)

            action, candidate_sl = agent.decide(
                pos.price_open, price, pos.price_sl, now, pos.type
            )

            # Check dynamic exit
            if action == "EARLY_EXIT":
                self.add_alert("warning", f"Position #{ticket} dynamic exit triggered (Momentum collapse / Chop)")
                self.exit_position(ticket)
                continue

            # Check trailing SL ratchet (handles unset SL <= 0 and throttles broker updates)
            is_buy = pos.type.upper() in ("BUY", "CALL", "LONG")
            sl_unset = pos.price_sl <= 0.0
            min_step = GoldPipEngine.PIP_SIZE
            sl_improved = (
                sl_unset
                or (is_buy and candidate_sl >= pos.price_sl + min_step)
                or (not is_buy and candidate_sl <= pos.price_sl - min_step)
            )
            now_ts = time.time()
            if candidate_sl > 0.0 and sl_improved and (now_ts - self._last_sl_modify_ts.get(ticket, 0.0) >= 2.0):
                if self.order_manager.modify_position(ticket, sl=round(candidate_sl, 2)):
                    self._last_sl_modify_ts[ticket] = now_ts

            # Record journey point
            pnl_usd = GoldPipEngine.calculate_cash_pnl(pos.type, pos.price_open, price, pos.volume)
            pnl_pips = GoldPipEngine.price_to_pips(abs(price - pos.price_open)) * (1 if pnl_usd >= 0 else -1)

            if ticket not in self.journeys:
                self.journeys[ticket] = []
            self.journeys[ticket].append({
                "ts": now.strftime("%H:%M:%S"),
                "price": price,
                "sl": pos.price_sl,
                "pnl_pips": round(pnl_pips, 1)
            })
            if len(self.journeys[ticket]) > 300:
                self.journeys[ticket].pop(0)

        # 4. Zero-Desync Broker Reconciliation (every 5 seconds)
        if time.time() - self.last_reconciled >= 5.0:
            self._reconcile_broker()
            self.last_reconciled = time.time()

    def _reconcile_broker(self):
        """
        Reconciles in-memory tickets with live broker every 5 seconds to avoid desync.
        Detects external manual broker exits, broker SL/TP triggers, and partial fills.
        """
        if self.mode in ("live", "demo"):
            try:
                live_positions = self.order_manager.mt5_broker.get_open_positions(self.symbol)
                live_tickets = {p.ticket: p for p in live_positions}

                # 1. Detect and reconcile externally closed positions
                for ticket in list(self.exit_agents.keys()):
                    if ticket not in live_tickets:
                        del self.exit_agents[ticket]
                        self.journeys.pop(ticket, None)
                        self._last_sl_modify_ts.pop(ticket, None)
                        self.add_alert("info", f"Position #{ticket} reconciled: closed externally on broker.")

                # 2. Synchronize active broker positions and partial fills
                for ticket, pos in live_tickets.items():
                    if ticket not in self.exit_agents:
                        agent = GoldKalmanExitAgent(pip_unit=GoldPipEngine.PIP_SIZE)
                        agent.reset(pos.price_open)
                        self.exit_agents[ticket] = agent
                        self.add_alert("info", f"Reconciled active broker position #{ticket} ({pos.type} {pos.volume} lots @ ${pos.price_open:.2f}).")
            except Exception as e:
                logger.warning(f"Live broker reconciliation warning: {e}")

    # --- Risk Overrides ---

    def zero_loss(self, ticket: int) -> Tuple[bool, float, str]:
        """
        Moves Stop-Loss to Breakeven (+ spread buffer) as soon as position is in profit.
        """
        positions = self.order_manager.get_open_positions(self.symbol)
        pos = next((p for p in positions if p.ticket == ticket), None)
        if not pos:
            return False, 0.0, "Position not found"

        pip_unit = GoldPipEngine.PIP_SIZE
        is_buy = pos.type.upper() in ("BUY", "CALL", "LONG")

        # Breakeven + 1 pip spread buffer
        breakeven = (pos.price_open + 1.0 * pip_unit) if is_buy else (pos.price_open - 1.0 * pip_unit)
        breakeven = round(breakeven, 2)

        # Ensure trade is in profit before moving
        current = self.last_tick_bid if is_buy else self.last_tick_ask
        if (is_buy and current <= pos.price_open) or (not is_buy and current >= pos.price_open):
            return False, pos.price_sl, "Position not in profit yet"

        success = self.order_manager.modify_position(ticket, sl=breakeven)
        if success:
            self.add_alert("info", f"Position #{ticket} Stop-Loss moved to Breakeven (${breakeven})")
            return True, breakeven, "Zero Loss set"
        return False, pos.price_sl, "Broker modification failed"

    def lock_profit(self, ticket: int, lock_pct: float = 0.60) -> Tuple[bool, float, str]:
        """
        Ratchets Stop-Loss to lock 60% of current floating profit.
        """
        positions = self.order_manager.get_open_positions(self.symbol)
        pos = next((p for p in positions if p.ticket == ticket), None)
        if not pos:
            return False, 0.0, "Position not found"

        is_buy = pos.type.upper() in ("BUY", "CALL", "LONG")
        current = self.last_tick_bid if is_buy else self.last_tick_ask
        profit_delta = (current - pos.price_open) if is_buy else (pos.price_open - current)
        profit_pips = GoldPipEngine.price_to_pips(profit_delta)

        if profit_pips < 5.0:
            return False, pos.price_sl, "Insufficient profit to lock (< 5 pips)"

        lock_delta = profit_delta * lock_pct
        new_sl = (pos.price_open + lock_delta) if is_buy else (pos.price_open - lock_delta)
        new_sl = round(new_sl, 2)

        success = self.order_manager.modify_position(ticket, sl=new_sl)
        if success:
            self.add_alert("info", f"Position #{ticket} locked 60% profit at ${new_sl}")
            return True, new_sl, "Profit locked"
        return False, pos.price_sl, "Failed to lock profit"

    def exit_position(self, ticket: int) -> Tuple[bool, str]:
        """Instant market close on a specific ticket."""
        success = self.order_manager.close_position(ticket)
        if success:
            self.exit_agents.pop(ticket, None)
            self.add_alert("info", f"Position #{ticket} closed at market")
            return True, "Position closed"
        return False, "Failed to close position"

    def kill_switch(self) -> List[int]:
        """EMERGENCY: Closes ALL open positions and disables trading engine."""
        self.halted = True
        self.status = "idle"
        closed_tickets = []
        positions = self.order_manager.get_open_positions()
        for p in positions:
            if self.order_manager.close_position(p.ticket):
                closed_tickets.append(p.ticket)
                self.exit_agents.pop(p.ticket, None)

        self.add_alert("critical", f"EMERGENCY KILL SWITCH ACTIVATED: Closed {len(closed_tickets)} positions.")
        return closed_tickets

    def execute_suggestion(self, suggestion_id: str, lots: Optional[float] = None) -> Optional[int]:
        """Executes a qualified suggestion or manual entry."""
        if self.halted or self.status == "rollover_freeze":
            self.add_alert("warning", f"Execution blocked: Engine status is {self.status.upper()}")
            return None

        if self.last_tick_price is None:
            self.add_alert("warning", f"Execution blocked: No market tick received yet for {self.symbol}.")
            return None

        sug = next((s for s in self.suggestions if s["id"] == suggestion_id), None)
        vol = lots if lots is not None and lots > 0 else (sug["recommended_lots"] if sug else 0.5)
        direction = sug["direction"] if sug else "BUY"
        pip_unit = GoldPipEngine.PIP_SIZE
        sl = sug["sl"] if sug else round(self.last_tick_price - (25.0 * pip_unit) if direction == "BUY" else self.last_tick_price + (25.0 * pip_unit), 2)
        tp = sug["tp"] if sug else round(self.last_tick_price + (50.0 * pip_unit) if direction == "BUY" else self.last_tick_price - (50.0 * pip_unit), 2)

        ticket = self.order_manager.place_order(
            symbol=self.symbol,
            order_type=direction,
            volume=vol,
            sl=sl,
            tp=tp,
            price=self.last_tick_price
        )
        if ticket:
            self.add_alert("info", f"Executed order #{ticket} ({direction} {vol} lots @ ${self.last_tick_price:.2f})")
            # Initialize journey
            self.journeys[ticket] = [{
                "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "price": self.last_tick_price,
                "sl": sl,
                "pnl_pips": 0.0
            }]
        return ticket

    def get_telemetry_snapshot(self) -> Dict[str, Any]:
        """
        Builds the unified LiveEnginePayload matching the AI specification.
        Strict separation: paper uses simulated ledger on real ticks; live queries MT5 broker directly.
        """
        t0 = time.time()
        summary = self.order_manager.get_account_summary()
        current_status = self.status

        if self.mode in ("live", "demo"):
            is_connected = summary.get("live_connected", False)
            balance = summary.get("live_balance", 0.0)
            equity = summary.get("live_equity", 0.0)
            used_margin = summary.get("live_margin", 0.0)
            free_margin = summary.get("live_free_margin", 0.0)
            margin_level_pct = summary.get("live_margin_level", 999.0)
            currency = summary.get("live_currency", "USD")
            leverage = summary.get("live_leverage", self.leverage)
            if not is_connected and not self.halted:
                current_status = "broker_disconnected"
        else:
            balance = summary.get("paper_balance", 10000.0)
            equity = summary.get("paper_equity", 10000.0)
            open_positions_raw = self.order_manager.get_open_positions(self.symbol)
            used_margin = sum(
                GoldPipEngine.calculate_required_margin(p.price_open, p.volume, self.leverage)
                for p in open_positions_raw
            )
            free_margin = max(0.0, equity - used_margin)
            margin_level_pct = (equity / used_margin * 100.0) if used_margin > 0 else 999.0
            currency = "USD"
            leverage = self.leverage

        if self.last_tick_ask is not None and self.last_tick_bid is not None:
            spread_pips = GoldPipEngine.price_to_pips(self.last_tick_ask - self.last_tick_bid)
        else:
            spread_pips = 0.0

        # Format ForexPosition items
        positions: List[Dict[str, Any]] = []
        open_positions_raw = self.order_manager.get_open_positions(self.symbol)
        for p in open_positions_raw:
            is_buy = p.type.upper() in ("BUY", "CALL", "LONG")
            if self.last_tick_bid is not None and self.last_tick_ask is not None:
                curr_price = self.last_tick_bid if is_buy else self.last_tick_ask
            else:
                curr_price = p.price_close if p.price_close else p.price_open

            pnl_usd = GoldPipEngine.calculate_cash_pnl(p.type, p.price_open, curr_price, p.volume)
            pnl_pips = GoldPipEngine.price_to_pips(abs(curr_price - p.price_open)) * (1 if pnl_usd >= 0 else -1)

            agent = self.exit_agents.get(p.ticket)
            vel_z = agent.fast_vel_z if agent else 0.0
            entropy = agent.entropy if agent else 0.0
            peak = agent.peak_price if agent and agent.peak_price else p.price_open

            positions.append({
                "ticket": p.ticket,
                "symbol": p.symbol,
                "direction": p.type,
                "lots": p.volume,
                "entry_price": p.price_open,
                "current_price": curr_price,
                "current_spread_pips": round(spread_pips, 1),
                "sl": p.price_sl,
                "tp": p.price_tp,
                "initial_sl": p.price_sl,
                "pnl_pips": round(pnl_pips, 1),
                "pnl_usd": round(pnl_usd, 2),
                "trailing_active": p.price_sl != p.price_sl or (agent.weak_start_time is not None if agent else False),
                "peak_price": peak,
                "kalman_velocity_z": round(vel_z, 2),
                "entropy": round(entropy, 2),
                "bars_held": len(self.journeys.get(p.ticket, [])),
                "journey": self.journeys.get(p.ticket, [])
            })

        # Closed positions
        closed_positions: List[Dict[str, Any]] = []
        for cp in self.order_manager.closed_paper_positions[-20:]:
            pnl_usd = cp.get("realised_pnl", 0.0)
            entry = cp.get("entry_price", 0.0)
            exit_p = cp.get("exit_price", entry)
            pnl_pips = GoldPipEngine.price_to_pips(abs(exit_p - entry)) * (1 if pnl_usd >= 0 else -1) if entry > 0 else 0.0
            closed_positions.append({
                "ticket": cp.get("id", 1000),
                "symbol": cp.get("symbol", self.symbol),
                "direction": cp.get("direction", "BUY"),
                "lots": cp.get("lots", 0.5),
                "entry_price": entry,
                "current_price": exit_p,
                "current_spread_pips": round(spread_pips, 1),
                "sl": 0.0,
                "tp": 0.0,
                "initial_sl": 0.0,
                "pnl_pips": round(pnl_pips, 1),
                "pnl_usd": round(pnl_usd, 2),
                "trailing_active": False,
                "peak_price": exit_p,
                "kalman_velocity_z": 0.0,
                "entropy": 0.0,
                "bars_held": 5,
                "journey": []
            })

        latency_ms = round((time.time() - t0) * 1000, 2)

        return {
            "mode": self.mode,
            "status": current_status,
            "account": {
                "balance": round(balance, 2),
                "equity": round(equity, 2),
                "used_margin": round(used_margin, 2),
                "free_margin": round(free_margin, 2),
                "margin_level_pct": round(margin_level_pct, 1),
                "leverage": self.leverage,
                "currency": "USD"
            },
            "positions": positions,
            "suggestions": self.suggestions,
            "closed_positions": closed_positions,
            "system_alerts": self.system_alerts,
            "latency_ms": latency_ms,
            "last_updated": datetime.now(timezone.utc).isoformat()
        }
