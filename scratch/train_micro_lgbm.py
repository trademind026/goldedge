import os
import sys
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
import numpy as np
import pandas as pd
import joblib
import optuna
import lightgbm as lgb
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import roc_auc_score

# Ensure src is in python path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from src.goldedge.infrastructure.ml.microstructure_feature_engine import MicrostructureFeatureEngine
from src.goldedge.infrastructure.ml.microstructure_labeler import MicrostructureExplosiveBreakoutLabeler
from src.goldedge.infrastructure.ml.forex_micro_model import SessionGroupTimeSeriesSplit
from src.goldedge.adapters.secondary.db.connection_pool import build_engine
from src.goldedge.adapters.secondary.db.tick_repository import TickRepository
from src.goldedge.config.settings import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("train_micro_lgbm")
optuna.logging.set_verbosity(optuna.logging.WARNING)


def fetch_historical_ticks(symbol: str = "XAUUSD", count: int = 100000) -> pd.DataFrame:
    """Loads historical ticks directly from the database or parquet/CSV fallback."""
    settings = Settings()
    engine = build_engine(settings.DB_URL)

    logger.info(f"Loading raw real ticks for {symbol} from database (target: up to {count} ticks)...")
    try:
        query = (
            "SELECT time, symbol, bid, ask, last as price, volume "
            "FROM market_ticks "
            "WHERE symbol = %(symbol)s "
            "ORDER BY time DESC "
            "LIMIT %(count)s"
        )
        df = pd.read_sql(query, engine, params={"symbol": symbol, "count": count})
        if not df.empty:
            df = df.iloc[::-1].reset_index(drop=True)
            logger.info(f"Loaded {len(df)} ticks from database for {symbol} ({df['time'].iloc[0]} to {df['time'].iloc[-1]}).")
    except Exception as e:
        logger.warning(f"Database query encountered issue: {e}. Checking cache...")
        df = pd.DataFrame()

    if df.empty or len(df) < 500:
        # Check cache
        cache_paths = [
            f"data/cache/{symbol}_ticks.parquet",
            f"data/{symbol}_ticks.parquet",
            f"data/{symbol}_ticks.csv",
        ]
        for cp in cache_paths:
            if os.path.exists(cp):
                logger.info(f"Loading ticks from cache file: {cp}")
                df = pd.read_parquet(cp) if cp.endswith(".parquet") else pd.read_csv(cp)
                if len(df) >= 500:
                    return df

        raise RuntimeError(
            f"INSUFFICIENT REAL TICK DATA: Found {len(df)} ticks for {symbol} (minimum required: 500). "
            "Zero synthetic fallback policy is strictly enforced.\n"
            "Please ingest real historical ticks into the database using:\n"
            f"  python scripts/seed_historical.py --symbol {symbol} --timeframe tick --start 2026-05-01 --end 2026-08-01\n"
            "or fetch via cTrader Open API:\n"
            f"  python scripts/fetch_history_cli.py --symbol {symbol} --days 14"
        )

    return df


