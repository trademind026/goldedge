import os
import sys
import argparse
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scripts.retrain_full import run_full_macro_retrain
from scratch.train_micro_lgbm import run_micro_lgbm_training

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("retrain_models")


def run_all_in_one_retrain(recent_days: int = 60, symbol: str = "XAUUSD"):
    logger.info("=" * 70)
    logger.info(f"ALL-IN-ONE RETRAIN: Training Macro & Micro Models on Real Data for {symbol}")
    logger.info("=" * 70)

    # 1. Macro Model Retrain
    logger.info("\n>>> [STEP 1/2]: Retraining Macro ML Meta-Model (32 Features, Purged CV, Isotonic Calib)...")
    try:
        run_full_macro_retrain(symbol=symbol)
        logger.info(">>> Macro Model Retrain COMPLETED successfully.")
    except Exception as e:
        logger.error(f">>> Macro Model Retrain FAILED: {e}")
        raise e

    # 2. Micro Model Retrain
    logger.info("\n>>> [STEP 2/2]: Retraining Microstructure ML Model (16 OFI Features, Optuna, Platt Calib)...")
    try:
        run_micro_lgbm_training(symbol=symbol, n_trials=30)
        logger.info(">>> Micro Model Retrain COMPLETED successfully.")
    except Exception as e:
        logger.error(f">>> Micro Model Retrain FAILED: {e}")
        raise e

    logger.info("\n" + "=" * 70)
    logger.info("ALL-IN-ONE RETRAINING COMPLETED SUCCESSFULLY!")
    logger.info("All model artifacts saved to models/saved/ and models/")
    logger.info("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="All-in-One Model Retraining")
    parser.add_argument("--recent", type=int, default=60, help="Days of recent history to include")
    parser.add_argument("--symbol", type=str, default="XAUUSD", help="Symbol to train on (default: XAUUSD)")
    args = parser.parse_args()

    run_all_in_one_retrain(recent_days=args.recent, symbol=args.symbol)
