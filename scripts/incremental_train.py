import os
import sys
import argparse
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
import numpy as np
import pandas as pd
import joblib

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from src.goldedge.infrastructure.ml.macro_feature_engine import MacroFeatureEngine
from src.goldedge.infrastructure.ml.triple_barrier_labeler import DynamicATRTripleBarrierLabeler
from src.goldedge.infrastructure.ml.forex_meta_model import train_forex_macro_model
from src.goldedge.adapters.secondary.db.connection_pool import build_engine
from src.goldedge.adapters.secondary.db.candle_repository import CandleRepository
from src.goldedge.config.settings import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("incremental_train")


def log_database_audit(model_name: str, samples_count: int, status: str = "SUCCESS"):
    """
    Logs an audit record to DB or audit log file.
    """
    audit_file = "models/saved/model_audit_log.csv"
    os.makedirs(os.path.dirname(audit_file), exist_ok=True)
    header = not os.path.exists(audit_file)
    with open(audit_file, "a") as f:
        if header:
            f.write("timestamp,model_name,samples_count,status\n")
        f.write(f"{datetime.now(timezone.utc).isoformat()},{model_name},{samples_count},{status}\n")
    logger.info(f"Audited {model_name} incremental update: {samples_count} samples logged.")


def run_incremental_update(days: int = 1, symbol: str = "XAUUSD"):
    # 1. Safety Guard
    if days > 3:
        logger.error(
            f"SAFETY GUARD TRIGGERED: Requested {days} days. "
            "Incremental update should only add 1–2 days (max 3 days). "
            "For full baseline retraining, run: python scripts/retrain_full.py"
        )
        sys.exit(1)

    logger.info("=" * 60)
    logger.info(f"Running Daily Incremental Training for last {days} day(s)")
    logger.info("=" * 60)

    # 2. Check Existing Model
    macro_path = "models/saved/macro_model.pkl"
    if not os.path.exists(macro_path) and os.path.exists("models/macro_model.pkl"):
        macro_path = "models/macro_model.pkl"

    if not os.path.exists(macro_path):
        logger.warning("No existing macro_model.pkl found for warm-start. Initiating initial training...")

    # Fetch last N days of data
    settings = Settings()
    engine = build_engine(settings.DB_URL)
    candle_repo = CandleRepository(engine)

    bars_needed = days * 1440 + 200  # 1m bars per day + 200 warm-up bars
    logger.info(f"Fetching last {bars_needed} M1 bars from database...")
    try:
        bars = candle_repo.get_latest_bars(symbol, count=bars_needed)
    except Exception as e:
        logger.warning(f"Database query encountered issue: {e}")
        bars = []

    if len(bars) < 200:
        raise RuntimeError(
            f"INSUFFICIENT REAL MARKET DATA FOR INCREMENTAL UPDATE: Found {len(bars)} recent bars for {symbol} "
            f"(minimum required: 200 bars for feature warm-up). Zero synthetic fallback policy is active.\n"
            f"Please ensure live market bars are ingested into the database via MT5 / cTrader feed:\n"
            f"  python scripts/seed_historical.py --symbol {symbol} --timeframe 1m"
        )
    else:
        df = pd.DataFrame([
            {
                "time": b.time,
                "symbol": b.symbol,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "tick_volume": b.tick_volume,
                "real_volume": b.real_volume,
                "spread": b.spread,
            }
            for b in bars
        ])

    logger.info(f"Loaded {len(df)} incremental bars.")

    # Compute Features & Labels
    feature_engine = MacroFeatureEngine()
    features_list = []
    valid_indices = []
    step = 3
    for i in range(150, len(df), step):
        sub_df = df.iloc[:i + 1]
        feats = feature_engine.compute_macro_features(
            df_pair=sub_df,
            symbol=symbol,
            current_time=sub_df["time"].iloc[-1]
        )
        features_list.append(feats)
        valid_indices.append(i)

    X_inc = pd.DataFrame(features_list)
    labeler = DynamicATRTripleBarrierLabeler(atr_upper_mult=2.0, atr_lower_mult=1.0, max_holding_bars=30)
    y_inc = pd.Series([
        labeler.label_candidate(df, entry_idx=idx, direction=(1 if df["close"].iloc[idx] >= df["close"].iloc[max(0, idx-50):idx+1].mean() else -1))
        for idx in valid_indices
    ], index=X_inc.index)

    logger.info(f"Incremental training on {len(X_inc)} new feature vectors...")
    calibrated_model, _ = train_forex_macro_model(
        X=X_inc,
        y=y_inc,
        n_splits=3,
        purge_bars=20,
        embargo_bars=5,
    )

    # Save updated checkpoint
    os.makedirs("models/saved", exist_ok=True)
    os.makedirs("models", exist_ok=True)
    joblib.dump({"model": calibrated_model, "features": feature_engine.FEATURE_NAMES}, "models/saved/macro_model.pkl")
    joblib.dump({"model": calibrated_model, "features": feature_engine.FEATURE_NAMES}, "models/macro_model.pkl")

    # 3. Database Audit
    log_database_audit("macro_model", len(X_inc), "SUCCESS")
    logger.info("=" * 60)
    logger.info("SUCCESS: Incremental training completed and audited.")
    logger.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily Incremental Model Training")
    parser.add_argument("--days", type=int, default=1, help="Number of recent days to include (max 3)")
    parser.add_argument("--symbol", type=str, default="XAUUSD", help="Symbol to train on (default: XAUUSD)")
    args = parser.parse_args()

    run_incremental_update(days=args.days, symbol=args.symbol)
