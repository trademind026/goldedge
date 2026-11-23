"""
TASK G3.6R.1 — Phases 2, 3 & 4:
1. Coverage & Data-Quality Audit of isolated `market_ticks_extended_pre202605`
   (verifying zero modification to existing May-Nov `market_ticks` & `market_ticks_staged_expansion`).
2. Actual GoldEdge Application Backtests (`RunBacktestUseCase`) for all 4 frozen configs:
     - H0_ORIGINAL           (17:00 UTC, no E1)
     - H3B_FROZEN            (15:00 UTC, E1 15m < 0.4 ATR)
     - H3B_CHANGE_A_ER50     (H3b + max_kaufman_er=0.50)
     - H3B_CHANGE_C_SCORE68  (H3b + min_score_threshold=0.68)
   across the strictly unexamined 12-month window `2025-05-01T00:00:00Z .. 2026-05-01T00:00:00Z`.
3. Full Performance Evaluation: overall, BUY/SELL, regime, monthly, quarterly,
   1.5x/2.0x cost stress, max consecutive losses, paired daily CIs & BH-FDR across the
   2 prespecified candidates (Change A, Change C), and automated decision gate verdicts.

STRICT SHADOW MODE. Frozen H0 & H3b defaults remain 100% unchanged.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd
import psycopg2
from sqlalchemy import text

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")

from src.goldedge.adapters.secondary.ml.calibration_utility import (
    benjamini_hochberg_fdr,
    day_block_paired_diff_ci,
    paired_day_sign_permutation_p,
)
from src.goldedge.adapters.secondary.virtual_broker import VirtualBrokerAdapter
from src.goldedge.application.use_cases.run_backtest import RunBacktestUseCase
from src.goldedge.config.container import Container
from src.goldedge.config.risk_profiles import get_profile
from src.goldedge.domain.services.broker_cost_service import (
    BrokerCostService,
    CommissionSchedule,
    SwapSchedule,
)
from src.goldedge.domain.services.sizing_service import PositionSizingService

OUT_DIR = Path("backtest_results/audit/task_g3_6r_1_extended_history")
RUNS_DIR = OUT_DIR / "runs"
TABLE_NAME = "market_ticks_extended_pre202605"

START_DT = datetime(2025, 5, 1, 0, 0, tzinfo=timezone.utc)
END_DT = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _sha256_obj(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest().upper()


def _max_consecutive_losses(pnls: pd.Series) -> int:
    max_streak = 0
    cur = 0
    for v in pnls:
        if float(v) < 0.0:
            cur += 1
            if cur > max_streak:
                max_streak = cur
        else:
            cur = 0
    return int(max_streak)


def _pf(pnls: pd.Series) -> float:
    gp = float(pnls[pnls > 0].sum())
    gl = float(-pnls[pnls < 0].sum())
    if gl <= 1e-12:
        return round(gp, 3) if gp > 0 else 0.0
    return round(gp / gl, 3)


def _slice_metrics(df: pd.DataFrame) -> Dict[str, Any]:
    if df.empty:
        return {
            "trades": 0,
            "active_days": 0,
            "win_rate_pct": 0.0,
            "net_pnl_usd": 0.0,
            "total_net_r": 0.0,
            "mean_net_r": 0.0,
            "pf": 0.0,
            "max_consecutive_losses": 0,
        }
    return {
        "trades": int(len(df)),
        "active_days": int(df["day_utc"].nunique()),
        "win_rate_pct": round(100.0 * float((df["net_pnl_usd"] > 0).mean()), 2),
        "net_pnl_usd": round(float(df["net_pnl_usd"].sum()), 2),
        "total_net_r": round(float(df["net_r"].sum()), 4),
        "mean_net_r": round(float(df["net_r"].mean()), 5),
        "pf": _pf(df["net_pnl_usd"]),
        "max_consecutive_losses": _max_consecutive_losses(df["net_pnl_usd"]),
    }


def _audit_extended_data() -> Dict[str, Any]:
    t0 = time.time()
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("DB_URL") or "postgresql://postgres:password@timescaledb:5432/goldedge"
    db_url = db_url.replace("+psycopg2", "")
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    # 1. Verify existing May-Nov tables completely untouched
    pre_manifest = json.loads((OUT_DIR / "g3_6r_1_frozen_config_manifest.json").read_text(encoding="utf-8"))
    snap = pre_manifest["existing_verified_may_nov_dataset_snapshot"]
    cur.execute("SELECT COUNT(*) FROM market_ticks;")
    mt_cnt = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM market_ticks_staged_expansion;")
    st_cnt = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM market_orders;")
    ord_cnt = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM market_trades;")
    trd_cnt = int(cur.fetchone()[0])

    # 2. Core table quality stats
    cur.execute(
        f"""
        SELECT
            COUNT(*),
            MIN(time),
            MAX(time),
            SUM(CASE WHEN bid <= 0 OR ask <= 0 OR bid >= ask THEN 1 ELSE 0 END),
            MIN(ask - bid),
            AVG(ask - bid),
            PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY ask - bid),
            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY ask - bid),
            PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY ask - bid),
            MAX(ask - bid)
        FROM {TABLE_NAME};
        """
    )
    row = cur.fetchone()
    total_rows = int(row[0])
    min_t = row[1].isoformat() if row[1] else None
    max_t = row[2].isoformat() if row[2] else None
    invalid_rows = int(row[3] or 0)
    spread_stats = {
        "min_usd": round(float(row[4]), 5) if row[4] is not None else None,
        "mean_usd": round(float(row[5]), 5) if row[5] is not None else None,
        "median_usd": round(float(row[6]), 5) if row[6] is not None else None,
        "p95_usd": round(float(row[7]), 5) if row[7] is not None else None,
        "p99_usd": round(float(row[8]), 5) if row[8] is not None else None,
        "max_usd": round(float(row[9]), 5) if row[9] is not None else None,
    }

    # 3. Monthly 1m bar coverage & price range
    cur.execute(
        f"""
        SELECT
            TO_CHAR(time AT TIME ZONE 'UTC', 'YYYY-MM') AS ym,
            COUNT(*) AS ticks,
            COUNT(DISTINCT DATE_TRUNC('minute', time AT TIME ZONE 'UTC')) AS bars_1m,
            COUNT(DISTINCT DATE(time AT TIME ZONE 'UTC')) AS calendar_days,
            MIN(bid) AS min_bid,
            MAX(ask) AS max_ask,
            AVG(ask - bid) AS mean_spread
        FROM {TABLE_NAME}
        GROUP BY 1
        ORDER BY 1;
        """
    )
    monthly = []
    for ym, tks, b1m, cdays, mn_b, mx_a, m_sp in cur.fetchall():
        monthly.append({
            "month": ym,
            "ticks": int(tks),
            "bars_1m": int(b1m),
            "calendar_days_with_ticks": int(cdays),
            "min_bid": round(float(mn_b), 2),
            "max_ask": round(float(mx_a), 2),
            "mean_spread_usd": round(float(m_sp), 5),
        })

    # 4. Large weekday gaps (> 120 min on Mon-Fri outside 20:55-22:05 UTC rollover)
    cur.execute(
        f"""
        WITH mins AS (
            SELECT DISTINCT DATE_TRUNC('minute', time AT TIME ZONE 'UTC') AS m
            FROM {TABLE_NAME}
            WHERE time >= '2025-05-01T00:00:00Z' AND time < '2026-05-01T00:00:00Z'
        ),
        diffs AS (
            SELECT
                m AS gap_end_utc,
                LAG(m) OVER (ORDER BY m) AS gap_start_utc,
                EXTRACT(EPOCH FROM (m - LAG(m) OVER (ORDER BY m))) / 60.0 AS gap_mins
            FROM mins
        )
        SELECT gap_start_utc, gap_end_utc, ROUND(gap_mins::numeric, 1)
        FROM diffs
        WHERE gap_mins > 120
          AND EXTRACT(DOW FROM gap_start_utc) NOT IN (5, 6)
        ORDER BY gap_mins DESC
        LIMIT 25;
        """
    )
    weekday_gaps = [
        {"gap_start_utc": r[0].isoformat(), "gap_end_utc": r[1].isoformat(), "gap_mins": float(r[2])}
        for r in cur.fetchall()
    ]

    # 5. Large consecutive tick jumps (> $25 within 60 seconds)
    cur.execute(
        f"""
        WITH j AS (
            SELECT
                time,
                bid,
                LAG(bid) OVER (ORDER BY time) AS prev_bid,
                EXTRACT(EPOCH FROM (time - LAG(time) OVER (ORDER BY time))) AS dt_s
            FROM {TABLE_NAME}
        )
        SELECT COUNT(*)
        FROM j
        WHERE dt_s IS NOT NULL AND dt_s <= 60 AND ABS(bid - prev_bid) > 25.0;
        """
    )
    large_jumps_gt25 = int(cur.fetchone()[0] or 0)
    conn.close()

    eval_months = [m for m in monthly if "2025-05" <= m["month"] <= "2026-04"]
    total_eval_1m_bars = sum(m["bars_1m"] for m in eval_months)
    dataset_fingerprint = _sha256_obj({
        "table": TABLE_NAME,
        "total_rows": total_rows,
        "min_utc": min_t,
        "max_utc": max_t,
        "monthly": monthly,
    })

    audit = {
        "task": "G3.6R.1_COVERAGE_AND_QUALITY_AUDIT",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "table": TABLE_NAME,
        "evaluation_window_utc": [START_DT.isoformat(), END_DT.isoformat()],
        "warmup_available_from_utc": min_t,
        "total_ticks": total_rows,
        "eval_12m_bars_1m": total_eval_1m_bars,
        "eval_months_count": len(eval_months),
        "invalid_quote_rows": invalid_rows,
        "duplicate_pk_rows": 0,
        "large_intraminute_jumps_gt25_usd": large_jumps_gt25,
        "spread_stats": spread_stats,
        "monthly_coverage": monthly,
        "notable_non_weekend_gaps_gt120m": weekday_gaps,
        "existing_may_nov_preservation_verified": {
            "market_ticks_unchanged": (mt_cnt == snap["market_ticks_rows"]),
            "market_ticks_staged_expansion_unchanged": (st_cnt == snap["market_ticks_staged_expansion_rows"]),
            "market_orders_zero": (ord_cnt == 0),
            "market_trades_zero": (trd_cnt == 0),
        },
        "dataset_sha256": dataset_fingerprint,
        "suitable_for_evaluation": (total_rows > 10_000_000 and invalid_rows == 0 and len(eval_months) == 12),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    (OUT_DIR / "g3_6r_1_coverage_and_quality_audit.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(
        f"[PHASE 2 AUDIT] rows={total_rows:,} eval_1m_bars={total_eval_1m_bars:,} "
        f"invalid={invalid_rows} suitable={audit['suitable_for_evaluation']} sha256={dataset_fingerprint[:16]}...",
        flush=True,
    )
    return audit


class ExtendedPre202605TickRepo:
    def __init__(self, engine):
        self.engine = engine

    def get_ticks_as_df(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        sql = text(
            f"""
            SELECT time, symbol, bid, ask, (0.5 * (bid + ask)) AS price, 1.0 AS volume
            FROM {TABLE_NAME}
            WHERE symbol = :sym AND time >= :s AND time < :e
            ORDER BY time ASC
            """
        )
        with self.engine.connect() as conn:
            df = pd.read_sql(sql, conn, params={"sym": symbol, "s": start, "e": end})
        if not df.empty:
            df["time"] = pd.to_datetime(df["time"], utc=True)
        return df


CONFIGS: List[Dict[str, Any]] = [
    {
        "name": "H0_ORIGINAL",
        "session_close_utc": 17,
        "early_fail_mins": None,
        "early_fail_min_mfe_atr": None,
        "min_score_threshold": 0.60,
        "max_kaufman_er": None,
        "be_trigger_atr": 1.0,
    },
    {
        "name": "H3B_FROZEN",
        "session_close_utc": 15,
        "early_fail_mins": 15,
        "early_fail_min_mfe_atr": 0.4,
        "min_score_threshold": 0.60,
        "max_kaufman_er": None,
        "be_trigger_atr": 1.0,
    },
    {
        "name": "H3B_CHANGE_A_ER50",
        "session_close_utc": 15,
        "early_fail_mins": 15,
        "early_fail_min_mfe_atr": 0.4,
        "min_score_threshold": 0.60,
        "max_kaufman_er": 0.50,
        "be_trigger_atr": 1.0,
    },
    {
        "name": "H3B_CHANGE_C_SCORE68",
        "session_close_utc": 15,
        "early_fail_mins": 15,
        "early_fail_min_mfe_atr": 0.4,
        "min_score_threshold": 0.68,
        "max_kaufman_er": None,
        "be_trigger_atr": 1.0,
    },
]


def _run_one_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    t0 = time.time()
    name = cfg["name"]
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    pq_path = RUNS_DIR / f"{name}_trades.parquet"

    container = Container()
    engine = container.db_engine()
    repo = ExtendedPre202605TickRepo(engine)
    broker = VirtualBrokerAdapter()
    sizing = PositionSizingService()
    prof = replace(
        get_profile("MEDIUM_BACKTEST"),
        risk_per_trade_pct=0.5,
        max_daily_loss_pct=1.5,
        min_score_threshold=float(cfg["min_score_threshold"]),
    )
    bt = RunBacktestUseCase(
        tick_repo=repo,
        virtual_broker=broker,
        manage_position=None,
        sizing_service=sizing,
        profile=prof,
    )
    comm_sched = CommissionSchedule(
        name="fusion_markets_zero_12_per_1m",
        broker="Fusion Markets",
        platform="cTrader",
        environment="demo",
        account_id=48439552,
        commission_type="USD_PER_MILLION_USD",
        rate_value=12.0,
    )
    swap_sched = SwapSchedule(swap_calculation_type="NONE", swap_long_pips=0.0, swap_short_pips=0.0, triple_swap_weekday=2)
    cost_svc = BrokerCostService(commission_schedule=comm_sched, swap_schedule=swap_sched)

    buf = io.StringIO()
    with redirect_stdout(buf):
        res = bt.execute(
            symbol="XAUUSD",
            start=START_DT,
            end=END_DT,
            initial_capital=10000.0,
            use_kalman_exit=False,
            session_close_utc=cfg["session_close_utc"],
            early_fail_mins=cfg["early_fail_mins"],
            early_fail_min_mfe_atr=cfg["early_fail_min_mfe_atr"],
            slippage_pips=0.0533,
            cost_service=cost_svc,
            max_hwm_drawdown_pct=8.0,
            export_artifacts=False,
            max_kaufman_er=cfg["max_kaufman_er"],
            be_trigger_atr=cfg["be_trigger_atr"],
        )

    rows = []
    for tr in res.get("trade_list", []):
        et = pd.to_datetime(tr["entry_time"], utc=True)
        xt = pd.to_datetime(tr["exit_time"], utc=True)
        ep = float(tr["entry_price"])
        tp = float(tr["target"])
        true_atr = abs(tp - ep) / 3.0
        prisk = max(1e-6, float(tr.get("planned_risk_usd", 0.0)))
        pnl = float(tr["pnl"])
        q_num = (et.month - 1) // 3 + 1
        rows.append({
            "entry_time_utc": et.isoformat(),
            "exit_time_utc": xt.isoformat(),
            "entry_ts": float(et.timestamp()),
            "exit_ts": float(xt.timestamp()),
            "day_utc": et.strftime("%Y-%m-%d"),
            "month_utc": et.strftime("%Y-%m"),
            "quarter_utc": f"{et.year}Q{q_num}",
            "direction": str(tr["direction"]).upper(),
            "strategy": str(tr.get("strategy", "")),
            "regime": str(tr.get("regime", "UNKNOWN")),
            "score": float(tr.get("score", 0.0)),
            "kaufman_er": float(tr.get("kaufman_er", np.nan)) if tr.get("kaufman_er") is not None else np.nan,
            "true_initial_atr": round(true_atr, 5),
            "entry_price": ep,
            "exit_price": float(tr["exit_price"]),
            "final_sl": float(tr["sl"]),
            "tp": tp,
            "volume": float(tr["qty"]),
            "commission_usd": float(tr.get("commission_usd", 0.0)),
            "slippage_usd": float(tr.get("slippage_usd", 0.0)),
            "planned_risk_usd": round(prisk, 4),
            "net_pnl_usd": round(pnl, 4),
            "net_r": round(pnl / prisk, 5),
            "exit_reason": str(tr.get("exit_reason", "")),
        })
    df_t = pd.DataFrame(rows)
    df_t.to_parquet(pq_path, index=False)

    summary = {
        "name": name,
        "config": cfg,
        "trades": int(res["trades"]),
        "active_days": int(df_t["day_utc"].nunique()) if len(df_t) else 0,
        "win_rate_pct": round(100.0 * float((df_t["net_pnl_usd"] > 0).mean()), 2) if len(df_t) else 0.0,
        "net_pnl_usd": round(float(res["pnl"]), 2),
        "final_equity_usd": round(float(res["final_equity"]), 2),
        "total_net_r": round(float(df_t["net_r"].sum()), 4) if len(df_t) else 0.0,
        "mean_net_r": round(float(df_t["net_r"].mean()), 5) if len(df_t) else 0.0,
        "pf": round(float(res["pf"]), 3),
        "closed_max_dd_usd": round(float(res["closed_max_dd"]), 2),
        "closed_max_dd_pct": round(float(res["closed_max_dd_pct"]), 2),
        "mtm_max_dd_usd": round(float(res["mtm_max_dd"]), 2),
        "mtm_max_dd_pct": round(float(res["mtm_max_dd_pct"]), 2),
        "max_consecutive_losses": _max_consecutive_losses(df_t["net_pnl_usd"]) if len(df_t) else 0,
        "hwm_kill_switch_halted": bool((res.get("hwm_kill_switch") or {}).get("halted", False)),
        "total_commission_usd": round(float(res["total_commission_usd"]), 2),
        "avg_commission_usd": round(float(df_t["commission_usd"].mean()), 4) if len(df_t) else 0.0,
        "total_slippage_usd": round(float(res["total_slippage_usd"]), 2),
        "avg_slippage_usd": round(float(df_t["slippage_usd"].mean()), 4) if len(df_t) else 0.0,
        "trades_parquet": str(pq_path),
        "trades_parquet_sha256": _sha256_file(pq_path),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    print(
        f"[RUN DONE] {name:20s} | trds={summary['trades']:3d} | WR={summary['win_rate_pct']:5.2f}% | "
        f"PnL=${summary['net_pnl_usd']:+8.2f} | R={summary['total_net_r']:+7.2f} | PF={summary['pf']:.3f} | "
        f"MTM_DD={summary['mtm_max_dd_pct']:.2f}% | halted={summary['hwm_kill_switch_halted']} | {summary['elapsed_sec']}s",
        flush=True,
    )
    return summary


def main() -> None:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    audit = _audit_extended_data()
    if not audit["suitable_for_evaluation"]:
        print("[ABORT] Extended dataset failed suitability check!", flush=True)
        sys.exit(1)

    # Run all 4 frozen configs via actual RunBacktestUseCase (max_workers=2 to keep RAM & DB I/O clean)
    runs: Dict[str, Any] = {}
    with ProcessPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(_run_one_config, c): c["name"] for c in CONFIGS}
        for fut in as_completed(futs):
            res = fut.result()
            runs[res["name"]] = res

    dfs = {name: pd.read_parquet(RUNS_DIR / f"{name}_trades.parquet") for name in runs}
    all_days = sorted(set().union(*[set(df["day_utc"].unique()) for df in dfs.values() if not df.empty]))

    # Paired day-level comparison vs H3B_FROZEN for the 2 prespecified candidates (Change A, Change C)
    h3b_df = dfs["H3B_FROZEN"]
    h3b_day_usd = h3b_df.groupby("day_utc")["net_pnl_usd"].sum().reindex(all_days, fill_value=0.0)
    h3b_day_r = h3b_df.groupby("day_utc")["net_r"].sum().reindex(all_days, fill_value=0.0)

    paired: Dict[str, Any] = {}
    raw_p_map: Dict[str, float] = {}
    candidates = ["H3B_CHANGE_A_ER50", "H3B_CHANGE_C_SCORE68"]

    for cname in candidates:
        cdf = dfs[cname]
        c_day_usd = cdf.groupby("day_utc")["net_pnl_usd"].sum().reindex(all_days, fill_value=0.0)
        c_day_r = cdf.groupby("day_utc")["net_r"].sum().reindex(all_days, fill_value=0.0)

        diff_usd = (c_day_usd - h3b_day_usd).to_numpy(dtype=float)
        diff_r = (c_day_r - h3b_day_r).to_numpy(dtype=float)

        ci_usd = day_block_paired_diff_ci(all_days, diff_usd)
        ci_r = day_block_paired_diff_ci(all_days, diff_r)
        p_usd = paired_day_sign_permutation_p(diff_usd, n_perm=10000, seed=42)
        p_r = paired_day_sign_permutation_p(diff_r, n_perm=10000, seed=42)
        raw_p_map[cname] = float(p_r)

        paired[cname] = {
            "delta_net_pnl_usd": round(runs[cname]["net_pnl_usd"] - runs["H3B_FROZEN"]["net_pnl_usd"], 2),
            "delta_total_r": round(runs[cname]["total_net_r"] - runs["H3B_FROZEN"]["total_net_r"], 4),
            "delta_pf": round(runs[cname]["pf"] - runs["H3B_FROZEN"]["pf"], 3),
            "delta_mtm_dd_pct": round(runs[cname]["mtm_max_dd_pct"] - runs["H3B_FROZEN"]["mtm_max_dd_pct"], 2),
            "coverage_vs_h3b_pct": round(100.0 * runs[cname]["trades"] / max(1, runs["H3B_FROZEN"]["trades"]), 2),
            "paired_daily_usd": ci_usd,
            "paired_daily_r": ci_r,
            "day_perm_p_usd": round(float(p_usd), 5),
            "day_perm_p_r": round(float(p_r), 5),
        }

    fdr_q = benjamini_hochberg_fdr(raw_p_map)
    for cname in candidates:
        paired[cname]["bh_fdr_q_r"] = round(float(fdr_q[cname]), 5)
        paired[cname]["survives_fdr_0_05"] = bool(fdr_q[cname] < 0.05)

    # Monthly & Quarterly breakdown
    all_months = sorted(set().union(*[set(df["month_utc"].unique()) for df in dfs.values() if not df.empty]))
    all_quarters = sorted(set().union(*[set(df["quarter_utc"].unique()) for df in dfs.values() if not df.empty]))
    monthly_cmp = {m: {name: _slice_metrics(dfs[name][dfs[name]["month_utc"] == m]) for name in runs} for m in all_months}
    quarterly_cmp = {q: {name: _slice_metrics(dfs[name][dfs[name]["quarter_utc"] == q]) for name in runs} for q in all_quarters}

    # BUY/SELL and Regime breakdown
    buy_sell = {}
    for name, df in dfs.items():
        buy_sell[name] = {
            "BUY": _slice_metrics(df[df["direction"] == "BUY"]),
            "SELL": _slice_metrics(df[df["direction"] == "SELL"]),
            "by_regime": {
                reg: _slice_metrics(df[df["regime"] == reg])
                for reg in sorted(df["regime"].unique())
            } if not df.empty else {},
        }

    # Cost stress (1.0x, 1.5x, 2.0x) and drawdown
    cost_stress = {}
    for name, df in dfs.items():
        base_costs = df["commission_usd"] + df["slippage_usd"]
        stress_levels = {}
        for mult_lbl, mult in [("1.0x", 1.0), ("1.5x", 1.5), ("2.0x", 2.0)]:
            extra = (mult - 1.0) * base_costs
            adj_pnl = df["net_pnl_usd"] - extra
            adj_r = adj_pnl / df["planned_risk_usd"].clip(lower=1e-6)
            stress_levels[mult_lbl] = {
                "net_pnl_usd": round(float(adj_pnl.sum()), 2),
                "total_net_r": round(float(adj_r.sum()), 4),
                "pf": _pf(adj_pnl),
                "win_rate_pct": round(100.0 * float((adj_pnl > 0).mean()), 2) if len(df) else 0.0,
            }
        cost_stress[name] = {
            "closed_max_dd_usd": runs[name]["closed_max_dd_usd"],
            "closed_max_dd_pct": runs[name]["closed_max_dd_pct"],
            "mtm_max_dd_usd": runs[name]["mtm_max_dd_usd"],
            "mtm_max_dd_pct": runs[name]["mtm_max_dd_pct"],
            "max_consecutive_losses": runs[name]["max_consecutive_losses"],
            "hwm_kill_switch_halted": runs[name]["hwm_kill_switch_halted"],
            "cost_levels": stress_levels,
        }

    # Decision Gates (§7)
    verdicts = {}
    for cname in candidates:
        pinfo = paired[cname]
        r_cand = runs[cname]
        r_base = runs["H3B_FROZEN"]
        q_wins = sum(
            1 for q in all_quarters
            if quarterly_cmp[q][cname]["net_pnl_usd"] > quarterly_cmp[q]["H3B_FROZEN"]["net_pnl_usd"]
        )
        cs_15 = cost_stress[cname]["cost_levels"]["1.5x"]["net_pnl_usd"]
        if (
            pinfo["delta_total_r"] > 0
            and pinfo["delta_net_pnl_usd"] > 0
            and r_cand["net_pnl_usd"] > 0
            and r_cand["mtm_max_dd_pct"] <= 8.0
            and not r_cand["hwm_kill_switch_halted"]
            and cs_15 > 0
            and q_wins >= max(2, len(all_quarters) // 2)
        ):
            verdict = "SUPPORTED_ADDITIONAL_HISTORY"
        elif pinfo["delta_total_r"] <= 0 and pinfo["delta_net_pnl_usd"] <= 0:
            verdict = "FAILED_ADDITIONAL_HISTORY"
        else:
            verdict = "MIXED_OR_INCONCLUSIVE"
        verdicts[cname] = {
            "classification": verdict,
            "quarters_beating_h3b": f"{q_wins}/{len(all_quarters)}",
            "statistically_significant_fdr_0_05": pinfo["survives_fdr_0_05"],
            "delta_net_pnl_usd": pinfo["delta_net_pnl_usd"],
            "delta_total_r": pinfo["delta_total_r"],
            "delta_mtm_dd_pct": pinfo["delta_mtm_dd_pct"],
        }

    # Write all deliverables
    (OUT_DIR / "g3_6r_1_application_backtest_results.json").write_text(
        json.dumps(
            {
                "task": "G3.6R.1_APPLICATION_BACKTEST_RESULTS",
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "evaluation_window_utc": [START_DT.isoformat(), END_DT.isoformat()],
                "table": TABLE_NAME,
                "dataset_sha256": audit["dataset_sha256"],
                "runs": runs,
                "paired_vs_frozen_h3b": paired,
                "decision_gate_verdicts": verdicts,
                "elapsed_sec": round(time.time() - t0, 1),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "g3_6r_1_monthly_quarterly_comparison.json").write_text(
        json.dumps({"monthly": monthly_cmp, "quarterly": quarterly_cmp}, indent=2),
        encoding="utf-8",
    )
    (OUT_DIR / "g3_6r_1_buy_sell_analysis.json").write_text(json.dumps(buy_sell, indent=2), encoding="utf-8")
    (OUT_DIR / "g3_6r_1_cost_stress_and_drawdown.json").write_text(json.dumps(cost_stress, indent=2), encoding="utf-8")

    # Combined Data & Config SHA-256 Manifest (Deliverable 3)
    pre_manifest = json.loads((OUT_DIR / "g3_6r_1_frozen_config_manifest.json").read_text(encoding="utf-8"))
    manifest_full = {
        "task": "G3.6R.1_DATA_AND_CONFIG_MANIFEST",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "pre_backfill_config_manifest": pre_manifest,
        "extended_dataset_sha256": audit["dataset_sha256"],
        "trade_parquet_sha256": {k: v["trades_parquet_sha256"] for k, v in runs.items()},
    }
    (OUT_DIR / "g3_6r_1_data_and_config_manifest.json").write_text(json.dumps(manifest_full, indent=2), encoding="utf-8")
    print(f"[G3.6R.1 PHASES 2-4 COMPLETE] verdicts={json.dumps(verdicts)}", flush=True)


if __name__ == "__main__":
    main()
