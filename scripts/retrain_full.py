import os
import sys
import shutil
import logging
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd
import joblib

# Ensure src is in python path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from src.goldedge.infrastructure.ml.macro_feature_engine import MacroFeatureEngine
from src.goldedge.infrastructure.ml.triple_barrier_labeler import DynamicATRTripleBarrierLabeler
from src.goldedge.infrastructure.ml.purged_cv import PurgedKFold
from src.goldedge.infrastructure.ml.forex_meta_model import train_forex_macro_model, ForexMetaLabeler
from src.goldedge.adapters.secondary.db.connection_pool import build_engine
from src.goldedge.adapters.secondary.db.candle_repository import CandleRepository
from src.goldedge.config.settings import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("retrain_full")


def backup_existing(model_path: str = "models/saved/macro_model.pkl"):
    """
    Step 1: Automatic Backup
    Before touching anything, copies the current model to models/saved/backups/macro_model_YYYYMMDD_HHMMSS.pkl.
    """
    if os.path.exists(model_path):
        backup_dir = "models/saved/backups"
        os.makedirs(backup_dir, exist_ok=True)
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_dest = os.path.join(backup_dir, f"macro_model_{timestamp_str}.pkl")
        shutil.copy2(model_path, backup_dest)
        logger.info(f"Backed up existing model to: {backup_dest}")
    else:
        logger.info(f"No existing model found at {model_path} to backup.")


def fetch_historical_candles(symbol: str = "XAUUSD", min_bars: int = 5000) -> pd.DataFrame:
    """
    Step 2: Database Ingestion
    Loads historical 1-minute candles from the database (or auto-aggregates from market_ticks).
    Enforces safety guard: if fewer than 5,000 bars exist, auto-aggregates from real ticks or loads cache.
    """
    settings = Settings()
    engine = build_engine(settings.DB_URL)
    candle_repo = CandleRepository(engine)

    logger.info(f"Loading M1 candles for {symbol} from database...")
    try:
        bars = candle_repo.get_latest_bars(symbol, count=200000)
    except Exception as e:
        logger.warning(f"Database query encountered issue: {e}. Checking local parquet/CSV cache...")
        bars = []

    if len(bars) < min_bars:
        logger.info(f"Only {len(bars)} bars found in market_bars_1m for {symbol}. Triggering auto-aggregation from market_ticks...")
        try:
            from src.goldedge.config.container import Container
            c = Container()
            aggregator = c.market_aggregator_service()
            bars_created = aggregator.repo.aggregate_ticks_to_bars(symbol)
            if bars_created > 0:
                bars = candle_repo.get_latest_bars(symbol, count=200000)
                logger.info(f"Successfully aggregated {len(bars)} bars directly from market_ticks!")
        except Exception as agg_err:
            logger.warning(f"Auto-aggregation attempt encountered: {agg_err}")

    if len(bars) < min_bars:
        # Check parquet or CSV cache in data/
        cache_paths = [
            f"data/cache/{symbol}_1m.parquet",
            f"data/{symbol}_1m.parquet",
            f"data/{symbol}_1m.csv",
        ]
        for cp in cache_paths:
            if os.path.exists(cp):
                logger.info(f"Loading candles from local real data cache file: {cp}")
                df = pd.read_parquet(cp) if cp.endswith(".parquet") else pd.read_csv(cp)
                if len(df) >= min_bars:
                    return df

        raise RuntimeError(
            f"INSUFFICIENT REAL MARKET DATA: Found {len(bars)} historical bars for {symbol} "
            f"(minimum required: {min_bars}). Zero synthetic fallback policy is strictly enforced.\n"
            f"Please ingest real historical bars into the database using:\n"
            f"  python scripts/aggregate_history_cli.py --symbol {symbol}\n"
            f"or seed via MT5/cTrader:\n"
            f"  python scripts/seed_historical.py --symbol {symbol} --timeframe 1m --start 2024-01-01 --end 2026-01-01"
        )

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
    logger.info(f"Loaded {len(df)} historical bars for {symbol}.")
    return df


