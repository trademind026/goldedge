"""
INSTITUTIONAL FOREX TICK-LEVEL REPLAY BACKTEST ENGINE
Adapted for high-frequency / quantitative currency and CFD trading.

=============================================================================
High-Level Architecture Flowchart:

[Raw Historical Ticks] (Sargable Query)
         │
         ▼
[Phase 1: Pre-Grouping 1m Buckets] + [Inject 250-Bar Warm-Up Buffer]
         │
         ▼
┌────────────────── Loop over Chronological Minutes ──────────────────┐
│                                                                     │
│  Phase 2: Active Trade Execution (Sub-Bar Intra-Tick Stepping)      │
│  - Prior SL Evaluation (Tick k+1 checks against level from Tick k)   │
│  - Spread Deduction (Bid/Ask half-spread slippage)                  │
│  - Kalman Dynamic Exit (ForexKalmanEntropyExitAgent early exit)      │
│  - Tiered Trailing Stop Ratchet (80% / 70% / 55% / 45% + Stagnation)│
│                                                                     │
│  Phase 3: Candle Synthesis & Feature Pipeline                        │
│  - Synthetic 1m Candle: Open=Tick_0, High=Max, Low=Min, Close=Tick_-1│
│  - 2D Kinematic Kalman Velocity + Market Regime Classification       │
│                                                                     │
│  Phase 4: Risk Gates & Safety Circuit Breakers                       │
│  - Daily Drawdown Cap (Cumulative Loss <= daily_loss_limit -> Halt)  │
│  - Consecutive Loss Cooling-Off (2 SL hits -> 45-bar pause)          │
│  - Ladder Cooldown (15 bars cooldown per strategy/direction)         │
│                                                                     │
│  Phase 5: Multi-Tiered Signal Gating                                 │
│  - Active Strategy Signal Candidate                                 │
│  - Quant Hard Veto (Score < 0.80 -> Discard)                         │
│  - Microstructure Confirmation (Tick momentum / Order Flow)          │
│  - LightGBM Meta-Probability Composite Score >= Threshold           │
│                                                                     │
│  Phase 6: Sizing & Entry Fill                                        │
│  - Half-Kelly: f* = ((p*b - q)/b) * 0.5 * RegimeMult                 │
│  - Affordability Guard (Cash Equity & Free Margin Buffer)            │
│  - Fill at Ask (BUY) or Bid (SELL) with Half-Spread Slippage         │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
         │
         ▼
Phase 7: EOD Settlement & Metric Generation
- EOD Force-Close Open Position at Terminal Bid/Ask
- Tearsheet Compilation: Win Rate %, Profit Factor, Sharpe & Sortino, Max Drawdown
=============================================================================
"""

