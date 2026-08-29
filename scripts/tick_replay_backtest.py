import argparse
import pandas as pd
import logging
from datetime import datetime, timedelta

# Mocking domain imports for standalone script execution
from src.goldedge.domain.models.kalman_state import KalmanState

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tick_replay")

class TickReplayEngine:
    """
    High-fidelity intra-minute simulator.
    Evaluates every tick against open positions to prevent intra-minute
    illusion wins (where a candle hits both SL and TP, and standard backtesters guess the order).
    """
    def __init__(self, stagnation_minutes: int = 15):
        self.stagnation_minutes = stagnation_minutes
        self.open_trades = []
        self.closed_trades = []
        
    def _ratchet_trailing(self, state: KalmanState, tick_time: datetime, current_price: float):
        """Stagnation Boost Trailing Stop (Ported from AI-trader)"""
        # Update peak profitability
        if state.direction == "BUY":
            if current_price > state.max_favorable_price:
                state.max_favorable_price = current_price
                state.last_update_time = tick_time
        else:
            if current_price < state.max_favorable_price:
                state.max_favorable_price = current_price
                state.last_update_time = tick_time

        # Check for stagnation
        if state.last_update_time:
            stagnation = (tick_time - state.last_update_time).total_seconds() / 60.0
            
            # If in profit but stagnating, tighten SL aggressively
            if stagnation > self.stagnation_minutes and state.current_profit_ticks(current_price) > 0:
                if state.direction == "BUY":
                    new_sl = current_price - 0.5 # Tighten to just below current price
                    if new_sl > state.current_sl:
                        state.current_sl = new_sl
                else:
                    new_sl = current_price + 0.5
                    if new_sl < state.current_sl:
                        state.current_sl = new_sl

    def _check_exit_tick(self, state: KalmanState, tick_time: datetime, current_price: float) -> bool:
        """Returns True if trade is closed."""
        self._ratchet_trailing(state, tick_time, current_price)
        
        if state.direction == "BUY":
            if current_price >= state.target_price:
                logger.info(f"[{tick_time}] WIN (TP Hit) @ {current_price}")
                return True
            if current_price <= state.current_sl:
                logger.info(f"[{tick_time}] LOSS (SL Hit) @ {current_price}")
                return True
        else:
            if current_price <= state.target_price:
                logger.info(f"[{tick_time}] WIN (TP Hit) @ {current_price}")
                return True
            if current_price >= state.current_sl:
                logger.info(f"[{tick_time}] LOSS (SL Hit) @ {current_price}")
                return True
                
        return False

    def run(self, tick_df: pd.DataFrame):
        logger.info(f"Starting Replay on {len(tick_df)} ticks.")
        
        # In a real backtest, you'd generate entry signals based on the Kalman features here.
        # For demonstration, we assume a random trade is opened.
        
        for idx, row in tick_df.iterrows():
            tick_time = row['timestamp']
            price = row['price']
            
            # Check open trades exactly on this tick
            for trade in self.open_trades[:]:
                if self._check_exit_tick(trade, tick_time, price):
                    self.closed_trades.append(trade)
                    self.open_trades.remove(trade)
                    
        logger.info(f"Backtest Complete. Total Trades: {len(self.closed_trades)}")

if __name__ == "__main__":
    import sys
    import os
    from sqlalchemy import create_engine
    
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
    from src.goldedge.config.settings import Settings
    
    settings = Settings()
    engine = create_engine(settings.DB_URL)
    
    logger.info("Fetching historical ticks from TimescaleDB for backtest...")
    # Normally you'd chunk this or specify a date range
    query = "SELECT * FROM xau_ticks ORDER BY timestamp ASC LIMIT 1000000"
    df = pd.read_sql(query, engine)
    
    if df.empty:
        logger.error("No ticks found in DB. Run fetch_history_cli.py first.")
    else:
        logger.info(f"Loaded {len(df)} ticks from database.")
        # engine_bt = TickReplayEngine()
        # engine_bt.run(df)
    
    logger.info("Tick Replay Backtester is ready.")
