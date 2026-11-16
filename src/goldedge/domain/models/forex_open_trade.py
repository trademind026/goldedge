from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from datetime import datetime
import pandas as pd
import numpy as np

from .pip_calculator import PipCalculator
from ..services.forex_kalman_exit_agent import ForexKalmanEntropyExitAgent

HALF_SPREAD_PCT = 0.0003  # ~0.3-0.5 pip on standard pairs (3.0e-4)
MAX_HOLD_BARS = 40
COMMISSION_PER_LOT = 5.0  # Standard $5.00 round-turn commission per lot
TRAILING_TRIGGER = 0.0015  # ~15 pips or 0.15% gain activates trailing
TRAILING_LOCK = 0.0005     # Locks ~5 pips profit immediately


@dataclass
class BacktestEngineState:
    initial_balance: float = 100_000.0
    equity: float = 100_000.0
    cash: float = 100_000.0
    peak_equity: float = 100_000.0
    current_drawdown_usd: float = 0.0
    max_drawdown_pct: float = 0.0
    daily_pnl: float = 0.0
    consecutive_losses: int = 0
    cooling_off_until_bar: int = 0
    last_trade_bar: Dict[str, int] = field(default_factory=dict)
    active_positions: List[Any] = field(default_factory=list)
    closed_trades: List[Dict[str, Any]] = field(default_factory=list)


def create_exit_record(trade: Any, exit_price: float, timestamp: Any, exit_reason: str) -> dict:
    """Creates a standardized closed trade audit record."""
    trade.exit_time = timestamp
    trade.exit_price = round(float(exit_price), 5)
    trade.result = exit_reason
    pip_size = PipCalculator.get_pip_unit(trade.symbol)

    dir_val = trade.direction_int if hasattr(trade, "direction_int") else (
        trade.direction if isinstance(trade.direction, int) else (
            1 if str(trade.direction).upper() in ("BUY", "CALL", "LONG", "+1") else -1
        )
    )

    price_delta = (trade.exit_price - trade.entry_price) if dir_val == 1 else (trade.entry_price - trade.exit_price)
    trade.pnl_pips = round(price_delta / pip_size, 2)
    units = getattr(trade, "units", trade.lot_size * trade.contract_size)
    gross_pnl = price_delta * units
    commission = trade.commission_per_lot * trade.lot_size
    swap = getattr(trade, "total_swap_usd", 0.0)
    trade.pnl = round(gross_pnl - commission + swap, 2)

    return {
        "trade_id": getattr(trade, "trade_id", f"{trade.symbol}_{trade.entry_time}"),
        "symbol": trade.symbol,
        "direction": "BUY" if dir_val == 1 else "SELL",
        "direction_int": dir_val,
        "entry_time": str(trade.entry_time),
        "exit_time": str(timestamp),
        "open_time": getattr(trade, "open_time", trade.entry_time),
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "lot_size": trade.lot_size,
        "lots": trade.lot_size,
        "units": units,
        "sl": round(trade.initial_sl, 5),
        "initial_sl_price": round(trade.initial_sl, 5),
        "current_sl_price": round(trade.current_sl_price, 5),
        "target": round(trade.target, 5),
        "target_price": round(trade.target, 5),
        "pnl_usd": trade.pnl,
        "pnl": trade.pnl,
        "pnl_pips": trade.pnl_pips,
        "mae_pips": round(getattr(trade, "mae_pips", 0.0), 2),
        "mfe_pips": round(getattr(trade, "mfe_pips", 0.0), 2),
        "peak_pnl_pips": round(getattr(trade, "peak_pnl_pips", 0.0), 2),
        "bars_held": getattr(trade, "bars_held", 0),
        "ticks_held": getattr(trade, "ticks_held", 0),
        "bars_since_peak": getattr(trade, "bars_since_peak", 0),
        "strategy_name": getattr(trade, "strategy_name", getattr(trade, "strategy", "")),
        "regime_at_entry": getattr(trade, "regime_at_entry", getattr(trade, "regime", "")),
        "exit_reason": exit_reason,
        "result": exit_reason,
        "total_swap_usd": swap,
        "journey": getattr(trade, "journey", [])
    }


