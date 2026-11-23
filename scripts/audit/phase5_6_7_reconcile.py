"""
Phases 3, 4, 5, 6, 7 — Reproducible XAUUSD Execution Integrity,
Look-Ahead Bias, Independent Simulator, Trade-Level P&L Ledger,
and Performance Metric Reconciliation.

Does NOT modify any strategy parameters, indicators, entry/exit conditions,
or production code. Reads TimescaleDB `market_ticks` and compares:
  1. Untouched RunBacktestUseCase (Baseline 17:00 UTC on 2026-08-24..2026-11-16,
     plus 4-day warmup 2026-08-20..2026-08-24) -> Original Engine trades & metrics.
  2. Look-Ahead Bias Audit at every signal bar (pre-minute causal Kalman vs
     end-of-minute post-lookahead Kalman currently used in RunBacktestUseCase).
  3. Independent Execution Simulator built from scratch (Mode A = Parity check,
     Mode B = Strict Causal + Gapped SL Market Fill + Intra-Entry-Minute Scan).
  4. Independent Trade-Level P&L Reconstruction on all 110 DB-covered trades
     from `backtest_results/frozen_111_baseline_trades.csv` (2026-08-21..2026-11-16).

Outputs written to `backtest_results/audit/`:
  - current_engine_trades.csv
  - lookahead_kalman_audit.csv
  - independent_parity_trades.csv
  - independent_strict_trades.csv
  - frozen_110_tick_reconciliation.csv
  - phase5_6_7_summary.json
"""
import contextlib
import csv
import io
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src")))

from goldedge.adapters.secondary.db.tick_repository import TickRepository
from goldedge.adapters.secondary.virtual_broker import VirtualBrokerAdapter
from goldedge.application.use_cases.run_backtest import RunBacktestUseCase
from goldedge.config.risk_profiles import MEDIUM_RISK_BACKTEST
from goldedge.domain.services.denoise_service import DenoiseService, VolumeAdaptiveKalmanFilter
from goldedge.domain.services.indicator_service import IndicatorService
from goldedge.domain.services.mtf_trend import RollingBarAggregator, compute_htf_ema_trend, passes_regime_filters
from goldedge.domain.services.regime_detector import RegimeDetectorService
from goldedge.domain.services.signal_generator import SignalGeneratorService
from goldedge.domain.services.sizing_service import PositionSizingService
from goldedge.domain.services.trade_scorer import TradeScorer

RAW_DB_URL = os.environ.get("DB_URL", "postgresql+psycopg2://postgres:password@timescaledb:5432/goldedge")
if RAW_DB_URL.startswith("postgresql://"):
    RAW_DB_URL = RAW_DB_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

OUT_DIR = Path("backtest_results/audit")
CONTRACT_SIZE = 100.0
TICK_SIZE = 0.01
TICK_VALUE = 1.0
PIP_UNIT = 0.10


def _iso(dt: Any) -> str:
    if dt is None:
        return ""
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


def _utc_min(val: Any) -> pd.Timestamp:
    ts = pd.Timestamp(val)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC").floor("min")
    return ts.tz_convert("UTC").floor("min")


