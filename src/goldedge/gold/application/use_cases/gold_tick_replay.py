import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import numpy as np
import pandas as pd

from ...domain.models.gold_pip_engine import GoldPipEngine
from ...domain.models.gold_open_trade import GoldOpenTrade
from ...domain.services.gold_decision_rules import GoldDecisionRules
from ...domain.services.gold_adaptive_sizing import gold_adaptive_position_size
from ...adapters.features.gold_kalman_filter import apply_gold_adaptive_kalman

logger = logging.getLogger("gold_tick_replay")


class GoldTickReplayUseCase:
    """
    Sub-Bar Intra-Tick Historical Replay Engine specifically dedicated to Spot Gold (XAUUSD).
    Enforces all institutional quant rules:
    - Zero look-ahead bias (indicators on closed candles, execution on intra-minute ticks).
    - Half-spread bid/ask slippage modeling.
    - Strict execution order: SL checked before peak ratchet.
    - 3-stage circuit breakers (consecutive loss cooldown, ladder cooldown, daily loss breaker).
    - GoldPipEngine 100 oz contract cash accounting.
    """

    def __init__(
        self,
        symbol: str = "XAUUSD",
        initial_equity: float = 10000.0,
        daily_loss_limit: float = 1500.0,
        consecutive_loss_limit: int = 2,
        cooldown_bars: int = 45,
        ladder_cooldown_bars: int = 15,
        half_spread_dollars: float = 0.15
    ):
        self.symbol = symbol
        self.initial_equity = initial_equity
        self.daily_loss_limit = daily_loss_limit
        self.consecutive_loss_limit = consecutive_loss_limit
        self.cooldown_bars = cooldown_bars
        self.ladder_cooldown_bars = ladder_cooldown_bars
        self.half_spread_dollars = half_spread_dollars

    def run(
        self,
        ticks_df: pd.DataFrame,
        meta_probabilities: Optional[Dict[datetime, float]] = None
    ) -> Dict[str, Any]:
        """
        Executes sub-bar tick replay over historical tick dataframe.
        Expected columns in ticks_df: ['timestamp', 'price', 'volume'] (optional 'bid', 'ask')
        """
        if ticks_df.empty:
            return {"trades": 0, "win_rate": 0.0, "net_pnl": 0.0, "profit_factor": 0.0}

        ticks = ticks_df.sort_values("timestamp").copy()
        ticks["minute"] = ticks["timestamp"].dt.floor("min")

        # 1. Aggregate into 1-minute candles for macro indicators
        candles = ticks.groupby("minute").agg(
            open=("price", "first"),
            high=("price", "max"),
            low=("price", "min"),
            close=("price", "last"),
            volume=("volume", "sum")
        ).reset_index()

        # Kalman Filter on 1-min candles
        kalman_df = apply_gold_adaptive_kalman(candles["close"], candles["volume"])
        candles["kalman_price"] = kalman_df["kalman_price"].values
        candles["kalman_vel"] = kalman_df["kalman_velocity"].values
        vel_std = (
            candles["kalman_vel"]
            .shift(1)
            .expanding(min_periods=20)
            .std()
            .clip(lower=1e-4)
        )
        candles["kalman_vel_z"] = (candles["kalman_vel"] / vel_std).fillna(0.0)

        # State Tracking
        equity = self.initial_equity
        daily_pnl = 0.0
        consecutive_losses = 0
        cooldown_counter = 0
        ladder_cooldowns: Dict[str, int] = {}

        open_trade: Optional[GoldOpenTrade] = None
        closed_trades: List[GoldOpenTrade] = []
        equity_curve: List[Dict[str, Any]] = []

        grouped_ticks = ticks.groupby("minute")

        for idx, row in candles.iterrows():
            bar_time = row["minute"]
            bar_ticks = grouped_ticks.get_group(bar_time) if bar_time in grouped_ticks.groups else pd.DataFrame()

            # Decrement ladder cooldowns
            for key in list(ladder_cooldowns.keys()):
                ladder_cooldowns[key] = max(0, ladder_cooldowns[key] - 1)
                if ladder_cooldowns[key] == 0:
                    del ladder_cooldowns[key]

            # Decrement consecutive loss cooldown
            if cooldown_counter > 0:
                cooldown_counter -= 1

            # Phase 1: Intra-Tick Trade Management for Open Position
            if open_trade is not None and not bar_ticks.empty:
                for _, t_row in bar_ticks.iterrows():
                    t_time = t_row["timestamp"]
                    t_price = float(t_row["price"])
                    t_bid = float(t_row.get("bid", t_price - self.half_spread_dollars))
                    t_ask = float(t_row.get("ask", t_price + self.half_spread_dollars))

                    remains_open = open_trade.process_tick(t_time, t_price, t_bid, t_ask)
                    if not remains_open:
                        # Trade closed
                        equity += open_trade.realised_pnl or 0.0
                        daily_pnl += open_trade.realised_pnl or 0.0
                        closed_trades.append(open_trade)

                        if (open_trade.realised_pnl or 0.0) < 0:
                            consecutive_losses += 1
                            if consecutive_losses >= self.consecutive_loss_limit:
                                cooldown_counter = self.cooldown_bars
                        else:
                            consecutive_losses = 0

                        ladder_cooldowns[f"{open_trade.strategy}_{open_trade.direction}"] = self.ladder_cooldown_bars
                        open_trade = None
                        break

            # If still open, log bar snapshot
            if open_trade is not None:
                open_trade.on_bar_closed(bar_time, float(row["close"]))

            # Phase 2: Check Circuit Breakers
            if daily_pnl <= -self.daily_loss_limit:
                continue  # Intraday drawdown halted
            if cooldown_counter > 0:
                continue  # In consecutive loss pause

            # Phase 3: Signal Generation on Closed Bar
            if open_trade is None and idx >= 5:
                vel_z = float(row["kalman_vel_z"])
                meta_prob = (meta_probabilities or {}).get(bar_time, 0.65 if abs(vel_z) > 0.6 else 0.50)

                direction = "BUY" if vel_z > 0.5 else "SELL" if vel_z < -0.5 else None
                if direction:
                    ladder_key = f"Quant_Gold_Kalman_{direction}"
                    if ladder_cooldowns.get(ladder_key, 0) == 0 and meta_prob >= 0.62:
                        entry_price = float(row["close"])
                        sl_price = entry_price - 2.00 if direction == "BUY" else entry_price + 2.00
                        tp_price = entry_price + 4.00 if direction == "BUY" else entry_price - 4.00

                        lots, _, _ = gold_adaptive_position_size(
                            account_equity=equity,
                            win_rate=0.55,
                            win_loss_ratio=1.8,
                            current_price=entry_price,
                            sl_price=sl_price,
                            regime="TRENDING",
                            contract_size_oz=GoldPipEngine.CONTRACT_SIZE_OZ
                        )

                        if lots > 0:
                            open_trade = GoldOpenTrade(
                                id=len(closed_trades) + 1,
                                entry_time=bar_time,
                                symbol=self.symbol,
                                direction=direction,
                                entry_price=entry_price,
                                initial_sl=sl_price,
                                target_price=tp_price,
                                lots=lots,
                                half_spread_dollars=self.half_spread_dollars
                            )

            equity_curve.append({"time": bar_time.isoformat(), "equity": equity})

        # Summary Metrics
        pnls = [t.realised_pnl or 0.0 for t in closed_trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        total_pnl = sum(pnls)
        win_rate = (len(wins) / len(pnls)) * 100.0 if pnls else 0.0
        profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else (99.0 if wins else 0.0)

        # Max Drawdown
        peak = self.initial_equity
        max_dd = 0.0
        for pt in equity_curve:
            eq = pt["equity"]
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd

        return {
            "symbol": self.symbol,
            "trades": len(closed_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 2),
            "profit_factor": round(profit_factor, 2),
            "net_pnl": round(total_pnl, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "final_equity": round(equity, 2),
            "equity_curve": equity_curve,
            "trade_list": [
                {
                    "id": t.id,
                    "direction": t.direction,
                    "entry_time": t.entry_time.isoformat(),
                    "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "lots": t.lots,
                    "pnl": t.realised_pnl,
                    "reason": t.exit_reason
                }
                for t in closed_trades
            ]
        }