def check_exit_tick(
    trade: Any,
    tick: dict,
    pip_size: float,
    current_spread_pips: float,
    kalman_velocity: float = 0.0,
    kalman_sigma_v: float = 0.0
) -> Optional[dict]:
    """
    Sub-bar tick check executing the exact 6 institutional triggers:
      1. Zero-Lookahead SL (prior-state level checked before ratchet)
      2. Target Limit Hit
      3. Kinematic Kalman Adverse Velocity Veto (|v_t| > 1.8 * sigma_v)
      4. Excursion & Peak tracking (MAE, MFE, peak_pnl_pips)
      5. 4-Tier Profit Ratchet (50% -> 80%, 35% -> 70%, 20% -> 55%, 10% -> Breakeven + 2x spread)
      6. Stagnation Defense Tightening (15-bar linear compression)
    """
    trade.ticks_held = getattr(trade, "ticks_held", 0) + 1
    bid = float(tick["bid"])
    ask = float(tick["ask"])
    ts = tick.get("timestamp", tick.get("time", datetime.now()))

    dir_val = trade.direction_int if hasattr(trade, "direction_int") else (
        trade.direction if isinstance(trade.direction, int) else (
            1 if str(trade.direction).upper() in ("BUY", "CALL", "LONG", "+1") else -1
        )
    )

    # --- 1. ZERO-LOOKAHEAD STOP-LOSS (Prior-State Tick Check) ---
    if dir_val == 1 and bid <= trade.current_sl_price:
        return create_exit_record(trade, bid, ts, "STOP_LOSS")
    elif dir_val == -1 and ask >= trade.current_sl_price:
        return create_exit_record(trade, ask, ts, "STOP_LOSS")

    # --- 2. TARGET LIMIT CHECK ---
    if dir_val == 1 and bid >= trade.target_price:
        return create_exit_record(trade, trade.target_price, ts, "TARGET")
    elif dir_val == -1 and ask <= trade.target_price:
        return create_exit_record(trade, trade.target_price, ts, "TARGET")

    # --- 3. KINEMATIC KALMAN VELOCITY EXIT (Adverse Velocity Veto) ---
    if trade.ticks_held > 10 and kalman_sigma_v > 0:
        if dir_val == 1 and kalman_velocity < (-1.8 * kalman_sigma_v):
            return create_exit_record(trade, bid, ts, "KALMAN_VELOCITY_REVERSAL")
        elif dir_val == -1 and kalman_velocity > (1.8 * kalman_sigma_v):
            return create_exit_record(trade, ask, ts, "KALMAN_VELOCITY_REVERSAL")

    # --- 4. EXCURSION & PEAK TRACKING ---
    raw_pips = ((bid - trade.entry_price) if dir_val == 1 else (trade.entry_price - ask)) / pip_size
    current_pips = round(raw_pips, 6)
    trade.mfe_pips = max(getattr(trade, "mfe_pips", 0.0), current_pips)
    trade.mae_pips = min(getattr(trade, "mae_pips", 0.0), current_pips)

    if current_pips > getattr(trade, "peak_pnl_pips", 0.0):
        trade.peak_pnl_pips = current_pips
        trade.bars_since_peak = 0
        trade.peak_price = bid if dir_val == 1 else ask

    # --- 5. TIERED PROFIT RATCHET (4 Institutional Tiers) ---
    peak = trade.peak_pnl_pips
    new_sl = trade.current_sl_price

    if peak >= 50.0:          # Tier 1: Lock 80%
        lock_pips = peak * 0.80
        cand_sl = trade.entry_price + (lock_pips * pip_size * dir_val)
        new_sl = max(new_sl, cand_sl) if dir_val == 1 else min(new_sl, cand_sl)
        trade.trailing_active = True
    elif peak >= 35.0:        # Tier 2: Lock 70%
        lock_pips = peak * 0.70
        cand_sl = trade.entry_price + (lock_pips * pip_size * dir_val)
        new_sl = max(new_sl, cand_sl) if dir_val == 1 else min(new_sl, cand_sl)
        trade.trailing_active = True
    elif peak >= 20.0:        # Tier 3: Lock 55%
        lock_pips = peak * 0.55
        cand_sl = trade.entry_price + (lock_pips * pip_size * dir_val)
        new_sl = max(new_sl, cand_sl) if dir_val == 1 else min(new_sl, cand_sl)
        trade.trailing_active = True
    elif peak >= 10.0:        # Tier 4: Breakeven + 2x Spread buffer
        be_pips = max(1.0, 2.0 * current_spread_pips)
        cand_sl = trade.entry_price + (be_pips * pip_size * dir_val)
        new_sl = max(new_sl, cand_sl) if dir_val == 1 else min(new_sl, cand_sl)
        trade.trailing_active = True

    trade.current_sl_price = new_sl
    trade.sl = new_sl

    # --- 6. STAGNATION DEFENSE TIGHTENING (15-bar linear compression) ---
    if getattr(trade, "bars_since_peak", 0) >= 15:
        compression_pips = (trade.bars_since_peak - 15) * 0.5
        compressed_sl = trade.current_sl_price + (compression_pips * pip_size * dir_val)
        if dir_val == 1:
            trade.current_sl_price = min(trade.entry_price + (trade.peak_pnl_pips * pip_size),
                                         max(trade.current_sl_price, compressed_sl))
        else:
            trade.current_sl_price = max(trade.entry_price - (trade.peak_pnl_pips * pip_size),
                                         min(trade.current_sl_price, compressed_sl))
        trade.sl = trade.current_sl_price

    return None


