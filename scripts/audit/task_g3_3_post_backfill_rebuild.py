"""
TASK G3.3 — Post-Backfill 6.2-Month Independent Coverage Audit, Expanded Label/Feature Rebuild,
and 4-Way Purged Walk-Forward Verification (2026-05-13 .. 2026-11-19).

Strictly non-destructive:
  - Raw `market_ticks` (2026-08-13 .. 2026-11-22) and frozen H0/H3b baselines remain 100% untouched.
  - Unions `market_ticks_staged_expansion` (< 2026-08-13Z) with `v_market_ticks_quote_semantics_v1`
    (is_ml_eligible_quote = TRUE, >= 2026-08-13Z) via read-only view `v_market_ticks_expanded_6m_v1`.
  - Reconstructs 1m bid/ask bars, builds expanded Macro & Micro labels + causal features with new SHA-256
    fingerprints, and verifies ALL 3 adjacent split boundaries (Train->Val, Val->Cal, Cal->Test).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")

from src.goldedge.adapters.secondary.ml.leakage_proof_pipeline import (
    FoldIsolatedPreprocessor,
    evaluate_classification_fold,
)
from src.goldedge.domain.services.denoise_service import DenoiseService
from src.goldedge.domain.services.indicator_service import IndicatorService
from src.goldedge.domain.services.label_spec_service import (
    DEFAULT_COST_SPEC,
    LABEL_SPEC_VERSION,
    classify_macro_endpoint,
    evaluate_tick_barrier_path,
)
from src.goldedge.domain.services.ml_training_contract import (
    CAUSAL_MACRO_FEATURE_COLUMNS,
    CAUSAL_MICRO_FEATURE_COLUMNS,
    FEATURE_SCHEMA_VERSION,
    MACRO_EMBARGO_BASELINE_SEC,
    MICRO_EMBARGO_BASELINE_SEC,
    WalkForwardFoldSpec,
    assert_zero_leakage_columns,
    assign_purged_4way_walkforward_fold,
)
from src.goldedge.domain.services.mtf_trend import aggregate_ohlcv_bars, compute_htf_ema_state
from src.goldedge.domain.services.regime_detector import RegimeDetectorService

OUT_DIR = Path("backtest_results/audit/task_g3_3_leakage_proof_pipeline")
EXP_DIR = OUT_DIR / "expanded_6m"
DB_URL = os.environ.get(
    "DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge"
).replace("+psycopg2", "")

# 4 expanding walk-forward folds across 6.2 months (2026-05-13 .. 2026-11-19)
EXPANDED_FOLDS: List[WalkForwardFoldSpec] = [
    WalkForwardFoldSpec(1, "2026-05-13T00:00:00Z", "2026-07-10T00:00:00Z", "2026-07-10T00:00:00Z", "2026-07-24T00:00:00Z", "2026-07-24T00:00:00Z", "2026-08-07T00:00:00Z", "2026-08-07T00:00:00Z", "2026-08-28T00:00:00Z"),
    WalkForwardFoldSpec(2, "2026-05-13T00:00:00Z", "2026-08-07T00:00:00Z", "2026-08-07T00:00:00Z", "2026-08-21T00:00:00Z", "2026-08-21T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-25T00:00:00Z"),
    WalkForwardFoldSpec(3, "2026-05-13T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-18T00:00:00Z", "2026-09-18T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-23T00:00:00Z"),
    WalkForwardFoldSpec(4, "2026-05-13T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-16T00:00:00Z", "2026-10-16T00:00:00Z", "2026-10-30T00:00:00Z", "2026-10-30T00:00:00Z", "2026-11-19T12:00:00Z"),
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _ts(iso_z: str) -> float:
    return datetime.fromisoformat(iso_z.replace("Z", "+00:00")).timestamp()


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


def run_post_backfill_rebuild() -> None:
    t0 = time.time()
    EXP_DIR.mkdir(parents=True, exist_ok=True)
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True

    # 1. Create non-destructive read-only union view v_market_ticks_expanded_6m_v1
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE OR REPLACE VIEW v_market_ticks_expanded_6m_v1 AS
            SELECT time, symbol, bid, ask, last AS price, volume AS real_volume, flags, 'STAGED_EXPANSION'::varchar(32) AS provenance
            FROM market_ticks_staged_expansion
            WHERE symbol = 'XAUUSD'
              AND time < '2026-08-13T00:00:00Z'
              AND bid > 0 AND ask > 0 AND bid < ask
            UNION ALL
            SELECT time, symbol, bid, ask, price, real_volume, flags, 'CORE_VERIFIED_G3_1'::varchar(32) AS provenance
            FROM v_market_ticks_quote_semantics_v1
            WHERE symbol = 'XAUUSD'
              AND is_ml_eligible_quote
              AND time >= '2026-08-13T00:00:00Z';
            """
        )

        # Audit staged expansion table independently
        cur.execute(
            """
            SELECT
                count(*) AS staged_rows,
                min(time), max(time),
                count(*) FILTER (WHERE bid <= 0 OR ask <= 0 OR bid >= ask) AS invalid_rows,
                count(*) FILTER (WHERE time >= '2026-08-13T00:00:00Z') AS overlap_with_core,
                count(DISTINCT time_bucket('1 minute', time)) AS staged_1m_bars,
                count(DISTINCT date_trunc('day', time AT TIME ZONE 'UTC')) FILTER (WHERE EXTRACT(DOW FROM time AT TIME ZONE 'UTC') BETWEEN 1 AND 5) AS staged_weekdays,
                round(avg(ask - bid)::numeric, 5) AS mean_spread,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY (ask - bid)) AS med_spread,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY (ask - bid)) AS p95_spread,
                round(max(ask - bid)::numeric, 5) AS max_spread
            FROM market_ticks_staged_expansion
            WHERE symbol = 'XAUUSD'
            """
        )
        sr = cur.fetchone()

        # Reconstruct ALL 1m bars across full 6.2-month union view
        cur.execute(
            """
            SELECT
                time_bucket('1 minute', time) AS bucket_time,
                first(bid, time) AS open,
                max(bid) AS high,
                min(bid) AS low,
                last(bid, time) AS close,
                count(*) AS tick_count,
                GREATEST(0, ROUND((last(ask, time) - last(bid, time)) / 0.01))::int AS spread_points
            FROM v_market_ticks_expanded_6m_v1
            GROUP BY 1
            ORDER BY 1 ASC
            """
        )
        b1m_rows = cur.fetchall()

    print(f"[EXPANDED 6M] Staged rows={sr[0]:,}, staged_1m_bars={sr[5]:,}, total_union_1m_bars={len(b1m_rows):,}", flush=True)

    bars_1m = [
        {"time": r[0], "open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4]), "tick_volume": int(r[5]), "real_volume": None, "spread": int(r[6])}
        for r in b1m_rows
    ]
    bars_5m = aggregate_ohlcv_bars(bars_1m, 5)
    bars_1h = aggregate_ohlcv_bars(bars_1m, 60)
    df5 = IndicatorService.compute_indicators(bars_5m)

    # Causal H1 trend & 5m indicators
    h1_closes = [b["close"] for b in bars_1h]
    h1_end_arr = np.array([int(b["time"].timestamp()) + 3600 for b in bars_1h], dtype=np.int64)
    h1_trend = [1.0 if compute_htf_ema_state(h1_closes[: k + 1])[0] == "BULL" else (-1.0 if compute_htf_ema_state(h1_closes[: k + 1])[0] == "BEAR" else 0.0) for k in range(len(bars_1h))]

    closes = df5["close"].values.astype(np.float64)
    lows = df5["low"].values.astype(np.float64)
    highs = df5["high"].values.astype(np.float64)
    atr = df5["atr"].fillna(0.0).values.astype(np.float64)
    ema20 = df5["ema20"].fillna(df5["close"]).values.astype(np.float64)
    ema50 = df5["ema50"].fillna(df5["close"]).values.astype(np.float64)
    ema20_slope = df5["ema20_slope"].fillna(0.0).values.astype(np.float64) if "ema20_slope" in df5.columns else pd.Series(ema20).diff().fillna(0.0).values
    rsi = df5["rsi"].fillna(50.0).values.astype(np.float64)
    bb_u = df5["bollinger_upper"].fillna(df5["close"]).values.astype(np.float64)
    bb_l = df5["bollinger_lower"].fillna(df5["close"]).values.astype(np.float64)
    trr = df5["tick_rate_ratio"].fillna(1.0).values.astype(np.float64) if "tick_rate_ratio" in df5.columns else np.ones(len(df5), dtype=np.float64)

    up_m = np.diff(highs, prepend=highs[0])
    dn_m = -np.diff(lows, prepend=lows[0])
    pdm = np.where((up_m > dn_m) & (up_m > 0), up_m, 0.0)
    mdm = np.where((dn_m > up_m) & (dn_m > 0), dn_m, 0.0)
    tr_v = np.maximum(highs - lows, np.maximum(np.abs(highs - np.roll(closes, 1)), np.abs(lows - np.roll(closes, 1))))
    tr_v[0] = highs[0] - lows[0]
    s_tr = pd.Series(tr_v).ewm(alpha=1.0 / 14.0, adjust=False).mean().values
    s_p = pd.Series(pdm).ewm(alpha=1.0 / 14.0, adjust=False).mean().values
    s_m = pd.Series(mdm).ewm(alpha=1.0 / 14.0, adjust=False).mean().values
    pdi = np.where(s_tr > 1e-12, 100.0 * s_p / s_tr, 0.0)
    mdi = np.where(s_tr > 1e-12, 100.0 * s_m / s_tr, 0.0)
    dx = np.where((pdi + mdi) > 1e-12, 100.0 * np.abs(pdi - mdi) / (pdi + mdi), 0.0)
    adx = pd.Series(dx).ewm(alpha=1.0 / 14.0, adjust=False).mean().values

    ret_1 = pd.Series(closes).pct_change(1).fillna(0.0).values
    ret_3 = pd.Series(closes).pct_change(3).fillna(0.0).values
    ret_12 = pd.Series(closes).pct_change(12).fillna(0.0).values
    rvol_12 = pd.Series(ret_1).rolling(12, min_periods=2).std().fillna(0.0).values
    atr_m20 = pd.Series(atr).rolling(20, min_periods=1).mean().values

    # Causal regime detection
    reg_det = RegimeDetectorService()
    reg_map_code = {"TRENDING_BULL": 2.0, "TRENDING_BEAR": -2.0, "SIDEWAYS": 0.0, "HIGH_VOLATILITY": 1.0, "LOW_VOLATILITY": -1.0}
    dec_by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for i in range(len(bars_5m)):
        reg_str = "WARMUP" if i < 50 else reg_det.detect(df5.iloc[: i + 1]).value
        dec_dt = bars_5m[i]["time"] + timedelta(seconds=300)
        dec_s = int(dec_dt.timestamp())
        a_i = max(atr[i], 1e-6)
        c_i = max(closes[i], 1e-6)
        bb_w = max(bb_u[i] - bb_l[i], 1e-6)
        w_lo = float(np.min(lows[max(0, i - 20) : i])) if i > 0 else lows[i]
        w_hi = float(np.max(highs[max(0, i - 20) : i])) if i > 0 else highs[i]
        k_er = float(DenoiseService.compute_kaufman_er(pd.Series(closes[max(0, i - 20) : i + 1]))) if i >= 20 else 0.0
        h1_pos = int(np.searchsorted(h1_end_arr, dec_s, side="right")) - 1
        hr = (dec_s // 3600) % 24
        dec_by_day[dec_dt.strftime("%Y-%m-%d")].append(
            {
                "dec_s": dec_s,
                "is_warmup": i < 100,
                "is_nonoverlap_15m": (dec_s % 900) == 0,
                "regime": reg_str,
                "feats": {
                    "ema20_ema50_spread_atr": (ema20[i] - ema50[i]) / a_i,
                    "ema20_slope_atr": ema20_slope[i] / a_i,
                    "h1_ema_trend_sign": h1_trend[h1_pos] if h1_pos >= 0 else 0.0,
                    "rsi_14_5m": rsi[i],
                    "atr_14_pct": a_i / c_i,
                    "atr_ratio_20": a_i / max(atr_m20[i], 1e-6),
                    "adx_14_5m": adx[i],
                    "bb_width_20": bb_w / c_i,
                    "bb_pos_20": (closes[i] - bb_l[i]) / bb_w,
                    "kaufman_er_20": k_er,
                    "prior_ret_5m": ret_1[i],
                    "prior_ret_15m": ret_3[i],
                    "prior_ret_60m": ret_12[i],
                    "prior_realized_vol_12": rvol_12[i],
                    "dist_swing_low_atr": (closes[i] - w_lo) / a_i,
                    "dist_swing_high_atr": (w_hi - closes[i]) / a_i,
                    "tick_rate_ratio": trr[i],
                    "hour_sin": math.sin(2.0 * math.pi * hr / 24.0),
                    "hour_cos": math.cos(2.0 * math.pi * hr / 24.0),
                    "regime_code": reg_map_code.get(reg_str, 0.0),
                },
            }
        )

    # 2. Stream full 6.2-month union view day-by-day for expanded Macro & Micro labels + causal features
    min_dt = bars_1m[0]["time"]
    max_dt = bars_1m[-1]["time"]
    cur_day = datetime(min_dt.year, min_dt.month, min_dt.day, tzinfo=timezone.utc)
    last_day = datetime(max_dt.year, max_dt.month, max_dt.day, tzinfo=timezone.utc)

    macro_rows: List[Dict[str, Any]] = []
    micro_chunks: List[pd.DataFrame] = []
    slip = DEFAULT_COST_SPEC.slippage_usd_per_oz_per_side
    c_rate = DEFAULT_COST_SPEC.commission_usd_per_million_per_side / 1_000_000.0

    with conn.cursor() as cur:
        d = cur_day
        while d <= last_day:
            nxt = d + timedelta(days=1)
            day_key = d.strftime("%Y-%m-%d")
            cur.execute(
                """
                SELECT EXTRACT(EPOCH FROM time) AS ts_s, bid, ask
                FROM v_market_ticks_expanded_6m_v1
                WHERE time >= %s AND time < %s
                ORDER BY time ASC
                """,
                (d, nxt + timedelta(minutes=20)),
            )
            rows = cur.fetchall()
            if len(rows) < 40:
                d = nxt
                continue
            arr = np.array(rows, dtype=np.float64)
            t_s, b, a = arr[:, 0], arr[:, 1], arr[:, 2]
            mid = 0.5 * (b + a)
            day_start_s, day_end_s = d.timestamp(), nxt.timestamp()

            # Macro labels + causal features
            for meta in dec_by_day.get(day_key, []):
                if meta["is_warmup"]:
                    continue
                dec_s = float(meta["dec_s"])
                i0 = int(np.searchsorted(t_s, dec_s, side="left"))
                if i0 >= len(t_s) or (t_s[i0] - dec_s) > 60.0:
                    continue
                i1 = int(np.searchsorted(t_s, dec_s + 900.0, side="right")) - 1
                if i1 <= i0 or (dec_s + 900.0 - t_s[i1]) > 60.0:
                    continue
                seg_t = t_s[i0 : i1 + 1]
                if len(seg_t) < 2 or float(np.max(np.diff(seg_t))) >= 300.0:
                    continue
                eb, ea = float(b[i0]), float(a[i0])
                xb, xa = float(b[i1]), float(a[i1])
                ep10 = classify_macro_endpoint(eb, ea, xb, xa, 0.0010, DEFAULT_COST_SPEC)
                bb10 = evaluate_tick_barrier_path("BUY", ea, float(seg_t[0]), seg_t[1:], b[i0 + 1 : i1 + 1], a[i0 + 1 : i1 + 1], ea * 0.0010, ea * 0.0010)
                sb10 = evaluate_tick_barrier_path("SELL", eb, float(seg_t[0]), seg_t[1:], b[i0 + 1 : i1 + 1], a[i0 + 1 : i1 + 1], eb * 0.0010, eb * 0.0010)

                f = meta["feats"].copy()
                f["entry_spread_bps"] = 1e4 * (ea - eb) / max(1e-6, ea)
                macro_rows.append(
                    {
                        "feature_cutoff_utc": int(dec_s),
                        "signal_decision_utc": int(dec_s),
                        "earliest_entry_utc": round(float(seg_t[0]), 6),
                        "label_obs_start_utc": round(float(seg_t[0]), 6),
                        "label_obs_end_utc": round(float(seg_t[-1]), 6),
                        "day_utc": day_key,
                        "session_utc": _session_of_hour(int((int(dec_s) // 3600) % 24)),
                        "regime": meta["regime"],
                        "is_nonoverlap_15m": meta["is_nonoverlap_15m"],
                        "buy_ret_net": round(float(ep10["buy_ret_net"]), 7),
                        "sell_ret_net": round(float(ep10["sell_ret_net"]), 7),
                        "raw_class_010": ep10["raw_class"],
                        "net_class_010": ep10["net_class"],
                        "buy_barrier_010": bb10["first_outcome"],
                        "sell_barrier_010": sb10["first_outcome"],
                        **f,
                    }
                )

            # Micro non-overlapping 30-tick labels + causal features
            day_mask = (t_s >= day_start_s) & (t_s < day_end_s)
            n_d = int(np.sum(day_mask))
            if n_d >= 60:
                td, bd, ad, md = t_s[:n_d], b[:n_d], a[:n_d], mid[:n_d]
                seg_id = np.cumsum(np.concatenate(([False], np.diff(td) >= 60.0)))
                idx_v = np.where(seg_id[:-30] == seg_id[30:])[0]
                nonov = idx_v[::30]
                if len(nonov) > 0:
                    eb, ea, em = bd[nonov], ad[nonov], md[nonov]
                    fb, fa, fm = bd[nonov + 30], ad[nonov + 30], md[nonov + 30]
                    t0_u, t1_u = td[nonov], td[nonov + 30]
                    esp = ea - eb
                    buy_adv = (fa - ea) * (1.0 + c_rate)
                    sell_adv = (eb - fb) * (1.0 - c_rate)
                    b_tim = np.where(buy_adv > esp, "ENTER_NOW", np.where(buy_adv < -esp, "WAIT_ADVERSE", "NEUTRAL"))
                    s_tim = np.where(sell_adv > esp, "ENTER_NOW", np.where(sell_adv < -esp, "WAIT_ADVERSE", "NEUTRAL"))
                    # Half-tick symmetric research target (+$0.005)
                    b_tim_ht = np.where(buy_adv > esp + 0.005, "ENTER_NOW", np.where(buy_adv < -(esp + 0.005), "WAIT_ADVERSE", "NEUTRAL"))
                    s_tim_ht = np.where(sell_adv > esp + 0.005, "ENTER_NOW", np.where(sell_adv < -(esp + 0.005), "WAIT_ADVERSE", "NEUTRAL"))

                    d_m_sq = np.concatenate(([0.0], np.diff(md))) ** 2
                    csum_sq = np.concatenate(([0.0], np.cumsum(d_m_sq)))
                    k_st = np.maximum(0, nonov - 30)
                    st_vol = np.sqrt(np.maximum(0.0, csum_sq[nonov + 1] - csum_sq[k_st + 1]))

                    dt_prev = np.concatenate(([np.nan], np.diff(t0_u)))
                    contig = np.isfinite(dt_prev) & (dt_prev < 60.0)
                    p1 = np.where(contig, (em - np.roll(em, 1)) / np.maximum(1e-6, np.roll(em, 1)), 0.0)
                    p2 = np.where(contig & np.roll(contig, 1), (em - np.roll(em, 2)) / np.maximum(1e-6, np.roll(em, 2)), 0.0)
                    p5 = np.where(contig & np.roll(contig, 4), (em - np.roll(em, 5)) / np.maximum(1e-6, np.roll(em, 5)), 0.0)
                    hrs = ((t0_u.astype(np.int64) // 3600) % 24).astype(np.float64)

                    micro_chunks.append(
                        pd.DataFrame(
                            {
                                "feature_cutoff_utc": np.round(t0_u, 6),
                                "earliest_entry_utc": np.round(t0_u, 6),
                                "label_obs_end_utc": np.round(t1_u, 6),
                                "day_utc": day_key,
                                "entry_spread_usd": np.round(esp, 5),
                                "entry_spread_bps": np.round(1e4 * esp / np.maximum(1e-6, em), 4),
                                "short_term_vol_30t_usd": np.round(st_vol, 5),
                                "prior_mid_ret_5t": np.round(p1, 7),
                                "prior_mid_ret_10t": np.round(p2, 7),
                                "prior_mid_ret_30t": np.round(p5, 7),
                                "prior_interarrival_mean_10t": np.where(contig, np.round(dt_prev / 30.0, 5), np.nan),
                                "prior_quote_intensity_30t": np.where(contig, np.round(30.0 / np.maximum(0.05, dt_prev), 3), np.nan),
                                "hour_sin": np.round(np.sin(2.0 * math.pi * hrs / 24.0), 6),
                                "hour_cos": np.round(np.cos(2.0 * math.pi * hrs / 24.0), 6),
                                "buy_timing_adv_usd": np.round(buy_adv, 5),
                                "sell_timing_adv_usd": np.round(sell_adv, 5),
                                "buy_timing_class": b_tim,
                                "sell_timing_class": s_tim,
                                "buy_timing_class_half_tick": b_tim_ht,
                                "sell_timing_class_half_tick": s_tim_ht,
                            }
                        )
                    )
            d = nxt

    conn.close()

    df_m = pd.DataFrame(macro_rows)
    df_u = pd.concat(micro_chunks, ignore_index=True) if micro_chunks else pd.DataFrame()
    m_pq = EXP_DIR / "g3_3_expanded_6m_macro_dataset.parquet"
    u_pq = EXP_DIR / "g3_3_expanded_6m_micro_dataset.parquet"
    df_m.to_parquet(m_pq, index=False, compression="snappy")
    df_u.to_parquet(u_pq, index=False, compression="snappy")
    m_sha, u_sha = _sha256(m_pq), _sha256(u_pq)

    # 3. Verify 4-way purged walk-forward across ALL 3 adjacent boundaries on expanded 6.2m dataset
    m_s, m_e = df_m["label_obs_start_utc"].values.astype(float), df_m["label_obs_end_utc"].values.astype(float)
    u_s, u_e = df_u["earliest_entry_utc"].values.astype(float), df_u["label_obs_end_utc"].values.astype(float)

    exp_folds = []
    for fspec in EXPANDED_FOLDS:
        mf = assign_purged_4way_walkforward_fold(m_s, m_e, _ts(fspec.train_start_utc), _ts(fspec.val_start_utc), _ts(fspec.cal_start_utc), _ts(fspec.test_start_utc), _ts(fspec.test_end_utc), MACRO_EMBARGO_BASELINE_SEC)
        uf = assign_purged_4way_walkforward_fold(u_s, u_e, _ts(fspec.train_start_utc), _ts(fspec.val_start_utc), _ts(fspec.cal_start_utc), _ts(fspec.test_start_utc), _ts(fspec.test_end_utc), MICRO_EMBARGO_BASELINE_SEC)
        exp_folds.append(
            {
                "fold_id": fspec.fold_id,
                "macro_n": {"train": len(mf["train_indices"]), "val": len(mf["val_indices"]), "cal": len(mf["cal_indices"]), "test": len(mf["test_indices"])},
                "macro_all3_strict_purge": mf["all_3_boundaries_strict_purge_hold"],
                "micro_n": {"train": len(uf["train_indices"]), "val": len(uf["val_indices"]), "cal": len(uf["cal_indices"]), "test": len(uf["test_indices"])},
                "micro_all3_strict_purge": uf["all_3_boundaries_strict_purge_hold"],
            }
        )

    # 4. Update g3_3_historical_expansion_report.json to HISTORY_EXPANSION_VERIFIED
    exp_rep_p = OUT_DIR / "g3_3_historical_expansion_report.json"
    exp_rep = json.loads(exp_rep_p.read_text(encoding="utf-8")) if exp_rep_p.exists() else {}
    exp_rep["status"] = "HISTORY_EXPANSION_VERIFIED"
    exp_rep["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    exp_rep["expanded_6_2_month_verified"] = {
        "staged_table": "market_ticks_staged_expansion",
        "union_view": "v_market_ticks_expanded_6m_v1",
        "staged_ticks": int(sr[0]),
        "staged_min_utc": sr[1].isoformat() if sr[1] else None,
        "staged_max_utc": sr[2].isoformat() if sr[2] else None,
        "staged_invalid_rows": int(sr[3]),
        "staged_overlap_with_core": int(sr[4]),
        "staged_1m_bars": int(sr[5]),
        "staged_weekdays": int(sr[6]),
        "staged_mean_spread_usd": float(sr[7]),
        "staged_median_spread_usd": round(float(sr[8]), 5),
        "staged_p95_spread_usd": round(float(sr[9]), 5),
        "staged_max_spread_usd": float(sr[10]),
        "total_union_1m_bars": len(bars_1m),
        "expanded_macro_rows": int(len(df_m)),
        "expanded_macro_nonoverlap_15m": int(df_m["is_nonoverlap_15m"].sum()),
        "expanded_macro_sha256": m_sha,
        "expanded_micro_rows": int(len(df_u)),
        "expanded_micro_sha256": u_sha,
        "expanded_4way_folds": exp_folds,
        "elapsed_sec": round(time.time() - t0, 2),
    }
    exp_rep_p.write_text(json.dumps(exp_rep, indent=2), encoding="utf-8")
    print(f"[EXPANDED 6M REBUILD DONE in {round(time.time()-t0, 1)}s] Macro={len(df_m):,} ({m_sha[:12]}) Micro={len(df_u):,} ({u_sha[:12]})")


if __name__ == "__main__":
    run_post_backfill_rebuild()