def run_full_macro_retrain(symbol: str = "XAUUSD"):
    logger.info("=" * 60)
    logger.info(f"Starting Full Macro Model Retrain on Real Data for {symbol} (Clean Slate)")
    logger.info("=" * 60)

    saved_model_path = "models/saved/macro_model.pkl"

    # 1. Automatic Backup
    backup_existing(saved_model_path)

    # 2. Database Ingestion
    df = fetch_historical_candles(symbol, min_bars=5000)

    # 3. Feature Calculation (32 Macro Features)
    logger.info("Computing 32 Macro Technical, Thermodynamic, and Cross-Currency Features...")
    feature_engine = MacroFeatureEngine()
    features_list = []
    
    # Process rolling slices (step of 1 or batch for speed)
    step = 5  # sample every 5 bars to reduce redundant collinear bars across large history
    valid_indices = []
    
    for i in range(250, len(df), step):
        sub_df = df.iloc[:i + 1]
        feats = feature_engine.compute_macro_features(
            df_pair=sub_df,
            symbol=symbol,
            current_time=sub_df["time"].iloc[-1] if "time" in sub_df.columns else None
        )
        features_list.append(feats)
        valid_indices.append(i)

    X = pd.DataFrame(features_list)
    logger.info(f"Engineered feature matrix shape: {X.shape} with {len(feature_engine.FEATURE_NAMES)} features.")

    # 4. Triple Barrier Labeling
    logger.info("Generating Dynamic ATR Triple Barrier Labels (+2.0 ATR TP, -1.0 ATR SL, 30-bar timeout)...")
    labeler = DynamicATRTripleBarrierLabeler(atr_upper_mult=2.0, atr_lower_mult=1.0, max_holding_bars=30)
    y_list = []
    for idx in valid_indices:
        # Determine candidate direction: trend following alignment (+1 if close > ema50 else -1)
        sub = df.iloc[:idx + 1]
        c = sub["close"].iloc[-1]
        ema50 = sub["close"].ewm(span=50, adjust=False).mean().iloc[-1]
        dir_val = 1 if c >= ema50 else -1
        label = labeler.label_candidate(df, entry_idx=idx, direction=dir_val)
        y_list.append(label)

    y = pd.Series(y_list, index=X.index)
    win_rate = y.mean() * 100.0
    logger.info(f"Target distribution: {len(y)} samples, Win Rate: {win_rate:.2f}%")

    # 5. SHAP TreeExplainer Feature Pruning
    try:
        import shap
        import lightgbm as lgb
        logger.info("Analyzing feature contributions via SHAP TreeExplainer...")
        prelim_clf = lgb.LGBMClassifier(n_estimators=50, max_depth=4, verbose=-1, random_state=42)
        prelim_clf.fit(X, y)
        explainer = shap.TreeExplainer(prelim_clf)
        shap_values = explainer.shap_values(X.iloc[:min(300, len(X))])
        if isinstance(shap_values, list):
            mean_shap = np.abs(shap_values[1]).mean(axis=0)
        else:
            mean_shap = np.abs(shap_values).mean(axis=0)
        top_features = [f for _, f in sorted(zip(mean_shap, X.columns), reverse=True)]
        logger.info(f"Top 5 most impactful features: {top_features[:5]}")
    except Exception as e:
        logger.warning(f"SHAP analysis skipped: {e}")

    # 6. Purged K-Fold Cross-Validation & Isotonic Probability Calibration
    logger.info("Fitting Calibrated LightGBM with Purged K-Fold (5 folds, 30-bar purge, 5-bar embargo)...")
    calibrated_model, params = train_forex_macro_model(
        X=X,
        y=y,
        n_splits=5,
        purge_bars=30,
        embargo_bars=5,
    )

    # 7. Model Persistence
    os.makedirs(os.path.dirname(saved_model_path), exist_ok=True)
    os.makedirs("models", exist_ok=True)
    
    # Save to primary location
    joblib.dump({"model": calibrated_model, "features": feature_engine.FEATURE_NAMES}, saved_model_path)
    # Also save to models/macro_model.pkl for direct container access
    joblib.dump({"model": calibrated_model, "features": feature_engine.FEATURE_NAMES}, "models/macro_model.pkl")

    logger.info("=" * 60)
    logger.info(f"SUCCESS: Calibrated Macro Model saved to {saved_model_path} and models/macro_model.pkl")
    logger.info("=" * 60)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Full Macro ML Model Retraining on Real Historical Data")
    parser.add_argument("--symbol", type=str, default="XAUUSD", help="Symbol to train on (default: XAUUSD)")
    args = parser.parse_args()
    run_full_macro_retrain(symbol=args.symbol)
