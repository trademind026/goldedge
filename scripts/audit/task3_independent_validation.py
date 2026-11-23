"""
Task 3 — XAUUSD Independent Post-Remediation Validation Script (Phases 1, 3, 4, 5, 6).

Strictly read-only against database & frozen artifacts.
Does NOT import RunBacktestUseCase or IndependentExecutionSimulator so that execution,
costs, spread accounting, and tick-level MTM drawdown are validated via a completely
independent SQL + pure-NumPy/Python calculation path.

Outputs:
  - backtest_results/audit/task3_phase1_reconciliation.csv
  - backtest_results/audit/task3_cost_reconciliation_118.csv
  - backtest_results/audit/task3_phase4_exclusions_daily.csv
  - backtest_results/audit/task3_phase4_gaps.csv
  - backtest_results/audit/task3_validation_summary.json
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

RAW_DB_URL = os.environ.get(
    "DB_URL", "postgresql+psycopg2://postgres:password@timescaledb:5432/goldedge"
)
if RAW_DB_URL.startswith("postgresql://"):
    RAW_DB_URL = RAW_DB_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

AUDIT_DIR = Path("backtest_results/audit")
FROZEN_PATH = Path("backtest_results/frozen_111_baseline_trades.csv")
EXPECTED_FROZEN_SHA256 = (
    "73306e1529adc48284bce9ab6ac35122f33b9999d2464af441867eb3bb6cf168"
)
POST_REM_BASE_CSV = AUDIT_DIR / "post_remediation_baseline_trades.csv"
POST_REM_SUMMARY = AUDIT_DIR / "post_remediation_summary.json"

CONTRACT_OZ = 100.0
PIP_SIZE = 0.10  # XAUUSD: 1 pip = $0.10 price move
TICK_SIZE = 0.01
TICK_VALUE_USD = 1.0  # $0.01 * 100 oz = $1.00 per standard lot
MIN_LOT = 0.01
MAX_LOT = 50.0
LOT_STEP = 0.01
RISK_PCT = 1.0  # MEDIUM_RISK_BACKTEST risk_per_trade_pct


def _parse_ts(val: Any) -> datetime:
    if isinstance(val, datetime):
        return val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
    s = str(val).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _utc_session(hour: int) -> str:
    if 0 <= hour < 7:
        return "Asia (00-06)"
    if 7 <= hour < 12:
        return "London (07-11)"
    if 12 <= hour < 17:
        return "Overlap (12-16)"
    if 17 <= hour < 21:
        return "NY Late (17-20)"
    return "Rollover (21-23)"


def _independent_position_size(
    equity: float,
    entry_px: float,
    atr: float,
    direction: str,
    commission_per_lot: float = 0.0,
) -> Tuple[Optional[float], float, float, float]:
    """
    Independent sizing check without calling PositionSizingService.
    SL = 1.5 * ATR, TP = 3.0 * ATR, risk budget = equity * 1%.
    """
    sl_dist = 1.5 * atr
    tp_dist = 3.0 * atr
    sl_px = (entry_px - sl_dist) if direction == "BUY" else (entry_px + sl_dist)
    tp_px = (entry_px + tp_dist) if direction == "BUY" else (entry_px - tp_dist)
    budget = equity * (RISK_PCT / 100.0)
    loss_per_lot = (abs(entry_px - sl_px) / TICK_SIZE) * TICK_VALUE_USD + float(
        commission_per_lot
    )
    if loss_per_lot <= 0 or MIN_LOT * loss_per_lot > budget + 1e-6:
        return None, sl_px, tp_px, 0.0
    lots = round(math.floor((budget / loss_per_lot + 1e-12) / LOT_STEP) * LOT_STEP, 2)
    while lots >= MIN_LOT and (lots * loss_per_lot) > budget + 1e-6:
        lots = round(lots - LOT_STEP, 2)
    if lots < MIN_LOT:
        return None, sl_px, tp_px, 0.0
    lots = min(MAX_LOT, lots)
    planned_risk_usd = lots * loss_per_lot
    return lots, sl_px, tp_px, planned_risk_usd


@dataclass
class IndependentTradeState:
    trade_idx: int
    direction: str
    strategy: str
    signal_candle_close: datetime
    entry_tick_ts: datetime
    entry_bid: float
    entry_ask: float
    entry_price: float
    sl: float
    tp: float
    atr: float
    lots: float
    planned_risk_usd: float
    commission_per_lot: float
    slippage_pips: float
    rollover_utc_hour: int = 21
    bars_held: int = 0
    peak_price: float = 0.0
    trail_active: bool = False
    flag: str = ""
    swap_usd: float = 0.0
    last_swap_date: Optional[Any] = None
    last_gap_usd: float = 0.0
    sl_gap_events: int = 0
    zero_tick_minutes: int = 0
    all_invalid_minutes: int = 0

    def __post_init__(self) -> None:
        self.peak_price = self.entry_price
        if (
            self.entry_tick_ts.hour >= self.rollover_utc_hour
        ):
            self.last_swap_date = self.entry_tick_ts.date()

    @property
    def is_bull(self) -> bool:
        return self.direction == "BUY"

    @property
    def slip_offset(self) -> float:
        return self.slippage_pips * PIP_SIZE

    @property
    def commission_total_usd(self) -> float:
        return self.commission_per_lot * self.lots

    def gross_pnl_exec(self, exit_price: float) -> float:
        diff = (exit_price - self.entry_price) if self.is_bull else (self.entry_price - exit_price)
        return diff * self.lots * CONTRACT_OZ

    def net_pnl(self, exit_price: float) -> float:
        return self.gross_pnl_exec(exit_price) - self.commission_total_usd - self.swap_usd

    def eff_max_hold(self) -> int:
        return 60 if (7 <= self.entry_tick_ts.hour < 20) else 90

    def ratchet(self, px: float) -> None:
        if (self.is_bull and px > self.peak_price) or (
            not self.is_bull and px < self.peak_price
        ):
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


def _independent_replay(
    engine,
    signals: List[Dict[str, Any]],
    start_dt: datetime,
    end_dt: datetime,
    commission_per_lot: float = 0.0,
    slippage_pips: float = 0.0,
    rollover_utc_hour: int = 21,
    entry_delay_seconds: float = 0.0,
    entry_delay_ticks: int = 0,
    hard_stop_hwm_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Completely independent raw-SQL tick replay using only entry timestamps + ATR + direction/strategy.
    Returns trades, tick-level MTM DD curve summary, missing-tick stats, and HWM gate counts.
    """
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
    mtm_peak_ts: Optional[datetime] = start_dt
    mtm_trough_ts: Optional[datetime] = start_dt
    mtm_trough_equity = 10000.0
    mtm_peak_at_max_dd = 10000.0
    raw_mtm_hwm = 10000.0
    raw_mtm_max_dd = 0.0

    hard_stop_active = False
    hard_stop_triggered_at: Optional[str] = None
    blocked_by_hard_stop = 0

    pos: Optional[IndependentTradeState] = None
    trades: List[Dict[str, Any]] = []
    last_valid_tick: Optional[Tuple[float, float, datetime]] = None

    def _update_mtm(eq_fill: float, eq_raw: float, ts: datetime, gap_usd: float = 0.0) -> None:
        nonlocal mtm_hwm, mtm_max_dd, mtm_max_dd_pct, mtm_peak_ts, mtm_trough_ts
        nonlocal mtm_trough_equity, mtm_peak_at_max_dd, raw_mtm_hwm, raw_mtm_max_dd
        nonlocal hard_stop_active, hard_stop_triggered_at
        if eq_fill > mtm_hwm:
            mtm_hwm = eq_fill
            mtm_peak_ts = ts
        dd = eq_fill - mtm_hwm
        dd_pct = (mtm_hwm - eq_fill) / mtm_hwm * 100.0 if mtm_hwm > 0 else 0.0
        if dd < mtm_max_dd:
            mtm_max_dd = dd
        if dd_pct > mtm_max_dd_pct:
            mtm_max_dd_pct = dd_pct
            mtm_trough_ts = ts
            mtm_trough_equity = eq_fill
            mtm_peak_at_max_dd = mtm_hwm
        if hard_stop_hwm_pct is not None and not hard_stop_active and dd_pct >= hard_stop_hwm_pct:
            hard_stop_active = True
            hard_stop_triggered_at = ts.isoformat()

        if eq_raw > raw_mtm_hwm:
            raw_mtm_hwm = eq_raw
        rdd = eq_raw - raw_mtm_hwm
        if rdd < raw_mtm_max_dd:
            raw_mtm_max_dd = rdd

    def _record_exit(
        p: IndependentTradeState,
        reason: str,
        ex_px: float,
        ex_bid: float,
        ex_ask: float,
        ex_ts: datetime,
        exit_slip_usd: float,
    ) -> None:
        nonlocal equity, closed_hwm, closed_max_dd, closed_max_dd_pct
        net = p.net_pnl(ex_px)
        equity += net
        if equity > closed_hwm:
            closed_hwm = equity
        cdd = equity - closed_hwm
        cdd_pct = (closed_hwm - equity) / closed_hwm * 100.0 if closed_hwm > 0 else 0.0
        if cdd < closed_max_dd:
            closed_max_dd = cdd
        if cdd_pct > closed_max_dd_pct:
            closed_max_dd_pct = cdd_pct

        entry_mid = 0.5 * (p.entry_bid + p.entry_ask)
        exit_mid = 0.5 * (ex_bid + ex_ask)
        gross_mid_pnl = (
            ((exit_mid - entry_mid) if p.is_bull else (entry_mid - exit_mid))
            * p.lots
            * CONTRACT_OZ
        )
        entry_spread_cost = 0.5 * (p.entry_ask - p.entry_bid) * p.lots * CONTRACT_OZ
        # On limit TP fill at self.tp, market exit quote may slightly exceed tp; decompose exactly:
        # gross_exec_pnl = (ex_px - entry_px) * lots * 100
        # For MARKET exits: ex_px = exec_quote +/- slip; for TP limit: ex_px = p.tp.
        raw_exit_q = ex_bid if p.is_bull else ex_ask
        limit_improvement = (
            ((ex_px - raw_exit_q) if p.is_bull else (raw_exit_q - ex_px)) * p.lots * CONTRACT_OZ
            if reason == "TARGET"
            else 0.0
        )
        exit_spread_cost = 0.5 * (ex_ask - ex_bid) * p.lots * CONTRACT_OZ - limit_improvement
        entry_slip_usd = p.slip_offset * p.lots * CONTRACT_OZ
        comm_half = 0.5 * p.commission_total_usd

        trades.append(
            {
                "trade_idx": p.trade_idx,
                "direction": p.direction,
                "strategy": p.strategy,
                "entry_time": p.entry_tick_ts.isoformat(),
                "signal_minute": p.signal_candle_close.isoformat(),
                "exit_time": ex_ts.isoformat(),
                "entry_bid": round(p.entry_bid, 4),
                "entry_ask": round(p.entry_ask, 4),
                "entry_price": round(p.entry_price, 6),
                "exit_bid": round(ex_bid, 4),
                "exit_ask": round(ex_ask, 4),
                "exit_price": round(ex_px, 6),
                "sl_final": round(p.sl, 6),
                "tp": round(p.tp, 6),
                "atr": round(p.atr, 6),
                "qty": p.lots,
                "exit_reason": reason,
                "bars_held": p.bars_held,
                "gross_mid_pnl_usd": round(gross_mid_pnl, 6),
                "entry_spread_cost_usd": round(entry_spread_cost, 6),
                "exit_spread_cost_usd": round(exit_spread_cost, 6),
                "total_spread_cost_usd": round(entry_spread_cost + exit_spread_cost, 6),
                "slippage_entry_usd": round(entry_slip_usd, 6),
                "slippage_exit_usd": round(exit_slip_usd, 6),
                "total_slippage_usd": round(entry_slip_usd + exit_slip_usd, 6),
                "commission_entry_usd": round(comm_half, 6),
                "commission_exit_usd": round(comm_half, 6),
                "total_commission_usd": round(p.commission_total_usd, 6),
                "swap_usd": round(p.swap_usd, 6),
                "net_pnl": round(net, 6),
                "equity_after": round(equity, 4),
                "sl_gap_usd": round(p.last_gap_usd, 6),
                "sl_gap_events": p.sl_gap_events,
                "zero_tick_mins_open": p.zero_tick_minutes,
                "all_invalid_mins_open": p.all_invalid_minutes,
                "planned_risk_usd": round(p.planned_risk_usd, 4),
            }
        )

    # Stream ticks in 7-day chunks cleanly via raw SQL
    chunk_start = start_dt
    t_idx_counter = 0
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

        # Group by floored UTC minute cleanly in Python
        by_min: Dict[datetime, List[Tuple[datetime, float, float]]] = defaultdict(list)
        for r_t, r_b, r_a in rows:
            dt = r_t if r_t.tzinfo is not None else r_t.replace(tzinfo=timezone.utc)
            m = dt.replace(second=0, microsecond=0)
            by_min[m].append((dt, float(r_b or 0.0), float(r_a or 0.0)))

        # Walk minute by minute across chunk (checking empty minutes if pos open)
        all_mins = sorted(by_min.keys())
        cur_min = all_mins[0]
        last_chunk_min = all_mins[-1]
        while cur_min <= last_chunk_min:
            ticks_in_min = by_min.get(cur_min)
            if not ticks_in_min:
                if pos is not None:
                    pos.zero_tick_minutes += 1
                    pos.bars_held += 1
                    # Note: if bars_held crosses eff_max_hold on zero-tick minute, wait for next tick (same as RunBacktestUseCase skipping empty minute? Wait: RunBacktestUseCase iterates df_min which skips zero-tick minutes!)
                cur_min += timedelta(minutes=1)
                continue

            valid_any = any(b > 0 and a > 0 and b < a for _, b, a in ticks_in_min)
            if pos is not None and not valid_any:
                pos.all_invalid_minutes += 1

            def _scan_ticks(start_k: int, eval_timeout: bool) -> bool:
                nonlocal pos, last_valid_tick
                if pos is None:
                    return False
                last_valid_pair: Optional[Tuple[float, float, float, datetime]] = None
                best_valid_q: Optional[float] = None
                for k in range(start_k, len(ticks_in_min)):
                    dt_k, b, a = ticks_in_min[k]
                    if b <= 0.0 or a <= 0.0 or b >= a:
                        continue
                    last_valid_tick = (b, a, dt_k)
                    q = b if pos.is_bull else a
                    last_valid_pair = (q, b, a, dt_k)
                    if best_valid_q is None or (pos.is_bull and q > best_valid_q) or (not pos.is_bull and q < best_valid_q):
                        best_valid_q = q
                    raw_unr = pos.net_pnl(q)

                    # Stop-loss
                    if (pos.is_bull and b <= pos.sl) or (not pos.is_bull and a >= pos.sl):
                        reason = pos.flag if pos.flag else ("TRAILING_SL" if pos.trail_active else "SL")
                        fill_px = (b - pos.slip_offset) if pos.is_bull else (a + pos.slip_offset)
                        ex_slip = pos.slip_offset * pos.lots * CONTRACT_OZ
                        gap_usd = pos.net_pnl(fill_px) - pos.net_pnl(pos.sl)
                        pos.last_gap_usd = gap_usd
                        pos.sl_gap_events += 1
                        _update_mtm(equity + pos.net_pnl(fill_px), equity + raw_unr, dt_k, gap_usd)
                        _record_exit(pos, reason, fill_px, b, a, dt_k, ex_slip)
                        pos = None
                        return True

                    # Take-profit (limit fill at TP)
                    if (pos.is_bull and b >= pos.tp) or (not pos.is_bull and a <= pos.tp):
                        fill_px = float(pos.tp)
                        _update_mtm(equity + pos.net_pnl(fill_px), equity + raw_unr, dt_k)
                        _record_exit(pos, "TARGET", fill_px, b, a, dt_k, 0.0)
                        pos = None
                        return True

                    _update_mtm(equity + raw_unr, equity + raw_unr, dt_k)
                    pos.ratchet(q)

                if eval_timeout and last_valid_pair is not None and pos.bars_held >= pos.eff_max_hold():
                    q_last, lb, la, ldt = last_valid_pair
                    ex_px = (q_last - pos.slip_offset) if pos.is_bull else (q_last + pos.slip_offset)
                    ex_slip = pos.slip_offset * pos.lots * CONTRACT_OZ
                    _record_exit(pos, "TIMEOUT", ex_px, lb, la, ldt, ex_slip)
                    pos = None
                    return True

                if best_valid_q is not None:
                    pos.ratchet(best_valid_q)
                return False

            if pos is not None:
                pos.bars_held += 1
                _scan_ticks(0, eval_timeout=True)
                cur_min += timedelta(minutes=1)
                continue

            if cur_min in sig_by_min:
                sig = sig_by_min[cur_min]
                direction = str(sig["direction"]).upper()
                if hard_stop_active:
                    blocked_by_hard_stop += 1
                    cur_min += timedelta(minutes=1)
                    continue

                # Find first valid quote >= entry delay
                base_en_i = None
                for k, (dt_k, b, a) in enumerate(ticks_in_min):
                    if b > 0.0 and a > 0.0 and b < a:
                        base_en_i = k
                        break
                if base_en_i is not None:
                    en_i = base_en_i
                    if entry_delay_ticks > 0:
                        rem = entry_delay_ticks
                        k = base_en_i + 1
                        while k < len(ticks_in_min) and rem > 0:
                            if ticks_in_min[k][1] > 0 and ticks_in_min[k][2] > 0 and ticks_in_min[k][1] < ticks_in_min[k][2]:
                                en_i = k
                                rem -= 1
                            k += 1
                    elif entry_delay_seconds > 0.0:
                        target_dt = ticks_in_min[base_en_i][0] + timedelta(seconds=entry_delay_seconds)
                        k = base_en_i
                        while k < len(ticks_in_min):
                            if ticks_in_min[k][0] >= target_dt and ticks_in_min[k][1] > 0 and ticks_in_min[k][2] > 0 and ticks_in_min[k][1] < ticks_in_min[k][2]:
                                en_i = k
                                break
                            k += 1

                    en_dt, en_b, en_a = ticks_in_min[en_i]
                    slip_off = slippage_pips * PIP_SIZE
                    raw_en_px = en_a if direction == "BUY" else en_b
                    en_px = (raw_en_px + slip_off) if direction == "BUY" else (raw_en_px - slip_off)
                    atr_val = float(sig["atr"])
                    lots, sl_px, tp_px, pr_usd = _independent_position_size(
                        equity, en_px, atr_val, direction, commission_per_lot
                    )
                    if lots is not None:
                        t_idx_counter += 1
                        pos = IndependentTradeState(
                            trade_idx=t_idx_counter,
                            direction=direction,
                            strategy=str(sig["strategy"]),
                            signal_candle_close=cur_min,
                            entry_tick_ts=en_dt,
                            entry_bid=en_b,
                            entry_ask=en_a,
                            entry_price=en_px,
                            sl=sl_px,
                            tp=tp_px,
                            atr=atr_val,
                            lots=lots,
                            planned_risk_usd=pr_usd,
                            commission_per_lot=commission_per_lot,
                            slippage_pips=slippage_pips,
                            rollover_utc_hour=rollover_utc_hour,
                        )
                        init_unr = -(en_a - en_b) * lots * CONTRACT_OZ - pos.commission_total_usd - (slip_off * lots * CONTRACT_OZ)
                        _update_mtm(equity + init_unr, equity + init_unr, en_dt)
                        if en_i + 1 < len(ticks_in_min):
                            _scan_ticks(en_i + 1, eval_timeout=False)

            cur_min += timedelta(minutes=1)

    if pos is not None and last_valid_tick is not None:
        lb, la, ldt = last_valid_tick
        q = lb if pos.is_bull else la
        ex_px = (q - pos.slip_offset) if pos.is_bull else (q + pos.slip_offset)
        ex_slip = pos.slip_offset * pos.lots * CONTRACT_OZ
        _record_exit(pos, "END_OF_BACKTEST", ex_px, lb, la, ldt, ex_slip)
        pos = None

    wins = sum(1 for t in trades if t["net_pnl"] > 0)
    losses = sum(1 for t in trades if t["net_pnl"] <= 0)
    gp = sum(t["net_pnl"] for t in trades if t["net_pnl"] > 0)
    gl = abs(sum(t["net_pnl"] for t in trades if t["net_pnl"] < 0))
    pf = (gp / gl) if gl > 0 else 999.0
    net_pnl = sum(t["net_pnl"] for t in trades)

    return {
        "trades": trades,
        "count": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wins / len(trades) * 100.0, 2) if trades else 0.0,
        "net_pnl": round(net_pnl, 2),
        "gross_profit": round(gp, 2),
        "gross_loss": round(gl, 2),
        "pf": round(pf, 4),
        "final_equity": round(equity, 2),
        "closed_max_dd": round(closed_max_dd, 2),
        "closed_max_dd_pct": round(closed_max_dd_pct, 2),
        "mtm_max_dd": round(mtm_max_dd, 2),
        "mtm_max_dd_pct": round(mtm_max_dd_pct, 2),
        "raw_mtm_max_dd": round(raw_mtm_max_dd, 2),
        "mtm_peak_at_max_dd": round(mtm_peak_at_max_dd, 2),
        "mtm_trough_equity": round(mtm_trough_equity, 2),
        "mtm_peak_ts": mtm_peak_ts.isoformat() if mtm_peak_ts else None,
        "mtm_trough_ts": mtm_trough_ts.isoformat() if mtm_trough_ts else None,
        "hard_stop_triggered_at": hard_stop_triggered_at,
        "blocked_by_hard_stop": blocked_by_hard_stop,
        "total_commission_usd": round(sum(t["total_commission_usd"] for t in trades), 2),
        "total_slippage_usd": round(sum(t["total_slippage_usd"] for t in trades), 2),
        "total_swap_usd": round(sum(t["swap_usd"] for t in trades), 2),
        "total_spread_cost_usd": round(sum(t["total_spread_cost_usd"] for t in trades), 2),
    }


