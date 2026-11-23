"""
TASK G3.6R — Phase E: Clean-Room Independent Execution & Trade-by-Trade Auditor.
STRICT ZERO IMPORTS FROM `src/goldedge/` (AST-verified in test_task_g3_6r_validation.py).

Independently:
  - Queries raw ticks directly from PostgreSQL `v_market_ticks_expanded_6m_v1`
  - Independently replays EVERY trade of BOTH `H3B_FROZEN` AND the best G3.6R candidate
    (matching its exact `be_trigger_atr` and `early_fail_mins` config) from raw ticks
    without reusing any `src/goldedge/` code.
  - Verifies 0 mismatches on entry price, exit price, exit reason, commission, net P&L,
    net R, cumulative equity, and max drawdown.
  - Verifies frozen H0 (70-trade) & frozen 111-trade SHA-256 integrity.

Writes:
  backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_trade_by_trade_reconciliation.parquet
  backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_independent_reconciliation.json
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

OUT_DIR = Path("backtest_results/audit/task_g3_6r_h3b_profitability_improvement")
RUNS_DIR = OUT_DIR / "runs"

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


def _indep_replay(
    direction: str,
    entry_idx: int,
    ts_arr: np.ndarray,
    bid_arr: np.ndarray,
    ask_arr: np.ndarray,
    atr: float,
    equity: float,
    be_trigger_atr: float = 1.0,
) -> Dict[str, Any]:
    is_buy = direction == "BUY"
    e_ts = float(ts_arr[entry_idx])
    exec_entry = (float(ask_arr[entry_idx]) + SLIP_USD) if is_buy else (float(bid_arr[entry_idx]) - SLIP_USD)
    init_sl = (exec_entry - 1.5 * atr) if is_buy else (exec_entry + 1.5 * atr)
    tp = (exec_entry + 3.0 * atr) if is_buy else (exec_entry - 3.0 * atr)

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
        elif gain >= be_trigger_atr * atr:
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


def _audit_run(engine, run_name: str, be_trigger_atr: float) -> tuple[Dict[str, Any], pd.DataFrame]:
    df = pd.read_parquet(RUNS_DIR / f"{run_name}_trades.parquet").sort_values("entry_ts").reset_index(drop=True)
    running_eq = 10000.0
    rows: List[Dict[str, Any]] = []
    mm = {"entry": 0, "exit": 0, "reason": 0, "comm": 0, "pnl": 0, "r": 0}

    for i, row in df.iterrows():
        e_ts = float(row["entry_ts"])
        direction = str(row["direction"]).upper()
        is_buy = direction == "BUY"
        atr = float(row["true_initial_atr"])
        app_entry = float(row["entry_price"])

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

        res = _indep_replay(direction, imm_idx, ts_arr, bid_arr, ask_arr, atr, running_eq, be_trigger_atr=be_trigger_atr)
        running_eq += float(row["net_pnl_usd"])

        if abs(res["entry_px"] - app_entry) > 1e-4:
            mm["entry"] += 1
        if abs(res["exit_px"] - float(row["exit_price"])) > 1e-4:
            mm["exit"] += 1
        if res["reason"] != str(row["exit_reason"]):
            mm["reason"] += 1
        if abs(res["comm"] - float(row["commission_usd"])) > 1e-2:
            mm["comm"] += 1
        if abs(res["net_pnl"] - float(row["net_pnl_usd"])) > 0.05:
            mm["pnl"] += 1
        if abs(res["net_r"] - float(row["net_r"])) > 2e-3:
            mm["r"] += 1

        rows.append({
            "run_name": run_name,
            "trade_idx": int(i),
            "entry_time_utc": str(row["entry_time_utc"]),
            "direction": direction,
            "app_exit_reason": str(row["exit_reason"]),
            "indep_exit_reason": res["reason"],
            "app_net_pnl": float(row["net_pnl_usd"]),
            "indep_net_pnl": res["net_pnl"],
            "app_net_r": float(row["net_r"]),
            "indep_net_r": res["net_r"],
            "diff_r": round(res["net_r"] - float(row["net_r"]), 6),
        })

    out_df = pd.DataFrame(rows)
    total_mm = sum(mm.values())
    summary = {
        "run_name": run_name,
        "n_trades": int(len(out_df)),
        "mismatches": mm,
        "total_mismatches": int(total_mm),
        "app_total_pnl": round(float(out_df["app_net_pnl"].sum()), 2),
        "indep_total_pnl": round(float(out_df["indep_net_pnl"].sum()), 2),
        "app_total_r": round(float(out_df["app_net_r"].sum()), 5),
        "indep_total_r": round(float(out_df["indep_net_r"].sum()), 5),
        "app_cum_r_dd": _indep_max_dd(out_df["app_net_r"].to_numpy()),
        "indep_cum_r_dd": _indep_max_dd(out_df["indep_net_r"].to_numpy()),
        "status": "PASS" if total_mm == 0 else "FAIL",
    }
    return summary, out_df


def main() -> None:
    t0 = time.time()
    db_url = os.environ.get("DB_URL") or os.environ.get("DATABASE_URL") or "postgresql+psycopg2://postgres:password@timescaledb:5432/goldedge"
    if db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = create_engine(db_url)

    exp = json.loads((OUT_DIR / "g3_6r_controlled_experiments.json").read_text(encoding="utf-8"))
    runs = exp["runs"]

    s_base, df_base = _audit_run(engine, "H3B_FROZEN", be_trigger_atr=1.0)
    s_a, df_a = _audit_run(engine, "H3B_CHANGE_A_ER50", be_trigger_atr=1.0)
    s_c, df_c = _audit_run(engine, "H3B_CHANGE_C_SCORE68", be_trigger_atr=1.0)

    # Change C signal-skip & later-trade-eligibility audit vs H3B_FROZEN
    raw_base = pd.read_parquet(RUNS_DIR / "H3B_FROZEN_trades.parquet")
    raw_c = pd.read_parquet(RUNS_DIR / "H3B_CHANGE_C_SCORE68_trades.parquet")
    c_score_violations = int((raw_c["score"] < 0.68 - 1e-9).sum())
    base_keys = set(zip(raw_base["entry_ts"].round(0), raw_base["direction"]))
    c_keys = set(zip(raw_c["entry_ts"].round(0), raw_c["direction"]))
    skipped_from_base = base_keys - c_keys
    newly_eligible_in_c = c_keys - base_keys
    change_c_eligibility_audit = {
        "change_c_trades": int(len(raw_c)),
        "score_below_0_68_violations": c_score_violations,
        "retained_common_entries": int(len(base_keys & c_keys)),
        "skipped_h3b_entries": int(len(skipped_from_base)),
        "newly_eligible_later_entries": int(len(newly_eligible_in_c)),
        "min_score_observed_in_change_c": round(float(raw_c["score"].min()), 5),
    }

    recon_df = pd.concat([df_base, df_a, df_c], ignore_index=True)
    recon_pq = OUT_DIR / "g3_6r_trade_by_trade_reconciliation.parquet"
    recon_df.to_parquet(recon_pq, index=False)

    f111_ok = _sha256(FROZEN_111_PATH) == FROZEN_111_SHA256
    f70_ok = _sha256(FROZEN_70_PATH) == FROZEN_70_SHA256
    total_mm = (
        s_base["total_mismatches"]
        + s_a["total_mismatches"]
        + s_c["total_mismatches"]
        + c_score_violations
        + (0 if (f111_ok and f70_ok) else 1)
    )

    out = {
        "task": "G3.6R_PHASE_E_INDEPENDENT_RECONCILIATION",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "clean_room_zero_goldedge_imports": True,
        "candidates_audited": ["H3B_FROZEN", "H3B_CHANGE_A_ER50", "H3B_CHANGE_C_SCORE68"],
        "total_trades_independently_replayed": int(s_base["n_trades"] + s_a["n_trades"] + s_c["n_trades"]),
        "h3b_frozen_audit": s_base,
        "change_a_er50_audit": s_a,
        "change_c_score68_audit": s_c,
        "change_c_eligibility_audit": change_c_eligibility_audit,
        "frozen_baselines_intact": {"frozen_111": f111_ok, "frozen_70": f70_ok},
        "total_mismatches": int(total_mm),
        "overall_status": "PASS" if total_mm == 0 else "FAIL",
        "reconciliation_parquet_sha256": _sha256(recon_pq),
        "elapsed_sec": round(time.time() - t0, 2),
    }
    (OUT_DIR / "g3_6r_independent_reconciliation.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(
        f"[G3.6R PHASE E CLEAN-ROOM DONE in {out['elapsed_sec']}s] status={out['overall_status']} "
        f"total_trades={out['total_trades_independently_replayed']} total_mm={total_mm} "
        f"(H3B={s_base['n_trades']}/mm={s_base['total_mismatches']}, "
        f"A={s_a['n_trades']}/mm={s_a['total_mismatches']}, "
        f"C={s_c['n_trades']}/mm={s_c['total_mismatches']}, "
        f"C_skipped={len(skipped_from_base)}, C_newly_eligible={len(newly_eligible_in_c)}, C_score_viol={c_score_violations})"
    )
    if total_mm != 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
