import logging
import pandas as pd
import json
from pathlib import Path
from datetime import datetime
from tqdm import tqdm

from ...domain.ports.outbound.tick_repo import ITickRepository
from ...domain.services.denoise_service import VolumeAdaptiveKalmanFilter
from ...domain.services.indicator_service import IndicatorService
from ...domain.services.regime_detector import RegimeDetectorService
from ...domain.services.signal_generator import SignalGeneratorService
from ...domain.services.trade_scorer import TradeScorer
from ...domain.models.trade import Position, TradeStatus
from ...config.risk_profiles import get_profile

logger = logging.getLogger("backtest_engine")

class RunBacktestUseCase:
    """
    High-Fidelity Tick-Replay Backtest Engine.
    Mirrors the AI-trader 'replay_day' continuous tick stream flow.
    Every tick is processed by the Kalman filter to maintain perfect state.
    """
    def __init__(self, tick_repo: ITickRepository):
        self.tick_repo = tick_repo
        self.profile = get_profile("MEDIUM_BACKTEST")
        
        self.regime_detector = RegimeDetectorService()
        self.trade_scorer = TradeScorer(w_ml=0.0, w_tech=1.0) # Quant Kalman acts as bonus/penalty

    def execute(self, symbol: str, start: datetime, end: datetime, initial_capital: float = 10000.0):
        print(f"\n{'='*60}")
        print(f"  TICK REPLAY BACKTEST - STARTING")
        print(f"  Symbol: {symbol} | Period: {start.date()} to {end.date()}")
        print(f"{'='*60}")

        # 1. Fetch All Ticks
        print("  [1/3] Fetching ticks from database...")
        ticks_df = self.tick_repo.get_ticks_as_df(symbol, start, end)
        if ticks_df.empty:
            print("  No ticks found in date range.")
            return

        print(f"  Loaded {len(ticks_df):,} ticks.")
        
        # 2. Setup State
        kalman_filter = VolumeAdaptiveKalmanFilter()
        candle_buffer = []
        open_trade = None
        all_trades = []
        
        current_minute = -1
        current_bar = {"time": None, "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0}

        print("  [2/3] Replaying ticks chronologically...")
        
        # We use itertuples for fast iteration
        for row in tqdm(ticks_df.itertuples(), total=len(ticks_df)):
            dt = row.time
            price = row.bid
            vol = row.volume
            
            # --- 1. Continuous Micro-Feature Update ---
            kalman_filter.update_tick(price, vol)
            
            # --- 2. Check Open Trade (Stop Loss / Take Profit on every tick) ---
            if open_trade:
                pnl = (price - open_trade["entry_price"]) * open_trade["qty"]
                if open_trade["direction"] == "SELL":
                    pnl = -pnl
                    
                if price <= open_trade["sl"] if open_trade["direction"] == "BUY" else price >= open_trade["sl"]:
                    open_trade["exit_price"] = price
                    open_trade["pnl"] = pnl
                    open_trade["result"] = "SL"
                    all_trades.append(open_trade)
                    open_trade = None
                elif price >= open_trade["tp"] if open_trade["direction"] == "BUY" else price <= open_trade["tp"]:
                    open_trade["exit_price"] = price
                    open_trade["pnl"] = pnl
                    open_trade["result"] = "TP"
                    all_trades.append(open_trade)
                    open_trade = None

            # --- 3. Minute Bar Aggregation ---
            if dt.minute != current_minute:
                if current_minute != -1:
                    # Finalize Bar
                    candle_buffer.append(current_bar)
                    # Keep max 150 bars for indicator calculation
                    if len(candle_buffer) > 150:
                        candle_buffer.pop(0)
                        
                    # Evaluate Trading Logic at the close of the minute
                    if len(candle_buffer) >= 60 and not open_trade:
                        df_bars = pd.DataFrame(candle_buffer)
                        df_ind = IndicatorService.compute_indicators(df_bars)
                        regime = self.regime_detector.detect(df_ind)
                        signals = SignalGeneratorService.generate_signals(df_ind, symbol, regime)
                        
                        kalman_features = {
                            "kalman_vel": kalman_filter.get_velocity(),
                            "kalman_accel": kalman_filter.get_acceleration()
                        }
                        
                        scored = self.trade_scorer.filter_and_rank(
                            signals, 
                            min_score_threshold=self.profile.min_score_threshold,
                            regime=regime.value,
                            features=kalman_features
                        )
                        
                        if scored:
                            best = scored[0]
                            latest_atr = df_ind["atr"].iloc[-1]
                            
                            sl = best.entry_price - (latest_atr * self.profile.atr_sl_multiplier) if best.direction.value == "BUY" else best.entry_price + (latest_atr * self.profile.atr_sl_multiplier)
                            tp = best.entry_price + (latest_atr * self.profile.atr_tp_multiplier) if best.direction.value == "BUY" else best.entry_price - (latest_atr * self.profile.atr_tp_multiplier)
                            
                            # Standard lot risk sizing ($100 per pip per lot standard)
                            # Simulated sizing logic
                            qty = 1.0 
                            
                            open_trade = {
                                "entry_time": dt,
                                "symbol": symbol,
                                "direction": best.direction.value,
                                "strategy": best.strategy,
                                "entry_price": best.entry_price,
                                "qty": qty,
                                "sl": sl,
                                "tp": tp,
                                "score": best.final_score
                            }

                # Start new bar
                current_bar = {"time": dt.replace(second=0, microsecond=0), "open": price, "high": price, "low": price, "close": price, "volume": vol}
                current_minute = dt.minute
            else:
                # Update current bar
                current_bar["high"] = max(current_bar["high"], price)
                current_bar["low"] = min(current_bar["low"], price)
                current_bar["close"] = price
                current_bar["volume"] += vol


        # 3. Final Report Generation
        print("\n" + "=" * 60)
        print("  TICK REPLAY BACKTEST - FINAL REPORT")
        print("=" * 60)

        if not all_trades:
            print("  No trades executed.")
            return

        df_trades = pd.DataFrame(all_trades)
        total_pnl = df_trades["pnl"].sum()
        n = len(df_trades)
        wins = (df_trades["pnl"] > 0).sum()
        losses = n - wins
        
        # Calculate Margin Used (Assuming 1:100 leverage on $2500 per lot XAUUSD approx)
        df_trades["margin_used"] = df_trades["qty"] * 2500 
        
        df_trades["cum_pnl"] = df_trades["pnl"].cumsum()
        dd = (df_trades["cum_pnl"] - df_trades["cum_pnl"].cummax()).min()
        
        max_margin = df_trades["margin_used"].max() if not df_trades.empty else 0
        required_capital = max_margin + abs(dd)
        roi = (total_pnl / required_capital * 100) if required_capital > 0 else 0.0

        print(f"\n  Total trades:      {n}")
        print(f"\n  Total P&L:         ${total_pnl:+,.2f}")
        print(f"  Avg P&L / trade:   ${df_trades['pnl'].mean():+,.2f}")
        print(f"\n  Wins:              {wins} ({wins/n*100:.0f}%)")
        print(f"  Losses:            {losses} ({losses/n*100:.0f}%)")
        
        print(f"\n  Max margin used:   ${max_margin:,.2f}")
        print(f"  Max drawdown:      ${dd:+,.2f}")
        print(f"  Required Capital:  ${required_capital:,.2f}")
        print(f"  Peak equity:       ${df_trades['cum_pnl'].max():+,.2f}")
        print(f"  Total ROI:         {roi:,.1f}%")

        print(f"\n  {'Strategy':<30s} {'Trades':>6s} {'WR':>5s} {'Total P&L':>10s}")
        print(f"  {'-'*30} {'-'*6} {'-'*5} {'-'*10}")
        for strat, g in df_trades.groupby("strategy"):
            wr = (g["pnl"] > 0).mean() * 100
            print(f"  {strat:<30s} {len(g):>6d} {wr:>4.0f}% {g['pnl'].sum():>+10,.2f}")

        # Export CSV
        out_dir = Path("backtest_results")
        out_dir.mkdir(exist_ok=True)
        csv_path = out_dir / "tick_replay_trades.csv"
        df_trades.to_csv(csv_path, index=False)
        print(f"\n  Trades exported to {csv_path}")
        print("=" * 60)