class ForexOpenTrade:
    """
    Tracks a single open Forex/CFD position with real intra-tick price monitoring.
    Enforces strict execution invariants:
      1. Zero-Lookahead Stop-Loss: tick k evaluated on prior SL before ratchet.
      2. Half-spread bid/ask slippage modeling.
      3. 4-Tier trailing ratchet + 15-bar stagnation defense.
      4. Kinematic Kalman velocity adverse reversal exit.
    """

    def __init__(
        self,
        entry_time: datetime,
        symbol: str,
        direction: Any = "BUY",
        strategy: str = "",
        entry_price: float = 0.0,
        tick_df: Optional[pd.DataFrame] = None,
        ml_prob: float = 0.65,
        strat_prob: float = 0.80,
        flow_score: float = 0.60,
        final_score: float = 0.82,
        regime: str = "TRENDING",
        entry_bar_idx: int = 0,
        sl_pct: float = 0.0030,  # ~30 pips
        tgt_pct: float = 0.0060, # ~60 pips
        lot_size: float = 1.0,
        contract_size: float = 100000.0,
        commission_per_lot: float = COMMISSION_PER_LOT,
        rl_agent: Any = "kalman",
        half_spread_pct: float = HALF_SPREAD_PCT,
        max_hold_bars: int = MAX_HOLD_BARS,
        trade_id: Optional[str] = None
    ):
        self.entry_time = entry_time
        self.open_time = entry_time
        self.symbol = symbol.upper()
        self.direction = direction
        self.strategy = strategy
        self.strategy_name = strategy
        self.entry_price = float(entry_price)
        self.tick_df = tick_df if tick_df is not None else pd.DataFrame()
        self.ml_prob = ml_prob
        self.strat_prob = strat_prob
        self.flow_score = flow_score
        self.final_score = final_score
        self.regime = regime
        self.regime_at_entry = regime
        self.entry_bar_idx = entry_bar_idx
        self.lot_size = float(lot_size)
        self.lots = float(lot_size)
        self.contract_size = float(contract_size)
        self.units = float(lot_size * contract_size)
        self.commission_per_lot = float(commission_per_lot)
        self.half_spread_pct = float(half_spread_pct)
        self.max_hold_bars = max_hold_bars
        self.pip_unit = PipCalculator.get_pip_unit(self.symbol)
        self.trade_id = trade_id or f"{self.symbol}_{entry_time.strftime('%Y%m%d%H%M%S') if hasattr(entry_time, 'strftime') else str(entry_time)}"

        # Direction resolution (+1 = BUY, -1 = SELL)
        if isinstance(direction, int):
            self.direction_int = 1 if direction > 0 else -1
            self.is_buy = self.direction_int == 1
        else:
            self.is_buy = str(direction).upper() in ("BUY", "CALL", "LONG", "+1")
            self.direction_int = 1 if self.is_buy else -1

        # Initial SL and Target calculations
        if self.is_buy:
            self.sl = self.entry_price * (1.0 - sl_pct)
            self.target = self.entry_price * (1.0 + tgt_pct)
        else:
            self.sl = self.entry_price * (1.0 + sl_pct)
            self.target = self.entry_price * (1.0 - tgt_pct)

        self.initial_sl = self.sl
        self.initial_sl_price = self.sl
        self.current_sl_price = self.sl
        self.target_price = self.target

        self.trailing_active = False
        self.peak_price = self.entry_price
        self.peak_pnl_pips = 0.0
        self.peak_bar_idx = entry_bar_idx
        self.bars_held = 0
        self.ticks_held = 0
        self.bars_since_peak = 0
        self.total_swap_usd = 0.0
        self.mae_pips = 0.0
        self.mfe_pips = 0.0

        self.price_history: List[float] = [self.entry_price]
        self.exit_time: Optional[datetime] = None
        self.exit_price: Optional[float] = None
        self.result: Optional[str] = None
        self.pnl: Optional[float] = None
        self.pnl_pips: Optional[float] = None
        self.exit_reason_override: Optional[str] = None

        # Exit Agent initialization
        if rl_agent == "kalman":
            self.rl_agent = ForexKalmanEntropyExitAgent(pip_unit=self.pip_unit)
            self.rl_agent.reset(entry_price=self.entry_price)
            self.is_kalman = True
        elif isinstance(rl_agent, ForexKalmanEntropyExitAgent):
            self.rl_agent = rl_agent
            self.rl_agent.reset(entry_price=self.entry_price)
            self.is_kalman = True
        else:
            self.rl_agent = rl_agent
            self.is_kalman = False

        self.journey = [{
            "ts": str(entry_time),
            "price": round(self.entry_price, 5),
            "sl": round(self.sl, 5),
            "bars_held": 0,
        }]


    def _ratchet_trailing(
        self,
        tick_price: float,
        bars_held: int,
        current_bar_idx: Optional[int] = None,
        current_spread_pips: float = 1.0
    ) -> None:
        """Update peak_price and ratchet trailing SL upward (BUY) or downward (SELL)."""
        pip_size = self.pip_unit
        dir_val = self.direction_int

        raw_pips = ((tick_price - self.entry_price) if dir_val == 1 else (self.entry_price - tick_price)) / pip_size
        current_pips = round(raw_pips, 6)
        self.mfe_pips = max(self.mfe_pips, current_pips)
        self.mae_pips = min(self.mae_pips, current_pips)

        if current_pips > self.peak_pnl_pips:
            self.peak_pnl_pips = current_pips
            self.bars_since_peak = 0
            self.peak_price = tick_price
            if current_bar_idx is not None:
                self.peak_bar_idx = current_bar_idx
        elif current_bar_idx is not None and current_bar_idx > self.peak_bar_idx:
            self.bars_since_peak = current_bar_idx - self.peak_bar_idx

        # 4-Tier Profit Ratchet
        peak = self.peak_pnl_pips
        new_sl = self.current_sl_price

        if peak >= 50.0:          # Tier 1: Lock 80%
            lock_pips = peak * 0.80
            cand_sl = self.entry_price + (lock_pips * pip_size * dir_val)
            new_sl = max(new_sl, cand_sl) if dir_val == 1 else min(new_sl, cand_sl)
            self.trailing_active = True
        elif peak >= 35.0:        # Tier 2: Lock 70%
            lock_pips = peak * 0.70
            cand_sl = self.entry_price + (lock_pips * pip_size * dir_val)
            new_sl = max(new_sl, cand_sl) if dir_val == 1 else min(new_sl, cand_sl)
            self.trailing_active = True
        elif peak >= 20.0:        # Tier 3: Lock 55%
            lock_pips = peak * 0.55
            cand_sl = self.entry_price + (lock_pips * pip_size * dir_val)
            new_sl = max(new_sl, cand_sl) if dir_val == 1 else min(new_sl, cand_sl)
            self.trailing_active = True
        elif peak >= 10.0:        # Tier 4: Breakeven + 2x Spread buffer
            be_pips = max(1.0, 2.0 * current_spread_pips)
            cand_sl = self.entry_price + (be_pips * pip_size * dir_val)
            new_sl = max(new_sl, cand_sl) if dir_val == 1 else min(new_sl, cand_sl)
            self.trailing_active = True

        self.current_sl_price = new_sl
        self.sl = new_sl

        # Stagnation Defense Tightening (15-bar linear compression)
        if self.bars_since_peak >= 15:
            compression_pips = (self.bars_since_peak - 15) * 0.5
            compressed_sl = self.current_sl_price + (compression_pips * pip_size * dir_val)
            if dir_val == 1:
                self.current_sl_price = min(self.entry_price + (self.peak_pnl_pips * pip_size),
                                             max(self.current_sl_price, compressed_sl))
            else:
                self.current_sl_price = max(self.entry_price - (self.peak_pnl_pips * pip_size),
                                             min(self.current_sl_price, compressed_sl))
            self.sl = self.current_sl_price

    def check_exit_tick(
        self,
        minute_ts: pd.Timestamp,
        bar_idx: int,
        bars_held: int,
        macro_close: float = 0.0,
        underlying_latest: dict = None,
        kalman_velocity: float = 0.0,
        kalman_sigma_v: float = 0.0
    ) -> bool:
        """
        Walks intra-minute ticks chronologically.
        Enforces strict execution ordering:
          1. SL breach checked using prior self.current_sl_price
          2. Target check
          3. Kinematic Kalman adverse velocity check (|v_t| > 1.8 * sigma_v)
          4. Ratchet trailing stop using THIS tick (strictly AFTER SL check)
        """
        self.bars_held = bars_held
        window_end = minute_ts + pd.Timedelta(minutes=1)
        time_col = "timestamp" if "timestamp" in self.tick_df.columns else ("time" if "time" in self.tick_df.columns else self.tick_df.columns[0])
        window = self.tick_df[
            (self.tick_df[time_col] >= minute_ts) &
            (self.tick_df[time_col] < window_end)
        ]

        if window.empty:
            if bars_held >= self.max_hold_bars:
                last_price = self.price_history[-1] if self.price_history else self.entry_price
                exit_price = last_price * (1.0 - self.half_spread_pct) if self.is_buy else last_price * (1.0 + self.half_spread_pct)
                self._finalize_exit(current_minute=minute_ts, exit_price=exit_price, result="TIMEOUT")
                return True
            return False

        last_close = None
        exit_price = None
        result = None

        for _, tick in window.iterrows():
            self.ticks_held += 1
            price_col = "price" if "price" in tick else ("close" if "close" in tick else "premium")
            tick_price = float(tick[price_col])

            # Half-spread executable prices
            if "bid" in tick and tick["bid"] > 0:
                tick_bid = float(tick["bid"]) * (1.0 - self.half_spread_pct)
            else:
                tick_bid = tick_price * (1.0 - self.half_spread_pct)

            if "ask" in tick and tick["ask"] > 0:
                tick_ask = float(tick["ask"]) * (1.0 + self.half_spread_pct)
            else:
                tick_ask = tick_price * (1.0 + self.half_spread_pct)

            last_close = tick_price
            spread_pips = max(0.5, (tick_ask - tick_bid) / self.pip_unit)

            # 1. SL Check (Prior-state SL)
            if self.is_buy:
                if tick_bid <= self.current_sl_price:
                    exit_price = self.current_sl_price
                    result = self.exit_reason_override or ("TRAILING_SL" if self.trailing_active else "SL")
                    break
            else:
                if tick_ask >= self.current_sl_price:
                    exit_price = self.current_sl_price
                    result = self.exit_reason_override or ("TRAILING_SL" if self.trailing_active else "SL")
                    break

            # 2. Target Check
            if self.is_buy:
                if tick_bid >= self.target:
                    exit_price = self.target
                    result = "TARGET"
                    break
            else:
                if tick_ask <= self.target:
                    exit_price = self.target
                    result = "TARGET"
                    break

            # 3. Kinematic Kalman Velocity Veto
            if self.ticks_held > 10 and kalman_sigma_v > 0:
                if self.direction_int == 1 and kalman_velocity < (-1.8 * kalman_sigma_v):
                    exit_price = tick_bid
                    result = "KALMAN_VELOCITY_REVERSAL"
                    break
                elif self.direction_int == -1 and kalman_velocity > (1.8 * kalman_sigma_v):
                    exit_price = tick_ask
                    result = "KALMAN_VELOCITY_REVERSAL"
                    break

            # Dynamic Exit Agent update
            if getattr(self, "is_kalman", False):
                tick_mid = (tick_bid + tick_ask) / 2.0
                is_valid = self.rl_agent.update(
                    price=tick_mid,
                    bid=tick_bid,
                    ask=tick_ask,
                    direction=self.direction if isinstance(self.direction, str) else ("BUY" if self.direction_int == 1 else "SELL")
                )
                if is_valid:
                    t_val = tick[time_col]
                    tick_dt = t_val.to_pydatetime() if hasattr(t_val, "to_pydatetime") else t_val
                    action, new_sl = self.rl_agent.decide(
                        entry_price=self.entry_price,
                        current_price=tick_mid,
                        current_sl=self.current_sl_price,
                        tick_time=tick_dt,
                        direction=self.direction if isinstance(self.direction, str) else ("BUY" if self.direction_int == 1 else "SELL")
                    )
                    if action == "EARLY_EXIT":
                        exit_price = tick_bid if self.is_buy else tick_ask
                        result = "KALMAN_EXIT"
                        break
                    elif action == "TIGHTEN":
                        if self.is_buy and new_sl > self.current_sl_price:
                            self.current_sl_price = new_sl
                            self.sl = new_sl
                            self.exit_reason_override = "KALMAN_TIGHTEN"
                            self.trailing_active = True
                        elif not self.is_buy and new_sl < self.current_sl_price:
                            self.current_sl_price = new_sl
                            self.sl = new_sl
                            self.exit_reason_override = "KALMAN_TIGHTEN"
                            self.trailing_active = True

            # 4. Ratchet trailing stop using THIS tick's price (strictly AFTER SL check)
            self._ratchet_trailing(tick_price, bars_held, current_bar_idx=bar_idx, current_spread_pips=spread_pips)

        # Record journey point for this minute
        if last_close is not None:
            self.price_history.append(last_close)
            self.journey.append({
                "ts": str(minute_ts),
                "price": round(last_close, 5),
                "sl": round(self.current_sl_price, 5),
                "bars_held": bars_held,
            })

        # Timeout Check
        if exit_price is None and bars_held >= self.max_hold_bars:
            last_p = last_close or self.entry_price
            exit_price = last_p * (1.0 - self.half_spread_pct) if self.is_buy else last_p * (1.0 + self.half_spread_pct)
            result = "TIMEOUT"

        if exit_price is not None:
            self._finalize_exit(current_minute=minute_ts, exit_price=exit_price, result=result)
            return True

        return False

    def _finalize_exit(self, current_minute: pd.Timestamp, exit_price: float, result: str):
        self.exit_time = current_minute
        self.exit_price = round(exit_price, 5)
        self.result = result

        # Forex PnL calculation
        if self.is_buy:
            price_delta = exit_price - self.entry_price
        else:
            price_delta = self.entry_price - exit_price

        self.pnl_pips = round(price_delta / self.pip_unit, 2)
        gross_pnl = price_delta * self.contract_size * self.lot_size
        total_commission = self.commission_per_lot * self.lot_size
        self.pnl = round(gross_pnl - total_commission + self.total_swap_usd, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "direction": "BUY" if self.direction_int == 1 else "SELL",
            "direction_int": self.direction_int,
            "strategy": self.strategy,
            "strategy_name": self.strategy_name,
            "regime": self.regime,
            "regime_at_entry": self.regime_at_entry,
            "entry_time": str(self.entry_time),
            "exit_time": str(self.exit_time),
            "open_time": str(self.open_time),
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "lot_size": self.lot_size,
            "lots": self.lot_size,
            "units": self.units,
            "sl": round(self.initial_sl, 5),
            "initial_sl_price": round(self.initial_sl, 5),
            "current_sl_price": round(self.current_sl_price, 5),
            "target": round(self.target, 5),
            "target_price": round(self.target, 5),
            "pnl": self.pnl,
            "pnl_usd": self.pnl,
            "pnl_pips": self.pnl_pips,
            "mae_pips": round(self.mae_pips, 2),
            "mfe_pips": round(self.mfe_pips, 2),
            "peak_pnl_pips": round(self.peak_pnl_pips, 2),
            "bars_held": self.bars_held,
            "ticks_held": self.ticks_held,
            "bars_since_peak": self.bars_since_peak,
            "result": self.result,
            "exit_reason": self.result,
            "total_swap_usd": self.total_swap_usd,
            "journey": self.journey
        }