import sys
import os
import argparse
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Tuple, Optional
import pandas as pd
import numpy as np

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.goldedge.domain.models.forex_open_trade import ForexOpenTrade, HALF_SPREAD_PCT
from src.goldedge.domain.models.pip_calculator import PipCalculator
from src.goldedge.domain.services.forex_adaptive_sizing import forex_adaptive_position_size
from src.goldedge.application.use_cases.run_tick_replay import (
    ForexTickReplayEngine,
    check_forex_micro_confirmation
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("tick_replay")


def generate_synthetic_forex_ticks(
    symbol: str = "XAUUSD",
    start_price: Optional[float] = None,
    n_minutes: int = 180,
    ticks_per_minute: int = 15
) -> pd.DataFrame:
    """Generates synthetic high-resolution tick data with intraday trends and noise."""
    np.random.seed(42)
    t0 = datetime(2026, 11, 10, 8, 0, 0, tzinfo=timezone.utc)
    records = []
    
    is_gold = "XAU" in symbol.upper() or "GOLD" in symbol.upper()
    current_price = start_price if start_price is not None else (2400.0 if is_gold else 1.0850)

    for m in range(n_minutes):
        minute_ts = t0 + timedelta(minutes=m)
        minute_drift = np.random.normal(0.15, 0.40) if is_gold else np.random.normal(0.00003, 0.00015)
        for s in range(ticks_per_minute):
            tick_ts = minute_ts + timedelta(seconds=s * (60.0 / ticks_per_minute))
            tick_shock = np.random.normal(0.0, 0.08) if is_gold else np.random.normal(0.0, 0.00004)
            current_price += (minute_drift / ticks_per_minute) + tick_shock
            spread = 0.25 if is_gold else 0.00012
            bid = current_price - (spread / 2.0)
            ask = current_price + (spread / 2.0)
            decimals = 2 if is_gold else 5
            records.append({
                "timestamp": tick_ts,
                "symbol": symbol,
                "price": round(current_price, decimals),
                "bid": round(bid, decimals),
                "ask": round(ask, decimals),
                "volume": np.random.randint(10, 200)
            })

    return pd.DataFrame(records)


def query_sargable_ticks_from_db(
    engine,
    symbol: str,
    replay_date: str
) -> pd.DataFrame:
    """
    Phase 1: Sargable tick query using indexed boundary timestamps.
    Eliminates full table scans on multi-gigabyte TimescaleDB tables.
    """
    from sqlalchemy import text
    query = text("""
        SELECT timestamp, price, volume, bid, ask
        FROM market_ticks
        WHERE symbol = :symbol
          AND timestamp >= CAST(:dt AS date)
          AND timestamp < (CAST(:dt AS date) + INTERVAL '1 day')
        ORDER BY timestamp ASC
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"symbol": symbol, "dt": replay_date})
    return df


def query_warmup_candles_from_db(
    engine,
    symbol: str,
    replay_date: str,
    lookback_bars: int = 250
) -> pd.DataFrame:
    """
    Phase 1: Injects historical bars prior to session open to eliminate indicator cold-start distortion.
    """
    from sqlalchemy import text
    query = text("""
        SELECT time, open, high, low, close, tick_volume as volume
        FROM market_bars_1m
        WHERE symbol = :symbol
          AND time < CAST(:dt AS date)
        ORDER BY time DESC
        LIMIT :limit
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"symbol": symbol, "dt": replay_date, "limit": lookback_bars})
    if not df.empty:
        df = df.sort_values("time").reset_index(drop=True)
    return df


