"""
Auto-retry resumable runner for TASK G3.6R.1 12-month isolated historical backfill
(2025-04-21T00:00:00Z .. 2026-05-01T00:00:00Z -> market_ticks_extended_pre202605).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

CHECKPOINT_FILE = Path("backtest_results/audit/task_g3_6r_1_extended_history/extended_backfill_checkpoint.json")
MAX_ATTEMPTS = 80


def _progress() -> tuple[int, int]:
    if not CHECKPOINT_FILE.exists():
        return 0, -1
    try:
        d = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
        return int(d.get("completed_count", 0)), int(d.get("total_chunks", -1))
    except Exception:
        return 0, -1


def main() -> None:
    t0 = time.time()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        done, tot = _progress()
        print(f"[EXT RUNNER] Attempt {attempt}/{MAX_ATTEMPTS} | checkpoint={done}/{tot}", flush=True)
        proc = subprocess.run(
            [sys.executable, "-u", "scripts/audit/task_g3_6r_1_extended_backfill.py"],
            cwd="/app",
        )
        done2, tot2 = _progress()
        print(
            f"[EXT RUNNER] Attempt {attempt} exited rc={proc.returncode} | "
            f"progress={done2}/{tot2} | elapsed={round((time.time() - t0) / 60.0, 1)}m",
            flush=True,
        )
        if tot2 > 0 and done2 >= tot2:
            print(f"[EXT RUNNER] COMPLETED 100% ({done2}/{tot2}) in {round((time.time() - t0) / 60.0, 1)} min!", flush=True)
            sys.exit(0)
        time.sleep(5.0)
    print("[EXT RUNNER] Exhausted max retry attempts before completion.", flush=True)
    sys.exit(1)


if __name__ == "__main__":
    main()