def _audit_phase4_data_quality(engine, signals: List[Dict[str, Any]], trades_118: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Full SQL & minute-level inspection of inverted, zero-spread, volatility clustering, and missing gaps."""
    # Daily & session query directly on TimescaleDB market_ticks
    with engine.connect() as conn:
        daily_rows = conn.execute(
            text(
                """
                SELECT
                    DATE(time AT TIME ZONE 'UTC') AS utc_day,
                    COUNT(*) AS total_ticks,
                    COUNT(*) FILTER (WHERE bid > ask) AS inverted_ticks,
                    COUNT(*) FILTER (WHERE bid = ask) AS zero_spread_ticks,
                    COUNT(*) FILTER (WHERE bid <= 0 OR ask <= 0) AS nonpos_ticks,
                    AVG(CASE WHEN ask > bid THEN ask - bid ELSE NULL END) AS avg_valid_spread,
                    MAX(CASE WHEN ask > bid THEN ask - bid ELSE NULL END) AS max_valid_spread
                FROM market_ticks
                WHERE symbol = 'XAUUSD'
                  AND time >= '2026-08-20 00:00:00+00'
                  AND time < '2026-11-17 00:00:00+00'
                GROUP BY 1
                ORDER BY 1
                """
            )
        ).fetchall()

        session_rows = conn.execute(
            text(
                """
                SELECT
                    CASE
                        WHEN EXTRACT(HOUR FROM time AT TIME ZONE 'UTC') BETWEEN 0 AND 6 THEN 'Asia (00-06)'
                        WHEN EXTRACT(HOUR FROM time AT TIME ZONE 'UTC') BETWEEN 7 AND 11 THEN 'London (07-11)'
                        WHEN EXTRACT(HOUR FROM time AT TIME ZONE 'UTC') BETWEEN 12 AND 16 THEN 'Overlap (12-16)'
                        WHEN EXTRACT(HOUR FROM time AT TIME ZONE 'UTC') BETWEEN 17 AND 20 THEN 'NY Late (17-20)'
                        ELSE 'Rollover (21-23)'
                    END AS session_utc,
                    COUNT(*) AS total_ticks,
                    COUNT(*) FILTER (WHERE bid > ask) AS inverted_ticks,
                    COUNT(*) FILTER (WHERE bid = ask) AS zero_spread_ticks,
                    AVG(CASE WHEN ask > bid THEN ask - bid ELSE NULL END) AS avg_valid_spread
                FROM market_ticks
                WHERE symbol = 'XAUUSD'
                  AND time >= '2026-08-24 00:00:00+00'
                  AND time < '2026-11-16 20:01:23+00'
                GROUP BY 1
                ORDER BY 2 DESC
                """
            )
        ).fetchall()

        # Volatility clustering: compare 1m mid-range on minutes WITH inverted/zero quotes vs clean minutes
        vol_cluster_rows = conn.execute(
            text(
                """
                WITH min_stats AS (
                    SELECT
                        date_trunc('minute', time) AS m_ts,
                        COUNT(*) AS n_ticks,
                        BOOL_OR(bid > ask) AS has_inv,
                        BOOL_OR(bid = ask) AS has_zero,
                        MAX((bid + ask)/2.0) - MIN((bid + ask)/2.0) AS range_1m,
                        AVG(CASE WHEN ask > bid THEN ask - bid ELSE NULL END) AS avg_sp
                    FROM market_ticks
                    WHERE symbol = 'XAUUSD'
                      AND time >= '2026-08-24 00:00:00+00'
                      AND time < '2026-11-16 20:01:23+00'
                    GROUP BY 1
                )
                SELECT
                    CASE
                        WHEN has_inv THEN 'minutes_with_inverted'
                        WHEN has_zero THEN 'minutes_with_zero_spread_only'
                        ELSE 'clean_minutes'
                    END AS bucket,
                    COUNT(*) AS minute_count,
                    AVG(range_1m) AS mean_1m_range,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY range_1m) AS median_1m_range,
                    AVG(avg_sp) AS mean_spread,
                    AVG(n_ticks) AS mean_ticks_per_min
                FROM min_stats
                GROUP BY 1
                """
            )
        ).fetchall()

        # Missing intervals > 60s across backtest window (excluding obvious weekend Sat/Sun closures or tagging them)
        gap_rows = conn.execute(
            text(
                """
                WITH ordered AS (
                    SELECT
                        time,
                        LAG(time) OVER (ORDER BY time) AS prev_time
                    FROM market_ticks
                    WHERE symbol = 'XAUUSD'
                      AND time >= '2026-08-24 00:00:00+00'
                      AND time < '2026-11-16 20:01:23+00'
                )
                SELECT
                    prev_time,
                    time AS next_time,
                    EXTRACT(EPOCH FROM (time - prev_time)) AS gap_seconds
                FROM ordered
                WHERE prev_time IS NOT NULL
                  AND EXTRACT(EPOCH FROM (time - prev_time)) > 60.0
                ORDER BY gap_seconds DESC
                """
            )
        ).fetchall()

        # Check inverted/zero-spread counts inside each open trade window AND entry minute: BUY vs SELL
        dir_excl = {"BUY": {"trades": 0, "entry_min_inv": 0, "entry_min_zero": 0, "open_win_inv": 0, "open_win_zero": 0, "open_win_ticks": 0},
                    "SELL": {"trades": 0, "entry_min_inv": 0, "entry_min_zero": 0, "open_win_inv": 0, "open_win_zero": 0, "open_win_ticks": 0}}
        for t in trades_118:
            d = str(t["direction"]).upper()
            sig_m = _parse_ts(t["entry_time"]).replace(second=0, microsecond=0)
            en_ts = _parse_ts(t["entry_time"])
            ex_ts = _parse_ts(t["exit_time"])
            r_m = conn.execute(
                text(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE bid > ask) AS inv,
                        COUNT(*) FILTER (WHERE bid = ask) AS zero
                    FROM market_ticks
                    WHERE symbol = 'XAUUSD' AND time >= :ms AND time < :me
                    """
                ),
                {"ms": sig_m, "me": sig_m + timedelta(minutes=1)},
            ).fetchone()
            r_w = conn.execute(
                text(
                    """
                    SELECT
                        COUNT(*) AS tot,
                        COUNT(*) FILTER (WHERE bid > ask) AS inv,
                        COUNT(*) FILTER (WHERE bid = ask) AS zero
                    FROM market_ticks
                    WHERE symbol = 'XAUUSD' AND time >= :es AND time <= :ex
                    """
                ),
                {"es": en_ts, "ex": ex_ts},
            ).fetchone()
            bkt = dir_excl[d]
            bkt["trades"] += 1
            bkt["entry_min_inv"] += int(r_m[0] or 0)
            bkt["entry_min_zero"] += int(r_m[1] or 0)
            bkt["open_win_ticks"] += int(r_w[0] or 0)
            bkt["open_win_inv"] += int(r_w[1] or 0)
            bkt["open_win_zero"] += int(r_w[2] or 0)

    daily_list = [
        {
            "utc_day": str(r[0]),
            "total_ticks": int(r[1]),
            "inverted_ticks": int(r[2]),
            "zero_spread_ticks": int(r[3]),
            "nonpos_ticks": int(r[4]),
            "avg_valid_spread": round(float(r[5] or 0.0), 5),
            "max_valid_spread": round(float(r[6] or 0.0), 5),
        }
        for r in daily_rows
    ]
    pd.DataFrame(daily_list).to_csv(AUDIT_DIR / "task3_phase4_exclusions_daily.csv", index=False)

    gap_list = []
    weekday_intraday_gaps = []
    # Check overlap with any open trade window
    open_intervals = [(_parse_ts(t["entry_time"]), _parse_ts(t["exit_time"]), t["direction"], t["trade_idx"] if "trade_idx" in t else i+1) for i, t in enumerate(trades_118)]
    gaps_overlapping_trades = 0
    for prev_t, next_t, gsec in gap_rows:
        p_dt = prev_t if prev_t.tzinfo is not None else prev_t.replace(tzinfo=timezone.utc)
        n_dt = next_t if next_t.tzinfo is not None else next_t.replace(tzinfo=timezone.utc)
        # Daily FX/Metals break is ~21:00-22:05 UTC (or Fri 21:00 -> Sun 22:00)
        is_weekend = (p_dt.weekday() == 4 and gsec > 3600) or (p_dt.weekday() >= 5)
        is_daily_break = (20 <= p_dt.hour <= 22) and (gsec <= 4500)
        overlaps_trade = any(p_dt < ex and n_dt > en for en, ex, _, _ in open_intervals)
        if overlaps_trade:
            gaps_overlapping_trades += 1
        rec = {
            "gap_start_utc": p_dt.isoformat(),
            "gap_end_utc": n_dt.isoformat(),
            "gap_seconds": round(float(gsec), 2),
            "is_weekend_closure": is_weekend,
            "is_daily_rollover_break": is_daily_break,
            "overlaps_open_trade": overlaps_trade,
        }
        gap_list.append(rec)
        if not is_weekend and not is_daily_break:
            weekday_intraday_gaps.append(rec)

    pd.DataFrame(gap_list).to_csv(AUDIT_DIR / "task3_phase4_gaps.csv", index=False)

    # Check causal signal bar lookup if lookahead_kalman_audit.csv exists
    signal_bar_excl = {}
    lh_path = AUDIT_DIR / "lookahead_kalman_audit.csv"
    if lh_path.exists():
        lh_df = pd.read_csv(lh_path)
        if "direction" in lh_df.columns and "minute_utc" in lh_df.columns:
            # Summarize signal counts by direction
            signal_bar_excl["total_causal_signal_rows"] = int(len(lh_df))

    return {
        "daily_rows_count": len(daily_list),
        "totals_aug24_nov16": {
            "total_ticks": sum(d["total_ticks"] for d in daily_list if d["utc_day"] >= "2026-08-24"),
            "inverted_ticks": sum(d["inverted_ticks"] for d in daily_list if d["utc_day"] >= "2026-08-24"),
            "zero_spread_ticks": sum(d["zero_spread_ticks"] for d in daily_list if d["utc_day"] >= "2026-08-24"),
            "nonpos_ticks": sum(d["nonpos_ticks"] for d in daily_list if d["utc_day"] >= "2026-08-24"),
        },
        "session_breakdown": [
            {
                "session": r[0],
                "total_ticks": int(r[1]),
                "inverted_ticks": int(r[2]),
                "inverted_pct": round(int(r[2]) / max(int(r[1]), 1) * 100.0, 4),
                "zero_spread_ticks": int(r[3]),
                "avg_valid_spread": round(float(r[4] or 0.0), 4),
            }
            for r in session_rows
        ],
        "volatility_clustering": [
            {
                "bucket": r[0],
                "minute_count": int(r[1]),
                "mean_1m_range": round(float(r[2] or 0.0), 4),
                "median_1m_range": round(float(r[3] or 0.0), 4),
                "mean_spread": round(float(r[4] or 0.0), 4),
                "mean_ticks_per_min": round(float(r[5] or 0.0), 2),
            }
            for r in vol_cluster_rows
        ],
        "exclusions_by_trade_direction": dir_excl,
        "gaps_over_60s_total": len(gap_list),
        "weekday_non_rollover_gaps_over_60s": len(weekday_intraday_gaps),
        "weekday_non_rollover_gaps_top10": weekday_intraday_gaps[:10],
        "gaps_overlapping_open_trades": gaps_overlapping_trades,
    }


