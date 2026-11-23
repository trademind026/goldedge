"""
TASK G3.1 — Phases B, C, D: Clean-Room Macro (15m), Micro (30-tick),
and Sample Independence / Leakage Audit.
ZERO imports from src/goldedge/.
Outputs:
  - backtest_results/audit/task_g3_1_ml_feasibility/g3_1_macro_feasibility.json
  - backtest_results/audit/task_g3_1_ml_feasibility/g3_1_micro_feasibility.json
  - backtest_results/audit/task_g3_1_ml_feasibility/g3_1_independence_leakage_audit.json
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import psycopg2

OUT_DIR = Path("backtest_results/audit/task_g3_1_ml_feasibility")
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


def _newey_west_n_eff(x: np.ndarray, max_lag: int) -> Dict[str, Any]:
    """Compute Bartlett-kernel autocorrelation-adjusted effective sample size."""
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < max_lag + 5:
        return {"n": n, "n_eff": float(n), "variance_inflation": 1.0, "lag_acfs": {}}
    xm = x - np.mean(x)
    var = float(np.dot(xm, xm))
    if var <= 1e-18:
        return {"n": n, "n_eff": float(n), "variance_inflation": 1.0, "lag_acfs": {}}
    acfs: Dict[str, float] = {}
    vif = 1.0
    for k in range(1, max_lag + 1):
        rho = float(np.dot(xm[:-k], xm[k:]) / var)
        if k <= 10 or k in (15, 20, 30, 60):
            acfs[f"lag_{k}"] = round(rho, 5)
        w = 1.0 - (k / (max_lag + 1.0))
        vif += 2.0 * w * rho
    vif = max(1.0, vif)
    return {
        "n": n,
        "n_eff": round(n / vif, 1),
        "variance_inflation": round(vif, 3),
        "lag_acfs": acfs,
    }


def _day_cluster_dependence(values: np.ndarray, day_ids: np.ndarray) -> Dict[str, Any]:
    """
    Compute day-level cluster dependence (one-way random effects ICC),
    Kish/cluster Design Effect (DEFF), cluster-adjusted effective sample size
    (N_eff_day_cluster), and day-level block standard error / 95% CI half-width.
    """
    v = np.asarray(values, dtype=np.float64)
    d = np.asarray(day_ids)
    mask = np.isfinite(v)
    v = v[mask]
    d = d[mask]
    n = len(v)
    if n < 20:
        return {"n_obs": n, "n_days": 0}

    uniq_days, inv = np.unique(d, return_inverse=True)
    g = len(uniq_days)
    if g < 3:
        return {"n_obs": n, "n_days": g}

    day_counts = np.bincount(inv).astype(np.float64)
    day_sums = np.bincount(inv, weights=v)
    day_means = day_sums / np.maximum(1.0, day_counts)

    grand_mean = float(np.mean(v))
    ss_between = float(np.sum(day_counts * (day_means - grand_mean) ** 2))
    ss_within = float(np.sum((v - day_means[inv]) ** 2))

    ms_between = ss_between / max(1.0, g - 1.0)
    ms_within = ss_within / max(1.0, n - g)

    # Effective cluster size n0 (Snedecor & Cochran)
    n0 = (n - float(np.sum(day_counts ** 2)) / n) / max(1.0, g - 1.0)
    sigma2_b = max(0.0, (ms_between - ms_within) / max(1e-9, n0))
    sigma2_w = max(1e-18, ms_within)
    icc = sigma2_b / (sigma2_b + sigma2_w)

    # Cluster design effect using RMS cluster size
    m_bar = float(n / g)
    deff_day = 1.0 + max(0.0, (n0 - 1.0) * icc)
    n_eff_day_cluster = n / max(1.0, deff_day)

    # Day-level block SE of daily means (robust to arbitrary within-day correlation)
    se_day_block = float(np.std(day_means, ddof=1) / math.sqrt(g))
    se_iid = float(np.std(v, ddof=1) / math.sqrt(n))
    block_vif = (se_day_block / max(1e-12, se_iid)) ** 2
    n_eff_block_se = n / max(1.0, block_vif)

    return {
        "n_obs": int(n),
        "n_days": int(g),
        "mean_obs_per_day": round(m_bar, 2),
        "icc_within_day": round(float(icc), 6),
        "deff_day_icc": round(float(deff_day), 3),
        "n_eff_day_icc": round(float(n_eff_day_cluster), 1),
        "day_block_se": round(se_day_block, 6),
        "iid_se": round(se_iid, 6),
        "day_block_variance_inflation": round(float(max(1.0, block_vif)), 3),
        "n_eff_day_block_se": round(float(n_eff_block_se), 1),
        "day_block_95pct_ci_halfwidth": round(1.96 * se_day_block, 6),
    }


def _ema_np(arr: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(arr), np.nan, dtype=np.float64)
    if len(arr) == 0:
        return out
    alpha = 2.0 / (period + 1.0)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out


def _compute_clean_room_regimes_5m(
    bar_ts_s: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Clean-room 5m regime classifier mirroring GoldEdge RegimeDetectorService
    (EMA20, EMA50, ATR14, ADX14, ER20) without importing src/goldedge/.
    """
    n = len(c)
    ema20 = _ema_np(c, 20)
    ema50 = _ema_np(c, 50)

    tr = np.zeros(n, dtype=np.float64)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr14 = _ema_np(tr, 14)

    # Wilder-style ADX14
    plus_dm = np.zeros(n, dtype=np.float64)
    minus_dm = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        up = h[i] - h[i - 1]
        dn = l[i - 1] - l[i]
        if up > dn and up > 0:
            plus_dm[i] = up
        if dn > up and dn > 0:
            minus_dm[i] = dn
    sm_tr = _ema_np(tr, 14)
    sm_p = _ema_np(plus_dm, 14)
    sm_m = _ema_np(minus_dm, 14)
    pdi = np.where(sm_tr > 1e-12, 100.0 * sm_p / sm_tr, 0.0)
    mdi = np.where(sm_tr > 1e-12, 100.0 * sm_m / sm_tr, 0.0)
    dx = np.where((pdi + mdi) > 1e-12, 100.0 * np.abs(pdi - mdi) / (pdi + mdi), 0.0)
    adx14 = _ema_np(dx, 14)

    # Rolling ATR baseline (mean over last 100 bars)
    atr_ratio = np.ones(n, dtype=np.float64)
    for i in range(n):
        s = max(0, i - 99)
        m = float(np.mean(atr14[s : i + 1]))
        atr_ratio[i] = atr14[i] / m if m > 1e-12 else 1.0

    regimes = np.empty(n, dtype=object)
    for i in range(n):
        if i < 50:
            regimes[i] = "WARMUP"
        elif atr_ratio[i] >= 1.8:
            regimes[i] = "HIGH_VOLATILITY"
        elif atr_ratio[i] <= 0.5:
            regimes[i] = "LOW_VOLATILITY"
        elif adx14[i] >= 22.0 and ema20[i] > ema50[i]:
            regimes[i] = "TRENDING_BULL"
        elif adx14[i] >= 22.0 and ema20[i] < ema50[i]:
            regimes[i] = "TRENDING_BEAR"
        else:
            regimes[i] = "SIDEWAYS"
    return regimes, atr14, adx14


