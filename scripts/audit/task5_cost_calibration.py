"""
Task 5 — Fusion Markets Actual Trading Cost Verification & Backtest Cost Calibration.

Strictly independent Python + raw-SQL audit script (imports NO production
execution, sizing, or cost-accounting modules). Uses authoritative cTrader Open API
account/symbol/deal evidence from `task5_account_specifications.json` and raw
TimescaleDB `market_ticks` (`2026-08-24 .. 2026-11-16`).

Generates:
  - backtest_results/audit/task5_commission_reconciliation.csv
  - backtest_results/audit/task5_spread_analysis.csv
  - backtest_results/audit/task5_swap_reconciliation.csv
  - backtest_results/audit/task5_cost_scenario_comparison.csv
  - backtest_results/audit/task5_trade_level_cost_ledger.csv
  - backtest_results/audit/task5_evidence_manifest.json
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

RAW_DB_URL = os.environ.get(
    "DB_URL", "postgresql+psycopg2://postgres:password@timescaledb:5432/goldedge"
)
if RAW_DB_URL.startswith("postgresql://"):
    RAW_DB_URL = RAW_DB_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

AUDIT_DIR = Path("backtest_results/audit")
SPEC_PATH = AUDIT_DIR / "task5_account_specifications.json"
FROZEN_T4_STRESSED_CSV = AUDIT_DIR / "task4_run_1.csv"
FROZEN_T4_ZERO_CSV = AUDIT_DIR / "task4_zero_cost_ref.csv"
FROZEN_111_CSV = Path("backtest_results/frozen_111_baseline_trades.csv")
EXPECTED_FROZEN_111_SHA = "73306e1529adc48284bce9ab6ac35122f33b9999d2464af441867eb3bb6cf168"

CONTRACT_OZ = 100.0
PIP_SIZE = 0.10
TICK_SIZE = 0.01
TICK_VALUE_USD = 1.0
MIN_LOT = 0.01
MAX_LOT = 50.0
LOT_STEP = 0.01
RISK_PCT = 1.0


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_ts(val: Any) -> datetime:
    if isinstance(val, datetime):
        return val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
    s = str(val).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _classify_outcome(pnl: float) -> str:
    cents = round(float(pnl), 2)
    if cents > 0.0:
        return "WIN"
    if cents < 0.0:
        return "LOSS"
    return "BREAKEVEN"


def _commission_side_notional(lots: float, exec_px: float, rate_per_million: float = 12.0) -> float:
    notional = lots * CONTRACT_OZ * exec_px
    return round(notional * rate_per_million / 1_000_000.0, 2)


def _reconcile_broker_deals(engine, spec: Dict[str, Any]) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    deals = sorted(spec["deal_history_180d"]["deals"], key=lambda x: x["executionTimestamp_utc"])
    comm_rate_per_m = spec["xauusd_spec"]["commission_raw"] / 100.0  # 1200 cents = $12.00 per $1M

    rows: List[Dict[str, Any]] = []
    slips_usd_per_oz: List[float] = []
    max_comm_diff = 0.0
    max_rt_comm_diff = 0.0

    with engine.connect() as conn:
        for d in deals:
            ts = _parse_ts(d["executionTimestamp_utc"])
            lots = float(d["volume_raw"]) / float(spec["xauusd_spec"]["lotSize_raw_cents"])
            px = float(d["executionPrice"])
            notional = lots * CONTRACT_OZ * px
            expected_side = -round(notional * comm_rate_per_m / 1_000_000.0, 2)
            actual_side = float(d["commission_usd"])
            side_diff = round(actual_side - expected_side, 6)
            max_comm_diff = max(max_comm_diff, abs(side_diff))

            cd = d.get("closePositionDetail")
            rt_expected = None
            rt_actual = None
            rt_diff = None
            implied_rt_per_lot = round(abs(actual_side) * 2.0 / lots, 4) if lots > 0 else 0.0
            if cd is not None:
                c_lots = float(cd["closedVolume_raw"]) / float(spec["xauusd_spec"]["lotSize_raw_cents"])
                en_px = float(cd["entryPrice"])
                rt_expected = -(
                    round(c_lots * CONTRACT_OZ * en_px * comm_rate_per_m / 1_000_000.0, 2)
                    + round(c_lots * CONTRACT_OZ * px * comm_rate_per_m / 1_000_000.0, 2)
                )
                rt_actual = float(cd["commission"])
                rt_diff = round(rt_actual - rt_expected, 6)
                max_rt_comm_diff = max(max_rt_comm_diff, abs(rt_diff))
                implied_rt_per_lot = round(abs(rt_actual) / c_lots, 4) if c_lots > 0 else 0.0

            # Lookup nearest pre-fill valid tick in market_ticks
            pre = conn.execute(
                text(
                    """
                    SELECT time, bid, ask
                    FROM market_ticks
                    WHERE symbol = 'XAUUSD' AND time >= :s AND time <= :e AND bid > 0 AND ask > bid
                    ORDER BY time DESC
                    LIMIT 1
                    """
                ),
                {"s": ts - timedelta(seconds=5), "e": ts},
            ).fetchone()
            pre_bid = float(pre[1]) if pre else None
            pre_ask = float(pre[2]) if pre else None
            pre_ms = round((ts - pre[0]).total_seconds() * 1000.0, 1) if pre else None
            slip_adv = None
            if pre is not None:
                slip_adv = (px - pre_ask) if d["tradeSide"] == "BUY" else (pre_bid - px)
                slips_usd_per_oz.append(slip_adv)

            rows.append(
                {
                    "deal_id": d["dealId"],
                    "position_id": d["positionId"],
                    "execution_time_utc": ts.isoformat(),
                    "trade_side": d["tradeSide"],
                    "event_type": "CLOSE" if cd is not None else "OPEN",
                    "lots": lots,
                    "exec_price": px,
                    "notional_usd": round(notional, 2),
                    "expected_side_comm_usd": expected_side,
                    "actual_side_comm_usd": actual_side,
                    "side_comm_diff_usd": side_diff,
                    "actual_rt_comm_usd": rt_actual,
                    "expected_rt_comm_usd": rt_expected,
                    "rt_comm_diff_usd": rt_diff,
                    "implied_rt_comm_per_lot_usd": implied_rt_per_lot,
                    "actual_swap_usd": float(cd["swap"]) if cd is not None else 0.0,
                    "pre_tick_bid": pre_bid,
                    "pre_tick_ask": pre_ask,
                    "pre_tick_age_ms": pre_ms,
                    "adverse_slip_usd_per_oz": round(slip_adv, 4) if slip_adv is not None else None,
                    "adverse_slip_pips": round(slip_adv / PIP_SIZE, 4) if slip_adv is not None else None,
                }
            )

    df_deals = pd.DataFrame(rows)
    summary = {
        "deals_count": len(rows),
        "closed_positions_count": int(df_deals["actual_rt_comm_usd"].notna().sum()),
        "commission_rate_per_million_usd_per_side": comm_rate_per_m,
        "max_side_comm_diff_usd": max_comm_diff,
        "max_rt_comm_diff_usd": max_rt_comm_diff,
        "mean_implied_rt_per_lot_usd": round(float(df_deals["implied_rt_comm_per_lot_usd"].mean()), 2),
        "min_implied_rt_per_lot_usd": round(float(df_deals["implied_rt_comm_per_lot_usd"].min()), 2),
        "max_implied_rt_per_lot_usd": round(float(df_deals["implied_rt_comm_per_lot_usd"].max()), 2),
        "demo_slippage_samples": len(slips_usd_per_oz),
        "demo_slippage_zero_count": sum(1 for x in slips_usd_per_oz if abs(x) < 1e-6),
        "demo_slippage_median_pips": round(float(np.median(slips_usd_per_oz) / PIP_SIZE), 4) if slips_usd_per_oz else None,
        "demo_slippage_mean_pips": round(float(np.mean(slips_usd_per_oz) / PIP_SIZE), 4) if slips_usd_per_oz else None,
        "demo_slippage_max_pips": round(float(np.max(slips_usd_per_oz) / PIP_SIZE), 4) if slips_usd_per_oz else None,
    }
    return df_deals, summary


def _analyze_spreads(engine) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Compute historical valid spread percentiles over the entire 85-day Task 4 replay window."""
    base_where = """
        symbol = 'XAUUSD'
        AND time >= '2026-08-24 00:00:00+00'
        AND time <  '2026-11-16 20:01:23+00'
        AND bid > 0 AND ask > 0 AND ask > bid
    """

    def _agg_sql(group_expr: str, extra_where: str = "") -> str:
        return f"""
            SELECT
                {group_expr} AS bucket,
                COUNT(*) AS valid_ticks,
                AVG(ask - bid) AS mean_usd,
                MIN(ask - bid) AS min_usd,
                MAX(ask - bid) AS max_usd,
                PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY ask - bid) AS p50_usd,
                PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY ask - bid) AS p75_usd,
                PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY ask - bid) AS p90_usd,
                PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY ask - bid) AS p95_usd,
                PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY ask - bid) AS p99_usd
            FROM market_ticks
            WHERE {base_where} {extra_where}
            GROUP BY 1
            ORDER BY 2 DESC
        """

    with engine.connect() as conn:
        overall = conn.execute(text(_agg_sql("'OVERALL'"))).fetchall()
        sessions = conn.execute(
            text(
                _agg_sql(
                    """
                    CASE
                        WHEN EXTRACT(HOUR FROM time AT TIME ZONE 'UTC') BETWEEN 0 AND 6 THEN 'Session: Asia (00-06 UTC)'
                        WHEN EXTRACT(HOUR FROM time AT TIME ZONE 'UTC') BETWEEN 7 AND 11 THEN 'Session: London (07-11 UTC)'
                        WHEN EXTRACT(HOUR FROM time AT TIME ZONE 'UTC') BETWEEN 12 AND 16 THEN 'Session: Overlap (12-16 UTC)'
                        WHEN EXTRACT(HOUR FROM time AT TIME ZONE 'UTC') BETWEEN 17 AND 20 THEN 'Session: NY Late (17-20 UTC)'
                        ELSE 'Session: Rollover Window (21-23 UTC)'
                    END
                    """
                )
            )
        ).fetchall()
        rollover = conn.execute(
            text(
                _agg_sql(
                    """
                    CASE
                        WHEN (EXTRACT(HOUR FROM time AT TIME ZONE 'UTC') = 20 AND EXTRACT(MINUTE FROM time AT TIME ZONE 'UTC') >= 55)
                          OR EXTRACT(HOUR FROM time AT TIME ZONE 'UTC') IN (21, 22)
                          OR (EXTRACT(HOUR FROM time AT TIME ZONE 'UTC') = 23 AND EXTRACT(MINUTE FROM time AT TIME ZONE 'UTC') <= 5)
                        THEN 'Rollover +/-5m (20:55-23:05 UTC)'
                        ELSE 'Non-Rollover Hours'
                    END
                    """
                )
            )
        ).fetchall()
        vol_buckets = conn.execute(
            text(
                f"""
                WITH m AS (
                    SELECT
                        date_trunc('minute', time) AS min_ts,
                        MAX((bid + ask) / 2.0) - MIN((bid + ask) / 2.0) AS range_1m
                    FROM market_ticks
                    WHERE {base_where}
                    GROUP BY 1
                ),
                thresh AS (
                    SELECT
                        PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY range_1m) AS q90,
                        PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY range_1m) AS q95
                    FROM m
                )
                SELECT
                    CASE
                        WHEN m.range_1m >= t.q95 THEN 'Volatility: Top 5% High-Vol Minutes (>=p95 1m range)'
                        WHEN m.range_1m >= t.q90 THEN 'Volatility: 90th-95th Pct Minutes'
                        ELSE 'Volatility: Normal Minutes (<p90 1m range)'
                    END AS bucket,
                    COUNT(*) AS valid_ticks,
                    AVG(mt.ask - mt.bid) AS mean_usd,
                    MIN(mt.ask - mt.bid) AS min_usd,
                    MAX(mt.ask - mt.bid) AS max_usd,
                    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY mt.ask - mt.bid) AS p50_usd,
                    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY mt.ask - mt.bid) AS p75_usd,
                    PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY mt.ask - mt.bid) AS p90_usd,
                    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY mt.ask - mt.bid) AS p95_usd,
                    PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY mt.ask - mt.bid) AS p99_usd
                FROM market_ticks mt
                JOIN m ON date_trunc('minute', mt.time) = m.min_ts
                CROSS JOIN thresh t
                WHERE {base_where.replace('symbol =', 'mt.symbol =').replace('time >=', 'mt.time >=').replace('time <', 'mt.time <').replace('bid >', 'mt.bid >').replace('ask >', 'mt.ask >')}
                GROUP BY 1
                ORDER BY 2 DESC
                """
            )
        ).fetchall()

    out_rows = []
    for grp_rows in (overall, sessions, rollover, vol_buckets):
        for r in grp_rows:
            out_rows.append(
                {
                    "segment": str(r[0]),
                    "valid_ticks": int(r[1]),
                    "mean_usd": round(float(r[2]), 4),
                    "min_usd": round(float(r[3]), 4),
                    "max_usd": round(float(r[4]), 4),
                    "median_p50_usd": round(float(r[5]), 4),
                    "p75_usd": round(float(r[6]), 4),
                    "p90_usd": round(float(r[7]), 4),
                    "p95_usd": round(float(r[8]), 4),
                    "p99_usd": round(float(r[9]), 4),
                    "mean_pips": round(float(r[2]) / PIP_SIZE, 4),
                    "median_p50_pips": round(float(r[5]) / PIP_SIZE, 4),
                    "p95_pips": round(float(r[8]) / PIP_SIZE, 4),
                    "p99_pips": round(float(r[9]) / PIP_SIZE, 4),
                }
            )
    df_sp = pd.DataFrame(out_rows)
    ov = df_sp[df_sp["segment"] == "OVERALL"].iloc[0].to_dict()
    return df_sp, ov


