from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any
from .gold_pip_engine import GoldPipEngine
from ..services.gold_kalman_exit_agent import GoldKalmanExitAgent


@dataclass
class GoldOpenTrade:
    """
    Sub-Bar Intra-Tick Position Tracker for Spot Gold (XAUUSD).
    Enforces strict chronological execution invariants:
    1. SL evaluation on tick k+1 uses the ratcheted SL level from tick k before
       updating peak for tick k+1.
    2. Half-spread bid/ask slippage modeling.
    3. Tiered trailing retention and stagnation guard.
    4. Sub-second Kalman-Entropy exit agent integration.
    """

    id: int
    entry_time: datetime
    symbol: str
    direction: str  # BUY or SELL
    entry_price: float  # Fill price (including half-spread slippage)
    initial_sl: float
    target_price: float
    lots: float
    strategy: str = "Quant_Gold_Kalman"
    final_score: float = 0.85
    session: str = "LONDON"
    entry_atr: float = 2.50
    half_spread_dollars: float = 0.15

    sl: float = field(init=False)
    peak_price: float = field(init=False)
    trailing_active: bool = False
    status: str = "OPEN"
    current_price: float = field(init=False)
    unrealised_pnl: float = 0.0
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    realised_pnl: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_reason_override: Optional[str] = None

    bars_held: int = 0
    consecutive_loss: bool = False
    journey_snapshots: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        self.direction = self.direction.upper()
        self.sl = self.initial_sl
        self.peak_price = self.entry_price
        self.current_price = self.entry_price
        self._exit_agent = GoldKalmanExitAgent(pip_unit=GoldPipEngine.PIP_SIZE)
        self._exit_agent.reset(self.entry_price)

    @property
    def is_buy(self) -> bool:
        return self.direction in ("BUY", "CALL", "LONG")

    def process_tick(
        self,
        tick_time: datetime,
        tick_price: float,
        bid: Optional[float] = None,
        ask: Optional[float] = None
    ) -> bool:
        """
        Processes an individual intra-minute tick in strict order.
        Returns True if trade remains open, False if exited on this tick.
        """
        if self.status != "OPEN":
            return False

        # Half-spread pricing
        if bid is not None and ask is not None and ask >= bid:
            eff_bid = bid
            eff_ask = ask
        else:
            eff_bid = tick_price - self.half_spread_dollars
            eff_ask = tick_price + self.half_spread_dollars

        eval_price = eff_bid if self.is_buy else eff_ask
        self.current_price = tick_price

        # --- STEP 1: PRIOR SL EVALUATION (Zero look-ahead) ---
        if self.is_buy:
            if eval_price <= self.sl:
                self._close_position(
                    exit_time=tick_time,
                    exit_price=min(eval_price, self.sl),
                    reason=self.exit_reason_override or ("TRAILING_SL" if self.trailing_active else "SL")
                )
                return False
        else:
            if eval_price >= self.sl:
                self._close_position(
                    exit_time=tick_time,
                    exit_price=max(eval_price, self.sl),
                    reason=self.exit_reason_override or ("TRAILING_SL" if self.trailing_active else "SL")
                )
                return False

        # --- STEP 2: TARGET / TAKE-PROFIT EVALUATION ---
        if self.is_buy:
            if eval_price >= self.target_price:
                self._close_position(
                    exit_time=tick_time,
                    exit_price=self.target_price,
                    reason="TARGET"
                )
                return False
        else:
            if eval_price <= self.target_price:
                self._close_position(
                    exit_time=tick_time,
                    exit_price=self.target_price,
                    reason="TARGET"
                )
                return False

        # --- STEP 3: PEAK PRICE UPDATE ---
        if self.is_buy:
            if tick_price > self.peak_price:
                self.peak_price = tick_price
        else:
            if tick_price < self.peak_price:
                self.peak_price = tick_price

        # --- STEP 4: KALMAN EXIT AGENT TICK UPDATE & DECISION ---
        self._exit_agent.update(tick_price, eff_bid, eff_ask, self.direction)
        decision, candidate_sl = self._exit_agent.decide(
            self.entry_price, tick_price, self.sl, tick_time, self.direction
        )

        if decision == "EARLY_EXIT":
            self.exit_reason_override = "MOMENTUM_COLLAPSE" if self.unrealised_pnl < 0 else "ENTROPY_CHOP_EXIT"
            self._close_position(
                exit_time=tick_time,
                exit_price=eval_price,
                reason=self.exit_reason_override
            )
            return False

        # --- STEP 5: TIERED RATCHET TRAILING STOP ---
        if self.is_buy:
            if candidate_sl > self.sl:
                self.sl = candidate_sl
                self.trailing_active = True
        else:
            if candidate_sl < self.sl:
                self.sl = candidate_sl
                self.trailing_active = True

        # Floating PnL calculation
        self.unrealised_pnl = GoldPipEngine.calculate_cash_pnl(
            self.direction, self.entry_price, eval_price, self.lots
        )

        return True

    def on_bar_closed(self, bar_time: datetime, bar_close: float):
        """Called at the end of each 1-minute candle."""
        if self.status != "OPEN":
            return
        self.bars_held += 1
        self.journey_snapshots.append({
            "time": bar_time.isoformat(),
            "close": bar_close,
            "sl": self.sl,
            "peak": self.peak_price,
            "pnl": self.unrealised_pnl,
            "bars_held": self.bars_held,
            "trailing_active": self.trailing_active
        })

    def _close_position(self, exit_time: datetime, exit_price: float, reason: str):
        self.status = "CLOSED"
        self.exit_time = exit_time
        self.exit_price = exit_price
        self.exit_reason = reason
        self.realised_pnl = GoldPipEngine.calculate_cash_pnl(
            self.direction, self.entry_price, exit_price, self.lots
        )
        self.unrealised_pnl = 0.0
        self.consecutive_loss = self.realised_pnl < 0.0
