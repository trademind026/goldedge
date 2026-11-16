import sys
from pathlib import Path

# Forward to scratch/train_micro_lgbm.py
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scratch.train_micro_lgbm import run_micro_lgbm_training

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Micro ML Model Retraining on Real Tick Data")
    parser.add_argument("--symbol", type=str, default="XAUUSD", help="Symbol to train on (default: XAUUSD)")
    parser.add_argument("--trials", type=int, default=30, help="Number of Optuna Bayesian trials (default: 30)")
    parser.add_argument("--count", type=int, default=100000, help="Number of recent ticks to load (default: 100000)")
    args = parser.parse_args()
    run_micro_lgbm_training(symbol=args.symbol, n_trials=args.trials, count=args.count)