@dataclass
class _TradeSim:
    trade_idx: int
    direction: str
    strategy: str
    signal_minute: datetime
    entry_tick_ts: datetime
    entry_bid: float
    entry_ask: float
    entry_price: float
    sl: float
    tp: float
    atr: float
    lots: float
    commission_fn: Callable[[float, float], float]
    slippage_pips: float
    target_spread_floor_usd: float = 0.0
    rollover_utc_hour: int = 21
    bars_held: int = 0
    peak_price: float = 0.0
    trail_active: bool = False
    flag: str = ""
    entry_commission_usd: float = 0.0
    exit_commission_usd: float = 0.0
    extra_spread_stress_usd: float = 0.0
    swap_usd: float = 0.0

    def __post_init__(self) -> None:
        self.peak_price = self.entry_price
        self.entry_commission_usd = self.commission_fn(self.lots, self.entry_price)
        actual_en_sp = self.entry_ask - self.entry_bid
        if self.target_spread_floor_usd > actual_en_sp:
            self.extra_spread_stress_usd += 0.5 * (self.target_spread_floor_usd - actual_en_sp) * self.lots * CONTRACT_OZ

    @property
    def is_bull(self) -> bool:
        return self.direction == "BUY"

    @property
    def slip_offset(self) -> float:
        return self.slippage_pips * PIP_SIZE

    def gross_pnl_exec(self, exit_px: float) -> float:
        diff = (exit_px - self.entry_price) if self.is_bull else (self.entry_price - exit_px)
        return diff * self.lots * CONTRACT_OZ

    def net_pnl_floating(self, quote_px: float) -> float:
        return self.gross_pnl_exec(quote_px) - self.entry_commission_usd - self.extra_spread_stress_usd - self.swap_usd

    def eff_max_hold(self) -> int:
        return 60 if (7 <= self.entry_tick_ts.hour < 20) else 90

    def ratchet(self, px: float) -> None:
        if (self.is_bull and px > self.peak_price) or (not self.is_bull and px < self.peak_price):
            self.peak_price = px
        gain = (self.peak_price - self.entry_price) if self.is_bull else (self.entry_price - self.peak_price)
        atr = max(self.atr, 1e-9)
        if gain >= 2.5 * atr:
            self.trail_active = True
            cand = (self.peak_price - 0.75 * atr) if self.is_bull else (self.peak_price + 0.75 * atr)
            if (self.is_bull and cand > self.sl) or (not self.is_bull and cand < self.sl):
                self.sl = cand
                self.flag = "TRAILING_SL"
        elif gain >= 1.6 * atr:
            self.trail_active = True
            cand = (self.peak_price - 1.10 * atr) if self.is_bull else (self.peak_price + 1.10 * atr)
            if (self.is_bull and cand > self.sl) or (not self.is_bull and cand < self.sl):
                self.sl = cand
                self.flag = "TRAILING_SL"
        elif gain >= 1.0 * atr:
            cand = (self.entry_price + 2.0 * PIP_SIZE) if self.is_bull else (self.entry_price - 2.0 * PIP_SIZE)
            if (self.is_bull and cand > self.sl) or (not self.is_bull and cand < self.sl):
                self.sl = cand
                self.flag = "BREAKEVEN"

        mhold = self.eff_max_hold()
        if mhold > 0 and self.bars_held / mhold >= 0.75:
            prog = min((self.bars_held / mhold - 0.75) / 0.25, 1.0)
            if self.is_bull:
                tsl = self.sl + prog * (px - self.sl)
                if tsl > self.sl:
                    self.sl = tsl
                    self.flag = "TIME_SQUEEZE"
            else:
                tsl = self.sl - prog * (self.sl - px)
                if tsl < self.sl:
                    self.sl = tsl
                    self.flag = "TIME_SQUEEZE"