def run_tick_replay(
    replay_date: Optional[str] = None,
    symbol: str = "EURUSD",
    initial_equity: float = 100000.0,
    tick_df: Optional[pd.DataFrame] = None,
    warmup_candles: Optional[pd.DataFrame] = None,
    meta_labeler: Any = None,
    verbose: bool = True,
    output_csv_path: Optional[str] = "forex_audit_log.csv"
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Main Simulation Entrypoint.
    Executes all 8 institutional phases.
    """
    if tick_df is None or tick_df.empty:
        logger.info(f"Generating synthetic {symbol} ticks for demo replay...")
        tick_df = generate_synthetic_forex_ticks(symbol=symbol, n_minutes=180, ticks_per_minute=20)

    engine = ForexTickReplayEngine(
        symbol=symbol,
        initial_equity=initial_equity,
        daily_loss_limit=-1500.0,
        consecutive_loss_trigger=2,
        consecutive_loss_cooldown_bars=45,
        ladder_cooldown_bars=15,
        contract_size=100000.0 if "XAU" not in symbol else 100.0,
        commission_per_lot=5.0,
        sl_pct=0.0030,
        tgt_pct=0.0060,
        max_hold_bars=40,
        quant_score_threshold=0.80,
        final_score_threshold=0.75
    )

    trades, metrics = engine.run(
        tick_df=tick_df,
        warmup_candles=warmup_candles,
        meta_labeler=meta_labeler,
        verbose=verbose,
        output_csv_path=output_csv_path
    )

    logger.info("=" * 70)
    logger.info("      FOREX INSTITUTIONAL PERFORMANCE TEARSHEET      ")
    logger.info("=" * 70)
    logger.info(f"Symbol:                 {symbol}")
    logger.info(f"Total Closed Trades:    {metrics.get('total_trades', 0)}")
    logger.info(f"Win Rate:               {metrics.get('win_rate', 0.0)}%")
    logger.info(f"Profit Factor:          {metrics.get('profit_factor', 0.0)}")
    logger.info(f"Expectancy Per Trade:   ${metrics.get('expectancy_usd', 0.0):,.2f}")
    logger.info(f"Gross Profit:           ${metrics.get('gross_profit', 0.0):,.2f}")
    logger.info(f"Gross Loss:             ${metrics.get('gross_loss', 0.0):,.2f}")
    logger.info(f"Net Realized PnL:       ${metrics.get('net_pnl', 0.0):,.2f}")
    logger.info(f"Final Account Equity:   ${metrics.get('final_equity', 0.0):,.2f}")
    logger.info(f"Max Drawdown:           ${metrics.get('max_drawdown', 0.0):,.2f} ({metrics.get('max_drawdown_pct', 0.0)}%)")
    logger.info(f"Calmar Ratio:           {metrics.get('calmar_ratio', 0.0)}")
    logger.info(f"Annualized Sharpe:      {metrics.get('sharpe_ratio', 0.0)}")
    logger.info(f"Annualized Sortino:     {metrics.get('sortino_ratio', 0.0)}")
    logger.info(f"Average MAE (Pips):     {metrics.get('avg_mae_pips', 0.0):.2f}")
    logger.info(f"Average MFE (Pips):     {metrics.get('avg_mfe_pips', 0.0):.2f}")
    logger.info("=" * 70)

    if output_csv_path:
        logger.info(f"[+] Full trade audit log saved to {output_csv_path}")

    if trades:
        logger.info("\nExecution Log Samples (First 5 Trades):")
        for i, t in enumerate(trades[:5], 1):
            logger.info(
                f"[{i}] {t['direction']} {t['symbol']} | In: {t['entry_price']:.5f} | Out: {t['exit_price']:.5f} | "
                f"PnL: ${t['pnl']:,.2f} ({t['pnl_pips']:+.1f} pips) | Exit: {t.get('exit_reason', t.get('result'))} | MAE: {t.get('mae_pips', 0.0):.1f}p | MFE: {t.get('mfe_pips', 0.0):.1f}p"
            )

    return trades, metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Institutional Forex Tick Replay Backtest Engine")
    parser.add_argument("--demo", action="store_true", default=True, help="Run simulation on synthetic ticks")
    parser.add_argument("--symbol", type=str, default="XAUUSD", help="Forex/Gold symbol to replay (default: XAUUSD)")
    parser.add_argument("--equity", type=float, default=100000.0, help="Initial account equity")
    parser.add_argument("--date", type=str, default=None, help="Replay date (YYYY-MM-DD) for database query")
    parser.add_argument("--csv", type=str, default=None, help="Path to tick CSV file")
    parser.add_argument("--export-audit", type=str, default="forex_audit_log.csv", help="Path to export full tick audit CSV")
    args = parser.parse_args()

    input_ticks = None
    warmup_candles = None

    if args.csv and os.path.exists(args.csv):
        logger.info(f"Loading ticks from CSV: {args.csv}")
        input_ticks = pd.read_csv(args.csv)
    elif args.date:
        try:
            from sqlalchemy import create_engine
            from src.goldedge.config.settings import Settings
            settings = Settings()
            engine = create_engine(settings.DB_URL)
            logger.info(f"Executing sargable query for {args.symbol} on {args.date}...")
            input_ticks = query_sargable_ticks_from_db(engine, args.symbol, args.date)
            warmup_candles = query_warmup_candles_from_db(engine, args.symbol, args.date)
            logger.info(f"Loaded {len(input_ticks)} raw ticks and {len(warmup_candles)} warm-up candles.")
        except Exception as e:
            logger.warning(f"Database query failed ({e}); falling back to synthetic ticks.")
            input_ticks = None

    run_tick_replay(
        replay_date=args.date,
        symbol=args.symbol,
        initial_equity=args.equity,
        tick_df=input_ticks,
        warmup_candles=warmup_candles,
        verbose=True,
        output_csv_path=args.export_audit
    )

