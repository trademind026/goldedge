"""
Resumable Auto-Retry Runner for Task G3.3 Staged +3 Month Historical Backfill
(2026-05-13T00:00:00Z .. 2026-08-13T00:00:00Z -> ~6.2 months total coverage).
Automatically restarts `task_g3_3_staged_backfill.py --months-3` from checkpoint
on any cTrader TCP disconnect until 100% of chunks are completed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

CHECKPOINT_FILE = Path("backtest_results/audit/task_g3_3_leakage_proof_pipeline/staged_backfill_checkpoint.json")
MAX_ATTEMPTS = 40


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
        print(f"[RUNNER] Attempt {attempt}/{MAX_ATTEMPTS} | checkpoint={done}/{tot}", flush=True)
        proc = subprocess.run(
            [sys.executable, "-u", "scripts/audit/task_g3_3_staged_backfill.py", "--months-3"],
            cwd="/app",
        )
        done2, tot2 = _progress()
        print(f"[RUNNER] Attempt {attempt} exited rc={proc.returncode} | progress={done2}/{tot2} | elapsed={round((time.time()-t0)/60.0, 1)}m", flush=True)
        if tot2 > 0 and done2 >= tot2:
            print(f"[RUNNER] COMPLETED 100% ({done2}/{tot2}) in {round((time.time() - t0)/60.0, 1)} min!", flush=True)
            sys.exit(0)
        time.sleep(5.0)
    print("[RUNNER] Exhausted max retry attempts before completion.", flush=True)
    sys.exit(1)


if __name__ == "__main__":
    main()