def _compute_metrics(trades: List[Dict[str, Any]], initial_capital: float = 10000.0) -> Dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "net_profit": 0.0,
            "profit_factor": 0.0,
            "closed_max_dd_usd": 0.0,
            "closed_max_dd_pct_hwm": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "expectancy_per_trade": 0.0,
            "rr": 0.0,
            "max_consecutive_wins": 0,
            "max_consecutive_losses": 0,
            "long_trades": 0,
            "long_wins": 0,
            "long_net_pnl": 0.0,
            "short_trades": 0,
            "short_wins": 0,
            "short_net_pnl": 0.0,
        }
    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net_profit = sum(pnls)
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    rr = (avg_win / abs(avg_loss)) if avg_loss != 0 else 0.0

    eq = initial_capital
    hwm = initial_capital
    max_dd = 0.0
    max_dd_pct = 0.0
    max_cw = 0
    max_cl = 0
    cw = 0
    cl = 0
    for p in pnls:
        eq += p
        if eq > hwm:
            hwm = eq
        dd = eq - hwm
        if dd < max_dd:
            max_dd = dd
        pct = ((hwm - eq) / hwm * 100.0) if hwm > 0 else 0.0
        if pct > max_dd_pct:
            max_dd_pct = pct
        if p > 0:
            cw += 1
            cl = 0
            max_cw = max(max_cw, cw)
        else:
            cl += 1
            cw = 0
            max_cl = max(max_cl, cl)

    longs = [t for t in trades if str(t.get("direction", "")).upper() == "BUY"]
    shorts = [t for t in trades if str(t.get("direction", "")).upper() == "SELL"]
    return {
        "total_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / n * 100.0, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_profit": round(net_profit, 2),
        "profit_factor": round(pf, 4),
        "closed_max_dd_usd": round(max_dd, 2),
        "closed_max_dd_pct_hwm": round(max_dd_pct, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy_per_trade": round(net_profit / n, 4),
        "rr": round(rr, 4),
        "max_consecutive_wins": max_cw,
        "max_consecutive_losses": max_cl,
        "long_trades": len(longs),
        "long_wins": sum(1 for t in longs if float(t["pnl"]) > 0),
        "long_net_pnl": round(sum(float(t["pnl"]) for t in longs), 2),
        "short_trades": len(shorts),
        "short_wins": sum(1 for t in shorts if float(t["pnl"]) > 0),
        "short_net_pnl": round(sum(float(t["pnl"]) for t in shorts), 2),
    }


def _reconstruct_frozen_baseline_from_ticks(engine) -> Dict[str, Any]:
    """
    Phase 5: Reconstruct the 111 frozen baseline trades from raw DB ticks
    (covering the 110 trades inside 2026-08-20..2026-11-16 retained in DB).
    """
    csv_path = Path("backtest_results/frozen_111_baseline_trades.csv")
    df_frozen = pd.read_csv(csv_path)
    records = []
    stats = {
        "frozen_total_trades": len(df_frozen),
        "db_covered_trades": 0,
        "entry_before_db_min": 0,
        "entry_exact_ask_for_buy": 0,
        "entry_exact_bid_for_sell": 0,
        "entry_was_bid0_not_ask0_for_buy": 0,
        "entry_inverted_quote_ticks": 0,
        "sl_gap_negative_count": 0,
        "total_sl_gap_extra_loss_usd": 0.0,
        "max_abs_pnl_formula_diff_usd": 0.0,
    }

    with engine.connect() as conn:
        for idx, row in df_frozen.iterrows():
            entry_min = pd.Timestamp(row["entry_time"]).tz_convert("UTC")
            exit_min = pd.Timestamp(row["exit_time"]).tz_convert("UTC")
            direction = str(row["direction"]).upper()
            recorded_entry = float(row["entry_price"])
            recorded_exit = float(row["exit_price"])
            qty = float(row["qty"])
            recorded_pnl = float(row["pnl"])
            exit_reason = str(row["exit_reason"])

            # Independent P&L arithmetic check using recorded entry/exit/qty
            calc_pnl = (
                (recorded_exit - recorded_entry) * qty * CONTRACT_SIZE
                if direction == "BUY"
                else (recorded_entry - recorded_exit) * qty * CONTRACT_SIZE
            )
            formula_diff = abs(calc_pnl - recorded_pnl)
            stats["max_abs_pnl_formula_diff_usd"] = max(stats["max_abs_pnl_formula_diff_usd"], formula_diff)

            if entry_min < pd.Timestamp("2026-08-20 00:00:00+00:00"):
                stats["entry_before_db_min"] += 1
                continue

            stats["db_covered_trades"] += 1
            # Query entry minute [entry_min, entry_min + 1m) and first tick >= entry_min
            en_rows = conn.execute(
                text("""
                    SELECT time, bid, ask
                    FROM market_ticks
                    WHERE symbol = 'XAUUSD' AND time >= :s AND time < :e
                    ORDER BY time ASC
                """),
                {"s": entry_min.to_pydatetime(), "e": (entry_min + timedelta(minutes=1)).to_pydatetime()},
            ).fetchall()

            ex_rows = conn.execute(
                text("""
                    SELECT time, bid, ask
                    FROM market_ticks
                    WHERE symbol = 'XAUUSD' AND time >= :s AND time < :e
                    ORDER BY time ASC
                """),
                {"s": exit_min.to_pydatetime(), "e": (exit_min + timedelta(minutes=1)).to_pydatetime()},
            ).fetchall()

            first_tick = en_rows[0] if en_rows else None
            en_ts = first_tick.time if first_tick else None
            en_bid = float(first_tick.bid) if first_tick else None
            en_ask = float(first_tick.ask) if first_tick else None
            exec_entry = (en_ask if direction == "BUY" else en_bid) if first_tick else None
            entry_spread = (en_ask - en_bid) if first_tick else None

            if first_tick:
                if en_bid > en_ask:
                    stats["entry_inverted_quote_ticks"] += 1
                if direction == "BUY":
                    if abs(recorded_entry - en_ask) < 1e-6:
                        stats["entry_exact_ask_for_buy"] += 1
                    if abs(recorded_entry - en_bid) < 1e-6 and abs(recorded_entry - en_ask) > 1e-6:
                        stats["entry_was_bid0_not_ask0_for_buy"] += 1
                else:
                    if abs(recorded_entry - en_bid) < 1e-6:
                        stats["entry_exact_bid_for_sell"] += 1

            # Check actual triggering tick for SL/TARGET/TIME_SQUEEZE/BREAKEVEN inside exit_min
            trigger_tick = None
            if ex_rows:
                if exit_reason in ("SL", "TRAILING_SL", "BREAKEVEN", "TIME_SQUEEZE"):
                    for r in ex_rows:
                        q = float(r.bid) if direction == "BUY" else float(r.ask)
                        if (direction == "BUY" and q <= recorded_exit + 1e-9) or (
                            direction == "SELL" and q >= recorded_exit - 1e-9
                        ):
                            trigger_tick = r
                            break
                elif exit_reason == "TARGET":
                    for r in ex_rows:
                        q = float(r.bid) if direction == "BUY" else float(r.ask)
                        if (direction == "BUY" and q >= recorded_exit - 1e-9) or (
                            direction == "SELL" and q <= recorded_exit + 1e-9
                        ):
                            trigger_tick = r
                            break
                if trigger_tick is None:
                    trigger_tick = ex_rows[-1]

            ex_ts = trigger_tick.time if trigger_tick else None
            ex_bid = float(trigger_tick.bid) if trigger_tick else None
            ex_ask = float(trigger_tick.ask) if trigger_tick else None
            actual_mkt_exit = (ex_bid if direction == "BUY" else ex_ask) if trigger_tick else recorded_exit

            # Compute gap slippage on protective stop exits (market quote past stop price)
            sl_gap_usd = 0.0
            if trigger_tick and exit_reason in ("SL", "TRAILING_SL", "BREAKEVEN", "TIME_SQUEEZE"):
                if direction == "BUY":
                    sl_gap_usd = (actual_mkt_exit - recorded_exit) * qty * CONTRACT_SIZE
                else:
                    sl_gap_usd = (recorded_exit - actual_mkt_exit) * qty * CONTRACT_SIZE
                if sl_gap_usd < -1e-4:
                    stats["sl_gap_negative_count"] += 1
                    stats["total_sl_gap_extra_loss_usd"] += sl_gap_usd

            # Recalculate true executable P&L (Buy at ask0 -> close at market exit bid; Sell at bid0 -> close at market exit ask)
            if exec_entry is not None and trigger_tick is not None:
                mkt_pnl = (
                    (actual_mkt_exit - exec_entry) * qty * CONTRACT_SIZE
                    if direction == "BUY"
                    else (exec_entry - actual_mkt_exit) * qty * CONTRACT_SIZE
                )
            else:
                mkt_pnl = recorded_pnl

            records.append({
                "trade_id": idx + 1,
                "direction": direction,
                "strategy": row["strategy"],
                "signal_5m_bar_time": str(entry_min - timedelta(minutes=5)),
                "signal_candle_close_ts": str(entry_min),
                "first_exec_tick_ts": str(en_ts),
                "entry_bid": en_bid,
                "entry_ask": en_ask,
                "entry_spread_usd": round(entry_spread, 4) if entry_spread is not None else None,
                "recorded_entry_fill": recorded_entry,
                "true_exec_entry_fill": exec_entry,
                "entry_fill_diff": round(recorded_entry - exec_entry, 4) if exec_entry is not None else None,
                "exit_minute_ts": str(exit_min),
                "exit_tick_ts": str(ex_ts),
                "exit_bid": ex_bid,
                "exit_ask": ex_ask,
                "exit_reason": exit_reason,
                "recorded_exit_fill": round(recorded_exit, 4),
                "actual_market_exit_quote": round(actual_mkt_exit, 4),
                "sl_gap_slippage_usd": round(sl_gap_usd, 4),
                "qty": qty,
                "recorded_pnl": round(recorded_pnl, 4),
                "independent_formula_pnl": round(calc_pnl, 4),
                "reconstructed_market_pnl": round(mkt_pnl, 4),
                "pnl_diff_recorded_vs_market": round(recorded_pnl - mkt_pnl, 4),
            })

    pd.DataFrame(records).to_csv(OUT_DIR / "frozen_110_tick_reconciliation.csv", index=False)
    stats["total_sl_gap_extra_loss_usd"] = round(stats["total_sl_gap_extra_loss_usd"], 2)
    stats["total_recorded_pnl_110"] = round(sum(r["recorded_pnl"] for r in records), 2)
    stats["total_market_reconstructed_pnl_110"] = round(sum(r["reconstructed_market_pnl"] for r in records), 2)
    stats["total_diff_recorded_minus_market_usd"] = round(
        stats["total_recorded_pnl_110"] - stats["total_market_reconstructed_pnl_110"], 2
    )
    return stats


def _audit_lookahead_and_capture_signals(engine, start: datetime, end: datetime) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Phase 3: Independently replay the 1m candle & 5m signal pipeline to capture:
      - pre-minute causal Kalman state (before ticks in minute_ts)
      - post-minute look-ahead Kalman state (after ticks in minute_ts, as RunBacktestUseCase does)
      - exact signal score differences and flipped approvals!
    """
    warmup_start = start - timedelta(days=4)
    tick_repo = TickRepository(engine)
    warmup_df = tick_repo.get_ticks_as_df("XAUUSD", warmup_start, start)

    kalman = VolumeAdaptiveKalmanFilter()
    agg_5m = RollingBarAggregator(5, maxlen=200)
    agg_1h = RollingBarAggregator(60, maxlen=100)

    if not warmup_df.empty:
        warmup_df["minute"] = warmup_df["time"].dt.floor("min")
        for minute_ts, grp in warmup_df.groupby("minute", sort=True):
            bids = grp["bid"].values
            vols = np.ones(len(grp))
            for b, v in zip(bids, vols):
                kalman.update_tick(float(b), float(v))
            w_c = {
                "time": minute_ts,
                "open": float(bids[0]),
                "high": float(bids.max()),
                "low": float(bids.min()),
                "close": float(bids[-1]),
                "volume": int(vols.sum()),
            }
            agg_5m.update(w_c)
            agg_1h.update(w_c)
    del warmup_df

    scorer = TradeScorer(w_ml=0.0, w_quant=1.0)
    detector_raw = RegimeDetectorService()

    audit_rows = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=7), end)
        ticks_df = tick_repo.get_ticks_as_df("XAUUSD", chunk_start, chunk_end)
        chunk_start = chunk_end
        if ticks_df.empty:
            continue
        ticks_df["minute"] = ticks_df["time"].dt.floor("min")
        for minute_ts, grp in ticks_df.groupby("minute", sort=True):
            bids = grp["bid"].values.astype(np.float64)
            asks = grp["ask"].values.astype(np.float64)
            vols = np.ones(len(grp), dtype=np.float64)

            # CAUSAL snapshot strictly BEFORE minute_ts ticks begin
            pre_vel = kalman.get_velocity()
            pre_accel = kalman.get_acceleration()

            candle = {
                "time": minute_ts,
                "open": float(bids[0]),
                "high": float(bids.max()),
                "low": float(bids.min()),
                "close": float(bids[-1]),
                "volume": int(vols.sum()),
            }
            closed_5m = agg_5m.update(candle)
            agg_1h.update(candle)

            # Advance Kalman across ticks of minute_ts (post-minute snapshot)
            for b, v in zip(bids, vols):
                kalman.update_tick(float(b), float(v))
            post_vel = kalman.get_velocity()
            post_accel = kalman.get_acceleration()

            if not closed_5m or len(agg_5m.bars) < 60:
                continue

            htf_trend = compute_htf_ema_trend(agg_1h.closed_closes())
            df_bars = pd.DataFrame(list(agg_5m.bars))
            df_ind = IndicatorService.compute_indicators(df_bars)
            if df_ind.empty:
                continue

            regime = detector_raw.detect(df_ind)
            signals = SignalGeneratorService.generate_signals(df_ind, "XAUUSD", regime)
            if not signals:
                continue

            last_row = df_ind.iloc[-1]
            close_s = df_ind["close"]
            ent = DenoiseService.compute_rolling_entropy(close_s.pct_change().dropna()) if len(close_s) > 30 else 2.0
            ker = DenoiseService.compute_kaufman_er(close_s) if len(close_s) > 15 else 0.3

            base_feat = {
                "shannon_entropy": ent,
                "kaufman_er": ker,
                "ema20_slope": float(last_row.get("ema20_slope") or 0.0),
                "vwap_dist": float(last_row.get("vwap_dist") or 0.0),
                "volume_ratio": float(last_row.get("volume_ratio") or 1.0),
                "rsi": float(last_row.get("rsi") or 50.0),
                "utc_hour": minute_ts.hour,
            }

            regime_str = regime.value if hasattr(regime, "value") else str(regime)
            for sig in signals:
                d_val = sig.direction.value if hasattr(sig.direction, "value") else str(sig.direction)
                sc_pre = scorer.compute_quant_score(
                    sig, regime_str, {**base_feat, "kalman_vel": pre_vel, "kalman_accel": pre_accel}
                )
                sc_post = scorer.compute_quant_score(
                    sig, regime_str, {**base_feat, "kalman_vel": post_vel, "kalman_accel": post_accel}
                )
                audit_rows.append({
                    "rollover_utc": str(minute_ts),
                    "closed_5m_ts": str(agg_5m.bars[-1]["time"]),
                    "first_tick_ts": str(grp["time"].iloc[0]),
                    "last_tick_ts": str(grp["time"].iloc[-1]),
                    "strategy": sig.strategy,
                    "direction": d_val,
                    "regime": regime_str,
                    "htf_trend": htf_trend,
                    "pre_kalman_vel": round(pre_vel, 6),
                    "post_kalman_vel": round(post_vel, 6),
                    "score_causal_pre": round(sc_pre, 4),
                    "score_lookahead_post": round(sc_post, 4),
                    "score_diff_post_minus_pre": round(sc_post - sc_pre, 4),
                    "pass_pre_060": sc_pre >= 0.60,
                    "pass_post_060": sc_post >= 0.60,
                    "bid0": float(bids[0]),
                    "ask0": float(asks[0]),
                    "atr": round(float(last_row.get("atr") or 1.0), 4),
                })

    pd.DataFrame(audit_rows).to_csv(OUT_DIR / "lookahead_kalman_audit.csv", index=False)
    diffs = [r["score_diff_post_minus_pre"] for r in audit_rows]
    flips_up = sum(1 for r in audit_rows if (not r["pass_pre_060"]) and r["pass_post_060"])
    flips_dn = sum(1 for r in audit_rows if r["pass_pre_060"] and (not r["pass_post_060"]))
    summary = {
        "total_raw_signals": len(audit_rows),
        "flips_fail_to_pass_060": flips_up,
        "flips_pass_to_fail_060": flips_dn,
        "mean_score_diff_post_minus_pre": round(float(np.mean(diffs)) if diffs else 0.0, 5),
        "max_abs_score_diff": round(float(np.max(np.abs(diffs))) if diffs else 0.0, 4),
    }
    return audit_rows, summary


class IndependentExecutionSimulator:
    """
    Phase 6: Completely independent simulator written from scratch (does NOT reuse
    _OpenPosition, sizing_service, or VirtualBrokerAdapter).
    Executes against TimescaleDB `market_ticks` in two modes:
      - parity_exact: mirrors engine rules (bids[0]/asks[0] entry, exact SL/TP limit fills, skip entry minute exits)
      - strict_causal: enforces causal entry-minute tick checks after fill, real market gapped fills on SL,
                       and skips inverted quotes (bid >= ask).
    """

    def __init__(self, engine, mode: str = "parity_exact"):
        self.engine = engine
        self.mode = mode  # "parity_exact" or "strict_causal"
        self.tick_repo = TickRepository(engine)

    def _independent_size(self, equity: float, entry_px: float, atr: float, direction: str) -> Tuple[Optional[float], float, float]:
        sl_dist = atr * 1.5
        tp_dist = atr * 3.0
        if sl_dist <= 0 or equity <= 0 or entry_px <= 0:
            return None, 0.0, 0.0
        sl_px = (entry_px - sl_dist) if direction == "BUY" else (entry_px + sl_dist)
        tp_px = (entry_px + tp_dist) if direction == "BUY" else (entry_px - tp_dist)
        budget = equity * 0.01  # 1.0% risk
        loss_per_lot = (abs(entry_px - sl_px) / TICK_SIZE) * TICK_VALUE
        if loss_per_lot * 0.01 > budget + 1e-6:
            return None, 0.0, 0.0
        lots = round(math.floor((budget / loss_per_lot + 1e-12) / 0.01) * 0.01, 2)
        while lots >= 0.01 and (loss_per_lot * lots) > budget + 1e-6:
            lots = round(lots - 0.01, 2)
        if lots < 0.01:
            return None, 0.0, 0.0
        lots = min(50.0, lots)
        return lots, sl_px, tp_px

    def _ratchet_sl(self, pos: dict, candidate_px: float, eff_max_hold: int) -> None:
        is_bull = pos["direction"] == "BUY"
        if (is_bull and candidate_px > pos["peak"]) or (not is_bull and candidate_px < pos["peak"]):
            pos["peak"] = candidate_px
        gain = (pos["peak"] - pos["entry"]) if is_bull else (pos["entry"] - pos["peak"])
        atr = pos["atr"]

        if gain >= atr * 2.5:
            pos["trail_active"] = True
            t_sl = (pos["peak"] - atr * 0.75) if is_bull else (pos["peak"] + atr * 0.75)
            if (is_bull and t_sl > pos["sl"]) or (not is_bull and t_sl < pos["sl"]):
                pos["sl"] = t_sl
                pos["flag"] = "TRAILING_SL"
        elif gain >= atr * 1.6:
            pos["trail_active"] = True
            t_sl = (pos["peak"] - atr * 1.10) if is_bull else (pos["peak"] + atr * 1.10)
            if (is_bull and t_sl > pos["sl"]) or (not is_bull and t_sl < pos["sl"]):
                pos["sl"] = t_sl
                pos["flag"] = "TRAILING_SL"
        elif gain >= atr * 1.0:
            c_sl = (pos["entry"] + 2.0 * PIP_UNIT) if is_bull else (pos["entry"] - 2.0 * PIP_UNIT)
            if (is_bull and c_sl > pos["sl"]) or (not is_bull and c_sl < pos["sl"]):
                pos["sl"] = c_sl
                pos["flag"] = "BREAKEVEN"

        if eff_max_hold > 0:
            hp = pos["bars_held"] / eff_max_hold
            if hp >= 0.75:
                prog = min((hp - 0.75) / 0.25, 1.0)
                if is_bull:
                    tsl = pos["sl"] + prog * (candidate_px - pos["sl"])
                    if tsl > pos["sl"]:
                        pos["sl"] = tsl
                        pos["flag"] = "TIME_SQUEEZE"
                else:
                    tsl = pos["sl"] - prog * (pos["sl"] - candidate_px)
                    if tsl < pos["sl"]:
                        pos["sl"] = tsl
                        pos["flag"] = "TIME_SQUEEZE"

    def run(self, start: datetime, end: datetime, frozen_signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Replays ticks cleanly and executes frozen_signals using independent execution rules.
        frozen_signals maps signal minute_ts -> signal dict (from original engine).
        """
        sig_by_min = {_utc_min(s["entry_time"]): s for s in frozen_signals}
        equity = 10000.0
        pos: Optional[dict] = None
        closed: List[Dict[str, Any]] = []

        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=7), end)
            df = self.tick_repo.get_ticks_as_df("XAUUSD", chunk_start, chunk_end)
            chunk_start = chunk_end
            if df.empty:
                continue
            df["minute"] = df["time"].dt.floor("min")
            for minute_ts, grp in df.groupby("minute", sort=True):
                bids = grp["bid"].values.astype(np.float64)
                asks = grp["ask"].values.astype(np.float64)
                times = grp["time"].values

                def _scan_exit(start_i: int) -> Optional[Tuple[str, float, Any]]:
                    nonlocal pos
                    is_bull = pos["direction"] == "BUY"
                    eff_max = 60 if (7 <= pos["entry_hour"] < 20) else 90
                    for k in range(start_i, len(bids)):
                        if self.mode == "strict_causal" and bids[k] >= asks[k]:
                            continue
                        q = float(bids[k]) if is_bull else float(asks[k])
                        if (is_bull and q <= pos["sl"]) or (not is_bull and q >= pos["sl"]):
                            reason = pos["flag"] if pos["flag"] else ("TRAILING_SL" if pos["trail_active"] else "SL")
                            fill = float(q) if self.mode == "strict_causal" else float(pos["sl"])
                            return reason, fill, times[k]
                        if (is_bull and q >= pos["tp"]) or (not is_bull and q <= pos["tp"]):
                            return "TARGET", float(pos["tp"]), times[k]
                        self._ratchet_sl(pos, q, eff_max)
                    if pos["bars_held"] >= eff_max:
                        q_last = float(bids[-1]) if is_bull else float(asks[-1])
                        return "TIMEOUT", q_last, times[-1]
                    bar_pk = float(bids.max()) if is_bull else float(asks.min())
                    self._ratchet_sl(pos, bar_pk, eff_max)
                    return None

                if pos is not None:
                    pos["bars_held"] += 1
                    res = _scan_exit(0)
                    if res is not None:
                        reason, ex_px, ex_ts = res
                        pnl = (
                            (ex_px - pos["entry"]) * pos["lots"] * CONTRACT_SIZE
                            if pos["direction"] == "BUY"
                            else (pos["entry"] - ex_px) * pos["lots"] * CONTRACT_SIZE
                        )
                        equity += pnl
                        closed.append({
                            "entry_time": pos["entry_time"],
                            "entry_tick_ts": pos["entry_tick_ts"],
                            "exit_time": str(minute_ts),
                            "exit_tick_ts": str(ex_ts),
                            "direction": pos["direction"],
                            "strategy": pos["strategy"],
                            "entry_price": pos["entry"],
                            "exit_price": ex_px,
                            "qty": pos["lots"],
                            "pnl": pnl,
                            "exit_reason": reason,
                        })
                        pos = None
                        continue
                    continue

                m_key = _utc_min(minute_ts)
                if m_key in sig_by_min:
                    sig = sig_by_min[m_key]
                    direction = str(sig["direction"]).upper()
                    # Choose entry quote
                    en_i = 0
                    if self.mode == "strict_causal":
                        while en_i < len(bids) and bids[en_i] >= asks[en_i]:
                            en_i += 1
                        if en_i >= len(bids):
                            en_i = 0
                    entry_px = float(asks[en_i]) if direction == "BUY" else float(bids[en_i])
                    atr = abs(float(sig["entry_price"]) - float(sig["sl"])) / 1.5
                    lots, sl_px, tp_px = self._independent_size(equity, entry_px, atr, direction)
                    if lots is None:
                        continue
                    pos = {
                        "direction": direction,
                        "strategy": sig["strategy"],
                        "entry_time": str(minute_ts),
                        "entry_tick_ts": str(times[en_i]),
                        "entry_hour": minute_ts.hour,
                        "entry": entry_px,
                        "sl": sl_px,
                        "tp": tp_px,
                        "atr": atr,
                        "lots": lots,
                        "peak": entry_px,
                        "bars_held": 0,
                        "trail_active": False,
                        "flag": "",
                    }
                    # In strict_causal mode, check remaining ticks AFTER entry tick in same minute!
                    if self.mode == "strict_causal" and en_i + 1 < len(bids):
                        res = _scan_exit(en_i + 1)
                        if res is not None:
                            reason, ex_px, ex_ts = res
                            pnl = (
                                (ex_px - pos["entry"]) * pos["lots"] * CONTRACT_SIZE
                                if pos["direction"] == "BUY"
                                else (pos["entry"] - ex_px) * pos["lots"] * CONTRACT_SIZE
                            )
                            equity += pnl
                            closed.append({
                                "entry_time": pos["entry_time"],
                                "entry_tick_ts": pos["entry_tick_ts"],
                                "exit_time": str(minute_ts),
                                "exit_tick_ts": str(ex_ts),
                                "direction": pos["direction"],
                                "strategy": pos["strategy"],
                                "entry_price": pos["entry"],
                                "exit_price": ex_px,
                                "qty": pos["lots"],
                                "pnl": pnl,
                                "exit_reason": reason,
                            })
                            pos = None

        return closed


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    engine = create_engine(RAW_DB_URL)

    # 1. Reconstruct frozen 111 baseline trades (110 inside DB retention)
    print("[1/4] Reconciling frozen_111_baseline_trades.csv against raw DB ticks...")
    frozen_stats = _reconstruct_frozen_baseline_from_ticks(engine)

    # 2. Run untouched RunBacktestUseCase over available DB window (2026-08-24 .. 2026-11-16, with 4d warmup 08-20..08-24)
    start = datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 16, 20, 1, 23, tzinfo=timezone.utc)
    print(f"[2/4] Running untouched RunBacktestUseCase ({start.date()} -> {end.date()})...")
    tick_repo = TickRepository(engine)
    uc = RunBacktestUseCase(
        tick_repo=tick_repo,
        virtual_broker=VirtualBrokerAdapter(initial_equity=10000.0),
        manage_position=None,
        sizing_service=PositionSizingService(contract_size=100.0),
        profile=MEDIUM_RISK_BACKTEST,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        orig_res = uc.execute("XAUUSD", start, end, initial_capital=10000.0, use_kalman_exit=False)
    (OUT_DIR / "engine_replay.log").write_text(buf.getvalue(), encoding="utf-8")

    orig_trades = orig_res["trade_list"]
    pd.DataFrame(orig_trades).to_csv(OUT_DIR / "current_engine_trades.csv", index=False)

    # 3. Look-ahead bias audit on Kalman features at 5m signal rollovers
    print("[3/4] Running Phase 3 Kalman look-ahead bias audit...")
    _, lookahead_summary = _audit_lookahead_and_capture_signals(engine, start, end)

    # 4. Run Independent Simulator (Parity mode & Strict Causal mode) on exact same signals
    print("[4/4] Running Phase 6 Independent Simulator (Parity + Strict Causal)...")
    # Pass original SL into signal dicts so ATR exact reconstruction works
    signals_for_sim = [
        {"entry_time": t["entry_time"], "direction": t["direction"], "strategy": t["strategy"], "entry_price": t["entry_price"], "sl": t["sl"]}
        for t in orig_trades
    ]
    sim_parity = IndependentExecutionSimulator(engine, mode="parity_exact").run(start, end, signals_for_sim)
    sim_strict = IndependentExecutionSimulator(engine, mode="strict_causal").run(start, end, signals_for_sim)

    pd.DataFrame(sim_parity).to_csv(OUT_DIR / "independent_parity_trades.csv", index=False)
    pd.DataFrame(sim_strict).to_csv(OUT_DIR / "independent_strict_trades.csv", index=False)

    # Compare per-trade parity vs original
    parity_max_entry_diff = 0.0
    parity_max_exit_diff = 0.0
    parity_max_pnl_diff = 0.0
    parity_qty_mismatches = 0
    for o, p_t in zip(orig_trades, sim_parity):
        parity_max_entry_diff = max(parity_max_entry_diff, abs(float(o["entry_price"]) - float(p_t["entry_price"])))
        parity_max_exit_diff = max(parity_max_exit_diff, abs(float(o["exit_price"]) - float(p_t["exit_price"])))
        parity_max_pnl_diff = max(parity_max_pnl_diff, abs(float(o["pnl"]) - float(p_t["pnl"])))
        if abs(float(o["qty"]) - float(p_t["qty"])) > 1e-6:
            parity_qty_mismatches += 1

    strict_pnl_diffs = []
    for o, s_t in zip(orig_trades, sim_strict):
        strict_pnl_diffs.append(float(s_t["pnl"]) - float(o["pnl"]))

    # Also compute realistic cost stress ($7/lot round-turn Fusion-style raw commission) on strict causal trades
    sim_strict_comm = [
        {**t, "pnl": float(t["pnl"]) - 7.0 * float(t["qty"])}
        for t in sim_strict
    ]
    total_lots_strict = sum(float(t["qty"]) for t in sim_strict)

    summary = {
        "window_utc": {"start": _iso(start), "end": _iso(end)},
        "frozen_110_baseline_reconciliation": frozen_stats,
        "lookahead_kalman_audit": lookahead_summary,
        "original_engine_reported": {
            "trades": orig_res["trades"],
            "win_rate_pct": round(orig_res["win_rate"] * 100.0, 2),
            "pnl": round(orig_res["pnl"], 2),
            "closed_max_dd": round(orig_res["closed_max_dd"], 2),
            "closed_max_dd_pct": round(orig_res["closed_max_dd_pct"], 2),
            "mtm_max_dd": round(orig_res["mtm_max_dd"], 2),
            "mtm_max_dd_pct": round(orig_res["mtm_max_dd_pct"], 2),
            "pf": round(orig_res["pf"], 4),
            "avg_win": round(orig_res["avg_win"], 2),
            "avg_loss": round(orig_res["avg_loss"], 2),
            "rr": round(orig_res["rr"], 4),
            "sl_slippage_events": orig_res["sl_slippage_events"],
            "worst_sl_gap_usd": round(orig_res["worst_sl_gap_usd"], 2),
        },
        "independent_metrics_on_original_trades": _compute_metrics(orig_trades),
        "independent_parity_simulator_metrics": _compute_metrics(sim_parity),
        "parity_trade_comparison": {
            "count_orig": len(orig_trades),
            "count_parity": len(sim_parity),
            "max_entry_price_diff": round(parity_max_entry_diff, 6),
            "max_exit_price_diff": round(parity_max_exit_diff, 6),
            "max_pnl_diff_usd": round(parity_max_pnl_diff, 6),
            "qty_mismatches": parity_qty_mismatches,
        },
        "independent_strict_causal_metrics": _compute_metrics(sim_strict),
        "strict_causal_vs_original_delta": {
            "count_orig": len(orig_trades),
            "count_strict": len(sim_strict),
            "net_pnl_delta_usd": round(_compute_metrics(sim_strict)["net_profit"] - round(orig_res["pnl"], 2), 2),
            "max_single_trade_pnl_delta_usd": round(max(strict_pnl_diffs, key=abs) if strict_pnl_diffs else 0.0, 2),
        },
        "commission_stress_7usd_per_lot": {
            "total_lots": round(total_lots_strict, 2),
            "total_commission_usd": round(7.0 * total_lots_strict, 2),
            "metrics": _compute_metrics(sim_strict_comm),
        },
    }

    (OUT_DIR / "phase5_6_7_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