def run_phases_bcd() -> None:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = psycopg2.connect(DB_URL)
    conn.set_session(readonly=True, autocommit=True)

    # ------------------------------------------------------------------
    # STEP 1: Build clean-room 1m bid/ask OHLC bars via single SQL pass
    #         Strictly ML-eligible quotes (excludes quarantined weekend ticks)
    # ------------------------------------------------------------------
    print("[BCD] Querying 1m bid/ask candles from ML-eligible XAUUSD quotes (v_market_ticks_quote_semantics_v1)...", flush=True)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                EXTRACT(EPOCH FROM time_bucket('1 minute', time))::bigint AS bucket_s,
                first(bid, time) AS bid_open,
                max(bid) AS bid_high,
                min(bid) AS bid_low,
                last(bid, time) AS bid_close,
                first(ask, time) AS ask_open,
                max(ask) AS ask_high,
                min(ask) AS ask_low,
                last(ask, time) AS ask_close,
                avg(ask - bid) AS spread_mean,
                count(*) AS tick_count
            FROM v_market_ticks_quote_semantics_v1
            WHERE symbol = 'XAUUSD'
              AND is_ml_eligible_quote
            GROUP BY 1
            ORDER BY 1 ASC
            """
        )
        bar_rows = cur.fetchall()

    b_arr = np.array(bar_rows, dtype=np.float64)
    ts_s = b_arr[:, 0].astype(np.int64)
    bid_o, bid_h, bid_l, bid_c = b_arr[:, 1], b_arr[:, 2], b_arr[:, 3], b_arr[:, 4]
    ask_o, ask_h, ask_l, ask_c = b_arr[:, 5], b_arr[:, 6], b_arr[:, 7], b_arr[:, 8]
    sp_mean = b_arr[:, 9]
    tk_cnt = b_arr[:, 10].astype(np.int64)
    n_bars = len(ts_s)
    print(f"[BCD] Loaded {n_bars:,} valid 1m bid/ask bars.", flush=True)

    # Map bucket_s -> index for O(1) exact horizon lookup
    ts_to_idx = {int(t): i for i, t in enumerate(ts_s)}

    # Build 5m bars cleanly (only completed 5m buckets)
    bucket_5m = (ts_s // 300) * 300
    uniq_5m, start_idx_5m = np.unique(bucket_5m, return_index=True)
    n_5m = len(uniq_5m)
    o5 = np.empty(n_5m)
    h5 = np.empty(n_5m)
    l5 = np.empty(n_5m)
    c5 = np.empty(n_5m)
    cnt5 = np.empty(n_5m, dtype=np.int64)
    for k in range(n_5m):
        s = int(start_idx_5m[k])
        e = int(start_idx_5m[k + 1]) if k + 1 < n_5m else n_bars
        o5[k] = bid_o[s]
        h5[k] = np.max(bid_h[s:e])
        l5[k] = np.min(bid_l[s:e])
        c5[k] = bid_c[e - 1]
        cnt5[k] = e - s

    regimes_5m, atr14_5m, adx14_5m = _compute_clean_room_regimes_5m(uniq_5m, o5, h5, l5, c5)
    # Map each 1m bar to the LAST COMPLETED 5m bar regime (causal!)
    regime_at_1m = np.empty(n_bars, dtype=object)
    b5_to_idx = {int(t): i for i, t in enumerate(uniq_5m)}
    for i in range(n_bars):
        # At close of 1m bar ts_s[i], if (ts_s[i] % 300) == 240, the 5m bucket (ts_s[i]//300)*300 just completed.
        # Otherwise use the previous 5m bucket ((ts_s[i]//300)*300 - 300).
        cur_b5 = int((ts_s[i] // 300) * 300)
        ref_b5 = cur_b5 if (ts_s[i] % 300) == 240 else (cur_b5 - 300)
        idx5 = b5_to_idx.get(ref_b5)
        regime_at_1m[i] = regimes_5m[idx5] if idx5 is not None else "WARMUP"

    # ------------------------------------------------------------------
    # PHASE B: 15-Minute Macro Prediction Feasibility
    # ------------------------------------------------------------------
    # Strictly contiguous 15m window: all 15 future 1m bars t+60 .. t+900 exist (no gap!)
    # Relaxed 15m window: t+900 exists AND max inter-bar gap inside (t, t+900] < 300s (no outage/rollover)
    strict_mask = np.zeros(n_bars, dtype=bool)
    relaxed_mask = np.zeros(n_bars, dtype=bool)
    buy_end_ret = np.full(n_bars, np.nan)
    sell_end_ret = np.full(n_bars, np.nan)
    mid_end_ret = np.full(n_bars, np.nan)
    buy_touch_max = np.full(n_bars, np.nan)
    sell_touch_max = np.full(n_bars, np.nan)

    for i in range(n_bars):
        t = int(ts_s[i])
        j = ts_to_idx.get(t + 900)
        if j is None:
            continue
        # Check gaps inside [i .. j]
        diffs = np.diff(ts_s[i : j + 1])
        if len(diffs) == 0:
            continue
        max_gap_s = int(np.max(diffs))
        if max_gap_s < 300:
            relaxed_mask[i] = True
            if j - i == 15 and max_gap_s == 60:
                strict_mask[i] = True
            entry_ask = ask_c[i]
            entry_bid = bid_c[i]
            entry_mid = 0.5 * (entry_ask + entry_bid)
            fut_mid = 0.5 * (ask_c[j] + bid_c[j])
            buy_end_ret[i] = (bid_c[j] - entry_ask) / entry_ask
            sell_end_ret[i] = (entry_bid - ask_c[j]) / entry_bid
            mid_end_ret[i] = (fut_mid - entry_mid) / entry_mid
            # Intrahorizon barrier touch over (i, j]
            buy_touch_max[i] = (float(np.max(bid_h[i + 1 : j + 1])) - entry_ask) / entry_ask
            sell_touch_max[i] = (entry_bid - float(np.min(ask_l[i + 1 : j + 1]))) / entry_bid

    strict_idxs = np.where(strict_mask)[0]
    relaxed_idxs = np.where(relaxed_mask)[0]
    # 5m decision-time indices (bars closing on :04, :09, :14, ..., :59 -> 5m bar just closed)
    idx_5m_strict = strict_idxs[(ts_s[strict_idxs] % 300) == 240]
    # Non-overlapping 15m grid (:14, :29, :44, :59)
    idx_15m_nonoverlap = strict_idxs[(ts_s[strict_idxs] % 900) == 840]

    def _threshold_stats(idxs: np.ndarray, thresholds: List[float]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        n_sel = len(idxs)
        be = buy_end_ret[idxs]
        se = sell_end_ret[idxs]
        bt = buy_touch_max[idxs]
        st = sell_touch_max[idxs]
        for thr in thresholds:
            lbl = f"{thr * 100:.2f}pct"
            up_e = int(np.sum(be >= thr))
            dn_e = int(np.sum(se >= thr))
            fl_e = n_sel - up_e - dn_e
            # Intrahorizon
            u_t = bt >= thr
            d_t = st >= thr
            both_t = int(np.sum(u_t & d_t))
            up_only_t = int(np.sum(u_t & ~d_t))
            dn_only_t = int(np.sum(~u_t & d_t))
            none_t = int(np.sum(~u_t & ~d_t))
            out[lbl] = {
                "threshold_decimal": thr,
                "n_samples": n_sel,
                "endpoint_executable": {
                    "UP_count": up_e,
                    "DOWN_count": dn_e,
                    "FLAT_count": fl_e,
                    "UP_pct": round(100.0 * up_e / max(1, n_sel), 2),
                    "DOWN_pct": round(100.0 * dn_e / max(1, n_sel), 2),
                    "FLAT_pct": round(100.0 * fl_e / max(1, n_sel), 2),
                    "directional_total_pct": round(100.0 * (up_e + dn_e) / max(1, n_sel), 2),
                },
                "intrahorizon_barrier_touch": {
                    "UP_only_count": up_only_t,
                    "DOWN_only_count": dn_only_t,
                    "BOTH_touched_count": both_t,
                    "NEITHER_count": none_t,
                    "UP_only_pct": round(100.0 * up_only_t / max(1, n_sel), 2),
                    "DOWN_only_pct": round(100.0 * dn_only_t / max(1, n_sel), 2),
                    "BOTH_touched_pct": round(100.0 * both_t / max(1, n_sel), 2),
                    "NEITHER_pct": round(100.0 * none_t / max(1, n_sel), 2),
                },
            }
        return out

    thresholds = [0.0005, 0.0010, 0.0015, 0.0020]
    macro_by_regime: Dict[str, Any] = {}
    for reg in ["TRENDING_BULL", "TRENDING_BEAR", "SIDEWAYS", "HIGH_VOLATILITY", "LOW_VOLATILITY", "WARMUP"]:
        r_idx = strict_idxs[regime_at_1m[strict_idxs] == reg]
        r_15 = idx_15m_nonoverlap[regime_at_1m[idx_15m_nonoverlap] == reg]
        n_r = len(r_idx)
        up10 = int(np.sum(buy_end_ret[r_idx] >= 0.0010))
        dn10 = int(np.sum(sell_end_ret[r_idx] >= 0.0010))
        macro_by_regime[reg] = {
            "strict_1m_windows": n_r,
            "strict_1m_pct": round(100.0 * n_r / max(1, len(strict_idxs)), 2),
            "nonoverlap_15m_windows": int(len(r_15)),
            "endpoint_0.10pct_UP_pct": round(100.0 * up10 / max(1, n_r), 2),
            "endpoint_0.10pct_DOWN_pct": round(100.0 * dn10 / max(1, n_r), 2),
            "endpoint_0.10pct_FLAT_pct": round(100.0 * (n_r - up10 - dn10) / max(1, n_r), 2),
        }

    # Monthly & session breakdown of strict 15m windows (0.10% endpoint)
    dts_strict = [datetime.fromtimestamp(int(t), tz=timezone.utc) for t in ts_s[strict_idxs]]
    unique_days_strict = len({dt.strftime("%Y-%m-%d") for dt in dts_strict})
    macro_by_month: Dict[str, Any] = {}
    macro_by_session: Dict[str, Any] = {}
    ym_arr = np.array([dt.strftime("%Y-%m") for dt in dts_strict])
    sess_arr = np.array([_session_of_hour(dt.hour) for dt in dts_strict])
    for ym in sorted(set(ym_arr)):
        m = ym_arr == ym
        sub = strict_idxs[m]
        up10 = int(np.sum(buy_end_ret[sub] >= 0.0010))
        dn10 = int(np.sum(sell_end_ret[sub] >= 0.0010))
        macro_by_month[ym] = {
            "strict_1m_windows": int(len(sub)),
            "nonoverlap_15m_approx": int(len(sub) // 15),
            "UP_0.10pct": round(100.0 * up10 / max(1, len(sub)), 2),
            "DOWN_0.10pct": round(100.0 * dn10 / max(1, len(sub)), 2),
            "FLAT_0.10pct": round(100.0 * (len(sub) - up10 - dn10) / max(1, len(sub)), 2),
            "median_abs_mid_ret_bps": round(float(np.median(np.abs(mid_end_ret[sub])) * 1e4), 2),
        }
    for sess in ["ASIA_00_07", "LONDON_07_12", "OVERLAP_12_17", "NY_17_20", "OFF_20_24"]:
        m = sess_arr == sess
        sub = strict_idxs[m]
        up10 = int(np.sum(buy_end_ret[sub] >= 0.0010))
        dn10 = int(np.sum(sell_end_ret[sub] >= 0.0010))
        macro_by_session[sess] = {
            "strict_1m_windows": int(len(sub)),
            "UP_0.10pct": round(100.0 * up10 / max(1, len(sub)), 2),
            "DOWN_0.10pct": round(100.0 * dn10 / max(1, len(sub)), 2),
            "FLAT_0.10pct": round(100.0 * (len(sub) - up10 - dn10) / max(1, len(sub)), 2),
            "median_abs_mid_ret_bps": round(float(np.median(np.abs(mid_end_ret[sub])) * 1e4), 2),
        }

    # Cost friction context (spread + Fusion Markets $4.50/lot RT = $0.045/oz)
    med_mid_price = float(np.median(0.5 * (bid_c + ask_c)))
    med_spread_usd = float(np.median(ask_c - bid_c))
    rt_comm_usd_oz = 0.045
    total_friction_bps = round(1e4 * (med_spread_usd + rt_comm_usd_oz) / med_mid_price, 2)

    macro_result = {
        "task": "G3.1_PHASE_B_MACRO_FEASIBILITY",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "candle_summary": {
            "total_valid_1m_bars": n_bars,
            "total_5m_buckets": n_5m,
            "strict_contiguous_15m_windows_1m_grid": int(len(strict_idxs)),
            "relaxed_no_outage_15m_windows_1m_grid": int(len(relaxed_idxs)),
            "strict_5m_decision_windows": int(len(idx_5m_strict)),
            "strict_nonoverlap_15m_windows": int(len(idx_15m_nonoverlap)),
            "unique_trading_days_strict": unique_days_strict,
            "median_price_usd": round(med_mid_price, 2),
            "median_bar_close_spread_usd": round(med_spread_usd, 4),
            "fusion_rt_commission_usd_per_oz": rt_comm_usd_oz,
            "approx_total_roundtrip_friction_bps": total_friction_bps,
        },
        "threshold_sensitivity_1m_strict": _threshold_stats(strict_idxs, thresholds),
        "threshold_sensitivity_5m_decision_grid": _threshold_stats(idx_5m_strict, thresholds),
        "threshold_sensitivity_nonoverlap_15m": _threshold_stats(idx_15m_nonoverlap, thresholds),
        "regime_distribution": macro_by_regime,
        "monthly_breakdown_0.10pct": macro_by_month,
        "session_breakdown_0.10pct": macro_by_session,
        "mid_return_quantiles_15m_bps": {
            "p05": round(float(np.percentile(mid_end_ret[strict_idxs], 5) * 1e4), 2),
            "p25": round(float(np.percentile(mid_end_ret[strict_idxs], 25) * 1e4), 2),
            "p50": round(float(np.percentile(mid_end_ret[strict_idxs], 50) * 1e4), 2),
            "p75": round(float(np.percentile(mid_end_ret[strict_idxs], 75) * 1e4), 2),
            "p95": round(float(np.percentile(mid_end_ret[strict_idxs], 95) * 1e4), 2),
            "abs_p50": round(float(np.percentile(np.abs(mid_end_ret[strict_idxs]), 50) * 1e4), 2),
            "abs_p75": round(float(np.percentile(np.abs(mid_end_ret[strict_idxs]), 75) * 1e4), 2),
            "abs_p90": round(float(np.percentile(np.abs(mid_end_ret[strict_idxs]), 90) * 1e4), 2),
        },
    }
    with open(OUT_DIR / "g3_1_macro_feasibility.json", "w", encoding="utf-8") as f:
        json.dump(macro_result, f, indent=2)
    print(f"[PHASE B DONE] strict_1m={len(strict_idxs):,}, 5m={len(idx_5m_strict):,}, nonoverlap_15m={len(idx_15m_nonoverlap):,}", flush=True)

    # ------------------------------------------------------------------
    # PHASE C: 30-Tick Micro Prediction Feasibility (day-by-day stream)
    # ------------------------------------------------------------------
    print("[BCD] Streaming valid XAUUSD ticks day-by-day for Phase C & D...", flush=True)
    raw_min = datetime.fromtimestamp(int(ts_s[0]), tz=timezone.utc)
    raw_max = datetime.fromtimestamp(int(ts_s[-1]), tz=timezone.utc)
    cur_day = datetime(raw_min.year, raw_min.month, raw_min.day, tzinfo=timezone.utc)
    last_day = datetime(raw_max.year, raw_max.month, raw_max.day, tzinfo=timezone.utc)

    # Accumulators (exact counts + stratified subsample for quantiles to keep RAM tiny)
    rng = np.random.default_rng(42)
    sess_names = ["ASIA_00_07", "LONDON_07_12", "OVERLAP_12_17", "NY_17_20", "OFF_20_24"]

    elap_samples = {h: defaultdict(list) for h in (10, 30, 60)}
    dt1_samples = defaultdict(list)
    micro_30_total = 0
    micro_30_nonoverlap_total = 0
    micro_30_nonoverlap_rets: List[np.ndarray] = []
    micro_30_nonoverlap_abs_moves: List[np.ndarray] = []
    micro_30_nonoverlap_spreads: List[np.ndarray] = []
    micro_30_nonoverlap_exec_up: List[np.ndarray] = []
    micro_30_nonoverlap_day_ids: List[np.ndarray] = []
    tick1_rets_sample: List[np.ndarray] = []

    # Exact counts for 30-tick neutral bands & executable bands
    mid_bands_usd = [0.10, 0.25, 0.50, 1.00]
    mid_band_counts = {str(b): {"UP": 0, "DOWN": 0, "FLAT": 0} for b in mid_bands_usd}
    sp_mults = [1.0, 1.5, 2.0]
    sp_mult_counts = {f"{m}x_spread": {"UP": 0, "DOWN": 0, "FLAT": 0} for m in sp_mults}
    exec_bands_usd = [0.00, 0.10, 0.25, 0.50]
    exec_band_counts = {str(b): {"UP": 0, "DOWN": 0, "FLAT": 0} for b in exec_bands_usd}
    abs_mid_30_samples: List[np.ndarray] = []
    exec_buy_30_samples: List[np.ndarray] = []
    exec_sell_30_samples: List[np.ndarray] = []

    # Fixed elapsed-time comparison (5s, 15s, 30s, 60s) sampled every 100th tick to be fast & exact-representative
    fixed_horizons_s = [5, 15, 30, 60]
    fixed_stats = {
        h: {"n": 0, "exec_up_gt0": 0, "exec_dn_gt0": 0, "exec_up_gt025": 0, "exec_dn_gt025": 0, "ticks_elapsed_sum": 0, "abs_mid_sum": 0.0}
        for h in fixed_horizons_s
    }

    # Macro-Micro concurrency check (collect pairs of (macro_15m_ret, mean_micro_30t_ret))
    macro_micro_pairs_macro: List[float] = []
    macro_micro_pairs_micro: List[float] = []

    with conn.cursor() as cur:
        d = cur_day
        while d <= last_day:
            nxt = d + timedelta(days=1)
            cur.execute(
                """
                SELECT
                    EXTRACT(EPOCH FROM time) AS ts_s,
                    bid,
                    ask
                FROM v_market_ticks_quote_semantics_v1
                WHERE symbol = 'XAUUSD'
                  AND time >= %s AND time < %s
                  AND is_ml_eligible_quote
                ORDER BY time ASC
                """,
                (d, nxt),
            )
            rows = cur.fetchall()
            if len(rows) < 70:
                d = nxt
                continue
            arr = np.array(rows, dtype=np.float64)
            t_s = arr[:, 0]
            b = arr[:, 1]
            a = arr[:, 2]
            mid = 0.5 * (b + a)
            sp = a - b
            n = len(t_s)

            # Identify contiguous segments (split on gap >= 60s)
            dt = np.diff(t_s)
            bad_gap = np.concatenate(([False], dt >= 60.0))
            seg_id = np.cumsum(bad_gap)

            # Tick-to-tick return sample for lag-1..10 ACF (10% of days / max 50k per day)
            if n > 2000:
                r1 = np.diff(mid)
                ok1 = dt < 60.0
                r1_ok = r1[ok1]
                if len(r1_ok) > 0:
                    tick1_rets_sample.append(r1_ok[: min(len(r1_ok), 10000)])

            # Hours for session attribution
            hrs = ((t_s.astype(np.int64) // 3600) % 24).astype(np.int64)

            # Interarrival subsample
            ok_dt = dt[dt < 60.0]
            if len(ok_dt) > 0:
                step_dt = max(1, len(ok_dt) // 2000)
                ok_hrs = hrs[:-1][dt < 60.0][::step_dt]
                for hr_val, val in zip(ok_hrs, ok_dt[::step_dt]):
                    dt1_samples[_session_of_hour(int(hr_val))].append(float(val))

            # Horizon 10, 30, 60 elapsed time (only inside same contiguous segment)
            for h_ticks in (10, 30, 60):
                valid_h = seg_id[:-h_ticks] == seg_id[h_ticks:]
                if not np.any(valid_h):
                    continue
                elap = t_s[h_ticks:][valid_h] - t_s[:-h_ticks][valid_h]
                h_hrs = hrs[:-h_ticks][valid_h]
                step_s = max(1, len(elap) // 1500)
                for hr_val, ev in zip(h_hrs[::step_s], elap[::step_s]):
                    elap_samples[h_ticks][_session_of_hour(int(hr_val))].append(float(ev))

                if h_ticks == 30:
                    idx_v = np.where(valid_h)[0]
                    cnt_v = len(idx_v)
                    micro_30_total += cnt_v

                    dmid30 = mid[idx_v + 30] - mid[idx_v]
                    ex_buy = b[idx_v + 30] - a[idx_v]
                    ex_sell = b[idx_v] - a[idx_v + 30]
                    sp0 = sp[idx_v]

                    for bv in mid_bands_usd:
                        u = int(np.sum(dmid30 >= bv))
                        dn = int(np.sum(dmid30 <= -bv))
                        mid_band_counts[str(bv)]["UP"] += u
                        mid_band_counts[str(bv)]["DOWN"] += dn
                        mid_band_counts[str(bv)]["FLAT"] += cnt_v - u - dn

                    for mult in sp_mults:
                        thr_v = mult * sp0
                        u = int(np.sum(dmid30 >= thr_v))
                        dn = int(np.sum(dmid30 <= -thr_v))
                        key = f"{mult}x_spread"
                        sp_mult_counts[key]["UP"] += u
                        sp_mult_counts[key]["DOWN"] += dn
                        sp_mult_counts[key]["FLAT"] += cnt_v - u - dn

                    for bv in exec_bands_usd:
                        u = int(np.sum(ex_buy > bv))
                        dn = int(np.sum(ex_sell > bv))
                        exec_band_counts[str(bv)]["UP"] += u
                        exec_band_counts[str(bv)]["DOWN"] += dn
                        exec_band_counts[str(bv)]["FLAT"] += cnt_v - u - dn

                    # Subsample for exact-like quantiles
                    st_q = max(1, cnt_v // 2000)
                    abs_mid_30_samples.append(np.abs(dmid30[::st_q]))
                    exec_buy_30_samples.append(ex_buy[::st_q])
                    exec_sell_30_samples.append(ex_sell[::st_q])

                    # Non-overlapping 30-tick windows inside valid segments
                    nonov = idx_v[::30]
                    # Filter so adjacent indices are actually >= 30 apart
                    if len(nonov) > 0:
                        micro_30_nonoverlap_total += len(nonov)
                        d_nonov = mid[nonov + 30] - mid[nonov]
                        micro_30_nonoverlap_rets.append(d_nonov)
                        micro_30_nonoverlap_abs_moves.append(np.abs(d_nonov))
                        micro_30_nonoverlap_spreads.append(sp[nonov])
                        micro_30_nonoverlap_exec_up.append((b[nonov + 30] - a[nonov] > 0.0).astype(np.float64))
                        day_int = int(d.strftime("%Y%m%d"))
                        micro_30_nonoverlap_day_ids.append(np.full(len(nonov), day_int, dtype=np.int64))

                        # Macro-Micro correlation on non-overlapping 30t windows (sample up to 200/day)
                        step_mm = max(1, len(nonov) // 200)
                        for p in nonov[::step_mm]:
                            b_min = int((t_s[p] // 60) * 60)
                            b_i = ts_to_idx.get(b_min)
                            if b_i is not None and strict_mask[b_i]:
                                macro_micro_pairs_macro.append(float(mid_end_ret[b_i]))
                                macro_micro_pairs_micro.append(float((mid[p + 30] - mid[p]) / mid[p]))

            # Fixed elapsed-time comparison (every 100th tick)
            probe = np.arange(0, n - 1, 100)
            for fh in fixed_horizons_s:
                target_t = t_s[probe] + fh
                j_pos = np.searchsorted(t_s, target_t, side="left")
                ok = (j_pos < n) & (seg_id[np.minimum(j_pos, n - 1)] == seg_id[probe]) & ((t_s[np.minimum(j_pos, n - 1)] - t_s[probe]) <= fh + 5.0)
                if np.any(ok):
                    p_ok = probe[ok]
                    j_ok = j_pos[ok]
                    eb = b[j_ok] - a[p_ok]
                    es = b[p_ok] - a[j_ok]
                    dm = np.abs(mid[j_ok] - mid[p_ok])
                    fs = fixed_stats[fh]
                    fs["n"] += int(len(p_ok))
                    fs["exec_up_gt0"] += int(np.sum(eb > 0.0))
                    fs["exec_dn_gt0"] += int(np.sum(es > 0.0))
                    fs["exec_up_gt025"] += int(np.sum(eb > 0.25))
                    fs["exec_dn_gt025"] += int(np.sum(es > 0.25))
                    fs["ticks_elapsed_sum"] += int(np.sum(j_ok - p_ok))
                    fs["abs_mid_sum"] += float(np.sum(dm))

            d = nxt

    conn.close()

    def _quantiles(vals: List[float]) -> Dict[str, Any]:
        if not vals:
            return {"n_sampled": 0}
        a = np.array(vals, dtype=np.float64)
        return {
            "n_sampled": int(len(a)),
            "p05": round(float(np.percentile(a, 5)), 4),
            "p25": round(float(np.percentile(a, 25)), 4),
            "p50_median": round(float(np.percentile(a, 50)), 4),
            "p75": round(float(np.percentile(a, 75)), 4),
            "p95": round(float(np.percentile(a, 95)), 4),
            "p99": round(float(np.percentile(a, 99)), 4),
            "max": round(float(np.max(a)), 4),
            "mean": round(float(np.mean(a)), 4),
        }

    # Summarize elapsed times for 10, 30, 60 ticks overall and by session
    elapsed_summary: Dict[str, Any] = {}
    for h_ticks in (10, 30, 60):
        all_v = []
        by_s = {}
        for s in sess_names:
            vs = elap_samples[h_ticks].get(s, [])
            by_s[s] = _quantiles(vs)
            all_v.extend(vs)
        elapsed_summary[f"{h_ticks}_ticks_sec"] = {
            "overall": _quantiles(all_v),
            "by_session": by_s,
        }

    interarrival_all = []
    interarrival_by_sess = {}
    for s in sess_names:
        vs = dt1_samples.get(s, [])
        interarrival_by_sess[s] = _quantiles(vs)
        interarrival_all.extend(vs)

    # Convert exact 30-tick band counts to pct
    def _add_pcts(d_counts: Dict[str, Dict[str, int]]) -> Dict[str, Any]:
        out = {}
        for k, v in d_counts.items():
            tot = max(1, v["UP"] + v["DOWN"] + v["FLAT"])
            out[k] = {
                **v,
                "UP_pct": round(100.0 * v["UP"] / tot, 2),
                "DOWN_pct": round(100.0 * v["DOWN"] / tot, 2),
                "FLAT_pct": round(100.0 * v["FLAT"] / tot, 2),
                "directional_pct": round(100.0 * (v["UP"] + v["DOWN"]) / tot, 2),
            }
        return out

    # Fixed horizon summary
    fixed_summary = {}
    for fh, fs in fixed_stats.items():
        n_f = max(1, fs["n"])
        fixed_summary[f"{fh}s"] = {
            "sampled_windows": fs["n"],
            "mean_ticks_elapsed": round(fs["ticks_elapsed_sum"] / n_f, 2),
            "mean_abs_mid_move_usd": round(fs["abs_mid_sum"] / n_f, 4),
            "exec_UP_gt0_pct": round(100.0 * fs["exec_up_gt0"] / n_f, 2),
            "exec_DOWN_gt0_pct": round(100.0 * fs["exec_dn_gt0"] / n_f, 2),
            "exec_FLAT_gt0_pct": round(100.0 * (n_f - fs["exec_up_gt0"] - fs["exec_dn_gt0"]) / n_f, 2),
            "exec_UP_gt025_pct": round(100.0 * fs["exec_up_gt025"] / n_f, 2),
            "exec_DOWN_gt025_pct": round(100.0 * fs["exec_dn_gt025"] / n_f, 2),
        }

    # Autocorrelation of tick-to-tick and non-overlapping 30-tick returns
    r1_all = np.concatenate(tick1_rets_sample) if tick1_rets_sample else np.array([])
    r30_nonov = np.concatenate(micro_30_nonoverlap_rets) if micro_30_nonoverlap_rets else np.array([])
    acf_tick1 = _newey_west_n_eff(r1_all, max_lag=10)
    acf_30t_nonov = _newey_west_n_eff(r30_nonov, max_lag=10)

    abs_mid_all = np.concatenate(abs_mid_30_samples) if abs_mid_30_samples else np.array([])
    ex_buy_all = np.concatenate(exec_buy_30_samples) if exec_buy_30_samples else np.array([])
    ex_sell_all = np.concatenate(exec_sell_30_samples) if exec_sell_30_samples else np.array([])

    micro_result = {
        "task": "G3.1_PHASE_C_MICRO_FEASIBILITY",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "order_book_and_volume_availability": {
            "level2_order_book_available": False,
            "traded_volume_available": False,
            "quote_type": "Top-of-book bid/ask quotes only (cTrader spot quotes; real_volume IS NULL)",
            "microstructure_repo_lee_ready_defect": "src/goldedge/adapters/secondary/features/microstructure_feature_repo.py sets aggressor=1 if price>=ask and -1 if price<=bid; since price=midpoint (bid+ask)/2 and bid<ask, aggressor is identically 0 on 100% of valid quotes.",
        },
        "sample_counts": {
            "contiguous_30_tick_windows_overlapping": micro_30_total,
            "nonoverlap_30_tick_windows": micro_30_nonoverlap_total,
        },
        "interarrival_time_sec": {
            "overall": _quantiles(interarrival_all),
            "by_session": interarrival_by_sess,
        },
        "elapsed_time_distributions": elapsed_summary,
        "move_distributions_30_tick_usd": {
            "abs_mid_move_usd": _quantiles(abs_mid_all.tolist()),
            "exec_buy_pnl_usd": _quantiles(ex_buy_all.tolist()),
            "exec_sell_pnl_usd": _quantiles(ex_sell_all.tolist()),
        },
        "neutral_band_sensitivity_midprice_30_tick": _add_pcts(mid_band_counts),
        "neutral_band_sensitivity_spread_multiple_30_tick": _add_pcts(sp_mult_counts),
        "executable_post_spread_30_tick": _add_pcts(exec_band_counts),
        "fixed_elapsed_time_comparison": fixed_summary,
        "short_horizon_autocorrelation": {
            "tick_to_tick_mid_delta": acf_tick1,
            "nonoverlap_30_tick_mid_delta": acf_30t_nonov,
        },
    }
    with open(OUT_DIR / "g3_1_micro_feasibility.json", "w", encoding="utf-8") as f:
        json.dump(micro_result, f, indent=2)
    print(f"[PHASE C DONE] overlap_30t={micro_30_total:,}, nonoverlap_30t={micro_30_nonoverlap_total:,}", flush=True)

    # ------------------------------------------------------------------
    # PHASE D: Independence, Effective Sample Size & Leakage Audit
    #          Includes day-level block clustering & non-overlapping
    #          feature/volatility autocorrelation (corrects naive VIF=1.0).
    # ------------------------------------------------------------------
    nw_macro_1m = _newey_west_n_eff(mid_end_ret[strict_idxs], max_lag=60)
    nw_macro_5m = _newey_west_n_eff(mid_end_ret[idx_5m_strict], max_lag=20)
    nw_macro_15m_nonov = _newey_west_n_eff(mid_end_ret[idx_15m_nonoverlap], max_lag=10)
    y10_1m = np.where(buy_end_ret[strict_idxs] >= 0.0010, 1.0, np.where(sell_end_ret[strict_idxs] >= 0.0010, -1.0, 0.0))
    nw_macro_y10_1m = _newey_west_n_eff(y10_1m, max_lag=60)
    y10_15m = np.where(buy_end_ret[idx_15m_nonoverlap] >= 0.0010, 1.0, np.where(sell_end_ret[idx_15m_nonoverlap] >= 0.0010, -1.0, 0.0))
    nw_macro_y10_15m = _newey_west_n_eff(y10_15m, max_lag=10)

    # Day-level block clustering on non-overlapping 15m Macro windows
    macro_nonov_days = np.array(
        [int(datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y%m%d")) for t in ts_s[idx_15m_nonoverlap]],
        dtype=np.int64,
    )
    macro_up10_bin = (buy_end_ret[idx_15m_nonoverlap] >= 0.0010).astype(np.float64)
    macro_dn10_bin = (sell_end_ret[idx_15m_nonoverlap] >= 0.0010).astype(np.float64)
    macro_dir10_bin = ((buy_end_ret[idx_15m_nonoverlap] >= 0.0010) | (sell_end_ret[idx_15m_nonoverlap] >= 0.0010)).astype(np.float64)
    macro_day_cluster_up10 = _day_cluster_dependence(macro_up10_bin, macro_nonov_days)
    macro_day_cluster_dn10 = _day_cluster_dependence(macro_dn10_bin, macro_nonov_days)
    macro_day_cluster_dir10 = _day_cluster_dependence(macro_dir10_bin, macro_nonov_days)
    macro_day_cluster_ret = _day_cluster_dependence(mid_end_ret[idx_15m_nonoverlap], macro_nonov_days)

    # Feature & volatility autocorrelation on non-overlapping 15m Macro windows
    atr_at_nonov = np.array(
        [atr14_5m[b5_to_idx[int((t // 300) * 300)]] for t in ts_s[idx_15m_nonoverlap] if int((t // 300) * 300) in b5_to_idx],
        dtype=np.float64,
    )
    adx_at_nonov = np.array(
        [adx14_5m[b5_to_idx[int((t // 300) * 300)]] for t in ts_s[idx_15m_nonoverlap] if int((t // 300) * 300) in b5_to_idx],
        dtype=np.float64,
    )
    bear_at_nonov = (regime_at_1m[idx_15m_nonoverlap] == "TRENDING_BEAR").astype(np.float64)
    macro_feature_dependence = {
        "atr14_5m_nonoverlap_15m": _newey_west_n_eff(atr_at_nonov, max_lag=20),
        "adx14_5m_nonoverlap_15m": _newey_west_n_eff(adx_at_nonov, max_lag=20),
        "regime_is_bear_nonoverlap_15m": _newey_west_n_eff(bear_at_nonov, max_lag=20),
        "abs_15m_return_vol_cluster": _newey_west_n_eff(np.abs(mid_end_ret[idx_15m_nonoverlap]), max_lag=20),
    }

    # Day-level block clustering & feature/volatility dependence on non-overlapping 30-tick Micro windows
    r30_abs_nonov = np.concatenate(micro_30_nonoverlap_abs_moves) if micro_30_nonoverlap_abs_moves else np.array([])
    sp30_nonov = np.concatenate(micro_30_nonoverlap_spreads) if micro_30_nonoverlap_spreads else np.array([])
    exup30_nonov = np.concatenate(micro_30_nonoverlap_exec_up) if micro_30_nonoverlap_exec_up else np.array([])
    days30_nonov = np.concatenate(micro_30_nonoverlap_day_ids) if micro_30_nonoverlap_day_ids else np.array([])

    micro_day_cluster_exec_up = _day_cluster_dependence(exup30_nonov, days30_nonov)
    micro_day_cluster_abs_move = _day_cluster_dependence(r30_abs_nonov, days30_nonov)
    micro_day_cluster_ret = _day_cluster_dependence(r30_nonov, days30_nonov)
    micro_feature_dependence = {
        "abs_30t_move_vol_cluster": _newey_west_n_eff(r30_abs_nonov, max_lag=20),
        "spread_30t_nonoverlap": _newey_west_n_eff(sp30_nonov, max_lag=20),
    }

    # Conservative Macro & Micro effective sample sizes (minimum across day-block SE & ICC)
    macro_n_eff_conservative = min(
        macro_day_cluster_dir10["n_eff_day_block_se"],
        macro_day_cluster_up10["n_eff_day_block_se"],
        macro_day_cluster_dn10["n_eff_day_block_se"],
    )
    micro_n_eff_conservative = min(
        micro_day_cluster_exec_up["n_eff_day_block_se"],
        micro_day_cluster_abs_move["n_eff_day_block_se"],
    )

    macro_uniqueness_1m = round(len(strict_idxs) / 15.0, 1)
    macro_uniqueness_5m = round(len(idx_5m_strict) / 3.0, 1)

    if len(macro_micro_pairs_macro) > 10:
        mm_corr = float(np.corrcoef(macro_micro_pairs_macro, macro_micro_pairs_micro)[0, 1])
    else:
        mm_corr = 0.0

    p99_30t_sec = elapsed_summary["30_ticks_sec"]["overall"].get("p99", 60.0)
    phase_d_result = {
        "task": "G3.1_PHASE_D_INDEPENDENCE_AND_LEAKAGE_AUDIT",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "independence_methodology_note": (
            "Non-overlapping windows remove direct horizon overlap (return lag-1 ACF ~ 0) "
            "but DO NOT yield i.i.d. observations. Shared trading-day regimes, volatility clustering, "
            "and autocorrelated features inflate variance substantially. Both day-level block SE / ICC "
            "effective sample sizes and non-overlapping feature ACF are reported below."
        ),
        "macro_effective_sample_size": {
            "raw_1m_overlapping_15m_windows": int(len(strict_idxs)),
            "lopez_de_prado_avg_concurrency_1m": 15.0,
            "uniqueness_weighted_n_1m": macro_uniqueness_1m,
            "decision_grid_5m_windows": int(len(idx_5m_strict)),
            "lopez_de_prado_avg_concurrency_5m": 3.0,
            "uniqueness_weighted_n_5m": macro_uniqueness_5m,
            "nonoverlap_15m_windows": int(len(idx_15m_nonoverlap)),
            "unique_trading_days": unique_days_strict,
            "newey_west_1m_mid_ret_lag60": nw_macro_1m,
            "newey_west_5m_mid_ret_lag20": nw_macro_5m,
            "newey_west_nonoverlap_15m_mid_ret_lag10": nw_macro_15m_nonov,
            "newey_west_1m_y10_label_lag60": nw_macro_y10_1m,
            "newey_west_nonoverlap_15m_y10_label_lag10": nw_macro_y10_15m,
            "day_level_cluster_dependence": {
                "up_0.10pct_binary": macro_day_cluster_up10,
                "down_0.10pct_binary": macro_day_cluster_dn10,
                "directional_0.10pct_binary": macro_day_cluster_dir10,
                "mid_return_15m": macro_day_cluster_ret,
                "conservative_day_clustered_n_eff": round(float(macro_n_eff_conservative), 1),
            },
            "nonoverlap_feature_and_volatility_dependence": macro_feature_dependence,
        },
        "micro_effective_sample_size": {
            "raw_overlapping_30_tick_windows": micro_30_total,
            "lopez_de_prado_avg_concurrency": 30.0,
            "uniqueness_weighted_n": round(micro_30_total / 30.0, 1),
            "nonoverlap_30_tick_windows": micro_30_nonoverlap_total,
            "newey_west_nonoverlap_30_tick_lag10": acf_30t_nonov,
            "day_level_cluster_dependence": {
                "exec_up_gt0_binary": micro_day_cluster_exec_up,
                "abs_30t_move_usd": micro_day_cluster_abs_move,
                "mid_return_30t": micro_day_cluster_ret,
                "conservative_day_clustered_n_eff": round(float(micro_n_eff_conservative), 1),
            },
            "nonoverlap_feature_and_volatility_dependence": micro_feature_dependence,
            "unique_trading_days": unique_days_strict,
        },
        "h3b_selection_bias_and_clustering": {
            "g2_candidate_rows": 2482,
            "g2_independent_eligible_setups": 140,
            "h3b_executed_trades": 90,
            "selection_rate_vs_5m_windows_pct": round(100.0 * 140 / max(1, len(idx_5m_strict)), 3),
            "warning": "Training ML exclusively on H3b-filtered entries (140 independent setups / 90 trades) introduces severe selection bias and insufficient EPV for nonlinear models. Base directional Macro/Micro models must train on the full eligible continuous timeline (or regime-stratified decision grid) and evaluate conditioned on strategy entries separately.",
        },
        "macro_micro_dependency": {
            "sampled_concurrent_pairs": len(macro_micro_pairs_macro),
            "pearson_corr_concurrent_15m_vs_30t": round(mm_corr, 5),
            "implication": "Concurrent 30-tick Micro returns are embedded inside the active 15m Macro bar; Macro and Micro models MUST share the same chronological train/validation/test cutoffs so Micro validation ticks never leak future price direction into Macro training or vice versa.",
        },
        "pipeline_leakage_audit": [
            {
                "component": "TickRepository.get_ticks_as_df",
                "file": "src/goldedge/adapters/secondary/db/tick_repository.py",
                "status": "CLEAN",
                "detail": "Uses half-open [start, end) range (time >= start AND time < end) and strict bid>0 & ask>0 & bid<ask.",
            },
            {
                "component": "TickRepository.get_ticks & stream_ticks",
                "file": "src/goldedge/adapters/secondary/db/tick_repository.py",
                "status": "DEFECT_MINOR_BOUNDARY",
                "detail": "Uses inclusive [start, end] (time >= :start AND time <= :end) without strict bid<ask filter; chunked callers using get_ticks/stream_ticks risk duplicate boundary ticks at exact chunk boundaries.",
            },
            {
                "component": "PandasAggregatorRepository.aggregate_ticks_to_bars",
                "file": "src/goldedge/adapters/secondary/database/pandas_aggregator_repo.py",
                "status": "DEFECT_MINOR_BOUNDARY",
                "detail": "Uses inclusive time <= %(end)s and legacy market_bars_1m (only 20,175 rows vs 117k+ 1m buckets in market_ticks); offline ML must read market_ticks or market_bars_1m_quote_semantics_v1 directly, never legacy market_bars_1m.",
            },
            {
                "component": "RollingBarAggregator (1m -> 5m -> 1h)",
                "file": "src/goldedge/application/use_cases/run_backtest.py",
                "status": "CLEAN_CAUSAL",
                "detail": "Verified in G1: emits completed bars only when next bucket starts; zero look-ahead across 1m/5m/1h boundaries.",
            },
            {
                "component": "VolumeAdaptiveKalmanFilter",
                "file": "src/goldedge/domain/services/denoise_service.py",
                "status": "CLEAN_CAUSAL",
                "detail": "Strictly recursive tick-by-tick state update with 24h warmup; zero future leakage.",
            },
            {
                "component": "IndicatorService & RegimeDetectorService",
                "file": "src/goldedge/domain/services/indicator_service.py",
                "status": "CLEAN_CAUSAL_WITH_WARMUP",
                "detail": "Strictly backward-looking rolling/EMA calculations; requires >= 50 H1 bars (~50h) for H1 EMA50 and >= 100 5m bars (~8.3h) for ATR baseline.",
            },
            {
                "component": "MicrostructureFeatureRepository.compute_forex_micro_features",
                "file": "src/goldedge/adapters/secondary/features/microstructure_feature_repo.py",
                "status": "DEFECT_ZERO_SIGNAL",
                "detail": "Lee-Ready aggressor rule (price>=ask / price<=bid) always evaluates to 0 on midpoint quotes (last=(bid+ask)/2); unusable as an order-flow feature.",
            },
            {
                "component": "Global Scalers / Quantile Normalization",
                "file": "ML Pipeline Design Requirement",
                "status": "REQUIRED_GUARD",
                "detail": "All feature scalers, encoders, and neutral-band thresholds MUST be fit strictly inside each training fold, never across the full Aug-Nov 2026 history.",
            },
            {
                "component": "Research-Exposure Leakage (Aug-Nov 2026)",
                "file": "Governance",
                "status": "CRITICAL_GOVERNANCE_WARNING",
                "detail": "Aug 13 - Nov 19, 2026 has already been inspected in Tasks G1, G2, and G3.1. Any model trained and tuned on this window cannot claim untouched out-of-sample status; genuine out-of-sample proof requires fresh post-freeze forward shadow data (post-Nov 22, 2026) or newly acquired unseen historical months.",
            },
        ],
        "recommended_chronological_validation_framework": {
            "scheme": "Expanding-window or rolling Purged Walk-Forward CV with Embargo (never random K-fold)",
            "macro_purge_length_minutes": 15,
            "macro_embargo_length_minutes": 60,
            "macro_embargo_justification": "Purge=15m removes exact label overlap; Embargo=60m (12x 5m bars) damps short-term ATR/regime hysteresis and intraday return autocorrelation.",
            "micro_purge_length_seconds": round(max(60.0, float(p99_30t_sec)), 1),
            "micro_embargo_length_seconds": 300,
            "micro_embargo_justification": f"p99 elapsed time for 30 valid ticks is {p99_30t_sec}s; 300s (5m) embargo eliminates rolling-window and quote-burst persistence across fold boundaries.",
            "suggested_walk_forward_folds": [
                {"fold": 1, "train": "2026-08-20 .. 2026-09-25", "val": "2026-09-26 .. 2026-10-12"},
                {"fold": 2, "train": "2026-08-20 .. 2026-10-12", "val": "2026-10-13 .. 2026-10-30"},
                {"fold": 3, "train": "2026-08-20 .. 2026-10-30", "val": "2026-10-31 .. 2026-11-16"},
            ],
            "unseen_holdout_requirement": "Reserve post-freeze forward shadow data (after 2026-11-22) as untouched prospective OOS evaluation.",
        },
        "elapsed_sec": round(time.time() - t0, 2),
    }
    with open(OUT_DIR / "g3_1_independence_leakage_audit.json", "w", encoding="utf-8") as f:
        json.dump(phase_d_result, f, indent=2)
    print(f"[PHASE D DONE in {phase_d_result['elapsed_sec']}s]", flush=True)


if __name__ == "__main__":
    run_phases_bcd()