def _phase6_stats(trades: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    df = pd.DataFrame(trades)
    pnls = df["net_pnl"].values.astype(np.float64)
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]

    def _subset_metrics(sub: pd.DataFrame) -> Dict[str, Any]:
        if sub.empty:
            return {"trades": 0, "win_rate_pct": 0.0, "net_pnl": 0.0, "pf": 0.0, "expectancy": 0.0}
        p = sub["net_pnl"].values.astype(np.float64)
        gp = float(p[p > 0].sum())
        gl = float(abs(p[p < 0].sum()))
        return {
            "trades": int(len(sub)),
            "wins": int((p > 0).sum()),
            "win_rate_pct": round(float((p > 0).mean() * 100.0), 2),
            "net_pnl": round(float(p.sum()), 2),
            "pf": round(gp / gl, 4) if gl > 0 else 999.0,
            "expectancy": round(float(p.mean()), 2),
        }

    # Streaks
    max_w_streak = max_l_streak = cur_w = cur_l = 0
    for val in pnls:
        if val > 0:
            cur_w += 1
            cur_l = 0
            max_w_streak = max(max_w_streak, cur_w)
        else:
            cur_l += 1
            cur_w = 0
            max_l_streak = max(max_l_streak, cur_l)

    df["entry_dt"] = pd.to_datetime(df["entry_time"], utc=True)
    df["session"] = df["entry_dt"].dt.hour.map(_utc_session)
    df["week"] = df["entry_dt"].dt.strftime("%Y-W%W")
    df["day"] = df["entry_dt"].dt.strftime("%Y-%m-%d")

    sorted_wins = df.sort_values("net_pnl", ascending=False)
    top5_w = sorted_wins.head(5)
    top5_l = df.sort_values("net_pnl", ascending=True).head(5)
    ex_top5 = sorted_wins.iloc[5:] if len(sorted_wins) > 5 else df.iloc[0:0]

    # Bootstrap CI (5,000 resamples, fixed seed)
    rng = np.random.default_rng(42)
    boot_means = []
    boot_pfs = []
    n = len(pnls)
    for _ in range(5000):
        sample = rng.choice(pnls, size=n, replace=True)
        boot_means.append(float(sample.mean()))
        sgp = float(sample[sample > 0].sum())
        sgl = float(abs(sample[sample < 0].sum()))
        boot_pfs.append(sgp / sgl if sgl > 0 else 10.0)

    daily_pnls = df.groupby("day")["net_pnl"].sum()
    weekly_pnls = df.groupby("week")["net_pnl"].sum()
    gp_tot = float(wins.sum())

    return {
        "label": label,
        "overall": _subset_metrics(df),
        "long": _subset_metrics(df[df["direction"] == "BUY"]),
        "short": _subset_metrics(df[df["direction"] == "SELL"]),
        "by_session": {
            sess: _subset_metrics(sub)
            for sess, sub in df.groupby("session")
        },
        "max_consecutive_wins": max_w_streak,
        "max_consecutive_losses": max_l_streak,
        "top5_winners_sum": round(float(top5_w["net_pnl"].sum()), 2),
        "top5_pct_of_gross_profit": round(float(top5_w["net_pnl"].sum()) / max(gp_tot, 1e-6) * 100.0, 2),
        "top5_winners": [
            {"trade_idx": int(r["trade_idx"]), "direction": r["direction"], "entry_time": r["entry_time"], "net_pnl": round(float(r["net_pnl"]), 2)}
            for _, r in top5_w.iterrows()
        ],
        "top5_losers": [
            {"trade_idx": int(r["trade_idx"]), "direction": r["direction"], "entry_time": r["entry_time"], "net_pnl": round(float(r["net_pnl"]), 2)}
            for _, r in top5_l.iterrows()
        ],
        "excluding_top5_winners": _subset_metrics(ex_top5),
        "daily_summary": {
            "trading_days_with_entries": int(len(daily_pnls)),
            "positive_days": int((daily_pnls > 0).sum()),
            "negative_days": int((daily_pnls <= 0).sum()),
            "best_day": round(float(daily_pnls.max()), 2),
            "worst_day": round(float(daily_pnls.min()), 2),
            "mean_day_pnl": round(float(daily_pnls.mean()), 2),
        },
        "weekly_summary": {
            "weeks_with_entries": int(len(weekly_pnls)),
            "positive_weeks": int((weekly_pnls > 0).sum()),
            "negative_weeks": int((weekly_pnls <= 0).sum()),
            "best_week": round(float(weekly_pnls.max()), 2),
            "worst_week": round(float(weekly_pnls.min()), 2),
            "weekly_breakdown": {str(k): round(float(v), 2) for k, v in weekly_pnls.items()},
        },
        "bootstrap_5000": {
            "expectancy_mean": round(float(np.mean(boot_means)), 2),
            "expectancy_se": round(float(np.std(boot_means, ddof=1)), 2),
            "expectancy_ci95_low": round(float(np.percentile(boot_means, 2.5)), 2),
            "expectancy_ci95_high": round(float(np.percentile(boot_means, 97.5)), 2),
            "prob_expectancy_gt_0_pct": round(float(np.mean(np.array(boot_means) > 0) * 100.0), 2),
            "pf_ci95_low": round(float(np.percentile(boot_pfs, 2.5)), 4),
            "pf_ci95_high": round(float(np.percentile(boot_pfs, 97.5)), 4),
        },
    }


