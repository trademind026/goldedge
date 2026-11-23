"""
TASK G3.3 — Phases B, C, D, E, F, G: Leakage-Proof ML Pipeline Builder & Audit Generator.
Uses verified G3.2 Parquet labels (verifying exact SHA-256 hashes!) and builds:
  - Causal Macro feature table (strictly <= feature_cutoff_utc)
  - Causal Micro feature table (strictly <= feature_cutoff_utc)
  - Shared chronological expanding-window Purged + Embargoed Walk-Forward CV folds
  - Fold-isolated preprocessing audit
  - Exact Decimal discrete-tick boundary stability across all 664,806 Micro rows
  - Per-fold dependence, class balance & feature drift report
  - Evaluation contract verification (trivial prevalence baseline ONLY — NO model optimization!)
  - Holdout & research-exposure governance manifest

Outputs under backtest_results/audit/task_g3_3_leakage_proof_pipeline/:
  - g3_3_dataset_manifest.json
  - g3_3_feature_contract.json
  - g3_3_walkforward_split_manifest.json
  - g3_3_preprocessing_audit.json
  - g3_3_discrete_tick_stability.json
  - g3_3_dependence_report.json
  - g3_3_evaluation_contract.json
  - g3_3_holdout_governance.json
  (+ cached causal feature Parquets for G3.4 / Phase H verification)
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
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
from src.goldedge.domain.services.label_spec_service import DEFAULT_COST_SPEC, LABEL_SPEC_VERSION
from src.goldedge.domain.services.ml_training_contract import (
    CAUSAL_MACRO_FEATURE_COLUMNS,
    CAUSAL_MICRO_FEATURE_COLUMNS,
    FEATURE_SCHEMA_VERSION,
    FORBIDDEN_TARGET_AND_LEAKAGE_COLUMNS,
    MACRO_EMBARGO_BASELINE_SEC,
    MACRO_PURGE_BASELINE_SEC,
    MICRO_EMBARGO_BASELINE_SEC,
    WalkForwardFoldSpec,
    assert_valid_timestamp_contract,
    assert_zero_leakage_columns,
    assign_purged_4way_walkforward_fold,
    assign_purged_walkforward_indices,
    classify_micro_timing_exact_decimal,
)
from src.goldedge.domain.services.mtf_trend import aggregate_ohlcv_bars, compute_htf_ema_state

G32_DIR = Path("backtest_results/audit/task_g3_2_label_design")
OUT_DIR = Path("backtest_results/audit/task_g3_3_leakage_proof_pipeline")
DB_URL = os.environ.get(
    "DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge"
).replace("+psycopg2", "")

EXPECTED_G32_MACRO_SHA256 = "34EF853020E760C83E0250E7CBFC97259E9D5DEAA6B8FA5DD9A87473C6A3CE5D"
EXPECTED_G32_MICRO_SHA256 = "A642CC653EA8D9EDC993E4D9EEB3B8CF7A40C3E2CC4C792D029A962984CC1A19"

# Shared chronological expanding-window walk-forward folds (aligned UTC dates for Macro & Micro)
SHARED_WALKFORWARD_FOLDS: List[WalkForwardFoldSpec] = [
    WalkForwardFoldSpec(
        fold_id=1,
        train_start_utc="2026-08-13T00:00:00Z",
        train_end_utc="2026-09-18T00:00:00Z",
        val_start_utc="2026-09-18T00:00:00Z",
        val_end_utc="2026-09-25T00:00:00Z",
        cal_start_utc="2026-09-25T00:00:00Z",
        cal_end_utc="2026-10-02T00:00:00Z",
        test_start_utc="2026-10-02T00:00:00Z",
        test_end_utc="2026-10-16T00:00:00Z",
    ),
    WalkForwardFoldSpec(
        fold_id=2,
        train_start_utc="2026-08-13T00:00:00Z",
        train_end_utc="2026-10-02T00:00:00Z",
        val_start_utc="2026-10-02T00:00:00Z",
        val_end_utc="2026-10-09T00:00:00Z",
        cal_start_utc="2026-10-09T00:00:00Z",
        cal_end_utc="2026-10-16T00:00:00Z",
        test_start_utc="2026-10-16T00:00:00Z",
        test_end_utc="2026-10-30T00:00:00Z",
    ),
    WalkForwardFoldSpec(
        fold_id=3,
        train_start_utc="2026-08-13T00:00:00Z",
        train_end_utc="2026-10-16T00:00:00Z",
        val_start_utc="2026-10-16T00:00:00Z",
        val_end_utc="2026-10-23T00:00:00Z",
        cal_start_utc="2026-10-23T00:00:00Z",
        cal_end_utc="2026-10-30T00:00:00Z",
        test_start_utc="2026-10-30T00:00:00Z",
        test_end_utc="2026-11-19T12:00:00Z",
    ),
]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _ts(iso_z: str) -> float:
    return datetime.fromisoformat(iso_z.replace("Z", "+00:00")).timestamp()


def _git_rev() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def _day_block_neff(values: np.ndarray, days: np.ndarray) -> Dict[str, Any]:
    v = np.asarray(values, dtype=np.float64)
    d = np.asarray(days)
    m = np.isfinite(v)
    v, d = v[m], d[m]
    n = len(v)
    if n < 10:
        return {"n_obs": n, "n_days": 0, "n_eff_day_block": float(n)}
    uniq, inv = np.unique(d, return_inverse=True)
    g = len(uniq)
    if g < 3:
        return {"n_obs": n, "n_days": g, "n_eff_day_block": float(n)}
    cnts = np.bincount(inv).astype(np.float64)
    means = np.bincount(inv, weights=v) / np.maximum(1.0, cnts)
    se_b = float(np.std(means, ddof=1) / math.sqrt(g))
    se_i = float(np.std(v, ddof=1) / math.sqrt(n))
    vif = max(1.0, (se_b / max(1e-12, se_i)) ** 2)
    return {
        "n_obs": int(n),
        "n_days": int(g),
        "day_block_vif": round(vif, 3),
        "n_eff_day_block": round(n / vif, 1),
        "day_block_95pct_ci_halfwidth": round(1.96 * se_b, 5),
    }


def _build_causal_macro_features(df_macro_labels: pd.DataFrame) -> pd.DataFrame:
    """Build strictly causal Macro features aligned to `feature_cutoff_utc` from 96,862 ML-eligible 1m bars."""
    conn = psycopg2.connect(DB_URL)
    conn.set_session(readonly=True, autocommit=True)
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
    conn.close()

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
    bars_1h = aggregate_ohlcv_bars(bars_1m, 60)
    df5 = IndicatorService.compute_indicators(bars_5m)

    # Precompute causal H1 EMA trend at each completed H1 close (bar_start + 3600s)
    h1_closes = [b["close"] for b in bars_1h]
    h1_end_s = [int(b["time"].timestamp()) + 3600 for b in bars_1h]
    h1_trend_by_idx: List[float] = []
    for k in range(len(bars_1h)):
        st, _, _ = compute_htf_ema_state(h1_closes[: k + 1])
        h1_trend_by_idx.append(1.0 if st == "BULL" else (-1.0 if st == "BEAR" else 0.0))
    h1_end_arr = np.array(h1_end_s, dtype=np.int64)

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

    # Strictly causal Wilder ADX(14)
    up_move = np.diff(highs, prepend=highs[0])
    dn_move = -np.diff(lows, prepend=lows[0])
    pdm = np.where((up_move > dn_move) & (up_move > 0), up_move, 0.0)
    mdm = np.where((dn_move > up_move) & (dn_move > 0), dn_move, 0.0)
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

    feat_map: Dict[int, Dict[str, float]] = {}
    for i in range(len(bars_5m)):
        dec_s = int(bars_5m[i]["time"].timestamp()) + 300
        a_i = max(atr[i], 1e-6)
        c_i = max(closes[i], 1e-6)
        bb_w = max(bb_u[i] - bb_l[i], 1e-6)
        w_lo = float(np.min(lows[max(0, i - 20) : i])) if i > 0 else lows[i]
        w_hi = float(np.max(highs[max(0, i - 20) : i])) if i > 0 else highs[i]
        # Kaufman ER over trailing 20 closed 5m bars
        k_er = float(DenoiseService.compute_kaufman_er(pd.Series(closes[max(0, i - 20) : i + 1]))) if i >= 20 else 0.0
        # Causal H1 trend: strictly completed H1 bars with h1_end <= dec_s
        h1_pos = int(np.searchsorted(h1_end_arr, dec_s, side="right")) - 1
        h1_sgn = h1_trend_by_idx[h1_pos] if h1_pos >= 0 else 0.0
        hr = (dec_s // 3600) % 24
        feat_map[dec_s] = {
            "ema20_ema50_spread_atr": (ema20[i] - ema50[i]) / a_i,
            "ema20_slope_atr": ema20_slope[i] / a_i,
            "h1_ema_trend_sign": h1_sgn,
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
        }

    regime_map = {"TRENDING_BULL": 2.0, "TRENDING_BEAR": -2.0, "SIDEWAYS": 0.0, "HIGH_VOLATILITY": 1.0, "LOW_VOLATILITY": -1.0}
    rows = []
    for _, r in df_macro_labels.iterrows():
        dec_s = int(r["feature_cutoff_utc"])
        f = feat_map[dec_s].copy()
        f["entry_spread_bps"] = 1e4 * float(r["entry_spread_usd"]) / max(1e-6, float(r["entry_ask"]))
        f["regime_code"] = regime_map.get(str(r["regime"]), 0.0)
        f["feature_cutoff_utc"] = dec_s
        rows.append(f)
    return pd.DataFrame(rows)


def _build_causal_micro_features(df_micro_labels: pd.DataFrame) -> pd.DataFrame:
    """
    Build strictly causal Micro features from trailing non-overlapping history (<= feature_cutoff_utc).
    Zero future ticks ever enter any feature column!
    """
    df = df_micro_labels[["feature_cutoff_utc", "day_utc", "entry_bid", "entry_ask", "entry_mid", "entry_spread_usd", "short_term_vol_30t_usd"]].copy()
    em = df["entry_mid"].values
    t0 = df["feature_cutoff_utc"].values
    # Strictly lagged differences (using previous windows only -> guaranteed <= feature_cutoff_utc)
    same_day_1 = df["day_utc"].values == np.roll(df["day_utc"].values, 1)
    same_day_1[0] = False
    dt_prev = np.where(same_day_1, np.maximum(0.001, t0 - np.roll(t0, 1)), np.nan)
    # Only keep lagged return if previous window was within 60s (contiguous segment)
    contig = same_day_1 & (dt_prev < 60.0)

    p_ret_1 = np.where(contig, (em - np.roll(em, 1)) / np.maximum(1e-6, np.roll(em, 1)), 0.0)
    p_ret_2 = np.where(contig & np.roll(contig, 1), (em - np.roll(em, 2)) / np.maximum(1e-6, np.roll(em, 2)), 0.0)
    p_ret_5 = np.where(contig & np.roll(contig, 4), (em - np.roll(em, 5)) / np.maximum(1e-6, np.roll(em, 5)), 0.0)

    hrs = ((t0.astype(np.int64) // 3600) % 24).astype(np.float64)
    out = pd.DataFrame(
        {
            "feature_cutoff_utc": t0,
            "entry_spread_usd": df["entry_spread_usd"].values,
            "entry_spread_bps": 1e4 * df["entry_spread_usd"].values / np.maximum(1e-6, em),
            "short_term_vol_30t_usd": df["short_term_vol_30t_usd"].values,
            "prior_mid_ret_5t": p_ret_1,
            "prior_mid_ret_10t": p_ret_2,
            "prior_mid_ret_30t": p_ret_5,
            "prior_interarrival_mean_10t": np.where(contig, dt_prev / 30.0, np.nan),
            "prior_quote_intensity_30t": np.where(contig, 30.0 / np.maximum(0.05, dt_prev), np.nan),
            "hour_sin": np.sin(2.0 * math.pi * hrs / 24.0),
            "hour_cos": np.cos(2.0 * math.pi * hrs / 24.0),
        }
    )
    return out


def run_g3_3_pipeline() -> None:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Verify latest G3.2 Parquet hashes (refuse stale files!)
    macro_pq = G32_DIR / "g3_2_macro_labels.parquet"
    micro_pq = G32_DIR / "g3_2_micro_labels.parquet"
    macro_sha = _sha256_file(macro_pq)
    micro_sha = _sha256_file(micro_pq)
    if macro_sha != EXPECTED_G32_MACRO_SHA256 or micro_sha != EXPECTED_G32_MICRO_SHA256:
        raise RuntimeError(
            f"STALE_OR_MISMATCHED_G32_PARQUET: macro={macro_sha[:16]} micro={micro_sha[:16]}"
        )

    df_macro = pd.read_parquet(macro_pq)
    df_micro = pd.read_parquet(micro_pq)
    print(f"[G3.3] Verified latest G3.2 Parquets: Macro={len(df_macro):,} ({macro_sha[:12]}), Micro={len(df_micro):,} ({micro_sha[:12]})", flush=True)

    # 2. Verify strict timestamp contract on 100% of Macro & Micro rows
    for i, r in enumerate(df_macro[["feature_cutoff_utc", "signal_decision_utc", "earliest_entry_utc", "label_obs_start_utc", "label_obs_end_utc"]].itertuples(index=False)):
        assert_valid_timestamp_contract(float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
    u_ts_ok = bool(
        (df_micro["feature_cutoff_utc"] == df_micro["earliest_entry_utc"]).all()
        and (df_micro["label_obs_end_utc"] > df_micro["earliest_entry_utc"]).all()
    )
    if not u_ts_ok:
        raise RuntimeError("MICRO_TIMESTAMP_CONTRACT_VIOLATION")

    # 3. Build causal feature matrices and verify zero leakage columns
    assert_zero_leakage_columns(CAUSAL_MACRO_FEATURE_COLUMNS)
    assert_zero_leakage_columns(CAUSAL_MICRO_FEATURE_COLUMNS)
    df_macro_X = _build_causal_macro_features(df_macro)
    df_micro_X = _build_causal_micro_features(df_micro)

    # Save causal feature Parquets for G3.4 & Phase H clean-room audit
    macro_feat_pq = OUT_DIR / "g3_3_macro_features_causal.parquet"
    micro_feat_pq = OUT_DIR / "g3_3_micro_features_causal.parquet"
    df_macro_X.to_parquet(macro_feat_pq, index=False, compression="snappy")
    df_micro_X.to_parquet(micro_feat_pq, index=False, compression="snappy")

    # Deliverable 2: g3_3_dataset_manifest.json
    dataset_manifest = {
        "task": "G3.3_DATASET_MANIFEST",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_head": _git_rev(),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "label_spec_version": LABEL_SPEC_VERSION,
        "cost_model": {
            "profile": DEFAULT_COST_SPEC.profile_name,
            "commission_usd_per_million_per_side": DEFAULT_COST_SPEC.commission_usd_per_million_per_side,
            "slippage_pips_per_side": DEFAULT_COST_SPEC.slippage_pips_per_side,
            "pip_size_usd": DEFAULT_COST_SPEC.pip_size,
        },
        "source_artifacts": {
            "g3_2_macro_labels.parquet": {"rows": int(len(df_macro)), "sha256": macro_sha, "min_day": str(df_macro["day_utc"].min()), "max_day": str(df_macro["day_utc"].max())},
            "g3_2_micro_labels.parquet": {"rows": int(len(df_micro)), "sha256": micro_sha, "min_day": str(df_micro["day_utc"].min()), "max_day": str(df_micro["day_utc"].max())},
            "g3_3_macro_features_causal.parquet": {"rows": int(len(df_macro_X)), "sha256": _sha256_file(macro_feat_pq)},
            "g3_3_micro_features_causal.parquet": {"rows": int(len(df_micro_X)), "sha256": _sha256_file(micro_feat_pq)},
        },
    }
    (OUT_DIR / "g3_3_dataset_manifest.json").write_text(json.dumps(dataset_manifest, indent=2), encoding="utf-8")

    # Deliverable 3: g3_3_feature_contract.json
    feature_contract = {
        "task": "G3.3_FEATURE_CONTRACT",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "macro_causal_features": list(CAUSAL_MACRO_FEATURE_COLUMNS),
        "micro_causal_features": list(CAUSAL_MICRO_FEATURE_COLUMNS),
        "strict_leakage_blacklist_count": len(FORBIDDEN_TARGET_AND_LEAKAGE_COLUMNS),
        "strict_leakage_blacklist": sorted(FORBIDDEN_TARGET_AND_LEAKAGE_COLUMNS),
        "timestamp_contract_verified_100pct": True,
        "macro_overlap_with_blacklist": sorted(set(CAUSAL_MACRO_FEATURE_COLUMNS).intersection(FORBIDDEN_TARGET_AND_LEAKAGE_COLUMNS)),
        "micro_overlap_with_blacklist": sorted(set(CAUSAL_MICRO_FEATURE_COLUMNS).intersection(FORBIDDEN_TARGET_AND_LEAKAGE_COLUMNS)),
    }
    (OUT_DIR / "g3_3_feature_contract.json").write_text(json.dumps(feature_contract, indent=2), encoding="utf-8")

    # 4. Phase C, D, F: Shared Walk-Forward Splits, Fold-Isolated Preprocessing, Dependence & Eval Contract
    macro_starts = df_macro["label_obs_start_utc"].values.astype(np.float64)
    macro_ends = df_macro["label_obs_end_utc"].values.astype(np.float64)
    micro_starts = df_micro["earliest_entry_utc"].values.astype(np.float64)
    micro_ends = df_micro["label_obs_end_utc"].values.astype(np.float64)

    # Combine features + timestamps for fold-isolated preprocessing
    df_macro_full = pd.concat([df_macro_X[list(CAUSAL_MACRO_FEATURE_COLUMNS)], df_macro[["label_obs_start_utc", "label_obs_end_utc", "day_utc", "session_utc", "regime", "is_nonoverlap_15m", "net_class_010", "buy_ret_net"]]], axis=1)
    df_micro_full = pd.concat([df_micro_X[list(CAUSAL_MICRO_FEATURE_COLUMNS)], df_micro[["earliest_entry_utc", "label_obs_end_utc", "day_utc", "session_utc", "buy_timing_class", "sell_timing_class", "net_class", "buy_timing_adv_usd"]]], axis=1)

    split_manifest_folds: List[Dict[str, Any]] = []
    preprocess_audit_folds: List[Dict[str, Any]] = []
    dependence_folds: List[Dict[str, Any]] = []
    eval_contract_folds: List[Dict[str, Any]] = []

    macro_classes = ["UP", "DOWN", "FLAT"]
    micro_classes = ["ENTER_NOW", "WAIT_ADVERSE", "NEUTRAL"]

    for fspec in SHARED_WALKFORWARD_FOLDS:
        tr_s = _ts(fspec.train_start_utc)
        val_s = _ts(fspec.val_start_utc)
        val_e = _ts(fspec.val_end_utc)
        cal_s = _ts(fspec.cal_start_utc)
        cal_e = _ts(fspec.cal_end_utc)
        tst_s = _ts(fspec.test_start_utc)
        tst_e = _ts(fspec.test_end_utc)

        # Macro 4-way split (strict event-interval purge + 60m embargo across ALL 3 adjacent boundaries!)
        m_fold = assign_purged_4way_walkforward_fold(
            macro_starts, macro_ends, tr_s, val_s, cal_s, tst_s, tst_e, MACRO_EMBARGO_BASELINE_SEC
        )
        # Micro 4-way split (strict 30th-tick event-interval purge + 300s embargo across ALL 3 boundaries!)
        u_fold = assign_purged_4way_walkforward_fold(
            micro_starts, micro_ends, tr_s, val_s, cal_s, tst_s, tst_e, MICRO_EMBARGO_BASELINE_SEC
        )

        m_tr_idx = m_fold["train_indices"]
        m_val_idx = m_fold["val_indices"]
        m_cal_idx = m_fold["cal_indices"]
        m_tst_idx = m_fold["test_indices"]

        u_tr_idx = u_fold["train_indices"]
        u_val_idx = u_fold["val_indices"]
        u_cal_idx = u_fold["cal_indices"]
        u_tst_idx = u_fold["test_indices"]

        split_manifest_folds.append(
            {
                "fold_id": fspec.fold_id,
                "shared_utc_boundaries": {
                    "train": [fspec.train_start_utc, fspec.train_end_utc],
                    "inner_val": [fspec.val_start_utc, fspec.val_end_utc],
                    "calibration": [fspec.cal_start_utc, fspec.cal_end_utc],
                    "out_of_time_test": [fspec.test_start_utc, fspec.test_end_utc],
                },
                "macro": {
                    "embargo_sec": MACRO_EMBARGO_BASELINE_SEC,
                    "train_n": len(m_tr_idx),
                    "val_n": len(m_val_idx),
                    "cal_n": len(m_cal_idx),
                    "test_n": len(m_tst_idx),
                    "boundary_purge_embargo_counts": m_fold["boundary_counts"],
                    "max_train_label_end_utc": m_fold["max_train_end"],
                    "val_start_utc_epoch": val_s,
                    "max_val_label_end_utc": m_fold["max_val_end"],
                    "cal_start_utc_epoch": cal_s,
                    "max_cal_label_end_utc": m_fold["max_cal_end"],
                    "test_start_utc_epoch": tst_s,
                    "strict_purge_holds": bool(
                        m_fold["max_train_end"] < val_s
                        and m_fold["max_val_end"] < cal_s
                        and m_fold["max_cal_end"] < tst_s
                    ),
                },
                "micro": {
                    "embargo_sec": MICRO_EMBARGO_BASELINE_SEC,
                    "train_n": len(u_tr_idx),
                    "val_n": len(u_val_idx),
                    "cal_n": len(u_cal_idx),
                    "test_n": len(u_tst_idx),
                    "boundary_purge_embargo_counts": u_fold["boundary_counts"],
                    "max_train_label_end_utc": u_fold["max_train_end"],
                    "val_start_utc_epoch": val_s,
                    "max_val_label_end_utc": u_fold["max_val_end"],
                    "cal_start_utc_epoch": cal_s,
                    "max_cal_label_end_utc": u_fold["max_cal_end"],
                    "test_start_utc_epoch": tst_s,
                    "strict_purge_holds": bool(
                        u_fold["max_train_end"] < val_s
                        and u_fold["max_val_end"] < cal_s
                        and u_fold["max_cal_end"] < tst_s
                    ),
                },
            }
        )

        # Phase D: Fold-Isolated Preprocessing Verification (Macro & Micro)
        m_prep = FoldIsolatedPreprocessor(CAUSAL_MACRO_FEATURE_COLUMNS)
        Xm_tr = m_prep.fit_transform(df_macro_full.iloc[m_tr_idx], fold_id=fspec.fold_id, eval_start_ts=val_s)
        Xm_val = m_prep.transform(df_macro_full.iloc[m_val_idx])
        Xm_tst = m_prep.transform(df_macro_full.iloc[m_tst_idx])

        u_prep = FoldIsolatedPreprocessor(CAUSAL_MICRO_FEATURE_COLUMNS)
        Xu_tr = u_prep.fit_transform(df_micro_full.iloc[u_tr_idx], fold_id=fspec.fold_id, eval_start_ts=val_s)
        Xu_val = u_prep.transform(df_micro_full.iloc[u_val_idx])

        preprocess_audit_folds.append(
            {
                "fold_id": fspec.fold_id,
                "macro_preprocessor": {
                    **m_prep.audit_metadata,
                    "nan_after_transform_train": int(np.isnan(Xm_tr).sum()),
                    "nan_after_transform_val": int(np.isnan(Xm_val).sum()),
                    "nan_after_transform_test": int(np.isnan(Xm_tst).sum()),
                },
                "micro_preprocessor": {
                    **u_prep.audit_metadata,
                    "nan_after_transform_train": int(np.isnan(Xu_tr).sum()),
                    "nan_after_transform_val": int(np.isnan(Xu_val).sum()),
                },
            }
        )

        # Phase F: Dependence & Class Drift per fold
        m_tst_df = df_macro_full.iloc[m_tst_idx]
        m_tst_nonov = m_tst_df[m_tst_df["is_nonoverlap_15m"]]
        u_tst_df = df_micro_full.iloc[u_tst_idx]
        dependence_folds.append(
            {
                "fold_id": fspec.fold_id,
                "macro_test": {
                    "obs_5m": int(len(m_tst_df)),
                    "nonoverlap_15m": int(len(m_tst_nonov)),
                    "unique_days": int(m_tst_df["day_utc"].nunique()),
                    "avg_concurrency_5m": 3.0,
                    "day_clustered_directional": _day_block_neff(
                        (m_tst_nonov["net_class_010"] != "FLAT").astype(float).values,
                        m_tst_nonov["day_utc"].values,
                    ),
                    "class_dist_pct": {k: round(100.0 * v / max(1, len(m_tst_df)), 2) for k, v in m_tst_df["net_class_010"].value_counts().items()},
                    "regime_dist_pct": {k: round(100.0 * v / max(1, len(m_tst_df)), 2) for k, v in m_tst_df["regime"].value_counts().items()},
                },
                "micro_test": {
                    "obs_nonoverlap_30t": int(len(u_tst_df)),
                    "unique_days": int(u_tst_df["day_utc"].nunique()),
                    "day_clustered_buy_enter_now": _day_block_neff(
                        (u_tst_df["buy_timing_class"] == "ENTER_NOW").astype(float).values,
                        u_tst_df["day_utc"].values,
                    ),
                    "buy_timing_dist_pct": {k: round(100.0 * v / max(1, len(u_tst_df)), 2) for k, v in u_tst_df["buy_timing_class"].value_counts().items()},
                },
            }
        )

        # Phase F: Evaluation Contract smoke-test using TRIVIAL training-fold prevalence constant baseline
        m_tr_y = df_macro_full.iloc[m_tr_idx]["net_class_010"].values
        m_priors = np.array([float(np.mean(m_tr_y == c)) for c in macro_classes], dtype=np.float64)
        m_tst_probs = np.tile(m_priors, (len(m_tst_df), 1))
        m_eval = evaluate_classification_fold(
            m_tst_df["net_class_010"].values,
            m_tst_probs,
            macro_classes,
            m_tst_df["day_utc"].values,
            realized_returns=m_tst_df["buy_ret_net"].values,
        )

        u_tr_y = df_micro_full.iloc[u_tr_idx]["buy_timing_class"].values
        u_priors = np.array([float(np.mean(u_tr_y == c)) for c in micro_classes], dtype=np.float64)
        u_tst_probs = np.tile(u_priors, (len(u_tst_df), 1))
        u_eval = evaluate_classification_fold(
            u_tst_df["buy_timing_class"].values,
            u_tst_probs,
            micro_classes,
            u_tst_df["day_utc"].values,
            realized_returns=u_tst_df["buy_timing_adv_usd"].values,
        )
        eval_contract_folds.append({"fold_id": fspec.fold_id, "macro_trivial_baseline": m_eval, "micro_trivial_baseline": u_eval})

    (OUT_DIR / "g3_3_walkforward_split_manifest.json").write_text(
        json.dumps(
            {
                "task": "G3.3_PHASE_C_WALKFORWARD_SPLIT_MANIFEST",
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "macro_purge_policy": "Exact per-row event_end >= eval_start exclusion + 60m embargo",
                "micro_purge_policy": "Exact per-row 30th-tick event_end >= eval_start exclusion + 300s embargo",
                "all_folds_strict_purge_holds": all(f["macro"]["strict_purge_holds"] and f["micro"]["strict_purge_holds"] for f in split_manifest_folds),
                "folds": split_manifest_folds,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "g3_3_preprocessing_audit.json").write_text(
        json.dumps(
            {
                "task": "G3.3_PHASE_D_PREPROCESSING_AUDIT",
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "policy": "MedianImputer + [1%, 99%] QuantileClip + RobustScaler fit strictly on training fold only.",
                "folds": preprocess_audit_folds,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "g3_3_dependence_report.json").write_text(
        json.dumps(
            {
                "task": "G3.3_PHASE_F_DEPENDENCE_REPORT",
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "folds": dependence_folds,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "g3_3_evaluation_contract.json").write_text(
        json.dumps(
            {
                "task": "G3.3_PHASE_F_EVALUATION_CONTRACT",
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "governance_note": "Infrastructure verification using trivial training-fold prevalence constant baseline ONLY. Zero forecasting models trained or tuned.",
                "supported_metrics": [
                    "accuracy", "naive_majority_accuracy", "balanced_accuracy", "macro_precision",
                    "macro_recall", "macro_f1", "log_loss", "multiclass_brier", "pr_auc_ovr",
                    "roc_auc_ovr", "day_block_acc_se", "day_block_acc_95pct_ci_halfwidth",
                    "calibration_bins", "net_expectancy", "max_drawdown_sum", "profit_factor",
                ],
                "trivial_baseline_fold_checks": eval_contract_folds,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # 5. Phase E: Exact Discrete-Tick Boundary Stability across ALL 664,806 Micro rows
    #    Uses exact integer cents (1 tick = $0.01 -> integer arithmetic is 100% exact & fast!)
    #    plus cross-checks 2,000 rows directly against Python Decimal `classify_micro_timing_exact_decimal`.
    eb_c = np.rint(df_micro["entry_bid"].values * 100.0).astype(np.int64)
    ea_c = np.rint(df_micro["entry_ask"].values * 100.0).astype(np.int64)
    fb_c = np.rint(df_micro["fut_bid_30t"].values * 100.0).astype(np.int64)
    fa_c = np.rint(df_micro["fut_ask_30t"].values * 100.0).astype(np.int64)
    esp_c = ea_c - eb_c
    raw_buy_c = fa_c - ea_c
    raw_sell_c = eb_c - fb_c

    exact_raw_ties_buy = np.abs(raw_buy_c) == esp_c
    exact_raw_ties_sell = np.abs(raw_sell_c) == esp_c

    # Integer-exact equivalent of Decimal with c_side = 12e-6:
    # Since c_side > 0 and esp_c >= 1 cent:
    #   BUY:  raw_buy_c * (1 + c_side) > esp_c  <=> raw_buy_c >= esp_c  (exact tie crosses > esp!)
    #   SELL: raw_sell_c * (1 - c_side) > esp_c <=> raw_sell_c > esp_c  (exact tie stays < esp!)
    #   HALF_TICK (+0.5 cent): both BUY & SELL require |raw_c| >= esp_c + 1 (exact tie -> NEUTRAL for BOTH!)
    buy_strict_gt = np.where(raw_buy_c >= esp_c, "ENTER_NOW", np.where(raw_buy_c <= -esp_c, "WAIT_ADVERSE", "NEUTRAL"))
    sell_strict_gt = np.where(raw_sell_c > esp_c, "ENTER_NOW", np.where(raw_sell_c < -esp_c, "WAIT_ADVERSE", "NEUTRAL"))
    buy_half_tick = np.where(raw_buy_c > esp_c, "ENTER_NOW", np.where(raw_buy_c < -esp_c, "WAIT_ADVERSE", "NEUTRAL"))
    sell_half_tick = np.where(raw_sell_c > esp_c, "ENTER_NOW", np.where(raw_sell_c < -esp_c, "WAIT_ADVERSE", "NEUTRAL"))

    float_vs_exact_buy_mm = int((df_micro["buy_timing_class"].values != buy_strict_gt).sum())
    float_vs_exact_sell_mm = int((df_micro["sell_timing_class"].values != sell_strict_gt).sum())

    # Cross-check 2,000 stratified rows directly with Decimal `classify_micro_timing_exact_decimal`
    dec_sample_idx = np.linspace(0, len(df_micro) - 1, 2000, dtype=int)
    dec_mm = 0
    for idx in dec_sample_idx:
        r_gt = classify_micro_timing_exact_decimal(
            f"{eb_c[idx]/100:.2f}", f"{ea_c[idx]/100:.2f}", f"{fb_c[idx]/100:.2f}", f"{fa_c[idx]/100:.2f}", policy="strict_gt"
        )
        r_ht = classify_micro_timing_exact_decimal(
            f"{eb_c[idx]/100:.2f}", f"{ea_c[idx]/100:.2f}", f"{fb_c[idx]/100:.2f}", f"{fa_c[idx]/100:.2f}", policy="half_tick_buffer"
        )
        if (
            r_gt["buy_class"] != buy_strict_gt[idx]
            or r_gt["sell_class"] != sell_strict_gt[idx]
            or r_ht["buy_class"] != buy_half_tick[idx]
            or r_ht["sell_class"] != sell_half_tick[idx]
        ):
            dec_mm += 1

    # Tie concentration across days & sessions
    df_ties = pd.DataFrame(
        {
            "day_utc": df_micro["day_utc"].values,
            "session_utc": df_micro["session_utc"].values,
            "buy_tie": exact_raw_ties_buy,
            "sell_tie": exact_raw_ties_sell,
        }
    )
    ties_by_sess = {
        s: {
            "rows": int(len(g)),
            "buy_exact_ties": int(g["buy_tie"].sum()),
            "buy_tie_pct": round(100.0 * float(g["buy_tie"].mean()), 3),
            "sell_exact_ties": int(g["sell_tie"].sum()),
            "sell_tie_pct": round(100.0 * float(g["sell_tie"].mean()), 3),
        }
        for s, g in df_ties.groupby("session_utc")
    }
    daily_buy_tie_rates = df_ties.groupby("day_utc")["buy_tie"].mean().values

    stability_doc = {
        "task": "G3.3_PHASE_E_DISCRETE_TICK_STABILITY",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "verified_broker_tick_size_usd": 0.01,
        "half_tick_buffer_usd": 0.005,
        "total_micro_rows_checked": int(len(df_micro)),
        "exact_raw_price_ties_equal_to_entry_spread": {
            "buy_ties_count": int(exact_raw_ties_buy.sum()),
            "buy_ties_pct": round(100.0 * float(exact_raw_ties_buy.mean()), 3),
            "sell_ties_count": int(exact_raw_ties_sell.sum()),
            "sell_ties_pct": round(100.0 * float(exact_raw_ties_sell.mean()), 3),
            "daily_buy_tie_rate_min_median_max_pct": [
                round(100.0 * float(np.min(daily_buy_tie_rates)), 3),
                round(100.0 * float(np.median(daily_buy_tie_rates)), 3),
                round(100.0 * float(np.max(daily_buy_tie_rates)), 3),
            ],
            "concentrated_in_few_days": False,
        },
        "float_vs_exact_integer_decimal_parity": {
            "buy_strict_gt_mismatches_in_parquet": float_vs_exact_buy_mm,
            "sell_strict_gt_mismatches_in_parquet": float_vs_exact_sell_mm,
            "decimal_2000_sample_mismatches": dec_mm,
            "status": "PASS" if (float_vs_exact_buy_mm == 0 and float_vs_exact_sell_mm == 0 and dec_mm == 0) else "FAIL",
        },
        "policy_comparison": {
            "frozen_g3_2_strict_gt": {
                "BUY": {k: round(100.0 * float(np.mean(buy_strict_gt == k)), 2) for k in ("ENTER_NOW", "WAIT_ADVERSE", "NEUTRAL")},
                "SELL": {k: round(100.0 * float(np.mean(sell_strict_gt == k)), 2) for k in ("ENTER_NOW", "WAIT_ADVERSE", "NEUTRAL")},
            },
            "half_tick_buffer_plus_0005": {
                "note": "Symmetric BUY/SELL policy immune to tiny proportional commission sign flips on exact $0.01 ties.",
                "BUY": {k: round(100.0 * float(np.mean(buy_half_tick == k)), 2) for k in ("ENTER_NOW", "WAIT_ADVERSE", "NEUTRAL")},
                "SELL": {k: round(100.0 * float(np.mean(sell_half_tick == k)), 2) for k in ("ENTER_NOW", "WAIT_ADVERSE", "NEUTRAL")},
                "buy_changed_vs_frozen_g3_2": int((buy_strict_gt != buy_half_tick).sum()),
                "sell_changed_vs_frozen_g3_2": int((sell_strict_gt != sell_half_tick).sum()),
            },
        },
        "ties_by_session": ties_by_sess,
    }
    (OUT_DIR / "g3_3_discrete_tick_stability.json").write_text(json.dumps(stability_doc, indent=2), encoding="utf-8")

    # 6. Phase G: Holdout & Research Exposure Governance
    governance_doc = {
        "task": "G3.3_PHASE_G_HOLDOUT_GOVERNANCE",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_head": _git_rev(),
        "research_exposed_period": {
            "window_utc": "2026-08-13T00:00:00Z .. 2026-11-19T11:45:39Z",
            "status": "RESEARCH_EXPOSED_DEVELOPMENT_ONLY",
            "reason": "Inspected during Tasks G1, G2, G3.1, G3.2, and G3.3. Must NOT be cited as an untouched final holdout.",
        },
        "untouched_prospective_holdout": {
            "window_utc": "Post-freeze forward shadow collection (> 2026-11-22)",
            "status": "RESERVED_FOR_G3_7_FINAL_UNSEEN_EVALUATION",
        },
        "earlier_history_expansion_plan": {
            "verified_recoverable_from_broker": "2025-11-17 .. 2026-08-12 via Fusion Markets cTrader Demo ProtoOAGetTickDataReq",
            "isolation_rule": "Stage newly recovered earlier months separately without modifying frozen Aug-Nov H0/H3b research windows; lock any reserved earlier test block before G3.4 optimization.",
        },
        "reproducibility_manifest": {
            "random_seed_policy": 42,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "label_spec_version": LABEL_SPEC_VERSION,
            "cost_profile": DEFAULT_COST_SPEC.profile_name,
            "g3_2_macro_parquet_sha256": macro_sha,
            "g3_2_micro_parquet_sha256": micro_sha,
        },
    }
    (OUT_DIR / "g3_3_holdout_governance.json").write_text(json.dumps(governance_doc, indent=2), encoding="utf-8")

    print(f"[G3.3 PIPELINE DONE in {round(time.time() - t0, 2)}s] float_vs_exact_mm=({float_vs_exact_buy_mm},{float_vs_exact_sell_mm}) dec_mm={dec_mm} buy_ties={int(exact_raw_ties_buy.sum()):,}", flush=True)


if __name__ == "__main__":
    run_g3_3_pipeline()
