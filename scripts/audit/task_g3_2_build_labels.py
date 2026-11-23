"""
TASK G3.2 — Phases B, C, D, F: Authoritative Macro (15m) & Micro (30-tick) Label Builder.

Uses the verified application domain services:
  - `label_spec_service.py` (DEFAULT_COST_SPEC: Fusion Markets $12/$1M/side + 0.0533p/side slippage)
  - `broker_cost_service.py` (FUSION_CTRADER_DEMO_48439552 + FUSION_XAUUSD_SWAP_SPEC)
  - `ml_feature_contract.py` (strictly excludes unavailable order-flow/L2/volume features)
  - `IndicatorService` & `RegimeDetectorService` (causal 5m context for regime stratification)

Outputs under backtest_results/audit/task_g3_2_label_design/:
  - g3_2_macro_labels.parquet
  - g3_2_micro_labels.parquet
  - g3_2_label_schema.json
  - g3_2_barrier_order_validation.json
  - g3_2_label_distribution.json
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")

from src.goldedge.domain.services.broker_cost_service import (
    BrokerCostService,
    FUSION_CTRADER_DEMO_48439552,
    FUSION_XAUUSD_SWAP_SPEC,
)
from src.goldedge.domain.services.indicator_service import IndicatorService
from src.goldedge.domain.services.label_spec_service import (
    DEFAULT_COST_SPEC,
    LABEL_SPEC_VERSION,
    classify_macro_endpoint,
    classify_micro_30tick,
    evaluate_tick_barrier_path,
)
from src.goldedge.domain.services.ml_feature_contract import (
    ALLOWED_MACRO_ML_FEATURES,
    ALLOWED_MICRO_ML_FEATURES,
    EXCLUDED_UNAVAILABLE_ML_FEATURES,
)
from src.goldedge.domain.services.mtf_trend import aggregate_ohlcv_bars
from src.goldedge.domain.services.regime_detector import RegimeDetectorService

OUT_DIR = Path("backtest_results/audit/task_g3_2_label_design")
DB_URL = os.environ.get(
    "DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge"
).replace("+psycopg2", "")


def _session_of_hour(h: int) -> str:
    if 0 <= h < 7:
        return "ASIA_00_07"
    if 7 <= h < 12:
        return "LONDON_07_12"
    if 12 <= h < 17:
        return "OVERLAP_12_17"
    if 17 <= h < 20:
        return "NY_17_20"
    return "OFF_20_24"


def _day_cluster_stats(values: np.ndarray, day_ids: np.ndarray) -> Dict[str, Any]:
    v = np.asarray(values, dtype=np.float64)
    d = np.asarray(day_ids)
    m = np.isfinite(v)
    v, d = v[m], d[m]
    n = len(v)
    if n < 20:
        return {"n_obs": n, "n_days": 0}
    uniq, inv = np.unique(d, return_inverse=True)
    g = len(uniq)
    if g < 3:
        return {"n_obs": n, "n_days": g}
    cnts = np.bincount(inv).astype(np.float64)
    means = np.bincount(inv, weights=v) / np.maximum(1.0, cnts)
    se_block = float(np.std(means, ddof=1) / math.sqrt(g))
    se_iid = float(np.std(v, ddof=1) / math.sqrt(n))
    vif = max(1.0, (se_block / max(1e-12, se_iid)) ** 2)
    return {
        "n_obs": int(n),
        "n_days": int(g),
        "mean": round(float(np.mean(v)), 5),
        "day_block_se": round(se_block, 6),
        "day_block_95pct_ci_halfwidth": round(1.96 * se_block, 6),
        "day_block_vif": round(vif, 3),
        "n_eff_day_block": round(n / vif, 1),
    }


def _simulate_h3b_isolated_trade(
    direction: str,
    entry_ts_s: float,
    entry_bid: float,
    entry_ask: float,
    atr: float,
    tick_t: np.ndarray,
    tick_b: np.ndarray,
    tick_a: np.ndarray,
    cost_svc: BrokerCostService,
) -> Dict[str, Any]:
    """
    Phase B3: Isolated chronological tick-by-tick trade simulation under frozen H3b
    risk & exit mechanics (SL=1.5*ATR, TP=3.0*ATR, BE at 1.0*ATR +$0.20, Trail T1 1.6*ATR/1.10*ATR,
    Trail T2 2.5*ATR/0.75*ATR, E1 early-fail 15m if MFE<0.4*ATR, timeout 60m London/NY or 90m off-hours,
    verified $12/$1M/side commission + 0.0533p/side slippage + swap if crossing 17:00 NY).
    """
    if atr <= 1e-6 or len(tick_t) == 0:
        return {
            "exit_reason": "NO_DATA",
            "net_usd_per_oz": np.nan,
            "r_multiple": np.nan,
            "hold_sec": np.nan,
            "exit_ts_s": np.nan,
        }

    is_buy = direction == "BUY"
    slip = DEFAULT_COST_SPEC.slippage_usd_per_oz_per_side
    entry_px = (entry_ask + slip) if is_buy else (entry_bid - slip)
    sl_dist = 1.5 * atr
    tp_dist = 3.0 * atr
    cur_sl = (entry_px - sl_dist) if is_buy else (entry_px + sl_dist)
    tp_px = (entry_px + tp_dist) if is_buy else (entry_px - tp_dist)

    entry_hour = int((int(entry_ts_s) // 3600) % 24)
    max_hold_s = 3600.0 if (7 <= entry_hour < 20) else 5400.0

    mfe = 0.0
    exit_reason = "TIMEOUT"
    exit_px = float(tick_b[-1] - slip) if is_buy else float(tick_a[-1] + slip)
    exit_ts = float(tick_t[-1])

    for idx in range(len(tick_t)):
        t = float(tick_t[idx])
        elapsed = t - entry_ts_s
        if elapsed > max_hold_s:
            # Close at last tick <= max_hold_s
            break
        px = float(tick_b[idx]) if is_buy else float(tick_a[idx])
        fav = (px - entry_px) if is_buy else (entry_px - px)
        if fav > mfe:
            mfe = fav

        # Trailing / breakeven updates (strictly causal)
        if mfe >= 2.5 * atr:
            tr_sl = (px - 0.75 * atr) if is_buy else (px + 0.75 * atr)
            cur_sl = max(cur_sl, tr_sl) if is_buy else min(cur_sl, tr_sl)
        elif mfe >= 1.6 * atr:
            tr_sl = (px - 1.10 * atr) if is_buy else (px + 1.10 * atr)
            cur_sl = max(cur_sl, tr_sl) if is_buy else min(cur_sl, tr_sl)
        elif mfe >= 1.0 * atr:
            be_sl = (entry_px + 0.20) if is_buy else (entry_px - 0.20)
            cur_sl = max(cur_sl, be_sl) if is_buy else min(cur_sl, be_sl)

        # Conservative SL-first check
        hit_sl = (px <= cur_sl) if is_buy else (px >= cur_sl)
        if hit_sl:
            exit_px = (px - slip) if is_buy else (px + slip)
            exit_ts = t
            exit_reason = "STOP_OR_TRAIL"
            break

        hit_tp = (px >= tp_px) if is_buy else (px <= tp_px)
        if hit_tp:
            exit_px = (px - slip) if is_buy else (px + slip)
            exit_ts = t
            exit_reason = "TARGET"
            break

        # E1 early-failure exit at 15m if MFE < 0.4 ATR
        if elapsed >= 900.0 and mfe < 0.4 * atr:
            exit_px = (px - slip) if is_buy else (px + slip)
            exit_ts = t
            exit_reason = "E1_EARLY_FAIL"
            break

        exit_px = (px - slip) if is_buy else (px + slip)
        exit_ts = t

    raw_pnl_oz = (exit_px - entry_px) if is_buy else (entry_px - exit_px)
    comm_oz = DEFAULT_COST_SPEC.round_trip_commission_usd_per_oz(entry_px, exit_px)

    # Check if trade crossed 17:00 America/New_York rollover for swap
    swap_oz = 0.0
    dt_in = datetime.fromtimestamp(entry_ts_s, tz=timezone.utc)
    dt_out = datetime.fromtimestamp(exit_ts, tz=timezone.utc)
    rollovers = cost_svc.rollover_events_between(dt_in, dt_out)
    for _r_utc, l_date, mult in rollovers:
        # calculate_swap_pnl_usd for 1.0 lot / 100 oz -> per oz
        swap_oz += cost_svc.calculate_swap_pnl_usd(direction, 1.0, l_date, mult) / 100.0

    net_oz = raw_pnl_oz - comm_oz + swap_oz
    r_mult = net_oz / sl_dist if sl_dist > 1e-6 else np.nan
    return {
        "exit_reason": exit_reason,
        "net_usd_per_oz": round(net_oz, 5),
        "r_multiple": round(r_mult, 4),
        "hold_sec": round(exit_ts - entry_ts_s, 3),
        "exit_ts_s": exit_ts,
    }


def build_labels() -> None:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    a1_path = OUT_DIR / "g3_2_candle_source_reconciliation.json"
    dataset_fp = json.loads(a1_path.read_text(encoding="utf-8"))["dataset_fingerprint_sha256"] if a1_path.exists() else "UNKNOWN"

    cost_svc = BrokerCostService(FUSION_CTRADER_DEMO_48439552, FUSION_XAUUSD_SWAP_SPEC)
    conn = psycopg2.connect(DB_URL)
    conn.set_session(readonly=True, autocommit=True)

    # ------------------------------------------------------------------
    # STEP 1: Load authoritative 96,862 ML-eligible 1m bars -> 5m bars -> causal indicators & regimes
    # ------------------------------------------------------------------
    print("[G3.2] Loading 96,862 authoritative ML-eligible 1m bars...", flush=True)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT time, open, high, low, close, tick_count, real_volume, spread_points
            FROM market_bars_1m_quote_semantics_v1
            WHERE symbol = 'XAUUSD'
            ORDER BY time ASC
            """
        )
        b1m_rows = cur.fetchall()
    bars_1m = [
        {
            "time": r[0],
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "tick_volume": int(r[5]),
            "real_volume": r[6],
            "spread": int(r[7]),
        }
        for r in b1m_rows
    ]
    bars_5m = aggregate_ohlcv_bars(bars_1m, 5)
    df_5m = IndicatorService.compute_indicators(bars_5m)
    print(f"[G3.2] Built {len(bars_5m):,} 5m bars and causal indicators.", flush=True)

    # Causal regime detection bar-by-bar (strictly no future look-ahead!)
    reg_detector = RegimeDetectorService()
    regimes_5m: List[str] = []
    for i in range(len(df_5m)):
        if i < 50:
            regimes_5m.append("WARMUP")
        else:
            regimes_5m.append(reg_detector.detect(df_5m.iloc[: i + 1]).value)
    df_5m["regime"] = regimes_5m

    # Map 5m decision timestamp (= bar_start + 300s = close of 5m bar) -> metadata
    dec_meta_by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for i in range(len(df_5m)):
        bar_start: datetime = bars_5m[i]["time"]
        dec_dt = bar_start + timedelta(seconds=300)
        day_key = dec_dt.strftime("%Y-%m-%d")
        atr_val = float(df_5m["atr"].iloc[i]) if pd.notna(df_5m["atr"].iloc[i]) else 0.0
        dec_meta_by_day[day_key].append(
            {
                "bar_idx": i,
                "bar_start_s": int(bar_start.timestamp()),
                "decision_ts_s": int(dec_dt.timestamp()),
                "atr": atr_val,
                "regime": regimes_5m[i],
                "is_warmup": i < 100,
                "is_nonoverlap_15m": (int(dec_dt.timestamp()) % 900) == 0,
            }
        )

    # ------------------------------------------------------------------
    # STEP 2: Stream ML-eligible ticks day-by-day (with +95m next-day lookahead buffer
    #         strictly for future LABEL observation only — never for features!)
    # ------------------------------------------------------------------
    print("[G3.2] Streaming ML-eligible ticks day-by-day to build Macro & Micro labels...", flush=True)
    min_dt = bars_1m[0]["time"]
    max_dt = bars_1m[-1]["time"]
    cur_day = datetime(min_dt.year, min_dt.month, min_dt.day, tzinfo=timezone.utc)
    last_day = datetime(max_dt.year, max_dt.month, max_dt.day, tzinfo=timezone.utc)

    macro_rows: List[Dict[str, Any]] = []
    macro_exclusions = defaultdict(int)
    barrier_validation = {
        "total_clean_5m_macro_windows": 0,
        "buy_010_both_touched_tick_resolved": 0,
        "sell_010_both_touched_tick_resolved": 0,
        "buy_010_same_1m_candle_ambiguous_if_ohlc_only": 0,
        "sell_010_same_1m_candle_ambiguous_if_ohlc_only": 0,
        "threshold_sensitivity_counts": {
            "0.05pct": {"buy_tp_first": 0, "buy_sl_first": 0, "buy_both": 0, "sell_tp_first": 0, "sell_sl_first": 0, "sell_both": 0},
            "0.10pct": {"buy_tp_first": 0, "buy_sl_first": 0, "buy_both": 0, "sell_tp_first": 0, "sell_sl_first": 0, "sell_both": 0},
            "0.15pct": {"buy_tp_first": 0, "buy_sl_first": 0, "buy_both": 0, "sell_tp_first": 0, "sell_sl_first": 0, "sell_both": 0},
            "0.20pct": {"buy_tp_first": 0, "buy_sl_first": 0, "buy_both": 0, "sell_tp_first": 0, "sell_sl_first": 0, "sell_both": 0},
        },
    }

    micro_chunks: List[pd.DataFrame] = []
    micro_exclusions = defaultdict(int)

    # C4 adverse selection tracking: micro 30-tick outcome immediately after Macro 5m entry vs 15m/H3b outcome
    c4_records: List[Dict[str, Any]] = []

    with conn.cursor() as cur:
        d = cur_day
        while d <= last_day:
            nxt = d + timedelta(days=1)
            day_key = d.strftime("%Y-%m-%d")
            # Fetch day + 95m buffer into next day for Macro/B3 label completion across midnight
            buf_end = nxt + timedelta(minutes=95)
            cur.execute(
                """
                SELECT EXTRACT(EPOCH FROM time) AS ts_s, bid, ask
                FROM v_market_ticks_quote_semantics_v1
                WHERE symbol = 'XAUUSD'
                  AND is_ml_eligible_quote
                  AND time >= %s AND time < %s
                ORDER BY time ASC
                """,
                (d, buf_end),
            )
            rows = cur.fetchall()
            if len(rows) < 40:
                d = nxt
                continue
            arr = np.array(rows, dtype=np.float64)
            t_s = arr[:, 0]
            b = arr[:, 1]
            a = arr[:, 2]
            mid = 0.5 * (b + a)
            sp = a - b
            day_start_s = d.timestamp()
            day_end_s = nxt.timestamp()

            # ---- MACRO LABELS for all 5m decisions falling in [day_start_s, day_end_s) ----
            for meta in dec_meta_by_day.get(day_key, []):
                dec_s = float(meta["decision_ts_s"])
                if meta["is_warmup"]:
                    macro_exclusions["WARMUP_LT_100_5M_BARS"] += 1
                    continue

                # Earliest executable entry tick strictly >= decision_ts_s within 60s
                i_entry = int(np.searchsorted(t_s, dec_s, side="left"))
                if i_entry >= len(t_s) or (t_s[i_entry] - dec_s) > 60.0:
                    macro_exclusions["NO_EXECUTABLE_ENTRY_TICK_WITHIN_60S"] += 1
                    continue

                # Horizon end: last valid tick <= dec_s + 900s (15m)
                hor_s = dec_s + 900.0
                i_end = int(np.searchsorted(t_s, hor_s, side="right")) - 1
                if i_end <= i_entry or (hor_s - t_s[i_end]) > 60.0:
                    macro_exclusions["INCOMPLETE_15M_HORIZON_GAP_OR_END"] += 1
                    continue

                # Check gap >= 300s inside [i_entry .. i_end]
                seg_t = t_s[i_entry : i_end + 1]
                if len(seg_t) < 2 or float(np.max(np.diff(seg_t))) >= 300.0:
                    macro_exclusions["GAP_GE_5M_INSIDE_15M_HORIZON"] += 1
                    continue

                seg_b = b[i_entry : i_end + 1]
                seg_a = a[i_entry : i_end + 1]
                e_bid, e_ask = float(seg_b[0]), float(seg_a[0])
                x_bid, x_ask = float(seg_b[-1]), float(seg_a[-1])
                e_ts = float(seg_t[0])
                x_ts = float(seg_t[-1])

                # B1: Endpoint labels at 0.05%, 0.10%, 0.15%, 0.20%
                ep10 = classify_macro_endpoint(e_bid, e_ask, x_bid, x_ask, 0.0010, DEFAULT_COST_SPEC)
                ep05 = classify_macro_endpoint(e_bid, e_ask, x_bid, x_ask, 0.0005, DEFAULT_COST_SPEC)
                ep15 = classify_macro_endpoint(e_bid, e_ask, x_bid, x_ask, 0.0015, DEFAULT_COST_SPEC)
                ep20 = classify_macro_endpoint(e_bid, e_ask, x_bid, x_ask, 0.0020, DEFAULT_COST_SPEC)

                # B2: Tick-chronological barrier path evaluation (subsequent ticks strictly after entry tick)
                sub_t = seg_t[1:]
                sub_b = seg_b[1:]
                sub_a = seg_a[1:]
                buy_b10 = evaluate_tick_barrier_path("BUY", e_ask, e_ts, sub_t, sub_b, sub_a, e_ask * 0.0010, e_ask * 0.0010)
                sell_b10 = evaluate_tick_barrier_path("SELL", e_bid, e_ts, sub_t, sub_b, sub_a, e_bid * 0.0010, e_bid * 0.0010)

                barrier_validation["total_clean_5m_macro_windows"] += 1
                if buy_b10["both_reached"]:
                    barrier_validation["buy_010_both_touched_tick_resolved"] += 1
                    if int((e_ts + buy_b10["time_to_tp_s"]) // 60) == int((e_ts + buy_b10["time_to_sl_s"]) // 60):
                        barrier_validation["buy_010_same_1m_candle_ambiguous_if_ohlc_only"] += 1
                if sell_b10["both_reached"]:
                    barrier_validation["sell_010_both_touched_tick_resolved"] += 1
                    if int((e_ts + sell_b10["time_to_tp_s"]) // 60) == int((e_ts + sell_b10["time_to_sl_s"]) // 60):
                        barrier_validation["sell_010_same_1m_candle_ambiguous_if_ohlc_only"] += 1

                # Sensitivity counts across 0.05%, 0.10%, 0.15%, 0.20%
                for lbl_k, thr_d in (("0.05pct", 0.0005), ("0.10pct", 0.0010), ("0.15pct", 0.0015), ("0.20pct", 0.0020)):
                    bb = buy_b10 if lbl_k == "0.10pct" else evaluate_tick_barrier_path("BUY", e_ask, e_ts, sub_t, sub_b, sub_a, e_ask * thr_d, e_ask * thr_d)
                    sb = sell_b10 if lbl_k == "0.10pct" else evaluate_tick_barrier_path("SELL", e_bid, e_ts, sub_t, sub_b, sub_a, e_bid * thr_d, e_bid * thr_d)
                    sc = barrier_validation["threshold_sensitivity_counts"][lbl_k]
                    if bb["first_outcome"] == "TP_FIRST":
                        sc["buy_tp_first"] += 1
                    elif bb["first_outcome"] == "SL_FIRST":
                        sc["buy_sl_first"] += 1
                    if bb["both_reached"]:
                        sc["buy_both"] += 1
                    if sb["first_outcome"] == "TP_FIRST":
                        sc["sell_tp_first"] += 1
                    elif sb["first_outcome"] == "SL_FIRST":
                        sc["sell_sl_first"] += 1
                    if sb["both_reached"]:
                        sc["sell_both"] += 1

                # B3: Isolated H3b tradable-outcome simulation (up to +90m window)
                i_h3b_end = int(np.searchsorted(t_s, e_ts + 5400.0, side="right"))
                h3b_t = t_s[i_entry + 1 : i_h3b_end]
                h3b_b = b[i_entry + 1 : i_h3b_end]
                h3b_a = a[i_entry + 1 : i_h3b_end]
                sim_buy = _simulate_h3b_isolated_trade("BUY", e_ts, e_bid, e_ask, meta["atr"], h3b_t, h3b_b, h3b_a, cost_svc)
                sim_sell = _simulate_h3b_isolated_trade("SELL", e_ts, e_bid, e_ask, meta["atr"], h3b_t, h3b_b, h3b_a, cost_svc)

                # C4: Immediate post-entry 30-tick Micro outcome at this exact Macro entry tick
                micro_30_dir_at_entry = "UNAVAILABLE"
                if i_entry + 30 < len(t_s) and float(np.max(np.diff(t_s[i_entry : i_entry + 31]))) < 60.0:
                    mc = classify_micro_30tick(e_bid, e_ask, float(b[i_entry + 30]), float(a[i_entry + 30]), 0.0, DEFAULT_COST_SPEC)
                    micro_30_dir_at_entry = str(mc["net_class"])
                    if meta["is_nonoverlap_15m"]:
                        c4_records.append(
                            {
                                "micro_net_class": micro_30_dir_at_entry,
                                "buy_ret_net": float(ep10["buy_ret_net"]),
                                "sell_ret_net": float(ep10["sell_ret_net"]),
                                "buy_mae_usd": float(buy_b10["mae_usd"]),
                                "sell_mae_usd": float(sell_b10["mae_usd"]),
                                "h3b_buy_r": float(sim_buy["r_multiple"]) if pd.notna(sim_buy["r_multiple"]) else np.nan,
                                "h3b_sell_r": float(sim_sell["r_multiple"]) if pd.notna(sim_sell["r_multiple"]) else np.nan,
                            }
                        )

                dec_hr = int((int(dec_s) // 3600) % 24)
                macro_rows.append(
                    {
                        "feature_cutoff_utc": int(dec_s),
                        "signal_decision_utc": int(dec_s),
                        "earliest_entry_utc": round(e_ts, 6),
                        "label_obs_start_utc": round(e_ts, 6),
                        "label_obs_end_utc": round(x_ts, 6),
                        "day_utc": day_key,
                        "session_utc": _session_of_hour(dec_hr),
                        "regime": meta["regime"],
                        "atr_5m": round(meta["atr"], 5),
                        "is_nonoverlap_15m": bool(meta["is_nonoverlap_15m"]),
                        "entry_bid": e_bid,
                        "entry_ask": e_ask,
                        "entry_spread_usd": round(e_ask - e_bid, 5),
                        "exit_bid_15m": x_bid,
                        "exit_ask_15m": x_ask,
                        "buy_ret_raw": round(float(ep10["buy_ret_raw"]), 7),
                        "sell_ret_raw": round(float(ep10["sell_ret_raw"]), 7),
                        "buy_ret_net": round(float(ep10["buy_ret_net"]), 7),
                        "sell_ret_net": round(float(ep10["sell_ret_net"]), 7),
                        "raw_class_005": ep05["raw_class"],
                        "net_class_005": ep05["net_class"],
                        "raw_class_010": ep10["raw_class"],
                        "net_class_010": ep10["net_class"],
                        "raw_class_015": ep15["raw_class"],
                        "net_class_015": ep15["net_class"],
                        "raw_class_020": ep20["raw_class"],
                        "net_class_020": ep20["net_class"],
                        "buy_barrier_010": buy_b10["first_outcome"],
                        "buy_both_reached_010": bool(buy_b10["both_reached"]),
                        "buy_first_touch_utc": round(float(buy_b10["first_touch_ts_s"]), 6) if buy_b10["first_touch_ts_s"] is not None else np.nan,
                        "buy_time_to_tp_010_s": round(float(buy_b10["time_to_tp_s"]), 3) if buy_b10["time_to_tp_s"] is not None else np.nan,
                        "buy_time_to_sl_010_s": round(float(buy_b10["time_to_sl_s"]), 3) if buy_b10["time_to_sl_s"] is not None else np.nan,
                        "buy_mfe_usd": round(float(buy_b10["mfe_usd"]), 5),
                        "buy_mae_usd": round(float(buy_b10["mae_usd"]), 5),
                        "sell_barrier_010": sell_b10["first_outcome"],
                        "sell_both_reached_010": bool(sell_b10["both_reached"]),
                        "sell_first_touch_utc": round(float(sell_b10["first_touch_ts_s"]), 6) if sell_b10["first_touch_ts_s"] is not None else np.nan,
                        "sell_time_to_tp_010_s": round(float(sell_b10["time_to_tp_s"]), 3) if sell_b10["time_to_tp_s"] is not None else np.nan,
                        "sell_time_to_sl_010_s": round(float(sell_b10["time_to_sl_s"]), 3) if sell_b10["time_to_sl_s"] is not None else np.nan,
                        "sell_mfe_usd": round(float(sell_b10["mfe_usd"]), 5),
                        "sell_mae_usd": round(float(sell_b10["mae_usd"]), 5),
                        "h3b_sim_buy_exit": sim_buy["exit_reason"],
                        "h3b_sim_buy_net_oz": sim_buy["net_usd_per_oz"],
                        "h3b_sim_buy_r": sim_buy["r_multiple"],
                        "h3b_sim_sell_exit": sim_sell["exit_reason"],
                        "h3b_sim_sell_net_oz": sim_sell["net_usd_per_oz"],
                        "h3b_sim_sell_r": sim_sell["r_multiple"],
                        "post_entry_micro_30t_class": micro_30_dir_at_entry,
                        "dataset_fingerprint": dataset_fp[:16],
                        "label_spec_version": LABEL_SPEC_VERSION,
                    }
                )

            # ---- MICRO LABELS (non-overlapping 30-tick windows strictly inside [day_start_s, day_end_s)) ----
            day_mask = (t_s >= day_start_s) & (t_s < day_end_s)
            n_day = int(np.sum(day_mask))
            if n_day >= 60:
                td = t_s[:n_day]
                bd = b[:n_day]
                ad = a[:n_day]
                md = mid[:n_day]
                spd = sp[:n_day]
                dt_d = np.diff(td)
                seg_id = np.cumsum(np.concatenate(([False], dt_d >= 60.0)))
                valid_30 = seg_id[:-30] == seg_id[30:]
                idx_v = np.where(valid_30)[0]
                micro_exclusions["BROKEN_BY_GAP_GE_60S_OR_DAY_END"] += int(n_day - len(idx_v))
                nonov = idx_v[::30]
                if len(nonov) > 0:
                    eb = bd[nonov]
                    ea = ad[nonov]
                    em = md[nonov]
                    fb = bd[nonov + 30]
                    fa = ad[nonov + 30]
                    fm = md[nonov + 30]
                    t0_u = td[nonov]
                    t1_u = td[nonov + 30]
                    elap = t1_u - t0_u
                    q_tps = 30.0 / np.maximum(0.001, elap)

                    # Short-term trailing vol over preceding 30 ticks (strictly causal!)
                    d_m_all = np.concatenate(([0.0], np.diff(md)))
                    d_m_sq = d_m_all * d_m_all
                    csum_sq = np.concatenate(([0.0], np.cumsum(d_m_sq)))
                    k_start = np.maximum(0, nonov - 30)
                    st_vol = np.sqrt(np.maximum(0.0, csum_sq[nonov + 1] - csum_sq[k_start + 1]))

                    # Vectorized exact cost calculation (matching label_spec_service)
                    slip = DEFAULT_COST_SPEC.slippage_usd_per_oz_per_side
                    c_rate = DEFAULT_COST_SPEC.commission_usd_per_million_per_side / 1_000_000.0
                    buy_raw = fb - ea
                    sell_raw = eb - fa
                    buy_net = (fb - slip) - (ea + slip) - ((ea + slip) + (fb - slip)) * c_rate
                    sell_net = (eb - slip) - (fa + slip) - ((eb - slip) + (fa + slip)) * c_rate

                    raw_cls = np.where(buy_raw > 0.0, "UP", np.where(sell_raw > 0.0, "DOWN", "FLAT"))
                    net_cls = np.where(buy_net > 0.0, "UP", np.where(sell_net > 0.0, "DOWN", "FLAT"))
                    net_cls_010 = np.where(buy_net > 0.10, "UP", np.where(sell_net > 0.10, "DOWN", "FLAT"))

                    # Entry-timing & short-term quote-movement Micro labels (distinct from standalone 8s RT scalp!)
                    entry_sp = ea - eb
                    d_mid = fm - em
                    q_move_1x_sp = np.where(d_mid > entry_sp, "UP", np.where(d_mid < -entry_sp, "DOWN", "FLAT"))
                    q_move_010 = np.where(d_mid > 0.10, "UP", np.where(d_mid < -0.10, "DOWN", "FLAT"))
                    buy_timing_adv = (fa - ea) * (1.0 + c_rate)
                    sell_timing_adv = (eb - fb) * (1.0 - c_rate)
                    buy_timing_cls = np.where(buy_timing_adv > entry_sp, "ENTER_NOW", np.where(buy_timing_adv < -entry_sp, "WAIT_ADVERSE", "NEUTRAL"))
                    sell_timing_cls = np.where(sell_timing_adv > entry_sp, "ENTER_NOW", np.where(sell_timing_adv < -entry_sp, "WAIT_ADVERSE", "NEUTRAL"))

                    # C3: Fixed elapsed-time comparison (5s, 10s, 15s) from the exact same origin ticks
                    fixed_cols: Dict[str, Any] = {}
                    for fh in (5, 10, 15):
                        j_pos = np.searchsorted(td, t0_u + fh, side="left")
                        ok_f = (j_pos < n_day) & (seg_id[np.minimum(j_pos, n_day - 1)] == seg_id[nonov]) & ((td[np.minimum(j_pos, n_day - 1)] - t0_u) <= fh + 5.0)
                        j_safe = np.minimum(j_pos, n_day - 1)
                        fb_f = bd[j_safe]
                        fa_f = ad[j_safe]
                        bn_f = (fb_f - slip) - (ea + slip) - ((ea + slip) + (fb_f - slip)) * c_rate
                        sn_f = (eb - slip) - (fa_f + slip) - ((eb - slip) + (fa_f + slip)) * c_rate
                        cls_f = np.where(~ok_f, "INCOMPLETE", np.where(bn_f > 0.0, "UP", np.where(sn_f > 0.0, "DOWN", "FLAT")))
                        fixed_cols[f"net_class_{fh}s"] = cls_f
                        fixed_cols[f"ticks_in_{fh}s"] = np.where(ok_f, j_safe - nonov, -1)

                    hrs_u = ((t0_u.astype(np.int64) // 3600) % 24).astype(np.int64)
                    sess_u = np.array([_session_of_hour(int(h)) for h in hrs_u])

                    micro_chunks.append(
                        pd.DataFrame(
                            {
                                "feature_cutoff_utc": np.round(t0_u, 6),
                                "earliest_entry_utc": np.round(t0_u, 6),
                                "label_obs_end_utc": np.round(t1_u, 6),
                                "day_utc": day_key,
                                "session_utc": sess_u,
                                "entry_bid": eb,
                                "entry_ask": ea,
                                "entry_mid": em,
                                "entry_spread_usd": np.round(ea - eb, 5),
                                "fut_bid_30t": fb,
                                "fut_ask_30t": fa,
                                "fut_mid_30t": fm,
                                "mid_delta_usd": np.round(fm - em, 5),
                                "buy_exec_raw_usd": np.round(buy_raw, 5),
                                "sell_exec_raw_usd": np.round(sell_raw, 5),
                                "buy_net_usd": np.round(buy_net, 5),
                                "sell_net_usd": np.round(sell_net, 5),
                                "buy_ret_net": np.round(buy_net / (ea + slip), 7),
                                "sell_ret_net": np.round(sell_net / (eb - slip), 7),
                                "raw_exec_class": raw_cls,
                                "net_class": net_cls,
                                "net_class_buf010": net_cls_010,
                                "quote_move_class_1x_spread": q_move_1x_sp,
                                "quote_move_class_010": q_move_010,
                                "buy_timing_adv_usd": np.round(buy_timing_adv, 5),
                                "sell_timing_adv_usd": np.round(sell_timing_adv, 5),
                                "buy_timing_class": buy_timing_cls,
                                "sell_timing_class": sell_timing_cls,
                                "elapsed_sec": np.round(elap, 4),
                                "quote_intensity_tps": np.round(q_tps, 3),
                                "short_term_vol_30t_usd": np.round(st_vol, 5),
                                **fixed_cols,
                            }
                        )
                    )

            d = nxt

    conn.close()

    df_macro = pd.DataFrame(macro_rows)
    df_micro = pd.concat(micro_chunks, ignore_index=True) if micro_chunks else pd.DataFrame()

    macro_pq = OUT_DIR / "g3_2_macro_labels.parquet"
    micro_pq = OUT_DIR / "g3_2_micro_labels.parquet"
    df_macro.to_parquet(macro_pq, index=False, compression="snappy")
    df_micro.to_parquet(micro_pq, index=False, compression="snappy")

    print(f"[G3.2] Saved Macro parquet: {len(df_macro):,} rows ({macro_pq.stat().st_size / 1e6:.2f} MB)", flush=True)
    print(f"[G3.2] Saved Micro parquet: {len(df_micro):,} rows ({micro_pq.stat().st_size / 1e6:.2f} MB)", flush=True)

    # ------------------------------------------------------------------
    # STEP 3: Save Schema, Barrier Validation & Phase F Statistical Distribution
    # ------------------------------------------------------------------
    schema_doc = {
        "task": "G3.2_LABEL_SCHEMA",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "label_spec_version": LABEL_SPEC_VERSION,
        "dataset_fingerprint_sha256": dataset_fp,
        "cost_model": {
            "profile": DEFAULT_COST_SPEC.profile_name,
            "account_id": DEFAULT_COST_SPEC.account_id,
            "commission_usd_per_million_per_side": DEFAULT_COST_SPEC.commission_usd_per_million_per_side,
            "slippage_pips_per_side": DEFAULT_COST_SPEC.slippage_pips_per_side,
            "pip_size_usd": DEFAULT_COST_SPEC.pip_size,
            "apply_swap": DEFAULT_COST_SPEC.apply_swap,
        },
        "excluded_unavailable_order_flow_features": sorted(EXCLUDED_UNAVAILABLE_ML_FEATURES),
        "allowed_macro_features": sorted(ALLOWED_MACRO_ML_FEATURES),
        "allowed_micro_features": sorted(ALLOWED_MICRO_ML_FEATURES),
        "macro_parquet_columns": {col: str(dtype) for col, dtype in df_macro.dtypes.items()},
        "micro_parquet_columns": {col: str(dtype) for col, dtype in df_micro.dtypes.items()},
    }
    (OUT_DIR / "g3_2_label_schema.json").write_text(json.dumps(schema_doc, indent=2), encoding="utf-8")

    (OUT_DIR / "g3_2_barrier_order_validation.json").write_text(
        json.dumps(
            {
                "task": "G3.2_BARRIER_ORDER_VALIDATION",
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "methodology": "Tick-chronological executable bid/ask barrier resolution (BUY enters ask / evaluates bid; SELL enters bid / evaluates ask). Never resolves dual-touch barriers using candle OHLC alone.",
                **barrier_validation,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Phase F: Distribution, cost-sensitive shift, session/regime breakdown, day-clustered N_eff, C3 & C4
    df_m_nonov = df_macro[df_macro["is_nonoverlap_15m"]].copy()

    def _dist_counts(series: pd.Series) -> Dict[str, Any]:
        vc = series.value_counts(dropna=False).to_dict()
        tot = max(1, len(series))
        return {str(k): {"count": int(v), "pct": round(100.0 * v / tot, 2)} for k, v in vc.items()}

    # C4 adverse selection summary
    df_c4 = pd.DataFrame(c4_records)
    c4_summary: Dict[str, Any] = {}
    if not df_c4.empty:
        for mc_val in ("UP", "DOWN", "FLAT"):
            sub = df_c4[df_c4["micro_net_class"] == mc_val]
            c4_summary[mc_val] = {
                "n_windows": int(len(sub)),
                "mean_15m_buy_ret_net_bps": round(float(sub["buy_ret_net"].mean() * 1e4), 3),
                "mean_15m_sell_ret_net_bps": round(float(sub["sell_ret_net"].mean() * 1e4), 3),
                "mean_15m_buy_mae_usd": round(float(sub["buy_mae_usd"].mean()), 4),
                "mean_15m_sell_mae_usd": round(float(sub["sell_mae_usd"].mean()), 4),
                "mean_h3b_buy_r": round(float(sub["h3b_buy_r"].mean()), 4),
                "mean_h3b_sell_r": round(float(sub["h3b_sell_r"].mean()), 4),
            }

    dist_doc = {
        "task": "G3.2_PHASE_F_LABEL_DISTRIBUTION",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "governance_note": "Descriptive label quality only. Well-constructed labels do NOT imply predictive accuracy.",
        "macro_summary": {
            "raw_5m_buckets_before_quarantine": len(bars_5m) + 1,
            "quarantined_sunday_5m_buckets": 1,
            "ml_eligible_5m_buckets": len(bars_5m),
            "total_5m_decision_labels": int(len(df_macro)),
            "total_exclusions": int(sum(macro_exclusions.values())),
            "unaccounted_5m_buckets": len(bars_5m) - int(len(df_macro)) - int(sum(macro_exclusions.values())),
            "nonoverlap_15m_labels": int(len(df_m_nonov)),
            "exclusions": dict(macro_exclusions),
            "cost_sensitive_class_shift_nonoverlap_15m": {
                "0.05pct_raw": _dist_counts(df_m_nonov["raw_class_005"]),
                "0.05pct_net_of_costs": _dist_counts(df_m_nonov["net_class_005"]),
                "0.10pct_raw": _dist_counts(df_m_nonov["raw_class_010"]),
                "0.10pct_net_of_costs": _dist_counts(df_m_nonov["net_class_010"]),
                "0.15pct_raw": _dist_counts(df_m_nonov["raw_class_015"]),
                "0.15pct_net_of_costs": _dist_counts(df_m_nonov["net_class_015"]),
                "0.20pct_raw": _dist_counts(df_m_nonov["raw_class_020"]),
                "0.20pct_net_of_costs": _dist_counts(df_m_nonov["net_class_020"]),
            },
            "barrier_010_nonoverlap_15m": {
                "BUY": _dist_counts(df_m_nonov["buy_barrier_010"]),
                "SELL": _dist_counts(df_m_nonov["sell_barrier_010"]),
            },
            "h3b_isolated_hypothetical_outcomes_nonoverlap": {
                "note": "Isolated hypothetical candidate entries under frozen H3b exits/costs — NOT portfolio-executed trades.",
                "BUY_exit_reasons": _dist_counts(df_m_nonov["h3b_sim_buy_exit"]),
                "BUY_mean_r": round(float(df_m_nonov["h3b_sim_buy_r"].mean()), 4),
                "SELL_exit_reasons": _dist_counts(df_m_nonov["h3b_sim_sell_exit"]),
                "SELL_mean_r": round(float(df_m_nonov["h3b_sim_sell_r"].mean()), 4),
            },
            "by_regime_net_010_nonoverlap": {
                reg: _dist_counts(g["net_class_010"]) for reg, g in df_m_nonov.groupby("regime")
            },
            "by_session_net_010_nonoverlap": {
                sess: _dist_counts(g["net_class_010"]) for sess, g in df_m_nonov.groupby("session_utc")
            },
            "day_clustered_uncertainty_nonoverlap_15m": {
                "net_UP_010": _day_cluster_stats((df_m_nonov["net_class_010"] == "UP").astype(float).values, df_m_nonov["day_utc"].values),
                "net_DOWN_010": _day_cluster_stats((df_m_nonov["net_class_010"] == "DOWN").astype(float).values, df_m_nonov["day_utc"].values),
                "net_DIRECTIONAL_010": _day_cluster_stats((df_m_nonov["net_class_010"] != "FLAT").astype(float).values, df_m_nonov["day_utc"].values),
            },
        },
        "micro_summary": {
            "total_nonoverlap_30_tick_labels": int(len(df_micro)),
            "exclusions": dict(micro_exclusions),
            "cost_sensitive_class_shift": {
                "raw_post_spread_gt0": _dist_counts(df_micro["raw_exec_class"]),
                "standalone_rt_net_of_spread_comm_slip_gt0": _dist_counts(df_micro["net_class"]),
                "standalone_rt_net_plus_010_buffer": _dist_counts(df_micro["net_class_buf010"]),
            },
            "entry_timing_and_quote_move_targets": {
                "note": "Primary Micro targets for H3b entry timing (no duplicate round-trip commission).",
                "quote_move_class_1x_spread": _dist_counts(df_micro["quote_move_class_1x_spread"]),
                "quote_move_class_010": _dist_counts(df_micro["quote_move_class_010"]),
                "buy_timing_class_enter_now_vs_wait": _dist_counts(df_micro["buy_timing_class"]),
                "sell_timing_class_enter_now_vs_wait": _dist_counts(df_micro["sell_timing_class"]),
            },
            "fixed_time_horizon_comparison": {
                "5s_net": _dist_counts(df_micro["net_class_5s"]),
                "10s_net": _dist_counts(df_micro["net_class_10s"]),
                "15s_net": _dist_counts(df_micro["net_class_15s"]),
                "agreement_30t_vs_5s_pct": round(100.0 * float((df_micro["net_class"] == df_micro["net_class_5s"]).mean()), 2),
                "agreement_30t_vs_10s_pct": round(100.0 * float((df_micro["net_class"] == df_micro["net_class_10s"]).mean()), 2),
                "agreement_30t_vs_15s_pct": round(100.0 * float((df_micro["net_class"] == df_micro["net_class_15s"]).mean()), 2),
            },
            "by_session_net_class": {
                sess: _dist_counts(g["net_class"]) for sess, g in df_micro.groupby("session_utc")
            },
            "day_clustered_uncertainty": {
                "net_UP": _day_cluster_stats((df_micro["net_class"] == "UP").astype(float).values, df_micro["day_utc"].values),
                "net_DOWN": _day_cluster_stats((df_micro["net_class"] == "DOWN").astype(float).values, df_micro["day_utc"].values),
                "net_DIRECTIONAL": _day_cluster_stats((df_micro["net_class"] != "FLAT").astype(float).values, df_micro["day_utc"].values),
            },
        },
        "phase_c4_adverse_selection_research": {
            "description": "Unconditional relationship between immediate post-entry 30-tick Micro net direction and subsequent 15m / H3b outcomes (non-overlapping 15m entries). Observational research only.",
            "by_post_entry_micro_30t_class": c4_summary,
        },
        "elapsed_sec": round(time.time() - t0, 2),
    }
    (OUT_DIR / "g3_2_label_distribution.json").write_text(json.dumps(dist_doc, indent=2), encoding="utf-8")
    print(f"[G3.2 LABEL BUILD DONE in {dist_doc['elapsed_sec']}s]", flush=True)


if __name__ == "__main__":
    build_labels()