def main() -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    frozen_sha = hashlib.sha256(FROZEN_PATH.read_bytes()).hexdigest()
    assert frozen_sha == EXPECTED_FROZEN_SHA256, f"Frozen artifact modified: {frozen_sha}"

    df_post_base = pd.read_csv(POST_REM_BASE_CSV)
    post_summary = json.loads(POST_REM_SUMMARY.read_text(encoding="utf-8"))
    ref_trades = df_post_base.to_dict(orient="records")
    # Reconstruct exact signal inputs (minute timestamp, direction, strategy, atr)
    signals = [
        {
            "entry_time": r["entry_time"],
            "direction": r["direction"],
            "strategy": r["strategy"],
            "atr": abs(float(r["entry_price"]) - float(r["sl"])) / 1.5,
        }
        for r in ref_trades
    ]

    engine = create_engine(RAW_DB_URL)
    start_dt = datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc)

    print("[1/5] Phase 1: Independent Raw-Tick Replay ($0 costs) & Ledger Reconciliation...")
    ind_zero = _independent_replay(engine, signals, start_dt, end_dt, commission_per_lot=0.0, slippage_pips=0.0)
    assert ind_zero["count"] == len(ref_trades), f"Count mismatch: {ind_zero['count']} vs {len(ref_trades)}"

    recon_rows = []
    max_entry_diff = 0.0
    max_exit_diff = 0.0
    max_pnl_diff = 0.0
    qty_mismatches = 0
    reason_mismatches = 0
    for ref, ind in zip(ref_trades, ind_zero["trades"]):
        ediff = abs(float(ref["entry_price"]) - float(ind["entry_price"]))
        xdiff = abs(float(ref["exit_price"]) - float(ind["exit_price"]))
        pdiff = abs(float(ref["pnl"]) - float(ind["net_pnl"]))
        qdiff = abs(float(ref["qty"]) - float(ind["qty"]))
        rmis = str(ref["exit_reason"]) != str(ind["exit_reason"])
        max_entry_diff = max(max_entry_diff, ediff)
        max_exit_diff = max(max_exit_diff, xdiff)
        max_pnl_diff = max(max_pnl_diff, pdiff)
        if qdiff > 1e-6:
            qty_mismatches += 1
        if rmis:
            reason_mismatches += 1
        recon_rows.append(
            {
                "trade_idx": ind["trade_idx"],
                "direction": ind["direction"],
                "strategy": ind["strategy"],
                "signal_minute": ind["signal_minute"],
                "entry_tick_ts": ind["entry_time"],
                "exit_tick_ts": ind["exit_time"],
                "ref_entry_px": ref["entry_price"],
                "ind_entry_px": ind["entry_price"],
                "entry_diff": round(ediff, 6),
                "ref_exit_px": ref["exit_price"],
                "ind_exit_px": ind["exit_price"],
                "exit_diff": round(xdiff, 6),
                "ref_qty": ref["qty"],
                "ind_qty": ind["qty"],
                "ref_reason": ref["exit_reason"],
                "ind_reason": ind["exit_reason"],
                "ref_pnl": ref["pnl"],
                "ind_pnl": ind["net_pnl"],
                "pnl_diff": round(pdiff, 6),
                "zero_tick_mins_open": ind["zero_tick_mins_open"],
                "all_invalid_mins_open": ind["all_invalid_mins_open"],
            }
        )
    pd.DataFrame(recon_rows).to_csv(AUDIT_DIR / "task3_phase1_reconciliation.csv", index=False)

    print("[2/5] Phase 3 & 5: Independent Cost Stress ($7/lot + 0.3p) & MTM Drawdown...")
    ind_cost = _independent_replay(engine, signals, start_dt, end_dt, commission_per_lot=7.0, slippage_pips=0.3)
    pd.DataFrame(ind_cost["trades"]).to_csv(AUDIT_DIR / "task3_cost_reconciliation_118.csv", index=False)

    # Check rollover crossings at 21:00Z and 22:00Z across all 118 trades
    crossed_21 = 0
    crossed_22 = 0
    for t in ind_cost["trades"]:
        en_t = _parse_ts(t["entry_time"])
        ex_t = _parse_ts(t["exit_time"])
        # check 21:00 UTC and 22:00 UTC boundary on entry date
        b21 = en_t.replace(hour=21, minute=0, second=0, microsecond=0)
        b22 = en_t.replace(hour=22, minute=0, second=0, microsecond=0)
        if en_t < b21 <= ex_t:
            crossed_21 += 1
        if en_t < b22 <= ex_t:
            crossed_22 += 1

    print("[3/5] Phase 5 Counterfactual: Simulating Explicit 8% HWM Hard-Stop Enforcement...")
    ind_hardstop = _independent_replay(
        engine, signals, start_dt, end_dt, commission_per_lot=7.0, slippage_pips=0.3, hard_stop_hwm_pct=8.0
    )

    print("[4/5] Phase 4: Auditing Excluded Quotes, Volatility Clustering, and Gaps...")
    phase4 = _audit_phase4_data_quality(engine, signals, ind_cost["trades"])

    print("[5/5] Phase 6: Statistical Robustness, Cost Stress Grid, and Execution Latency Tests...")
    stats_zero = _phase6_stats(ind_zero["trades"], "remediated_zero_cost")
    stats_cost = _phase6_stats(ind_cost["trades"], "remediated_7usd_03p")

    cost_grid = []
    for comm, slip in [
        (0.0, 0.0),
        (4.5, 0.2),   # Fusion Zero per-lot rate ($4.50/lot) if fixed per lot + 0.2p
        (5.0, 0.2),
        (7.0, 0.3),   # Task 2/3 benchmark stress
        (10.0, 0.5),  # Severe stress
        (18.0, 0.3),  # Approximate Fusion cTrader $4.50/$100k notional at ~$4,000/oz XAUUSD (~$18/lot)
    ]:
        r = _independent_replay(engine, signals, start_dt, end_dt, commission_per_lot=comm, slippage_pips=slip)
        cost_grid.append(
            {
                "commission_per_lot": comm,
                "slippage_pips": slip,
                "trades": r["count"],
                "win_rate_pct": r["win_rate_pct"],
                "net_pnl": r["net_pnl"],
                "pf": r["pf"],
                "closed_max_dd": r["closed_max_dd"],
                "closed_max_dd_pct": r["closed_max_dd_pct"],
                "mtm_max_dd": r["mtm_max_dd"],
                "mtm_max_dd_pct": r["mtm_max_dd_pct"],
                "total_comm_usd": r["total_commission_usd"],
                "total_slip_usd": r["total_slippage_usd"],
            }
        )

    latency_grid = []
    for label, dt_ticks, dt_secs in [
        ("baseline_0s", 0, 0.0),
        ("delay_1_tick", 1, 0.0),
        ("delay_1s", 0, 1.0),
        ("delay_5s", 0, 5.0),
    ]:
        r_zero = _independent_replay(engine, signals, start_dt, end_dt, commission_per_lot=0.0, slippage_pips=0.0, entry_delay_ticks=dt_ticks, entry_delay_seconds=dt_secs)
        r_cost = _independent_replay(engine, signals, start_dt, end_dt, commission_per_lot=7.0, slippage_pips=0.3, entry_delay_ticks=dt_ticks, entry_delay_seconds=dt_secs)
        latency_grid.append(
            {
                "delay_scenario": label,
                "zero_cost_net_pnl": r_zero["net_pnl"],
                "zero_cost_pf": r_zero["pf"],
                "zero_cost_wr_pct": r_zero["win_rate_pct"],
                "stressed_net_pnl": r_cost["net_pnl"],
                "stressed_pf": r_cost["pf"],
                "stressed_wr_pct": r_cost["win_rate_pct"],
                "stressed_mtm_dd_pct": r_cost["mtm_max_dd_pct"],
            }
        )

    # Identify closed & MTM peak/trough trades on cost-stressed run
    df_c = pd.DataFrame(ind_cost["trades"])
    eq_series = pd.concat([pd.Series([10000.0]), df_c["equity_after"]], ignore_index=True)
    hwm_series = eq_series.cummax()
    dd_series = eq_series - hwm_series
    trough_idx = int(dd_series.idxmin())
    peak_idx = int(eq_series.iloc[: trough_idx + 1].idxmax()) if trough_idx > 0 else 0

    summary = {
        "frozen_baseline_sha256": frozen_sha,
        "frozen_baseline_intact": frozen_sha == EXPECTED_FROZEN_SHA256,
        "phase1_reconciliation": {
            "ref_count": len(ref_trades),
            "ind_count": ind_zero["count"],
            "max_entry_price_diff": round(max_entry_diff, 6),
            "max_exit_price_diff": round(max_exit_diff, 6),
            "max_pnl_diff_usd": round(max_pnl_diff, 6),
            "qty_mismatches": qty_mismatches,
            "reason_mismatches": reason_mismatches,
            "ind_zero_cost_summary": {k: v for k, v in ind_zero.items() if k != "trades"},
        },
        "phase3_cost_reconciliation": {
            "engine_post_remediation_reported": post_summary["remediated_baseline_cost_stress_7usd_03p"],
            "independent_stressed_summary": {k: v for k, v in ind_cost.items() if k != "trades"},
            "rollover_crossings_21utc": crossed_21,
            "rollover_crossings_22utc": crossed_22,
            "spread_cost_sum_usd": round(float(df_c["total_spread_cost_usd"].sum()), 2),
            "gross_mid_pnl_sum_usd": round(float(df_c["gross_mid_pnl_usd"].sum()), 2),
            "commission_sum_usd": round(float(df_c["total_commission_usd"].sum()), 2),
            "slippage_sum_usd": round(float(df_c["total_slippage_usd"].sum()), 2),
            "swap_sum_usd": round(float(df_c["swap_usd"].sum()), 2),
            "net_pnl_sum_usd": round(float(df_c["net_pnl"].sum()), 2),
            "accounting_identity_error_usd": round(
                float(
                    (
                        df_c["gross_mid_pnl_usd"]
                        - df_c["total_spread_cost_usd"]
                        - df_c["total_slippage_usd"]
                        - df_c["total_commission_usd"]
                        - df_c["swap_usd"]
                        - df_c["net_pnl"]
                    ).abs().max()
                ),
                6,
            ),
        },
        "phase4_data_quality": phase4,
        "phase5_drawdown_and_hard_stop": {
            "closed_peak_trade_idx": peak_idx,
            "closed_peak_equity": round(float(eq_series.iloc[peak_idx]), 2),
            "closed_trough_trade_idx": trough_idx,
            "closed_trough_equity": round(float(eq_series.iloc[trough_idx]), 2),
            "closed_max_dd_usd": ind_cost["closed_max_dd"],
            "closed_max_dd_pct": ind_cost["closed_max_dd_pct"],
            "mtm_max_dd_usd": ind_cost["mtm_max_dd"],
            "mtm_max_dd_pct": ind_cost["mtm_max_dd_pct"],
            "mtm_peak_at_max_dd": ind_cost["mtm_peak_at_max_dd"],
            "mtm_trough_equity": ind_cost["mtm_trough_equity"],
            "mtm_peak_ts": ind_cost["mtm_peak_ts"],
            "mtm_trough_ts": ind_cost["mtm_trough_ts"],
            "mtm_vs_closed_gap_usd": round(abs(ind_cost["mtm_max_dd"]) - abs(ind_cost["closed_max_dd"]), 2),
            "with_8pct_hwm_entry_gate": {k: v for k, v in ind_hardstop.items() if k != "trades"},
        },
        "phase6_robustness": {
            "zero_cost": stats_zero,
            "cost_stressed_7usd_03p": stats_cost,
            "cost_sensitivity_grid": cost_grid,
            "latency_sensitivity_grid": latency_grid,
        },
    }

    (AUDIT_DIR / "task3_validation_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "phase1_recon": summary["phase1_reconciliation"],
        "phase3_cost": {k: v for k, v in summary["phase3_cost_reconciliation"].items() if k != "engine_post_remediation_reported"},
        "phase5_dd": summary["phase5_drawdown_and_hard_stop"],
        "cost_grid": cost_grid,
        "latency_grid": latency_grid,
    }, indent=2))


if __name__ == "__main__":
    main()
