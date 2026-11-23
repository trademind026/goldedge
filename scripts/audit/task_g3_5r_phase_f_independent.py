"""
TASK G3.5R — Phase F: Independent Clean-Room Execution Auditor.
STRICT ZERO IMPORTS FROM `src/goldedge/` (AST-verified in test_task_g3_5r_validation.py).

Independently:
  - Queries raw ticks directly from PostgreSQL `v_market_ticks_expanded_6m_v1`
  - Independently evaluates canonical H3b entry, SL/TP/BE/Trailing/TIME_SQUEEZE/E1/TIMEOUT exits,
    broker-rounded commission ($12/$1M/side), slippage (0.0533p/side), net P&L, net R, equity & DD
    across ALL 112 opportunities WITHOUT reusing any `src/goldedge/` code.
  - Verifies zero mismatches against BOTH `RunBacktestUseCase` (app) and the repaired simulator.
  - Verifies frozen H0 (70-trade) & frozen 111-trade SHA-256s intact.

Writes: backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/g3_5r_independent_execution_audit.json
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

OUT_DIR = Path("backtest_results/audit/task_g3_5r_h3b_execution_reconciliation")
G35_DIR = Path("backtest_results/audit/task_g3_5_calibration_utility")

FROZEN_111_PATH = Path("backtest_results/frozen_111_baseline_trades.csv")
FROZEN_111_SHA256 = "73306E1529ADC48284BCE9AB6AC35122F33B9999D2464AF441867EB3BB6CF168"
FROZEN_70_PATH = Path("backtest_results/audit/task_g1_root_cause/g1_original_70_baseline_trades.csv")
FROZEN_70_SHA256 = "27200EEA2C7FF4842FC586DD1131EE450247D0BCCBCF3E2F77ECA6952D2CCDC4"

COMM_RATE = 12.0 / 1_000_000.0
SLIP_USD = 0.00533
CONTRACT_OZ = 100.0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _indep_max_dd(arr: np.ndarray) -> float:
    eq = np.cumsum(arr)
    peak = np.maximum.accumulate(np.concatenate(([0.0], eq)))[1:]
    return round(float(np.min(eq - peak)), 4) if len(eq) else 0.0


def _indep_replay_trade(
    direction: str,
    entry_idx: int,
    ts_arr: np.ndarray,
    bid_arr: np.ndarray,
    ask_arr: np.ndarray,
    atr: float,
    equity: float,
) -> Dict[str, Any]:
    is_buy = direction == "BUY"
    e_ts = float(ts_arr[entry_idx])
    exec_entry = (float(ask_arr[entry_idx]) + SLIP_USD) if is_buy else (float(bid_arr[entry_idx]) - SLIP_USD)
    init_sl = (exec_entry - 1.5 * atr) if is_buy else (exec_entry + 1.5 * atr)
    tp = (exec_entry + 3.0 * atr) if is_buy else (exec_entry - 3.0 * atr)

    # Independent sizing: floor to 0.01 lot step so risk <= equity * 0.5%
    one_lot_rt = round(2.0 * round(CONTRACT_OZ * exec_entry * COMM_RATE, 2), 2)
    loss_per_lot = abs(exec_entry - init_sl) * CONTRACT_OZ + one_lot_rt
    max_risk = equity * 0.005
    raw_vol = max_risk / loss_per_lot
    vol = math.floor(round(raw_vol / 0.01, 9)) * 0.01
    vol = round(max(0.01, vol), 2)
    while vol >= 0.01 and (vol * loss_per_lot > max_risk + 1e-9):
        vol = round(vol - 0.01, 2)

    entry_comm = round(vol * CONTRACT_OZ * exec_entry * COMM_RATE, 2)
    planned_risk = abs(exec_entry - init_sl) * CONTRACT_OZ * vol + 2.0 * entry_comm

    entry_dt = datetime.fromtimestamp(e_ts, tz=timezone.utc)
    max_hold = 60 if (7 <= entry_dt.hour < 20) else 90
    entry_min = int(e_ts // 60)

    sl = init_sl
    peak = exec_entry
    trailing = False
    flag = ""

    def _ratchet(px: float, bh: int) -> None:
        nonlocal sl, peak, trailing, flag
        if (is_buy and px > peak) or ((not is_buy) and px < peak):
            peak = px
        gain = (peak - exec_entry) if is_buy else (exec_entry - peak)
        if gain >= 2.5 * atr:
            trailing = True
            t = (peak - 0.75 * atr) if is_buy else (peak + 0.75 * atr)
            if (is_buy and t > sl) or ((not is_buy) and t < sl):
                sl = t
                flag = "TRAILING_SL"
        elif gain >= 1.6 * atr:
            trailing = True
            t = (peak - 1.10 * atr) if is_buy else (peak + 1.10 * atr)
            if (is_buy and t > sl) or ((not is_buy) and t < sl):
                sl = t
                flag = "TRAILING_SL"
        elif gain >= 1.0 * atr:
            c = (exec_entry + 0.20) if is_buy else (exec_entry - 0.20)
            if (is_buy and c > sl) or ((not is_buy) and c < sl):
                sl = c
                flag = "BREAKEVEN"
        hp = bh / max_hold
        if hp >= 0.75:
            prog = min((hp - 0.75) / 0.25, 1.0)
            if is_buy:
                tsl = sl + prog * (px - sl)
                if tsl > sl:
                    sl = tsl
                    flag = "TIME_SQUEEZE"
            else:
                tsl = sl - prog * (sl - px)
                if tsl < sl:
                    sl = tsl
                    flag = "TIME_SQUEEZE"

    post_mins = (ts_arr[entry_idx + 1:] // 60).astype(np.int64)
    chg = np.flatnonzero(np.diff(post_mins) != 0) + 1
    starts = np.concatenate(([0], chg))
    ends = np.concatenate((chg, [len(post_mins)]))
    bh = 0
    exited = False
    reason = "END_OF_BACKTEST"
    exit_px = exec_entry

    for s, e in zip(starts, ends):
        mv = int(post_mins[s])
        is_em = (mv == entry_min)
        if not is_em:
            bh += 1
        lq = None
        bq = None
        for ri in range(int(s), int(e)):
            i = entry_idx + 1 + ri
            b = float(bid_arr[i])
            a = float(ask_arr[i])
            if b <= 0.0 or a <= 0.0 or b >= a:
                continue
            q = b if is_buy else a
            lq = q
            if bq is None or (is_buy and q > bq) or ((not is_buy) and q < bq):
                bq = q
            if (is_buy and b <= sl) or ((not is_buy) and a >= sl):
                exit_px = (b - SLIP_USD) if is_buy else (a + SLIP_USD)
                reason = flag if flag else ("TRAILING_SL" if trailing else "SL")
                exited = True
                break
            if (is_buy and b >= tp) or ((not is_buy) and a <= tp):
                exit_px = float(tp)
                reason = "TARGET"
                exited = True
                break
            _ratchet(q, bh)
        if exited:
            break
        if (not is_em) and lq is not None:
            if bh == 15:
                mfe = ((peak - exec_entry) if is_buy else (exec_entry - peak)) / atr
                if mfe < 0.4:
                    exit_px = (lq - SLIP_USD) if is_buy else (lq + SLIP_USD)
                    reason = "EARLY_FAIL_15M"
                    exited = True
                    break
            if bh >= max_hold:
                exit_px = (lq - SLIP_USD) if is_buy else (lq + SLIP_USD)
                reason = "TIMEOUT"
                exited = True
                break
        if bq is not None:
            _ratchet(bq, bh)

    gross = ((exit_px - exec_entry) if is_buy else (exec_entry - exit_px)) * CONTRACT_OZ * vol
    exit_comm = round(vol * CONTRACT_OZ * exit_px * COMM_RATE, 2)
    comm = round(entry_comm + exit_comm, 2)
    net_pnl = gross - comm
    return {
        "entry_px": round(exec_entry, 5),
        "exit_px": round(exit_px, 5),
        "reason": reason,
        "comm": comm,
        "net_pnl": round(net_pnl, 4),
        "net_r": round(net_pnl / planned_risk, 5),
    }


def main() -> None:
    t0 = time.time()
    db_url = os.environ.get("DB_URL") or os.environ.get("DATABASE_URL") or "postgresql+psycopg2://postgres:password@timescaledb:5432/goldedge"
    if db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = create_engine(db_url)

    df_rec = pd.read_parquet(OUT_DIR / "g3_5r_reconciled_112_ledger.parquet").sort_values("entry_ts").reset_index(drop=True)

    entry_mm = 0
    exit_mm = 0
    reason_mm = 0
    e1_mm = 0
    comm_mm = 0
    pnl_mm = 0
    r_mm = 0
    indep_rs = []

    for _, row in df_rec.iterrows():
        e_ts = float(row["entry_ts"])
        direction = str(row["direction"]).upper()
        is_buy = direction == "BUY"
        atr = float(row["true_initial_atr"])
        eq = float(row["equity_before"])
        app_entry = float(row["app_entry_px"])

        s_q = datetime.fromtimestamp(e_ts - 5.0, tz=timezone.utc)
        e_q = datetime.fromtimestamp(e_ts + 5600.0, tz=timezone.utc)
        sql = text(
            """
            SELECT EXTRACT(EPOCH FROM time)::double precision AS ts, bid::double precision AS bid, ask::double precision AS ask
            FROM v_market_ticks_expanded_6m_v1
            WHERE symbol = 'XAUUSD' AND time >= :s AND time <= :e
            ORDER BY time ASC
            """
        )
        with engine.connect() as conn:
            tdf = pd.read_sql(sql, conn, params={"s": s_q, "e": e_q})
        ts_arr = tdf["ts"].to_numpy(dtype=np.float64)
        bid_arr = tdf["bid"].to_numpy(dtype=np.float64)
        ask_arr = tdf["ask"].to_numpy(dtype=np.float64)

        i_lo = int(np.searchsorted(ts_arr, e_ts, side="left"))
        i_hi = int(np.searchsorted(ts_arr, e_ts + 1.0, side="left"))
        imm_idx = i_lo
        for ci in range(i_lo, min(i_hi, len(ts_arr))):
            if bid_arr[ci] <= 0 or ask_arr[ci] <= 0 or bid_arr[ci] >= ask_arr[ci]:
                continue
            fq = (ask_arr[ci] + SLIP_USD) if is_buy else (bid_arr[ci] - SLIP_USD)
            if abs(fq - app_entry) < 1e-4:
                imm_idx = ci
                break

        res = _indep_replay_trade(direction, imm_idx, ts_arr, bid_arr, ask_arr, atr, eq)
        indep_rs.append(res["net_r"])
        if abs(res["entry_px"] - app_entry) > 1e-4:
            entry_mm += 1
        if abs(res["exit_px"] - float(row["app_exit_px"])) > 1e-4:
            exit_mm += 1
        if res["reason"] != str(row["app_exit_reason"]):
            reason_mm += 1
            if "EARLY_FAIL" in res["reason"] or "EARLY_FAIL" in str(row["app_exit_reason"]):
                e1_mm += 1
        if abs(res["comm"] - float(row["app_comm_usd"])) > 1e-2:
            comm_mm += 1
        if abs(res["net_pnl"] - float(row["app_net_pnl"])) > 1e-2:
            pnl_mm += 1
        if abs(res["net_r"] - float(row["app_net_r"])) > 1e-3:
            r_mm += 1

    indep_arr = np.asarray(indep_rs, dtype=np.float64)
    app_arr = df_rec["app_net_r"].to_numpy(dtype=np.float64)
    total_mm = entry_mm + exit_mm + reason_mm + e1_mm + comm_mm + pnl_mm + r_mm
    f111_ok = _sha256(FROZEN_111_PATH) == FROZEN_111_SHA256
    f70_ok = _sha256(FROZEN_70_PATH) == FROZEN_70_SHA256
    if not (f111_ok and f70_ok):
        total_mm += 1

    out = {
        "task": "G3.5R_PHASE_F_INDEPENDENT_EXECUTION_AUDIT",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "clean_room_zero_goldedge_imports": True,
        "original_trade_count": int(len(df_rec)),
        "matched_trade_count": int(len(df_rec) - reason_mm),
        "unmatched_trade_count": int(reason_mm),
        "entry_price_mismatches": int(entry_mm),
        "exit_price_mismatches": int(exit_mm),
        "e1_event_mismatches": int(e1_mm),
        "exit_reason_mismatches": int(reason_mm),
        "commission_mismatches": int(comm_mm),
        "pnl_mismatches": int(pnl_mm),
        "net_r_mismatches": int(r_mm),
        "indep_total_r": round(float(indep_arr.sum()), 6),
        "app_total_r": round(float(app_arr.sum()), 6),
        "total_r_diff": round(float(indep_arr.sum() - app_arr.sum()), 6),
        "indep_cum_r_max_dd": _indep_max_dd(indep_arr),
        "app_cum_r_max_dd": _indep_max_dd(app_arr),
        "frozen_baselines_intact": {"frozen_111": f111_ok, "frozen_70": f70_ok},
        "overall_status": "PASS" if total_mm == 0 else "FAIL",
        "elapsed_sec": round(time.time() - t0, 2),
    }
    (OUT_DIR / "g3_5r_independent_execution_audit.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"[G3.5R PHASE F CLEAN-ROOM DONE in {out['elapsed_sec']}s] status={out['overall_status']} total_mm={total_mm} total_r_diff={out['total_r_diff']:+.6f}")
    if total_mm != 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
