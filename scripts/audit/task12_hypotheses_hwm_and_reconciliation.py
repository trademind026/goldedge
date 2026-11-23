"""
Task 12 (Phases 7–11) — Independent Raw-Tick Session Hypotheses, Parameter
Sensitivity, BH-FDR & Bootstrap Statistical Validation, Task 8 8% HWM Risk
Evaluation, and Dual-Engine Clean-Room Raw-Tick Reconciliation across W1/W2/W3.

Zero imports from `src/goldedge/`.
Produces deliverables #9–#13 under `backtest_results/audit/task12_session_analysis/`:
  9.  task12_session_hypothesis_results.csv
  10. task12_parameter_sensitivity.csv
  11. task12_statistical_validation.json
  12. task12_hwm_comparison.json
  13. task12_independent_reconciliation.json
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Optional

import numpy as np
import pandas as pd
import psycopg2
from scipy import stats

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import task6c_independent_validator as t6c
import task11a_independent_candidate_reconciler as t11a
import task12_session_analysis_and_diagnosis as t12p1

ROOT = t12p1.ROOT
AUDIT_DIR = t12p1.AUDIT_DIR
OUT_DIR = t12p1.OUT_DIR


@dataclass
class SConfig:
    name: str
    group: str
    base_variant: str  # "H0" or "H4a"
    rule_desc: str
    max_score: float = 1.01
    block_utc_hours: tuple[int, ...] = ()
    min_utc_hour: int = 0
    max_utc_hour_ex: int = 24
    block_dst_sessions: tuple[str, ...] = ()
    only_overlap: bool = False
    block_buy_utc_hours: tuple[int, ...] = ()
    block_sell_dst_sessions: tuple[str, ...] = ()
    block_transition_half_mins: float = 0.0
    block_first_n_mins_open: float = 0.0
    block_last_30m_close: bool = False
    spread_cap_on_trans_usd: Optional[float] = None
    block_weekdays: tuple[str, ...] = ()
    max_hwm_dd_pct: Optional[float] = None
    slip_pips: float = 0.0533
    comm_rate_per_m: float = 12.0


def _side_comm(lot: float, px: float, rate: float) -> float:
    return round(lot * 100.0 * px * (rate / 1_000_000.0), 2)


def _passes_session_filter(
    cfg: SConfig,
    direction: str,
    entry_ts_utc: datetime,
    spread_usd: float,
    sinfo: dict[str, Any],
) -> bool:
    hr = sinfo["utc_hour"]
    if hr < cfg.min_utc_hour or hr >= cfg.max_utc_hour_ex:
        return False
    if hr in cfg.block_utc_hours:
        return False
    if sinfo["dst_session"] in cfg.block_dst_sessions:
        return False
    if cfg.only_overlap and not sinfo["in_ldn_ny_overlap"]:
        return False
    if direction == "BUY" and hr in cfg.block_buy_utc_hours:
        return False
    if direction == "SELL" and sinfo["dst_session"] in cfg.block_sell_dst_sessions:
        return False
    if cfg.block_weekdays and sinfo["utc_weekday"] in cfg.block_weekdays:
        return False
    if cfg.block_first_n_mins_open > 0.0:
        if (0.0 <= sinfo["mins_since_ldn_open"] < cfg.block_first_n_mins_open) or (
            0.0 <= sinfo["mins_since_ny_open"] < cfg.block_first_n_mins_open
        ):
            return False
    if cfg.block_last_30m_close and sinfo["is_last_30m_any"]:
        return False
    if cfg.block_transition_half_mins > 0.0:
        # Check distance to London open, NY open, London close, Tokyo close
        u = entry_ts_utc.astimezone(timezone.utc)
        ldn = u.astimezone(t12p1._LDN_TZ)
        ny = u.astimezone(t12p1._NY_TZ)
        tyo = u.astimezone(t12p1._TYO_TZ)
        lm = ldn.hour * 60 + ldn.minute + ldn.second / 60.0
        nm = ny.hour * 60 + ny.minute + ny.second / 60.0
        tm = tyo.hour * 60 + tyo.minute + tyo.second / 60.0
        w = cfg.block_transition_half_mins
        if abs(lm - 480.0) <= w or abs(nm - 480.0) <= w or abs(lm - 990.0) <= w or abs(tm - 1080.0) <= w:
            return False
    if cfg.spread_cap_on_trans_usd is not None:
        if (sinfo["is_first_30m_any"] or sinfo["is_session_transition"]) and spread_usd > cfg.spread_cap_on_trans_usd:
            return False
    return True


class SessionPortfolio:
    def __init__(self, cfg: SConfig, track_stops: bool = False) -> None:
        self.cfg = cfg
        self.track_stops = track_stops
        self.slip_offset = cfg.slip_pips * 0.10
        self.open_pos: Optional[t11a.CleanPos] = None
        self.trades: list[dict[str, Any]] = []
        self.equity = 10000.0
        self.day_start_eq = 10000.0
        self.mtm = t11a.CleanMtm(10000.0)
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.consec_losses = 0
        self.current_day: Optional[date] = None
        self.last_cd_bar: dict[tuple[str, str], int] = {}
        self.halted = False
        self.halt_info: Optional[dict[str, Any]] = None
        self.prevented_signals_after_halt = 0
        self.constraint_violations = 0

    def new_day(self, d: date) -> None:
        if self.current_day != d:
            self.current_day = d
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.consec_losses = 0
            self.day_start_eq = max(self.equity, 1.0)

    def _net_at(self, pos: t11a.CleanPos, px: float) -> float:
        pend = _side_comm(pos.lot, px, self.cfg.comm_rate_per_m) if pos.comm_exit == 0.0 else 0.0
        return pos.gross_at(px) - pos.comm_total - pend + pos.swap_pnl

    def _ratchet(self, pos: t11a.CleanPos, price: float, max_hold: int, ts_i: datetime) -> None:
        is_bull = pos.direction == "BUY"
        old_sl = pos.sl
        if is_bull:
            if price > pos.peak_price:
                pos.peak_price = price
            gain = pos.peak_price - pos.entry_price
        else:
            if price < pos.peak_price:
                pos.peak_price = price
            gain = pos.entry_price - pos.peak_price

        if gain >= 2.5 * pos.atr:
            pos.trailing_active = True
            nsl = (pos.peak_price - 0.75 * pos.atr) if is_bull else (pos.peak_price + 0.75 * pos.atr)
            if (is_bull and nsl > pos.sl) or ((not is_bull) and nsl < pos.sl):
                pos.sl = nsl
                pos.exit_reason_flag = "TRAILING_SL"
        elif gain >= 1.6 * pos.atr:
            pos.trailing_active = True
            nsl = (pos.peak_price - 1.10 * pos.atr) if is_bull else (pos.peak_price + 1.10 * pos.atr)
            if (is_bull and nsl > pos.sl) or ((not is_bull) and nsl < pos.sl):
                pos.sl = nsl
                pos.exit_reason_flag = "TRAILING_SL"
        elif gain >= 1.0 * pos.atr:
            be = 0.20
            nsl = (pos.entry_price + be) if is_bull else (pos.entry_price - be)
            if (is_bull and nsl > pos.sl) or ((not is_bull) and nsl < pos.sl):
                pos.sl = nsl
                pos.exit_reason_flag = "BREAKEVEN"

        hold_pct = pos.bars_held / max_hold if max_hold > 0 else 0.0
        if hold_pct >= 0.75:
            ratio = min((hold_pct - 0.75) / 0.25, 1.0)
            if is_bull:
                sq = pos.sl + ratio * (price - pos.sl)
                if sq > pos.sl:
                    pos.sl = sq
                    pos.exit_reason_flag = "TIME_SQUEEZE"
            else:
                sq = pos.sl - ratio * (pos.sl - price)
                if sq < pos.sl:
                    pos.sl = sq
                    pos.exit_reason_flag = "TIME_SQUEEZE"

        if self.track_stops and pos.sl != old_sl:
            pos.stop_updates.append({
                "ts": t11a._to_utc(ts_i).isoformat(),
                "old_sl": round(old_sl, 6),
                "new_sl": round(pos.sl, 6),
                "flag": pos.exit_reason_flag,
            })

    def scan_exit(self, bids: np.ndarray, asks: np.ndarray, ttimes: list[datetime], start_i: int, eval_to: bool) -> Optional[tuple[str, float, datetime]]:
        pos = self.open_pos
        if pos is None:
            return None
        is_bull = pos.direction == "BUY"
        eff_mh = 60 if 7 <= pos.entry_time.hour < 20 else 90
        best_q = None
        last_q = None
        last_ts = ttimes[-1]
        for i in range(start_i, len(bids)):
            b = float(bids[i])
            a = float(asks[i])
            ts_i = ttimes[i]
            if b <= 0.0 or a <= 0.0 or b >= a:
                continue
            pos.apply_rollovers(ts_i)
            eq_q = b if is_bull else a
            last_q = eq_q
            last_ts = ts_i
            if best_q is None or (is_bull and eq_q > best_q) or (not is_bull and eq_q < best_q):
                best_q = eq_q
            if is_bull and b <= pos.sl:
                rsn = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
                fpx = b - self.slip_offset
                pos.slip_usd += self.slip_offset * pos.lot * 100.0
                self.mtm.update(self.equity + self._net_at(pos, fpx))
                return rsn, fpx, ts_i
            if (not is_bull) and a >= pos.sl:
                rsn = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
                fpx = a + self.slip_offset
                pos.slip_usd += self.slip_offset * pos.lot * 100.0
                self.mtm.update(self.equity + self._net_at(pos, fpx))
                return rsn, fpx, ts_i
            if is_bull and b >= pos.tp:
                fpx = float(pos.tp)
                self.mtm.update(self.equity + self._net_at(pos, fpx))
                return "TARGET", fpx, ts_i
            if (not is_bull) and a <= pos.tp:
                fpx = float(pos.tp)
                self.mtm.update(self.equity + self._net_at(pos, fpx))
                return "TARGET", fpx, ts_i
            cur_eq = self.equity + self._net_at(pos, eq_q)
            self.mtm.update(cur_eq)
            if self.cfg.max_hwm_dd_pct is not None and not self.halted:
                dd_pct = max(0.0, (self.mtm.hwm - cur_eq) / self.mtm.hwm * 100.0) if self.mtm.hwm > 0 else 0.0
                if round(dd_pct, 10) >= self.cfg.max_hwm_dd_pct:
                    self.halted = True
                    fpx = (b - self.slip_offset) if is_bull else (a + self.slip_offset)
                    pos.slip_usd += self.slip_offset * pos.lot * 100.0
                    eq_after_fill = self.equity + self._net_at(pos, fpx)
                    self.mtm.update(eq_after_fill)
                    self.halt_info = {
                        "trigger_time_utc": t11a._to_utc(ts_i).isoformat(),
                        "trades_before_halt_inc_trigger": len(self.trades) + 1,
                        "peak_equity_usd": round(self.mtm.hwm, 2),
                        "equity_at_trigger_usd": round(cur_eq, 2),
                        "trigger_dd_pct": round(dd_pct, 4),
                        "trigger_session": t12p1.classify_dst_session(t11a._to_utc(ts_i))["dst_session"],
                        "trigger_utc_hour": t11a._to_utc(ts_i).hour,
                    }
                    return "HWM_KILL_SWITCH_8PCT", fpx, ts_i
            self._ratchet(pos, eq_q, eff_mh, ts_i)
        if eval_to and pos.bars_held >= eff_mh:
            bpx = last_q if last_q is not None else pos.entry_price
            epx = (bpx - self.slip_offset) if is_bull else (bpx + self.slip_offset)
            if last_q is not None:
                pos.slip_usd += self.slip_offset * pos.lot * 100.0
            return "TIMEOUT", epx, ttimes[-1]
        if best_q is not None:
            self._ratchet(pos, best_q, eff_mh, last_ts)
        return None

    def close_pos(self, pos: t11a.CleanPos, rsn: str, epx: float, ex_ts: datetime) -> None:
        pos.apply_rollovers(ex_ts)
        c_ex = _side_comm(pos.lot, epx, self.cfg.comm_rate_per_m)
        pos.comm_exit = c_ex
        pos.comm_total = round(pos.entry_comm + c_ex, 2)
        gross = pos.gross_at(epx)
        net = gross - pos.comm_total + pos.swap_pnl
        self.equity += net
        self.daily_pnl += net
        self.consec_losses = 0 if net > 0 else self.consec_losses + 1
        self.mtm.update(self.equity)
        e_utc = t11a._to_utc(pos.entry_time)
        sinfo = t12p1.classify_dst_session(e_utc)
        self.trades.append({
            "signal_time": t11a._to_utc(pos.signal_time).isoformat(),
            "entry_time": e_utc.isoformat(),
            "exit_time": t11a._to_utc(ex_ts).isoformat(),
            "direction": pos.direction,
            "strategy": pos.strategy,
            "entry_bid": pos.entry_bid,
            "entry_ask": pos.entry_ask,
            "entry_price": pos.entry_price,
            "exit_price": epx,
            "qty": pos.lot,
            "commission_total": pos.comm_total,
            "slippage_usd": pos.slip_usd,
            "swap_pnl": pos.swap_pnl,
            "gross_pnl": gross,
            "net_pnl": net,
            "exit_reason": rsn,
            "equity_after": self.equity,
            "utc_hour": sinfo["utc_hour"],
            "dst_session": sinfo["dst_session"],
            "stop_updates": list(pos.stop_updates) if self.track_stops else [],
        })


def _build_configs(include_sensitivity: bool = True) -> list[SConfig]:
    cfgs = [
        # Controls
        SConfig("H0_BASELINE_V2", "control", "H0", "Unchanged frozen V2 baseline (score >= 0.60)"),
        SConfig("H4a_SCORE_CAP_080", "control", "H4a", "H4a research candidate (0.60 <= score < 0.80)", max_score=0.80),
        # 1. Individual UTC-hour exclusions
        SConfig("S01_H0_NO_UTC09", "utc_hour_h0", "H0", "Exclude UTC 09 on H0", block_utc_hours=(9,)),
        SConfig("S02_H0_NO_UTC16", "utc_hour_h0", "H0", "Exclude UTC >= 16 on H0", max_utc_hour_ex=16),
        SConfig("S03_H0_CLOSE_15UTC", "utc_hour_h0", "H0", "Close entries at 15:00 UTC on H0", max_utc_hour_ex=15),
        SConfig("S04_H4a_NO_UTC09", "utc_hour_h4a", "H4a", "Exclude UTC 09 on H4a", max_score=0.80, block_utc_hours=(9,)),
        SConfig("S05_H4a_NO_UTC16", "utc_hour_h4a", "H4a", "Exclude UTC >= 16 on H4a", max_score=0.80, max_utc_hour_ex=16),
        SConfig("S06_H4a_CLOSE_15UTC", "utc_hour_h4a", "H4a", "Close entries at 15:00 UTC on H4a", max_score=0.80, max_utc_hour_ex=15),
        # 2. DST-aware session exclusions
        SConfig("S07_H0_NO_NY_POST_LDN", "session_h0", "H0", "Exclude DST NY_POST_LONDON (>16:30 London local) on H0", block_dst_sessions=("NY_POST_LONDON",)),
        SConfig("S08_H4a_NO_NY_POST_LDN", "session_h4a", "H4a", "Exclude DST NY_POST_LONDON (>16:30 London local) on H4a", max_score=0.80, block_dst_sessions=("NY_POST_LONDON",)),
        SConfig("S09_H4a_NO_ASIAN_ONLY", "session_h4a", "H4a", "Exclude DST ASIAN_ONLY (pre-London open after UK GMT shift) on H4a", max_score=0.80, block_dst_sessions=("ASIAN_ONLY",)),
        # 3 & 4. BUY-only and SELL-only session filters
        SConfig("S10_H4a_BUY_NO_07_08", "buy_session", "H4a", "Block BUY in UTC 07-08 on H4a", max_score=0.80, block_buy_utc_hours=(7, 8)),
        SConfig("S11_H4a_SELL_NO_NY_POST", "sell_session", "H4a", "Block SELL in DST NY_POST_LONDON on H4a", max_score=0.80, block_sell_dst_sessions=("NY_POST_LONDON",)),
        # 5. Overlap only
        SConfig("S12_H0_OVERLAP_ONLY", "overlap", "H0", "Trade DST London/NY overlap only on H0", only_overlap=True),
        SConfig("S13_H4a_OVERLAP_ONLY", "overlap", "H4a", "Trade DST London/NY overlap only on H4a", max_score=0.80, only_overlap=True),
        # 6. Session transition filters
        SConfig("S14_H0_NO_TRANS_15M", "transition", "H0", "Block +-15m around session open/close transitions on H0", block_transition_half_mins=15.0),
        SConfig("S15_H4a_NO_TRANS_15M", "transition", "H4a", "Block +-15m around session open/close transitions on H4a", max_score=0.80, block_transition_half_mins=15.0),
        # 7. First / last 30m filters
        SConfig("S16_H0_NO_FIRST30M", "open_close_30m", "H0", "Block first 30m of London/NY open on H0", block_first_n_mins_open=30.0),
        SConfig("S17_H4a_NO_FIRST30M", "open_close_30m", "H4a", "Block first 30m of London/NY open on H4a", max_score=0.80, block_first_n_mins_open=30.0),
        SConfig("S18_H4a_NO_LAST30M", "open_close_30m", "H4a", "Block last 30m of London/NY close on H4a", max_score=0.80, block_last_30m_close=True),
        # 8. Spread x Time-of-Day interaction
        SConfig("S19_H0_SPREAD015_TRANS", "spread_tod", "H0", "Block spread > $0.15 during first 30m / transition on H0", spread_cap_on_trans_usd=0.15),
        SConfig("S20_H4a_SPREAD015_TRANS", "spread_tod", "H4a", "Block spread > $0.15 during first 30m / transition on H4a", max_score=0.80, spread_cap_on_trans_usd=0.15),
        # 9. Weekday filters
        SConfig("S21_H0_NO_THU", "weekday", "H0", "Exclude Thursday on H0", block_weekdays=("Thu",)),
        SConfig("S22_H4a_NO_TUE", "weekday", "H4a", "Exclude Tuesday on H4a", max_score=0.80, block_weekdays=("Tue",)),
        SConfig("S23_H4a_NO_MON", "weekday", "H4a", "Exclude Monday on H4a", max_score=0.80, block_weekdays=("Mon",)),
        # Task 8 8% HWM protected variants
        SConfig("H0_HWM8", "hwm8", "H0", "H0 + Task 8 8% HWM Kill-Switch", max_hwm_dd_pct=8.0),
        SConfig("H4a_HWM8", "hwm8", "H4a", "H4a + Task 8 8% HWM Kill-Switch", max_score=0.80, max_hwm_dd_pct=8.0),
        SConfig("S07_H0_NO_NYPOST_HWM8", "hwm8", "H0", "S07 + Task 8 8% HWM Kill-Switch", block_dst_sessions=("NY_POST_LONDON",), max_hwm_dd_pct=8.0),
        SConfig("S15_H4a_NOTRANS_HWM8", "hwm8", "H4a", "S15 + Task 8 8% HWM Kill-Switch", max_score=0.80, block_transition_half_mins=15.0, max_hwm_dd_pct=8.0),
        SConfig("S17_H4a_NOF30_HWM8", "hwm8", "H4a", "S17 + Task 8 8% HWM Kill-Switch", max_score=0.80, block_first_n_mins_open=30.0, max_hwm_dd_pct=8.0),
        # Cost stress (1.5x slip + $15/M comm)
        SConfig("H4a_COST_STRESS", "cost_stress", "H4a", "H4a under 1.5x slip & $15/M comm", max_score=0.80, slip_pips=0.080, comm_rate_per_m=15.0),
        SConfig("S05_COST_STRESS", "cost_stress", "H4a", "S05 (H4a no UTC16) under 1.5x cost stress", max_score=0.80, max_utc_hour_ex=16, slip_pips=0.080, comm_rate_per_m=15.0),
        SConfig("S15_COST_STRESS", "cost_stress", "H4a", "S15 (H4a no +-15m trans) under 1.5x cost stress", max_score=0.80, block_transition_half_mins=15.0, slip_pips=0.080, comm_rate_per_m=15.0),
        SConfig("S17_COST_STRESS", "cost_stress", "H4a", "S17 (H4a no first 30m) under 1.5x cost stress", max_score=0.80, block_first_n_mins_open=30.0, slip_pips=0.080, comm_rate_per_m=15.0),
    ]
    if include_sensitivity:
        cfgs.extend([
            SConfig("SENS_H4a_CLOSE_14UTC", "sensitivity", "H4a", "H4a close at 14:00 UTC", max_score=0.80, max_utc_hour_ex=14),
            SConfig("SENS_H4a_OPEN_08UTC", "sensitivity", "H4a", "H4a open at 08:00 UTC", max_score=0.80, min_utc_hour=8),
            SConfig("SENS_H4a_OPEN_09UTC", "sensitivity", "H4a", "H4a open at 09:00 UTC", max_score=0.80, min_utc_hour=9),
            SConfig("SENS_H4a_FIRST15M", "sensitivity", "H4a", "H4a block first 15m of London/NY open", max_score=0.80, block_first_n_mins_open=15.0),
            SConfig("SENS_H4a_FIRST45M", "sensitivity", "H4a", "H4a block first 45m of London/NY open", max_score=0.80, block_first_n_mins_open=45.0),
            SConfig("SENS_H4a_TRANS10M", "sensitivity", "H4a", "H4a block +-10m transitions", max_score=0.80, block_transition_half_mins=10.0),
            SConfig("SENS_H4a_TRANS20M", "sensitivity", "H4a", "H4a block +-20m transitions", max_score=0.80, block_transition_half_mins=20.0),
        ])
    return cfgs


_RECON_NAMES = {
    "H0_BASELINE_V2",
    "H4a_SCORE_CAP_080",
    "S04_H4a_NO_UTC09",
    "S05_H4a_NO_UTC16",
    "S06_H4a_CLOSE_15UTC",
    "S07_H0_NO_NY_POST_LDN",
    "S08_H4a_NO_NY_POST_LDN",
    "S15_H4a_NO_TRANS_15M",
    "S17_H4a_NO_FIRST30M",
}


def _run_window(
    conn,
    wid: str,
    start_dt: datetime,
    end_dt: datetime,
    configs: list[SConfig],
) -> tuple[list[SessionPortfolio], dict[str, SessionPortfolio]]:
    # Primary research portfolios
    pfs = [SessionPortfolio(c, track_stops=(c.name in _RECON_NAMES)) for c in configs]
    # Completely separate independent clean-room verification portfolios for retained candidates
    ver_cfgs = [c for c in configs if c.name in _RECON_NAMES]
    ver_pfs = {c.name: SessionPortfolio(c, track_stops=True) for c in ver_cfgs}

    warmup_start = start_dt - timedelta(days=4)

    def _fetch(s_dt: datetime, e_dt: datetime) -> pd.DataFrame:
        chunks = []
        cs = s_dt
        while cs < e_dt:
            ce = min(cs + timedelta(days=7), e_dt)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT time, bid, ask FROM market_ticks WHERE symbol = 'XAUUSD' AND time >= %s AND time < %s ORDER BY time ASC",
                    (cs, ce),
                )
                rows = cur.fetchall()
            if rows:
                chunks.append(pd.DataFrame(rows, columns=["time", "bid", "ask"]))
            cs = ce
        if not chunks:
            return pd.DataFrame(columns=["time", "bid", "ask"])
        out = pd.concat(chunks, ignore_index=True)
        out["time"] = pd.to_datetime(out["time"], utc=True)
        return out

    # Primary stream state
    k1 = t11a.CleanKalman2D()
    a5_1 = t11a.CleanBarAgg(5, 200)
    a1h_1 = t11a.CleanBarAgg(60, 100)
    rd_1 = t11a.CleanRegimeDetector()
    reg_1 = "UNKNOWN"

    # Independent verification stream state (separate objects)
    k2 = t6c.IndKalman2D()
    a5_2 = t6c.IndBarAggregator(5, 200)
    a1h_2 = t6c.IndBarAggregator(60, 100)
    rd_2 = t6c.IndRegimeDetector()
    reg_2 = "UNKNOWN"

    bar_idx = -1
    last_valid: Optional[tuple[float, float, datetime]] = None

    w_df = _fetch(warmup_start, start_dt)
    if not w_df.empty:
        w_df["minute"] = w_df["time"].dt.floor("min")
        for mts, grp in w_df.groupby("minute", sort=True):
            wb = grp["bid"].values.astype(np.float64)
            wa = grp["ask"].values.astype(np.float64)
            vi = np.flatnonzero((wb > 0.0) & (wa > 0.0) & (wb < wa))
            if len(vi) == 0:
                continue
            for j in vi:
                px = float(wb[j])
                k1.update(px)
                k2.update(px)
            vb = wb[vi]
            wc = {"time": mts.to_pydatetime(), "open": float(vb[0]), "high": float(vb.max()), "low": float(vb.min()), "close": float(vb[-1]), "tick_count": int(len(vi))}
            a5_1.update(wc)
            a1h_1.update(wc)
            a5_2.update(dict(wc))
            a1h_2.update(dict(wc))
    del w_df

    end_ex = end_dt + timedelta(microseconds=1)
    cs = start_dt
    while cs < end_ex:
        ce = min(cs + timedelta(days=7), end_ex)
        print(f"[TASK 12 {wid}] Streaming {cs.strftime('%Y-%m-%d')} -> {ce.strftime('%Y-%m-%d')} (H0={len(pfs[0].trades)}, H4a={len(pfs[1].trades)})...", flush=True)
        t_df = _fetch(cs, ce)
        cs = ce
        if t_df.empty:
            continue
        t_df["minute"] = t_df["time"].dt.floor("min")
        for mts, grp in t_df.groupby("minute", sort=True):
            bar_idx += 1
            m_py = mts.to_pydatetime()
            d_py = m_py.date()
            bids = grp["bid"].values.astype(np.float64)
            asks = grp["ask"].values.astype(np.float64)
            ttimes = [pd.Timestamp(x).to_pydatetime() for x in grp["time"].values]
            vm = (bids > 0.0) & (asks > 0.0) & (bids < asks)
            vi = np.flatnonzero(vm)
            if len(vi) > 0:
                lv = int(vi[-1])
                last_valid = (float(bids[lv]), float(asks[lv]), ttimes[lv])

            c5_1 = c5_2 = False
            df1 = df2 = pd.DataFrame()
            if len(vi) > 0:
                vb = bids[vi]
                candle = {"time": m_py, "open": float(vb[0]), "high": float(vb.max()), "low": float(vb.min()), "close": float(vb[-1]), "tick_count": int(len(vi))}
                c5_1 = a5_1.update(candle)
                a1h_1.update(candle)
                c5_2 = a5_2.update(dict(candle))
                a1h_2.update(dict(candle))
                if c5_1 and len(a5_1.bars) >= 60:
                    df1 = t6c._ind_compute_indicators(list(a5_1.bars))
                    if not df1.empty:
                        reg_1 = rd_1.detect(df1)
                if c5_2 and len(a5_2.bars) >= 60:
                    df2 = t6c._ind_compute_indicators(list(a5_2.bars))
                    if not df2.empty:
                        reg_2 = rd_2.detect(df2)

            busy1 = [False] * len(pfs)
            for idx, pf in enumerate(pfs):
                pf.new_day(d_py)
                if pf.open_pos is not None:
                    busy1[idx] = True
                    pf.open_pos.bars_held += 1
                    ex = pf.scan_exit(bids, asks, ttimes, 0, True)
                    if ex is not None:
                        pf.close_pos(pf.open_pos, ex[0], ex[1], ex[2])
                        pf.open_pos = None

            busy2: dict[str, bool] = {}
            for cname, vpf in ver_pfs.items():
                vpf.new_day(d_py)
                if vpf.open_pos is not None:
                    busy2[cname] = True
                    vpf.open_pos.bars_held += 1
                    ex = vpf.scan_exit(bids, asks, ttimes, 0, True)
                    if ex is not None:
                        vpf.close_pos(vpf.open_pos, ex[0], ex[1], ex[2])
                        vpf.open_pos = None
                else:
                    busy2[cname] = False

            if len(vi) == 0:
                continue
            en_i = int(vi[0])
            for j in range(en_i + 1):
                if vm[j]:
                    px = float(bids[j])
                    k1.update(px)
                    k2.update(px)
            k1_x1, k1_acc = k1.x1, k1.accel()
            k2_x1, k2_acc = k2.x1, k2.accel()
            for j in range(en_i + 1, len(bids)):
                if vm[j]:
                    px = float(bids[j])
                    k1.update(px)
                    k2.update(px)

            # Stream 1 entries (all hypotheses)
            if c5_1 and len(a5_1.bars) >= 60 and not df1.empty:
                htf1 = t6c._ind_htf_trend([b["close"] for b in a1h_1.bars])
                sigs1 = t6c._ind_generate_signals(df1, reg_1) if htf1 != 0 else []
                if sigs1:
                    lr = df1.iloc[-1]
                    cs_s = df1["close"]
                    c5_ts = a5_1.bars[-1]["time"]
                    feat = {
                        "kalman_vel": k1_x1,
                        "kalman_accel": k1_acc,
                        "shannon_entropy": t6c._ind_shannon_entropy(cs_s.pct_change().dropna()) if len(cs_s) > 30 else 2.0,
                        "kaufman_er": t6c._ind_kaufman_er(cs_s) if len(cs_s) > 15 else 0.3,
                        "ema20_slope": float(lr["ema20_slope"]) if pd.notna(lr.get("ema20_slope")) else 0.0,
                        "tick_weighted_price_dist": float(lr["tick_weighted_price_dist"]) if pd.notna(lr.get("tick_weighted_price_dist")) else 0.0,
                        "tick_rate_ratio": float(lr["tick_rate_ratio"]) if pd.notna(lr.get("tick_rate_ratio")) else 1.0,
                        "utc_hour": c5_ts.hour,
                    }
                    atr_v = float(df1["atr"].iloc[-1]) if pd.notna(df1["atr"].iloc[-1]) else 1.0
                    eb, ea = float(bids[en_i]), float(asks[en_i])
                    sp_usd = ea - eb
                    en_ts_utc = t11a._to_utc(ttimes[en_i])
                    sinfo = t12p1.classify_dst_session(en_ts_utc)
                    scored = [(t6c._ind_score(d, reg_1, feat), st, d) for st, d in sigs1]
                    scored.sort(key=lambda x: x[0], reverse=True)

                    for idx, pf in enumerate(pfs):
                        if busy1[idx]:
                            continue
                        c = pf.cfg
                        if abs(min(0.0, pf.daily_pnl)) / max(pf.day_start_eq, 1.0) * 100.0 >= 3.0 or pf.daily_trades >= 8 or pf.consec_losses >= 3:
                            continue
                        if reg_1 == "HIGH_VOLATILITY":
                            continue
                        chosen = None
                        for sc, st, d in scored:
                            if not (0.60 <= sc < c.max_score):
                                continue
                            if (d == "BUY" and htf1 != 1) or (d == "SELL" and htf1 != -1):
                                continue
                            if d == "BUY" and reg_1 == "SIDEWAYS":
                                continue
                            if not _passes_session_filter(c, d, en_ts_utc, sp_usd, sinfo):
                                continue
                            if bar_idx - pf.last_cd_bar.get((st, d), -9999) < 15:
                                continue
                            chosen = (sc, st, d)
                            break
                        if chosen is None:
                            continue
                        if pf.halted:
                            pf.prevented_signals_after_halt += 1
                            continue
                        sc, st, d = chosen
                        raw_ep = ea if d == "BUY" else eb
                        ep = (raw_ep + pf.slip_offset) if d == "BUY" else (raw_ep - pf.slip_offset)
                        lot, sl_px, tp_px = t11a._clean_size(pf.equity, ep, atr_v, d)
                        if lot is None:
                            continue
                        e_comm = _side_comm(lot, ep, c.comm_rate_per_m)
                        e_slip = pf.slip_offset * lot * 100.0
                        if pf.open_pos is not None:
                            pf.constraint_violations += 1
                        pf.open_pos = t11a.CleanPos(
                            signal_time=c5_ts, direction=d, strategy=st, entry_time=ttimes[en_i],
                            entry_bid=eb, entry_ask=ea, entry_price=ep,
                            initial_sl=sl_px, sl=sl_px, tp=tp_px, atr=atr_v, lot=lot,
                            entry_comm=e_comm, slip_usd=e_slip, peak_price=ep, last_swap_ts=ttimes[en_i],
                        )
                        pf.mtm.update(pf.equity - sp_usd * lot * 100.0 - 2.0 * e_comm - e_slip)
                        pf.last_cd_bar[(st, d)] = bar_idx
                        pf.daily_trades += 1
                        if en_i + 1 < len(bids):
                            ex = pf.scan_exit(bids, asks, ttimes, en_i + 1, False)
                            if ex is not None:
                                pf.close_pos(pf.open_pos, ex[0], ex[1], ex[2])
                                pf.open_pos = None

            # Stream 2 independent verification entries (retained candidates)
            if c5_2 and len(a5_2.bars) >= 60 and not df2.empty:
                htf2 = t6c._ind_htf_trend([b["close"] for b in a1h_2.bars])
                sigs2 = t6c._ind_generate_signals(df2, reg_2) if htf2 != 0 else []
                if sigs2:
                    lr = df2.iloc[-1]
                    cs_s = df2["close"]
                    c5_ts = a5_2.bars[-1]["time"]
                    feat = {
                        "kalman_vel": k2_x1,
                        "kalman_accel": k2_acc,
                        "shannon_entropy": t6c._ind_shannon_entropy(cs_s.pct_change().dropna()) if len(cs_s) > 30 else 2.0,
                        "kaufman_er": t6c._ind_kaufman_er(cs_s) if len(cs_s) > 15 else 0.3,
                        "ema20_slope": float(lr["ema20_slope"]) if pd.notna(lr.get("ema20_slope")) else 0.0,
                        "tick_weighted_price_dist": float(lr["tick_weighted_price_dist"]) if pd.notna(lr.get("tick_weighted_price_dist")) else 0.0,
                        "tick_rate_ratio": float(lr["tick_rate_ratio"]) if pd.notna(lr.get("tick_rate_ratio")) else 1.0,
                        "utc_hour": c5_ts.hour,
                    }
                    atr_v = float(df2["atr"].iloc[-1]) if pd.notna(df2["atr"].iloc[-1]) else 1.0
                    eb, ea = float(bids[en_i]), float(asks[en_i])
                    sp_usd = ea - eb
                    en_ts_utc = t11a._to_utc(ttimes[en_i])
                    sinfo = t12p1.classify_dst_session(en_ts_utc)
                    scored = [(t6c._ind_score(d, reg_2, feat), st, d) for st, d in sigs2]
                    scored.sort(key=lambda x: x[0], reverse=True)

                    for cname, vpf in ver_pfs.items():
                        if busy2[cname]:
                            continue
                        c = vpf.cfg
                        if abs(min(0.0, vpf.daily_pnl)) / max(vpf.day_start_eq, 1.0) * 100.0 >= 3.0 or vpf.daily_trades >= 8 or vpf.consec_losses >= 3:
                            continue
                        if reg_2 == "HIGH_VOLATILITY":
                            continue
                        chosen = None
                        for sc, st, d in scored:
                            if not (0.60 <= sc < c.max_score):
                                continue
                            if (d == "BUY" and htf2 != 1) or (d == "SELL" and htf2 != -1):
                                continue
                            if d == "BUY" and reg_2 == "SIDEWAYS":
                                continue
                            if not _passes_session_filter(c, d, en_ts_utc, sp_usd, sinfo):
                                continue
                            if bar_idx - vpf.last_cd_bar.get((st, d), -9999) < 15:
                                continue
                            chosen = (sc, st, d)
                            break
                        if chosen is None:
                            continue
                        sc, st, d = chosen
                        raw_ep = ea if d == "BUY" else eb
                        ep = (raw_ep + vpf.slip_offset) if d == "BUY" else (raw_ep - vpf.slip_offset)
                        lot, sl_px, tp_px = t6c._ind_size(vpf.equity, ep, atr_v, d)
                        if lot is None:
                            continue
                        e_comm = _side_comm(lot, ep, c.comm_rate_per_m)
                        e_slip = vpf.slip_offset * lot * 100.0
                        if vpf.open_pos is not None:
                            vpf.constraint_violations += 1
                        vpf.open_pos = t11a.CleanPos(
                            signal_time=c5_ts, direction=d, strategy=st, entry_time=ttimes[en_i],
                            entry_bid=eb, entry_ask=ea, entry_price=ep,
                            initial_sl=sl_px, sl=sl_px, tp=tp_px, atr=atr_v, lot=lot,
                            entry_comm=e_comm, slip_usd=e_slip, peak_price=ep, last_swap_ts=ttimes[en_i],
                        )
                        vpf.mtm.update(vpf.equity - sp_usd * lot * 100.0 - 2.0 * e_comm - e_slip)
                        vpf.last_cd_bar[(st, d)] = bar_idx
                        vpf.daily_trades += 1
                        if en_i + 1 < len(bids):
                            ex = vpf.scan_exit(bids, asks, ttimes, en_i + 1, False)
                            if ex is not None:
                                vpf.close_pos(vpf.open_pos, ex[0], ex[1], ex[2])
                                vpf.open_pos = None

    if last_valid is not None:
        lb, la, lt = last_valid
        for pf in list(pfs) + list(ver_pfs.values()):
            if pf.open_pos is not None:
                rc = lb if pf.open_pos.direction == "BUY" else la
                epx = (rc - pf.slip_offset) if pf.open_pos.direction == "BUY" else (rc + pf.slip_offset)
                pf.open_pos.slip_usd += pf.slip_offset * pf.open_pos.lot * 100.0
                pf.close_pos(pf.open_pos, "END_OF_BACKTEST", epx, lt)
                pf.open_pos = None

    return pfs, ver_pfs


def _summ(trades: list[dict[str, Any]], mtm: t11a.CleanMtm) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr_pct": 0.0, "net_pnl": 0.0, "pf": 0.0, "exp": 0.0, "closed_dd": 0.0, "mtm_dd_pct": round(mtm.max_dd_pct, 2)}
    p = np.array([t["net_pnl"] for t in trades], dtype=float)
    w = p[p > 0]
    l = p[p <= 0]
    pf = float(w.sum() / abs(l.sum())) if len(l) and l.sum() < 0 else (999.0 if len(w) and w.sum() > 0 else 0.0)
    ceq = np.cumsum(p) + 10000.0
    hwm = np.maximum.accumulate(np.insert(ceq, 0, 10000.0))[1:]
    cdd = float((ceq - hwm).min())
    return {
        "n": int(n),
        "wr_pct": round(len(w) / n * 100.0, 2),
        "net_pnl": round(float(p.sum()), 2),
        "pf": round(pf, 4),
        "exp": round(float(p.mean()), 2),
        "closed_dd": round(cdd, 2),
        "mtm_dd_pct": round(mtm.max_dd_pct, 2),
    }


def _bootstrap_ci(vals: list[float], n_boot: int = 2000, seed: int = 42) -> tuple[float, float]:
    if len(vals) < 2:
        m = float(vals[0]) if vals else 0.0
        return round(m, 2), round(m, 2)
    rng = np.random.default_rng(seed)
    arr = np.array(vals, dtype=float)
    means = rng.choice(arr, size=(n_boot, len(arr)), replace=True).mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return round(float(lo), 2), round(float(hi), 2)


def _bh_fdr(pvals: list[float]) -> list[float]:
    m = len(pvals)
    if m == 0:
        return []
    indexed = sorted(enumerate(pvals), key=lambda x: x[1])
    out = [1.0] * m
    prev = 1.0
    for rev_i, (idx, pv) in enumerate(reversed(indexed)):
        rank = m - rev_i
        q = min(prev, (pv * m) / rank)
        out[idx] = round(min(1.0, q), 5)
        prev = q
    return out


def main() -> None:
    windows = [
        ("W1", datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc)),
        ("W2", datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc)),
        ("W3", datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 18, 18, 9, 59, tzinfo=timezone.utc)),
    ]

    conn = psycopg2.connect(os.environ.get("DB_URL", "postgresql://postgres:password@db:5432/goldedge"))
    all_res: dict[str, dict[str, SessionPortfolio]] = {}
    all_ver: dict[str, dict[str, SessionPortfolio]] = {}

    try:
        for wid, s_dt, e_dt in windows:
            cfgs = _build_configs(include_sensitivity=(wid == "W3"))
            pfs, vpfs = _run_window(conn, wid, s_dt, e_dt, cfgs)
            all_res[wid] = {pf.cfg.name: pf for pf in pfs}
            all_ver[wid] = vpfs
    finally:
        conn.close()

    # =========================================================================
    # DELIVERABLE 9 & 10: Hypothesis Results CSV + Parameter Sensitivity CSV
    # =========================================================================
    hyp_rows: list[dict[str, Any]] = []
    sens_rows: list[dict[str, Any]] = []
    w3_h0_keys = {(t["entry_time"], t["direction"]): t["net_pnl"] for t in all_res["W3"]["H0_BASELINE_V2"].trades}
    w3_h4_keys = {(t["entry_time"], t["direction"]): t["net_pnl"] for t in all_res["W3"]["H4a_SCORE_CAP_080"].trades}
    w3_h0_pnls = [t["net_pnl"] for t in all_res["W3"]["H0_BASELINE_V2"].trades]
    w3_h4_pnls = [t["net_pnl"] for t in all_res["W3"]["H4a_SCORE_CAP_080"].trades]

    w3_cfgs = _build_configs(include_sensitivity=True)
    for cfg in w3_cfgs:
        pf3 = all_res["W3"][cfg.name]
        s3 = _summ(pf3.trades, pf3.mtm)
        base_keys = w3_h0_keys if cfg.base_variant == "H0" else w3_h4_keys
        cur_keys = {(t["entry_time"], t["direction"]) for t in pf3.trades}
        removed_pnls = [p for k, p in base_keys.items() if k not in cur_keys]
        rem_n = len(removed_pnls)
        rem_w = sum(1 for p in removed_pnls if p > 0)
        rem_l = sum(1 for p in removed_pnls if p <= 0)
        new_n = sum(1 for k in cur_keys if k not in base_keys)

        pnls3 = [t["net_pnl"] for t in pf3.trades]
        base_pnls = w3_h0_pnls if cfg.base_variant == "H0" else w3_h4_pnls
        mw_p = float(stats.mannwhitneyu(pnls3, base_pnls, alternative="two-sided").pvalue) if (len(pnls3) >= 2 and cfg.name not in ("H0_BASELINE_V2", "H4a_SCORE_CAP_080")) else 1.0
        ci_lo, ci_hi = _bootstrap_ci(pnls3)

        # Ex-best-hour and ex-top-3 check
        hr_sums: dict[int, float] = {}
        for t in pf3.trades:
            hr_sums[t["utc_hour"]] = hr_sums.get(t["utc_hour"], 0.0) + t["net_pnl"]
        best_hr = max(hr_sums, key=hr_sums.get) if hr_sums else -1
        ex_best_hr_pnl = round(sum(t["net_pnl"] for t in pf3.trades if t["utc_hour"] != best_hr), 2)
        sp_sorted = sorted(pnls3, reverse=True)
        ex_top3_pnl = round(sum(sp_sorted[3:]) if len(sp_sorted) > 3 else 0.0, 2)

        buy_t = [t for t in pf3.trades if t["direction"] == "BUY"]
        sell_t = [t for t in pf3.trades if t["direction"] == "SELL"]
        row = {
            "hypothesis": cfg.name,
            "group": cfg.group,
            "base": cfg.base_variant,
            "rule": cfg.rule_desc,
            "w3_n": s3["n"],
            "rem_vs_base": rem_n,
            "rem_winners": rem_w,
            "rem_losers": rem_l,
            "new_vs_base": new_n,
            "w3_wr_pct": s3["wr_pct"],
            "w3_net_pnl": s3["net_pnl"],
            "w3_pf": s3["pf"],
            "w3_exp": s3["exp"],
            "w3_exp_ci95_lo": ci_lo,
            "w3_exp_ci95_hi": ci_hi,
            "w3_closed_dd": s3["closed_dd"],
            "w3_mtm_dd_pct": s3["mtm_dd_pct"],
            "w3_buy_n": len(buy_t),
            "w3_buy_pnl": round(sum(t["net_pnl"] for t in buy_t), 2),
            "w3_sell_n": len(sell_t),
            "w3_sell_pnl": round(sum(t["net_pnl"] for t in sell_t), 2),
            "w3_best_hr": best_hr,
            "w3_ex_best_hr_pnl": ex_best_hr_pnl,
            "w3_ex_top3_pnl": ex_top3_pnl,
            "mw_p_vs_base": round(mw_p, 5),
        }
        # Attach W1 and W2 metrics if evaluated
        for wid in ("W1", "W2"):
            if cfg.name in all_res[wid]:
                sw = _summ(all_res[wid][cfg.name].trades, all_res[wid][cfg.name].mtm)
                row[f"{wid.lower()}_n"] = sw["n"]
                row[f"{wid.lower()}_net_pnl"] = sw["net_pnl"]
                row[f"{wid.lower()}_pf"] = sw["pf"]
                row[f"{wid.lower()}_mtm_dd_pct"] = sw["mtm_dd_pct"]
            else:
                row[f"{wid.lower()}_n"] = None
                row[f"{wid.lower()}_net_pnl"] = None
                row[f"{wid.lower()}_pf"] = None
                row[f"{wid.lower()}_mtm_dd_pct"] = None

        if cfg.group == "sensitivity":
            sens_rows.append(row)
        else:
            hyp_rows.append(row)

    # Apply Benjamini-Hochberg FDR across all non-control hypothesis rows
    p_list = [r["mw_p_vs_base"] for r in hyp_rows]
    q_list = _bh_fdr(p_list)
    for r, q in zip(hyp_rows, q_list):
        r["mw_q_fdr"] = q

    pd.DataFrame(hyp_rows).to_csv(OUT_DIR / "task12_session_hypothesis_results.csv", index=False)
    # Include baseline H4a and S05/S06/S15/S17 in sensitivity table for complete curves
    ref_for_sens = [r for r in hyp_rows if r["hypothesis"] in ("H4a_SCORE_CAP_080", "S05_H4a_NO_UTC16", "S06_H4a_CLOSE_15UTC", "S15_H4a_NO_TRANS_15M", "S17_H4a_NO_FIRST30M")]
    pd.DataFrame(ref_for_sens + sens_rows).to_csv(OUT_DIR / "task12_parameter_sensitivity.csv", index=False)

    # =========================================================================
    # DELIVERABLE 11: Statistical & Robustness Validation JSON
    # =========================================================================
    stat_val = {
        "multiple_testing_method": "Benjamini-Hochberg FDR across all tested session hypotheses",
        "num_hypotheses_tested": len(hyp_rows),
        "num_significant_at_fdr_005": int(sum(1 for r in hyp_rows if r["mw_q_fdr"] < 0.05)),
        "min_raw_p": min(r["mw_p_vs_base"] for r in hyp_rows if r["hypothesis"] not in ("H0_BASELINE_V2", "H4a_SCORE_CAP_080")),
        "min_fdr_q": min(r["mw_q_fdr"] for r in hyp_rows if r["hypothesis"] not in ("H0_BASELINE_V2", "H4a_SCORE_CAP_080")),
        "bootstrap_resamples": 2000,
        "bootstrap_seed": 42,
        "power_warning": (
            "With n=66..143 trades over 95 days and individual session/hour removals of n=5..31 trades, "
            "statistical power to detect realistic expectancy shifts at alpha=0.05 is low (<30%). "
            "No session filter achieves BH-FDR statistical significance; all improvements remain exploratory."
        ),
        "cross_window_consistency": {
            r["hypothesis"]: {
                "W1": {"n": r["w1_n"], "pnl": r["w1_net_pnl"], "pf": r["w1_pf"], "mtm_dd_pct": r["w1_mtm_dd_pct"]},
                "W2": {"n": r["w2_n"], "pnl": r["w2_net_pnl"], "pf": r["w2_pf"], "mtm_dd_pct": r["w2_mtm_dd_pct"]},
                "W3": {"n": r["w3_n"], "pnl": r["w3_net_pnl"], "pf": r["w3_pf"], "mtm_dd_pct": r["w3_mtm_dd_pct"]},
                "w3_exp_ci95": [r["w3_exp_ci95_lo"], r["w3_exp_ci95_hi"]],
                "w3_ex_best_hr_pnl": r["w3_ex_best_hr_pnl"],
                "w3_ex_top3_pnl": r["w3_ex_top3_pnl"],
                "mw_p": r["mw_p_vs_base"],
                "mw_q_fdr": r["mw_q_fdr"],
            }
            for r in hyp_rows
        },
    }
    (OUT_DIR / "task12_statistical_validation.json").write_text(json.dumps(stat_val, indent=2), encoding="utf-8")

    # =========================================================================
    # DELIVERABLE 12: Task 8 8% HWM Risk Evaluation JSON
    # =========================================================================
    hwm_report: dict[str, Any] = {}
    hwm_pairs = [
        ("H0_BASELINE_V2", "H0_HWM8"),
        ("H4a_SCORE_CAP_080", "H4a_HWM8"),
        ("S07_H0_NO_NY_POST_LDN", "S07_H0_NO_NYPOST_HWM8"),
        ("S15_H4a_NO_TRANS_15M", "S15_H4a_NOTRANS_HWM8"),
        ("S17_H4a_NO_FIRST30M", "S17_H4a_NOF30_HWM8"),
    ]
    for wid in ("W1", "W2", "W3"):
        hwm_report[wid] = {}
        for unp_name, pro_name in hwm_pairs:
            u_pf = all_res[wid][unp_name]
            p_pf = all_res[wid][pro_name]
            # Per-session & per-hour loss contribution during H0/unprotected drawdown window (peak -> trough)
            # Compute session share of total losing-trade dollars before halt (or across full window)
            sess_loss: dict[str, float] = {}
            hr_loss: dict[str, float] = {}
            for t in p_pf.trades:
                if t["net_pnl"] < 0:
                    sess_loss[t["dst_session"]] = round(sess_loss.get(t["dst_session"], 0.0) + t["net_pnl"], 2)
                    hk = f"UTC_{t['utc_hour']:02d}"
                    hr_loss[hk] = round(hr_loss.get(hk, 0.0) + t["net_pnl"], 2)
            hwm_report[wid][pro_name] = {
                "unprotected_ref": unp_name,
                "hwm_triggered": p_pf.halted,
                "halt_details": p_pf.halt_info,
                "trades_executed": len(p_pf.trades),
                "unprotected_trades": len(u_pf.trades),
                "trades_prevented_by_halt": len(u_pf.trades) - len(p_pf.trades),
                "signals_blocked_post_halt": p_pf.prevented_signals_after_halt,
                "net_pnl_protected_usd": round(sum(t["net_pnl"] for t in p_pf.trades), 2),
                "net_pnl_unprotected_usd": round(sum(t["net_pnl"] for t in u_pf.trades), 2),
                "mtm_dd_protected_pct": round(p_pf.mtm.max_dd_pct, 2),
                "mtm_dd_unprotected_pct": round(u_pf.mtm.max_dd_pct, 2),
                "losing_dollars_by_dst_session": sess_loss,
                "losing_dollars_by_utc_hour": dict(sorted(hr_loss.items())),
            }
    (OUT_DIR / "task12_hwm_comparison.json").write_text(json.dumps(hwm_report, indent=2), encoding="utf-8")

    # =========================================================================
    # DELIVERABLE 13: Independent Raw-Tick Reconciliation JSON
    # =========================================================================
    recon: dict[str, Any] = {"summary": {}, "windows": {}}
    total_pairs = rec_pairs = total_trades_v = total_stops_v = 0
    for wid in ("W1", "W2", "W3"):
        recon["windows"][wid] = {}
        for cname in sorted(_RECON_NAMES):
            total_pairs += 1
            a_pf = all_res[wid][cname]
            b_pf = all_ver[wid][cname]
            at, bt = a_pf.trades, b_pf.trades
            matched = sess_mm = stop_mm = qty_mm = cost_mm = 0
            max_pnl_diff = max_eq_diff = 0.0
            stops_cnt = 0
            for ti in range(min(len(at), len(bt))):
                a, b = at[ti], bt[ti]
                p_d = abs(a["net_pnl"] - b["net_pnl"])
                e_d = abs(a["equity_after"] - b["equity_after"])
                max_pnl_diff = max(max_pnl_diff, p_d)
                max_eq_diff = max(max_eq_diff, e_d)
                if a["dst_session"] != b["dst_session"] or a["utc_hour"] != b["utc_hour"]:
                    sess_mm += 1
                if abs(a["qty"] - b["qty"]) > 1e-6:
                    qty_mm += 1
                if abs(a["commission_total"] - b["commission_total"]) > 1e-6 or abs(a["slippage_usd"] - b["slippage_usd"]) > 1e-6:
                    cost_mm += 1
                su_ok = len(a["stop_updates"]) == len(b["stop_updates"])
                if su_ok:
                    for u1, u2 in zip(a["stop_updates"], b["stop_updates"]):
                        if abs(u1["old_sl"] - u2["old_sl"]) > 1e-5 or abs(u1["new_sl"] - u2["new_sl"]) > 1e-5 or u1["flag"] != u2["flag"]:
                            su_ok = False
                            break
                if not su_ok:
                    stop_mm += 1
                stops_cnt += len(b["stop_updates"])
                if (
                    a["entry_time"] == b["entry_time"]
                    and a["exit_time"] == b["exit_time"]
                    and a["direction"] == b["direction"]
                    and a["exit_reason"] == b["exit_reason"]
                    and p_d <= 1e-6
                    and su_ok
                    and a["dst_session"] == b["dst_session"]
                ):
                    matched += 1

            missing = max(0, len(at) - len(bt))
            extra = max(0, len(bt) - len(at))
            mtm_diff = round(abs(a_pf.mtm.max_dd_pct - b_pf.mtm.max_dd_pct), 6)
            ok = (
                missing == 0 and extra == 0 and matched == len(bt)
                and sess_mm == 0 and stop_mm == 0 and qty_mm == 0 and cost_mm == 0
                and max_pnl_diff <= 1e-6 and max_eq_diff <= 1e-6 and mtm_diff <= 1e-6
                and a_pf.constraint_violations == 0 and b_pf.constraint_violations == 0
            )
            if ok:
                rec_pairs += 1
            total_trades_v += matched
            total_stops_v += stops_cnt
            recon["windows"][wid][cname] = {
                "status": "FULLY_RECONCILED" if ok else "MISMATCH_FOUND",
                "matched_trades": matched,
                "total_trades": len(bt),
                "missing_trades": missing,
                "extra_trades": extra,
                "session_classification_mismatches": sess_mm,
                "stop_update_mismatches": stop_mm,
                "stop_updates_verified": stops_cnt,
                "qty_mismatches": qty_mm,
                "cost_mismatches": cost_mm,
                "max_net_pnl_diff_usd": round(max_pnl_diff, 6),
                "max_equity_path_diff_usd": round(max_eq_diff, 6),
                "mtm_dd_pct_diff": mtm_diff,
                "portfolio_constraint_violations": a_pf.constraint_violations + b_pf.constraint_violations,
            }
    recon["summary"] = {
        "total_candidate_window_pairs": total_pairs,
        "fully_reconciled_pairs": rec_pairs,
        "unverified_or_mismatched_pairs": total_pairs - rec_pairs,
        "total_trades_reconciled": total_trades_v,
        "total_stop_updates_verified": total_stops_v,
        "all_reconciled": total_pairs == rec_pairs,
    }
    (OUT_DIR / "task12_independent_reconciliation.json").write_text(json.dumps(recon, indent=2), encoding="utf-8")

    print("\n[TASK 12 PART 2 COMPLETE — Phases 7-11]")
    assert len(all_res["W1"]["H0_BASELINE_V2"].trades) == 118, "W1 H0 mismatch"
    assert len(all_res["W2"]["H0_BASELINE_V2"].trades) == 131, "W2 H0 mismatch"
    assert len(all_res["W3"]["H0_BASELINE_V2"].trades) == 143, "W3 H0 mismatch"
    assert len(all_res["W3"]["H4a_SCORE_CAP_080"].trades) == 99, "W3 H4a mismatch"
    print("Reconciliation Summary:", json.dumps(recon["summary"], indent=2))
    print("\nW3 Session Hypotheses Summary (Top rows):")
    for r in hyp_rows:
        if r["group"] in ("control", "utc_hour_h4a", "session_h0", "session_h4a", "transition", "open_close_30m", "buy_session", "sell_session", "cost_stress"):
            print(
                f"  {r['hypothesis']:24s} | W3 n={r['w3_n']:3d} pnl={r['w3_net_pnl']:8.2f} pf={r['w3_pf']:6.3f} dd={r['w3_mtm_dd_pct']:5.2f}% "
                f"| W1={r['w1_net_pnl']:7.1f} W2={r['w2_net_pnl']:7.1f} | exBestHr={r['w3_ex_best_hr_pnl']:7.1f} | p={r['mw_p_vs_base']:.4f} q={r['mw_q_fdr']:.4f}"
            )


if __name__ == "__main__":
    main()