def run_micro_lgbm_training(symbol: str = "XAUUSD", n_trials: int = 30, count: int = 100000):
    logger.info("=" * 60)
    logger.info(f"Starting Micro Model Training on Real Tick Data for {symbol} (Sub-Second Tape Sniper)")
    logger.info("=" * 60)

    # 1. Tick Ingestion
    ticks_df = fetch_historical_ticks(symbol, count=count)

    # 2. 1-Second Micro Resampling / Microstructure Feature Engine
    logger.info("Computing 16 Order Flow Imbalance (OFI), Depth Skew, and Velocity Features...")
    feature_engine = MicrostructureFeatureEngine()
    time_col = "timestamp" if "timestamp" in ticks_df.columns else "time"
    ticks_df[time_col] = pd.to_datetime(ticks_df[time_col])
    ticks_df["bid"] = pd.to_numeric(ticks_df["bid"], errors="coerce")
    ticks_df["ask"] = pd.to_numeric(ticks_df["ask"], errors="coerce")
    if "price" not in ticks_df.columns:
        ticks_df["price"] = (ticks_df["bid"] + ticks_df["ask"]) / 2.0
    else:
        ticks_df["price"] = pd.to_numeric(ticks_df["price"], errors="coerce").fillna((ticks_df["bid"] + ticks_df["ask"]) / 2.0)
    if "volume" not in ticks_df.columns:
        ticks_df["volume"] = 1.0
    else:
        ticks_df["volume"] = pd.to_numeric(ticks_df["volume"], errors="coerce").fillna(1.0)

    features_list = []
    valid_indices = []
    window_ticks = 40

    step = 10 if len(ticks_df) >= 40000 else 5
    for i in range(window_ticks, len(ticks_df), step):
        sub_ticks = ticks_df.iloc[i - window_ticks:i + 1]
        feats = feature_engine.compute_features(
            ticks_df=sub_ticks,
            symbol=symbol,
            current_time=sub_ticks[time_col].iloc[-1]
        )
        features_list.append(feats)
        valid_indices.append(i)

    X = pd.DataFrame(features_list)
    logger.info(f"Engineered micro feature matrix shape: {X.shape}")

    # 3. Explosive Breakout Labeling
    logger.info("Labeling ticks using Explosive Micro-Breakout Formula (>= 2.5x spread in 60s, <= 0.5x adverse)...")
    labeler = MicrostructureExplosiveBreakoutLabeler(
        favorable_spread_mult=2.5,
        max_adverse_spread_mult=0.5,
        horizon_seconds=60.0
    )

    hours = ticks_df[time_col].iloc[valid_indices].dt.hour
    sessions_list = [
        "LONDON" if (7 <= h < 12) else ("NY" if (12 <= h < 17) else "ASIA")
        for h in hours
    ]
    y_array = labeler.label_ticks(ticks_df, valid_indices, direction=1)

    y = pd.Series(y_array, index=X.index)
    groups = pd.Series(sessions_list, index=X.index)

    # 4. Class Imbalance Balancing
    n_pos = int((y == 1).sum())
    n_neg = int((y == 0).sum())
    scale_pos_weight = float(n_neg / max(1, n_pos))
    logger.info(f"Class distribution: {n_pos} Positive Breakouts, {n_neg} Negative/Traps. scale_pos_weight: {scale_pos_weight:.2f}")

    # 5. Optuna Bayesian Hyperparameter Search
    logger.info(f"Starting {n_trials}-trial Bayesian Optimization tuning LightGBM for Sub-Second Precision...")
    cv = SessionGroupTimeSeriesSplit(n_splits=3)
    splits = list(cv.split(X, y, groups=groups))

    def objective(trial):
        params = {
            "objective": "binary",
            "metric": "auc",
            "boosting_type": "gbdt",
            "n_estimators": trial.suggest_int("n_estimators", 50, 150),
            "learning_rate": trial.suggest_float("learning_rate", 0.02, 0.15, log=True),
            "max_depth": trial.suggest_int("max_depth", 3, 9),
            "num_leaves": trial.suggest_int("num_leaves", 8, 32),
            "min_child_samples": trial.suggest_int("min_child_samples", 10, 80),
            "subsample": trial.suggest_float("subsample", 0.60, 0.90),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.60, 0.95),
            "reg_alpha": trial.suggest_float("reg_alpha", 0.1, 5.0, log=True),
            "reg_lambda": trial.suggest_float("reg_lambda", 0.1, 5.0, log=True),
            "scale_pos_weight": scale_pos_weight,
            "verbose": -1,
            "random_state": 42,
        }

        scores = []
        for tr_idx, val_idx in splits:
            if len(np.unique(y.iloc[tr_idx])) < 2 or len(np.unique(y.iloc[val_idx])) < 2:
                continue
            clf = lgb.LGBMClassifier(**params)
            clf.fit(X.iloc[tr_idx], y.iloc[tr_idx])
            preds = clf.predict_proba(X.iloc[val_idx])[:, 1]
            scores.append(roc_auc_score(y.iloc[val_idx], preds))

        return float(np.mean(scores)) if scores else 0.5

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=n_trials)
    best_params = study.best_params
    best_params.update({
        "objective": "binary",
        "metric": "auc",
        "scale_pos_weight": scale_pos_weight,
        "verbose": -1,
        "random_state": 42
    })
    logger.info(f"Optimal Micro LightGBM Hyperparameters: {best_params}")

    # 6. Platt Scaling (Sigmoid Logistic Calibration)
    logger.info("Fitting Platt Scaler (Sigmoid Logistic) on Group Time-Series Splits...")
    base_clf = lgb.LGBMClassifier(**best_params)
    calibrated_model = CalibratedClassifierCV(estimator=base_clf, method="sigmoid", cv=splits)
    calibrated_model.fit(X, y)

    # 7. Saves Model
    saved_model_path = "models/saved/micro_model.pkl"
    os.makedirs(os.path.dirname(saved_model_path), exist_ok=True)
    os.makedirs("models", exist_ok=True)

    joblib.dump({"model": calibrated_model, "features": feature_engine.FEATURE_NAMES}, saved_model_path)
    joblib.dump({"model": calibrated_model, "features": feature_engine.FEATURE_NAMES}, "models/micro_model.pkl")

    logger.info("=" * 60)
    logger.info(f"SUCCESS: Calibrated Micro Model saved to {saved_model_path} and models/micro_model.pkl")
    logger.info("=" * 60)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Micro ML Model Retraining on Real Tick Data")
    parser.add_argument("--symbol", type=str, default="XAUUSD", help="Symbol to train on (default: XAUUSD)")
    parser.add_argument("--trials", type=int, default=30, help="Number of Optuna Bayesian trials (default: 30)")
    parser.add_argument("--count", type=int, default=100000, help="Number of recent ticks to load (default: 100000)")
    args = parser.parse_args()
    run_micro_lgbm_training(symbol=args.symbol, n_trials=args.trials, count=args.count)