def _run_independent_scenario(
    engine,
    signals: List[Dict[str, Any]],
    scenario_name: str,
    description: str,
    side_comm_fn: Callable[[float, float], float],
    sizing_rt_comm_per_lot_fn: Callable[[float], float],
    slippage_pips: float,
    target_spread_floor_usd: float = 0.0,
    fixed_lots_by_idx: Optional[List[float]] = None,
) -> Dict[str, Any]:
    start_dt = datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc)
    sig_by_min = {
        _parse_ts(s["entry_time"]).replace(second=0, microsecond=0): s for s in signals
    }

    equity = 10000.0
    closed_hwm = 10000.0
    closed_max_dd = 0.0
    closed_max_dd_pct = 0.0
    mtm_hwm = 10000.0
    mtm_max_dd = 0.0
    mtm_max_dd_pct = 0.0

    pos: Optional[_TradeSim] = None
    trades: List[Dict[str, Any]] = []
    last_valid_tick: Optional[Tuple[float, float, datetime]] = None
    t_counter = 0

    def _upd_mtm(eq_val: float) -> None:
        nonlocal mtm_hwm, mtm_max_dd, mtm_max_dd_pct
        if eq_val > mtm_hwm:
            mtm_hwm = eq_val
        dd = eq_val - mtm_hwm
        dd_pct = (mtm_hwm - eq_val) / mtm_hwm * 100.0 if mtm_hwm > 0 else 0.0
        if dd < mtm_max_dd:
            mtm_max_dd = dd
        if dd_pct > mtm_max_dd_pct:
            mtm_max_dd_pct = dd_pct

    def _size_trade(eq: float, en_px: float, atr: float, d: str, idx0: int) -> Tuple[Optional[float], float, float]:
        sl_px = (en_px - 1.5 * atr) if d == "BUY" else (en_px + 1.5 * atr)
        tp_px = (en_px + 3.0 * atr) if d == "BUY" else (en_px - 3.0 * atr)
        if fixed_lots_by_idx is not None:
            return float(fixed_lots_by_idx[idx0]), sl_px, tp_px
        budget = eq * (RISK_PCT / 100.0)
        est_comm = sizing_rt_comm_per_lot_fn(en_px)
        loss_per_lot = (abs(en_px - sl_px) / TICK_SIZE) * TICK_VALUE_USD + est_comm
        if loss_per_lot <= 0 or MIN_LOT * loss_per_lot > budget + 1e-6:
            return None, sl_px, tp_px
        lots = round(math.floor((budget / loss_per_lot + 1e-12) / LOT_STEP) * LOT_STEP, 2)
        while lots >= MIN_LOT and (lots * loss_per_lot) > budget + 1e-6:
            lots = round(lots - LOT_STEP, 2)
        if lots < MIN_LOT:
            return None, sl_px, tp_px
        return min(MAX_LOT, lots), sl_px, tp_px

    def _close_trade(
        p: _TradeSim,
        reason: str,
        ex_px: float,
        ex_b: float,
        ex_a: float,
        ex_ts: datetime,
        ex_slip_usd: float,
    ) -> None:
        nonlocal equity, closed_hwm, closed_max_dd, closed_max_dd_pct
        ex_comm = side_comm_fn(p.lots, ex_px)
        p.exit_commission_usd = ex_comm
        actual_ex_sp = ex_a - ex_b
        if target_spread_floor_usd > actual_ex_sp:
            p.extra_spread_stress_usd += 0.5 * (target_spread_floor_usd - actual_ex_sp) * p.lots * CONTRACT_OZ

        tot_comm = p.entry_commission_usd + p.exit_commission_usd
        entry_slip = p.slip_offset * p.lots * CONTRACT_OZ
        tot_slip = entry_slip + ex_slip_usd

        # Mid-to-mid decomposition so zero spread is double-counted
        entry_mid = 0.5 * (p.entry_bid + p.entry_ask)
        exit_mid = 0.5 * (ex_b + ex_a)
        gross_mid = ((exit_mid - entry_mid) if p.is_bull else (entry_mid - exit_mid)) * p.lots * CONTRACT_OZ
        en_sp_cost = 0.5 * (p.entry_ask - p.entry_bid) * p.lots * CONTRACT_OZ
        raw_ex_q = ex_b if p.is_bull else ex_a
        limit_imp = (((ex_px - raw_ex_q) if p.is_bull else (raw_ex_q - ex_px)) * p.lots * CONTRACT_OZ) if reason == "TARGET" else 0.0
        ex_sp_cost = 0.5 * (ex_a - ex_b) * p.lots * CONTRACT_OZ - limit_imp
        actual_sp_cost = en_sp_cost + ex_sp_cost

        net = p.gross_pnl_exec(ex_px) - tot_comm - p.extra_spread_stress_usd - p.swap_usd
        equity += net
        if equity > closed_hwm:
            closed_hwm = equity
        cdd = equity - closed_hwm
        cdd_pct = (closed_hwm - equity) / closed_hwm * 100.0 if closed_hwm > 0 else 0.0
        if cdd < closed_max_dd:
            closed_max_dd = cdd
        if cdd_pct > closed_max_dd_pct:
            closed_max_dd_pct = cdd_pct

        trades.append(
            {
                "trade_idx": p.trade_idx,
                "direction": p.direction,
                "strategy": p.strategy,
                "entry_time": p.entry_tick_ts.isoformat(),
                "exit_time": ex_ts.isoformat(),
                "entry_bid": p.entry_bid,
                "entry_ask": p.entry_ask,
                "entry_price": p.entry_price,
                "exit_bid": ex_b,
                "exit_ask": ex_a,
                "exit_price": ex_px,
                "qty": p.lots,
                "exit_reason": reason,
                "gross_mid_pnl_usd": round(gross_mid, 6),
                "actual_tick_spread_usd": round(actual_sp_cost, 6),
                "extra_spread_stress_usd": round(p.extra_spread_stress_usd, 6),
                "total_spread_cost_usd": round(actual_sp_cost + p.extra_spread_stress_usd, 6),
                "commission_entry_usd": round(p.entry_commission_usd, 6),
                "commission_exit_usd": round(p.exit_commission_usd, 6),
                "total_commission_usd": round(tot_comm, 6),
                "slippage_entry_usd": round(entry_slip, 6),
                "slippage_exit_usd": round(ex_slip_usd, 6),
                "total_slippage_usd": round(tot_slip, 6),
                "swap_usd": round(p.swap_usd, 6),
                "net_pnl": net,
                "net_pnl_rounded": round(net, 6),
                "outcome": _classify_outcome(net),
                "equity_after": round(equity, 4),
            }
        )

    chunk_start = start_dt
    while chunk_start < end_dt:
        chunk_end = min(chunk_start + timedelta(days=7), end_dt)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT time, bid, ask
                    FROM market_ticks
                    WHERE symbol = 'XAUUSD' AND time >= :s AND time < :e
                    ORDER BY time ASC
                    """
                ),
                {"s": chunk_start, "e": chunk_end},
            ).fetchall()
        chunk_start = chunk_end
        if not rows:
            continue

        by_min: Dict[datetime, List[Tuple[datetime, float, float]]] = defaultdict(list)
        for r_t, r_b, r_a in rows:
            dt = r_t if r_t.tzinfo is not None else r_t.replace(tzinfo=timezone.utc)
            by_min[dt.replace(second=0, microsecond=0)].append((dt, float(r_b or 0.0), float(r_a or 0.0)))

        for cur_min in sorted(by_min.keys()):
            ticks = by_min[cur_min]

            def _scan(start_k: int, eval_timeout: bool) -> bool:
                nonlocal pos, last_valid_tick
                if pos is None:
                    return False
                last_pair = None
                best_q = None
                for k in range(start_k, len(ticks)):
                    dt_k, b, a = ticks[k]
                    if b <= 0.0 or a <= 0.0 or b >= a:
                        continue
                    last_valid_tick = (b, a, dt_k)
                    q = b if pos.is_bull else a
                    last_pair = (q, b, a, dt_k)
                    if best_q is None or (pos.is_bull and q > best_q) or (not pos.is_bull and q < best_q):
                        best_q = q

                    # Compute approximate floating equity including expected exit comm
                    unr = pos.net_pnl_floating(q) - side_comm_fn(pos.lots, q)
                    if (pos.is_bull and b <= pos.sl) or (not pos.is_bull and a >= pos.sl):
                        reason = pos.flag if pos.flag else ("TRAILING_SL" if pos.trail_active else "SL")
                        ex_px = (b - pos.slip_offset) if pos.is_bull else (a + pos.slip_offset)
                        ex_slip = pos.slip_offset * pos.lots * CONTRACT_OZ
                        _upd_mtm(equity + pos.net_pnl_floating(ex_px) - side_comm_fn(pos.lots, ex_px))
                        _close_trade(pos, reason, ex_px, b, a, dt_k, ex_slip)
                        pos = None
                        return True
                    if (pos.is_bull and b >= pos.tp) or (not pos.is_bull and a <= pos.tp):
                        ex_px = float(pos.tp)
                        _upd_mtm(equity + pos.net_pnl_floating(ex_px) - side_comm_fn(pos.lots, ex_px))
                        _close_trade(pos, "TARGET", ex_px, b, a, dt_k, 0.0)
                        pos = None
                        return True
                    _upd_mtm(equity + unr)
                    pos.ratchet(q)

                if eval_timeout and last_pair is not None and pos.bars_held >= pos.eff_max_hold():
                    ql, lb, la, ldt = last_pair
                    ex_px = (ql - pos.slip_offset) if pos.is_bull else (ql + pos.slip_offset)
                    ex_slip = pos.slip_offset * pos.lots * CONTRACT_OZ
                    _close_trade(pos, "TIMEOUT", ex_px, lb, la, ldt, ex_slip)
                    pos = None
                    return True
                if best_q is not None:
                    pos.ratchet(best_q)
                return False

            if pos is not None:
                pos.bars_held += 1
                _scan(0, eval_timeout=True)
                continue

            if cur_min in sig_by_min:
                sig = sig_by_min[cur_min]
                d = str(sig["direction"]).upper()
                first_v = next(((k, dt_k, b, a) for k, (dt_k, b, a) in enumerate(ticks) if b > 0 and a > 0 and b < a), None)
                if first_v is not None:
                    en_i, en_dt, en_b, en_a = first_v
                    slip_off = slippage_pips * PIP_SIZE
                    raw_en = en_a if d == "BUY" else en_b
                    en_px = (raw_en + slip_off) if d == "BUY" else (raw_en - slip_off)
                    lots, sl_px, tp_px = _size_trade(equity, en_px, float(sig["atr"]), d, t_counter)
                    if lots is not None:
                        t_counter += 1
                        pos = _TradeSim(
                            trade_idx=t_counter,
                            direction=d,
                            strategy=str(sig["strategy"]),
                            signal_minute=cur_min,
                            entry_tick_ts=en_dt,
                            entry_bid=en_b,
                            entry_ask=en_a,
                            entry_price=en_px,
                            sl=sl_px,
                            tp=tp_px,
                            atr=float(sig["atr"]),
                            lots=lots,
                            commission_fn=side_comm_fn,
                            slippage_pips=slippage_pips,
                            target_spread_floor_usd=target_spread_floor_usd,
                        )
                        init_unr = -(en_a - en_b) * lots * CONTRACT_OZ - pos.entry_commission_usd - pos.extra_spread_stress_usd - (slip_off * lots * CONTRACT_OZ)
                        _upd_mtm(equity + init_unr)
                        if en_i + 1 < len(ticks):
                            _scan(en_i + 1, eval_timeout=False)

    if pos is not None and last_valid_tick is not None:
        lb, la, ldt = last_valid_tick
        q = lb if pos.is_bull else la
        ex_px = (q - pos.slip_offset) if pos.is_bull else (q + pos.slip_offset)
        ex_slip = pos.slip_offset * pos.lots * CONTRACT_OZ
        _close_trade(pos, "END_OF_BACKTEST", ex_px, lb, la, ldt, ex_slip)
        pos = None

    n = len(trades)
    wins = sum(1 for t in trades if t["outcome"] == "WIN")
    losses = sum(1 for t in trades if t["outcome"] == "LOSS")
    be = sum(1 for t in trades if t["outcome"] == "BREAKEVEN")
    gp = sum(t["net_pnl"] for t in trades if t["outcome"] == "WIN")
    gl = abs(sum(t["net_pnl"] for t in trades if t["outcome"] == "LOSS"))
    pf = gp / gl if gl > 0 else 999.0
    net_sum = sum(t["net_pnl"] for t in trades)
    gross_mid_sum = sum(t["gross_mid_pnl_usd"] for t in trades)
    sp_sum = sum(t["total_spread_cost_usd"] for t in trades)
    comm_sum = sum(t["total_commission_usd"] for t in trades)
    slip_sum = sum(t["total_slippage_usd"] for t in trades)
    swap_sum = sum(t["swap_usd"] for t in trades)

    max_id_err = max(
        abs(t["gross_mid_pnl_usd"] - t["total_spread_cost_usd"] - t["total_commission_usd"] - t["total_slippage_usd"] - t["swap_usd"] - t["net_pnl"])
        for t in trades
    ) if trades else 0.0

    return {
        "scenario": scenario_name,
        "description": description,
        "trades": n,
        "wins": wins,
        "losses": losses,
        "breakevens": be,
        "win_rate_pct": round(wins / n * 100.0, 2) if n else 0.0,
        "gross_mid_pnl_usd": round(gross_mid_sum, 2),
        "spread_cost_usd": round(sp_sum, 2),
        "commission_usd": round(comm_sum, 2),
        "slippage_usd": round(slip_sum, 2),
        "swap_usd": round(swap_sum, 2),
        "net_pnl_usd": round(net_sum, 2),
        "pf": round(pf, 4),
        "final_equity_usd": round(equity, 2),
        "closed_max_dd_usd": round(closed_max_dd, 2),
        "closed_max_dd_pct": round(closed_max_dd_pct, 2),
        "mtm_max_dd_usd": round(mtm_max_dd, 2),
        "mtm_max_dd_pct": round(mtm_max_dd_pct, 2),
        "max_identity_error_usd": round(max_id_err, 6),
        "trade_list": trades,
    }


def main() -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    frozen_111_sha = _sha256(FROZEN_111_CSV)
    assert frozen_111_sha == EXPECTED_FROZEN_111_SHA

    t4_str_sha_before = _sha256(FROZEN_T4_STRESSED_CSV)
    t4_zero_sha_before = _sha256(FROZEN_T4_ZERO_CSV)

    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8-sig"))
    engine = create_engine(RAW_DB_URL)

    print("[1/5] Reconciling 15 actual broker deals on CTRADER_ACCOUNT_ID=48439552...")
    df_deals, deal_summary = _reconcile_broker_deals(engine, spec)
    df_deals.to_csv(AUDIT_DIR / "task5_commission_reconciliation.csv", index=False)

    print("[2/5] Computing historical XAUUSD spread statistics (~17.3M ticks)...")
    df_spreads, sp_overall = _analyze_spreads(engine)
    df_spreads.to_csv(AUDIT_DIR / "task5_spread_analysis.csv", index=False)
    p95_spread_usd = float(sp_overall["p95_usd"])

    print("[3/5] Auditing overnight rollover & swap across all 118 trades...")
    df_t4_str = pd.read_csv(FROZEN_T4_STRESSED_CSV)
    df_t4_zero = pd.read_csv(FROZEN_T4_ZERO_CSV)
    xspec = spec["xauusd_spec"]
    swap_long_usd_per_lot = float(xspec["swapLong"]) * (10 ** (-int(xspec["pipPosition"]))) * CONTRACT_OZ
    swap_short_usd_per_lot = float(xspec["swapShort"]) * (10 ** (-int(xspec["pipPosition"]))) * CONTRACT_OZ

    swap_rows = []
    crossed_21_total = 0
    crossed_22_total = 0
    for idx, r in df_t4_str.iterrows():
        en_t = _parse_ts(r["entry_time"])
        ex_t = _parse_ts(r["exit_time"])
        b21 = en_t.replace(hour=21, minute=0, second=0, microsecond=0)
        b22 = en_t.replace(hour=22, minute=0, second=0, microsecond=0)
        c21 = en_t < b21 <= ex_t
        c22 = en_t < b22 <= ex_t
        if c21:
            crossed_21_total += 1
        if c22:
            crossed_22_total += 1
        lots = float(r["qty"])
        is_wed = en_t.weekday() == 2
        mult = 3 if is_wed else 1
        hypo_1night = (swap_long_usd_per_lot if r["direction"] == "BUY" else swap_short_usd_per_lot) * lots * mult
        swap_rows.append(
            {
                "trade_idx": idx + 1,
                "direction": r["direction"],
                "entry_time": en_t.isoformat(),
                "exit_time": ex_t.isoformat(),
                "weekday": en_t.strftime("%A"),
                "lots": lots,
                "crossed_21utc_edt": c21,
                "crossed_22utc_est": c22,
                "wednesday_3x_applies": is_wed and (c21 or c22),
                "realized_swap_usd": 0.0 if not (c21 or c22) else round(hypo_1night, 2),
                "task4_swap_usd": float(r["swap_usd"]),
                "hypothetical_overnight_swap_if_held_usd": round(hypo_1night, 2),
            }
        )
    pd.DataFrame(swap_rows).to_csv(AUDIT_DIR / "task5_swap_reconciliation.csv", index=False)

    print("[4/5] Running Independent Scenarios A, B, C0, C, D across 118 signals...")
    sigs = [
        {
            "entry_time": r["entry_time"],
            "direction": r["direction"],
            "strategy": r["strategy"],
            "atr": abs(float(r["entry_price"]) - float(r["sl"])) / 1.5,
        }
        for _, r in df_t4_zero.iterrows()
    ]

    rate_m = float(xspec["commission_raw"]) / 100.0  # 12.0 USD per $1M notional per side
    demo_mean_slip_pips = float(deal_summary["demo_slippage_mean_pips"] or 0.0)  # ~0.0533p

    scen_A = _run_independent_scenario(
        engine, sigs, "A_zero_cost", "Zero-cost reference ($0 comm, 0.0p slip, $0 swap)",
        side_comm_fn=lambda l, px: 0.0,
        sizing_rt_comm_per_lot_fn=lambda px: 0.0,
        slippage_pips=0.0,
    )
    scen_B = _run_independent_scenario(
        engine, sigs, "B_task4_stress_7usd_03p", "Task 4 baseline stress ($7/lot RT comm, 0.3p/side slip, $0 swap)",
        side_comm_fn=lambda l, px: 3.5 * l,
        sizing_rt_comm_per_lot_fn=lambda px: 7.0,
        slippage_pips=0.3,
    )
    scen_C0 = _run_independent_scenario(
        engine, sigs, "C0_verified_broker_demo_median_0p", "Verified broker notional comm ($12/$1M/side) + 0.0p demo median slip + actual swap ($0)",
        side_comm_fn=lambda l, px: _commission_side_notional(l, px, rate_m),
        sizing_rt_comm_per_lot_fn=lambda px: 2.0 * _commission_side_notional(1.0, px, rate_m),
        slippage_pips=0.0,
    )
    scen_C = _run_independent_scenario(
        engine, sigs, "C_verified_broker_demo_mean", f"Verified broker notional comm ($12/$1M/side) + {demo_mean_slip_pips:.4f}p demo mean slip + actual swap ($0)",
        side_comm_fn=lambda l, px: _commission_side_notional(l, px, rate_m),
        sizing_rt_comm_per_lot_fn=lambda px: 2.0 * _commission_side_notional(1.0, px, rate_m),
        slippage_pips=demo_mean_slip_pips,
    )
    scen_D = _run_independent_scenario(
        engine, sigs, "D_conservative_p95_spread_03p_slip", f"Verified broker notional comm ($12/$1M/side) + p95 spread floor (${p95_spread_usd:.2f}) + 0.3p/side slip + actual swap ($0)",
        side_comm_fn=lambda l, px: _commission_side_notional(l, px, rate_m),
        sizing_rt_comm_per_lot_fn=lambda px: 2.0 * _commission_side_notional(1.0, px, rate_m),
        slippage_pips=0.3,
        target_spread_floor_usd=p95_spread_usd,
    )

    # Also compute exact fixed-quantity attribution on frozen Task 4 zero-cost lots so cost deltas isolate purely cost terms
    fixed_lots_zero = [float(x) for x in df_t4_zero["qty"]]
    scen_C_fixed = _run_independent_scenario(
        engine, sigs, "C_fixed_qty_check", "Fixed-qty check under verified broker costs",
        side_comm_fn=lambda l, px: _commission_side_notional(l, px, rate_m),
        sizing_rt_comm_per_lot_fn=lambda px: 2.0 * _commission_side_notional(1.0, px, rate_m),
        slippage_pips=demo_mean_slip_pips,
        fixed_lots_by_idx=fixed_lots_zero,
    )

    scenarios = [scen_A, scen_B, scen_C0, scen_C, scen_D]
    comp_rows = [
        {k: v for k, v in s.items() if k != "trade_list"}
        for s in scenarios
    ]
    pd.DataFrame(comp_rows).to_csv(AUDIT_DIR / "task5_cost_scenario_comparison.csv", index=False)

    print("[5/5] Building 118-Trade Cost Ledger & Reconciling Scenarios A and B against Task 4...")
    # Verify exact $0.000000 match vs Task 4 frozen files
    max_b_pnl_diff = max(abs(float(r["pnl"]) - float(t["net_pnl"])) for (_, r), t in zip(df_t4_str.iterrows(), scen_B["trade_list"]))
    max_a_pnl_diff = max(abs(float(r["pnl"]) - float(t["net_pnl"])) for (_, r), t in zip(df_t4_zero.iterrows(), scen_A["trade_list"]))
    assert max_b_pnl_diff < 1e-6, f"Scenario B mismatch: {max_b_pnl_diff}"
    assert max_a_pnl_diff < 1e-6, f"Scenario A mismatch: {max_a_pnl_diff}"

    ledger_rows = []
    for i in range(118):
        ta = scen_A["trade_list"][i]
        tb = scen_B["trade_list"][i]
        tc0 = scen_C0["trade_list"][i]
        tc = scen_C["trade_list"][i]
        td = scen_D["trade_list"][i]
        implied_rt_per_lot_b = round(tb["total_commission_usd"] / tb["qty"], 2) if tb["qty"] else 0.0
        implied_rt_per_lot_c = round(tc["total_commission_usd"] / tc["qty"], 2) if tc["qty"] else 0.0
        ledger_rows.append(
            {
                "trade_idx": i + 1,
                "direction": tb["direction"],
                "entry_time": tb["entry_time"],
                "exit_time": tb["exit_time"],
                "exit_reason_B": tb["exit_reason"],
                "exit_reason_C": tc["exit_reason"],
                "qty_B": tb["qty"],
                "qty_C": tc["qty"],
                "gross_mid_pnl_A": ta["gross_mid_pnl_usd"],
                "gross_mid_pnl_B": tb["gross_mid_pnl_usd"],
                "gross_mid_pnl_C": tc["gross_mid_pnl_usd"],
                "gross_mid_pnl_D": td["gross_mid_pnl_usd"],
                "spread_cost_A": ta["total_spread_cost_usd"],
                "spread_cost_B": tb["total_spread_cost_usd"],
                "spread_cost_C": tc["total_spread_cost_usd"],
                "spread_cost_D_incl_p95_floor": td["total_spread_cost_usd"],
                "comm_B_7usd": tb["total_commission_usd"],
                "comm_C0_verified": tc0["total_commission_usd"],
                "comm_C_verified": tc["total_commission_usd"],
                "comm_D_verified": td["total_commission_usd"],
                "rt_comm_per_lot_B": implied_rt_per_lot_b,
                "rt_comm_per_lot_C": implied_rt_per_lot_c,
                "slip_B_03p": tb["total_slippage_usd"],
                "slip_C0_0p": tc0["total_slippage_usd"],
                "slip_C_demo_mean": tc["total_slippage_usd"],
                "slip_D_03p": td["total_slippage_usd"],
                "swap_usd": 0.0,
                "net_pnl_A": round(ta["net_pnl"], 6),
                "net_pnl_B_task4": round(tb["net_pnl"], 6),
                "net_pnl_C0_demo_median": round(tc0["net_pnl"], 6),
                "net_pnl_C_demo_mean": round(tc["net_pnl"], 6),
                "net_pnl_D_stress": round(td["net_pnl"], 6),
            }
        )
    df_ledger = pd.DataFrame(ledger_rows)
    df_ledger.to_csv(AUDIT_DIR / "task5_trade_level_cost_ledger.csv", index=False)

    # Confirm Task 4 frozen files unmodified
    assert _sha256(FROZEN_T4_STRESSED_CSV) == t4_str_sha_before
    assert _sha256(FROZEN_T4_ZERO_CSV) == t4_zero_sha_before
    assert _sha256(FROZEN_111_CSV) == EXPECTED_FROZEN_111_SHA

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "outcome_tag": "BROKER_COST_BASELINE_V1",
        "account_evidence": {
            "broker": spec["account"]["brokerName"],
            "account_id": spec["ctid_trader_account_id"],
            "environment": spec["trading_environment"],
            "account_mode_ctrader_enum": spec["account"]["accountType"],
            "retail_marketing_tier": "UNVERIFIED_ENUM (inferred commission-bearing Raw/Zero structure from $12/$1M USD_PER_MILLION_USD rate and $0 minCommission)",
            "currency": spec["account"]["depositCurrency"],
            "leverage": spec["account"]["leverageRatio"],
            "swap_free": spec["account"]["swapFree"],
            "balance_usd": spec["account"]["balance"],
            "open_positions": spec["open_broker_state"]["open_positions"],
            "pending_orders": spec["open_broker_state"]["pending_orders"],
        },
        "deal_and_commission_evidence": deal_summary,
        "spread_overall": sp_overall,
        "swap_evidence": {
            "swap_calculation_type": xspec["swapCalculationType"],
            "swap_long_pips": xspec["swapLong"],
            "swap_long_usd_per_lot_night": round(swap_long_usd_per_lot, 2),
            "swap_short_pips": xspec["swapShort"],
            "swap_short_usd_per_lot_night": round(swap_short_usd_per_lot, 2),
            "triple_swap_day": xspec["swapRollover3Days"],
            "swap_rollover_time_utc_minutes": xspec["swapTime_minutes_from_midnight"],
            "swap_rollover_time_utc": f"{int(xspec['swapTime_minutes_from_midnight']) // 60:02d}:{int(xspec['swapTime_minutes_from_midnight']) % 60:02d} UTC",
            "rollover_crossings_21utc_in_118": crossed_21_total,
            "rollover_crossings_22utc_in_118": crossed_22_total,
        },
        "reconciliation_checks": {
            "max_scenario_A_vs_task4_zero_diff_usd": round(max_a_pnl_diff, 8),
            "max_scenario_B_vs_task4_stressed_diff_usd": round(max_b_pnl_diff, 8),
            "frozen_111_sha256_intact": True,
            "frozen_task4_sha256_intact": True,
        },
        "scenarios": comp_rows,
        "fixed_qty_check_C": {k: v for k, v in scen_C_fixed.items() if k != "trade_list"},
        "ledger_effective_rt_commission_per_lot": {
            "task4_assumed": 7.0,
            "verified_c_mean": round(float(df_ledger["rt_comm_per_lot_C"].mean()), 2),
            "verified_c_min": round(float(df_ledger["rt_comm_per_lot_C"].min()), 2),
            "verified_c_max": round(float(df_ledger["rt_comm_per_lot_C"].max()), 2),
        },
    }
    (AUDIT_DIR / "task5_evidence_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
