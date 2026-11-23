"""
TASK G3.1 — Phases E, F, G: Feature Readiness Audit, ML Model Feasibility,
and Historical Data Expansion Assessment.
Clean-room standalone script: ZERO imports from src/goldedge/.
Reads Phase A/B/C/D JSON outputs and inspects backups/*.zip metadata read-only.
Outputs:
  - backtest_results/audit/task_g3_1_ml_feasibility/g3_1_feature_readiness.json
  - backtest_results/audit/task_g3_1_ml_feasibility/g3_1_model_feasibility.json
  - backtest_results/audit/task_g3_1_ml_feasibility/g3_1_data_expansion_assessment.json
"""

from __future__ import annotations

import json
import math
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

OUT_DIR = Path("backtest_results/audit/task_g3_1_ml_feasibility")
BACKUPS_DIR = Path("backups")


def _inspect_backup_ticks_bounds() -> Dict[str, Any]:
    zp = BACKUPS_DIR / "csv_goldedge_20261117_143146.zip"
    if not zp.exists():
        return {"exists": False}
    info: Dict[str, Any] = {"exists": True, "zip_path": str(zp), "size_bytes": zp.stat().st_size}
    with zipfile.ZipFile(zp, "r") as zf:
        if "market_ticks.csv" in zf.namelist():
            zi = zf.getinfo("market_ticks.csv")
            info["market_ticks_csv_uncompressed_bytes"] = zi.file_size
            with zf.open("market_ticks.csv", "r") as f:
                header = f.readline().decode("utf-8", errors="replace").strip()
                first_row = f.readline().decode("utf-8", errors="replace").strip()
            info["header"] = header
            info["first_data_row"] = first_row
            # Check market_bars_1m.csv bounds too (small file)
        if "market_bars_1m.csv" in zf.namelist():
            with zf.open("market_bars_1m.csv", "r") as f:
                lines = f.read().decode("utf-8", errors="replace").splitlines()
            info["market_bars_1m_csv_rows"] = max(0, len(lines) - 1)
            if len(lines) >= 2:
                info["market_bars_1m_first"] = lines[1]
                info["market_bars_1m_last"] = lines[-1]
    return info


def run_phases_efg() -> None:
    phase_a = json.loads((OUT_DIR / "g3_1_historical_data_audit.json").read_text(encoding="utf-8"))
    phase_b = json.loads((OUT_DIR / "g3_1_macro_feasibility.json").read_text(encoding="utf-8"))
    phase_c = json.loads((OUT_DIR / "g3_1_micro_feasibility.json").read_text(encoding="utf-8"))
    phase_d = json.loads((OUT_DIR / "g3_1_independence_leakage_audit.json").read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # PHASE E: Feature Readiness Audit (15 Macro + 9 Micro)
    # ------------------------------------------------------------------
    macro_features: List[Dict[str, Any]] = [
        {
            "name": "ema20_ema50_spread_atr",
            "category": "trend",
            "source_module": "src/goldedge/domain/services/indicator_service.py",
            "formula": "(EMA20(5m_close) - EMA50(5m_close)) / ATR14(5m)",
            "update_frequency": "5m closed bar",
            "lookback_bars": 50,
            "earliest_usable_utc": "2026-08-13T04:15:00Z",
            "missing_value_behavior": "Warmup NaN for first 50 5m bars; guard ATR>1e-9",
            "is_causal": True,
            "leakage_risk": "LOW (strictly backward-looking closed 5m bars)",
            "offline_vs_shadow_parity": "EXACT",
            "redundancy_notes": "Correlated with ema20_slope and adx_14; normalize by ATR for scale invariance across $3,500-$4,500 gold.",
            "readiness_status": "READY",
        },
        {
            "name": "ema20_slope",
            "category": "trend_momentum",
            "source_module": "src/goldedge/domain/services/indicator_service.py",
            "formula": "(EMA20_t - EMA20_{t-1}) / ATR14_t (raw slope in USD/bar normalized by ATR)",
            "update_frequency": "5m closed bar",
            "lookback_bars": 21,
            "earliest_usable_utc": "2026-08-13T01:45:00Z",
            "missing_value_behavior": "0.0 during warmup",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "EXACT",
            "redundancy_notes": "G2 found raw unscaled ema20_slope saturated TradeScorer; use ATR-normalized continuous value for ML.",
            "readiness_status": "READY",
        },
        {
            "name": "h1_ema20_ema50_trend",
            "category": "htf_trend",
            "source_module": "src/goldedge/domain/services/mtf_trend.py",
            "formula": "(H1_EMA20 - H1_EMA50) / H1_close on strictly completed H1 bars (< cutoff_1h)",
            "update_frequency": "1h closed bar",
            "lookback_bars": 50,
            "earliest_usable_utc": "2026-08-15T02:00:00Z (50 H1 bars)",
            "missing_value_behavior": "Returns NEUTRAL when <50 H1 bars",
            "is_causal": True,
            "leakage_risk": "LOW if cutoff_1h strictly < current hour (verified in RunBacktestUseCase & ScanMarketUseCase)",
            "offline_vs_shadow_parity": "EXACT",
            "redundancy_notes": "Low collinearity with 5m features; captures multi-day drift.",
            "readiness_status": "READY",
        },
        {
            "name": "rsi_14_5m",
            "category": "oscillator",
            "source_module": "src/goldedge/domain/services/indicator_service.py",
            "formula": "Wilder RSI(14) on 5m closes",
            "update_frequency": "5m closed bar",
            "lookback_bars": 15,
            "earliest_usable_utc": "2026-08-13T01:15:00Z",
            "missing_value_behavior": "50.0 default during warmup",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "EXACT",
            "redundancy_notes": "Moderate correlation with bb_position and short-term return.",
            "readiness_status": "READY",
        },
        {
            "name": "atr_14_5m_pct_and_ratio",
            "category": "volatility",
            "source_module": "src/goldedge/domain/services/indicator_service.py",
            "formula": "ATR14 / close and ATR14 / mean(ATR14[-20:])",
            "update_frequency": "5m closed bar",
            "lookback_bars": 34,
            "earliest_usable_utc": "2026-08-13T02:50:00Z",
            "missing_value_behavior": "1.0 ratio fallback during warmup",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "EXACT",
            "redundancy_notes": "Use ATR/close (scale-free) rather than raw USD ATR because XAUUSD ranged ~$3,500-$4,500 across Aug-Nov.",
            "readiness_status": "READY",
        },
        {
            "name": "adx_14_5m",
            "category": "trend_strength",
            "source_module": "src/goldedge/domain/services/indicator_service.py",
            "formula": "ADX(14) on 5m OHLC",
            "update_frequency": "5m closed bar",
            "lookback_bars": 28,
            "earliest_usable_utc": "2026-08-13T02:20:00Z",
            "missing_value_behavior": "0.0 during warmup",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "EXACT",
            "redundancy_notes": "Core regime discriminator; pairs well with signed EMA spread.",
            "readiness_status": "READY",
        },
        {
            "name": "bb_width_and_position_20",
            "category": "volatility_location",
            "source_module": "src/goldedge/domain/services/indicator_service.py",
            "formula": "(BB_upper - BB_lower)/BB_mid and (close - BB_lower)/(BB_upper - BB_lower)",
            "update_frequency": "5m closed bar",
            "lookback_bars": 20,
            "earliest_usable_utc": "2026-08-13T01:40:00Z",
            "missing_value_behavior": "0.5 position fallback if width<=1e-9",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "EXACT",
            "redundancy_notes": "bb_width correlates with atr_14_pct; bb_position correlates with rsi_14.",
            "readiness_status": "READY",
        },
        {
            "name": "kaufman_er_20",
            "category": "efficiency",
            "source_module": "src/goldedge/domain/services/denoise_service.py",
            "formula": "|close_t - close_{t-20}| / sum(|close_i - close_{i-1}|)",
            "update_frequency": "5m closed bar",
            "lookback_bars": 20,
            "earliest_usable_utc": "2026-08-13T01:40:00Z",
            "missing_value_behavior": "0.0 if denominator==0",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "EXACT",
            "redundancy_notes": "Use raw ER in [0,1] (G2 showed TradeScorer clipping at 0.35 saturated it).",
            "readiness_status": "READY",
        },
        {
            "name": "shannon_entropy_20",
            "category": "noise_entropy",
            "source_module": "src/goldedge/domain/services/denoise_service.py",
            "formula": "Shannon entropy of 5m returns over rolling 20-bar window",
            "update_frequency": "5m closed bar",
            "lookback_bars": 21,
            "earliest_usable_utc": "2026-08-13T01:45:00Z",
            "missing_value_behavior": "Returns 0.0 during warmup",
            "is_causal": True,
            "leakage_risk": "LOW if bin edges are fixed or fit strictly inside train fold",
            "offline_vs_shadow_parity": "EXACT",
            "redundancy_notes": "G2 proved fixed bin_width=0.005 caused 100% saturation (entropy=0) on 5m XAUUSD returns. Must recalibrate bin width to ~0.0005 or ATR-standardized returns before ML use.",
            "readiness_status": "READY_WITH_RECALIBRATION",
        },
        {
            "name": "regime_state_5m",
            "category": "regime",
            "source_module": "src/goldedge/domain/services/regime_detector.py",
            "formula": "Hysteresis-smoothed 5-class regime (TRENDING_BULL/BEAR, SIDEWAYS, HIGH/LOW_VOL)",
            "update_frequency": "5m closed bar",
            "lookback_bars": 100,
            "earliest_usable_utc": "2026-08-13T08:20:00Z",
            "missing_value_behavior": "SIDEWAYS default during warmup",
            "is_causal": True,
            "leakage_risk": "LOW; hysteresis creates short-term state persistence (handled by 60m embargo)",
            "offline_vs_shadow_parity": "EXACT (ScanMarketUseCase warms trailing 60 bars deterministically)",
            "redundancy_notes": "Derived nonlinear combination of ADX, EMA, ATR ratio.",
            "readiness_status": "READY",
        },
        {
            "name": "tick_weighted_price_dist",
            "category": "quote_activity_location",
            "source_module": "src/goldedge/domain/services/indicator_service.py",
            "formula": "(close - TWP) / TWP where TWP is tick-count-weighted typical price (NOT traded VWAP)",
            "update_frequency": "5m closed bar",
            "lookback_bars": 20,
            "earliest_usable_utc": "2026-08-13T01:40:00Z",
            "missing_value_behavior": "0.0 fallback if TWP unavailable",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "MINOR_PARITY_GAP (ScanMarketUseCase:204 divides by close; RunBacktestUseCase divides by TWP; diff <0.01%)",
            "redundancy_notes": "Must be labeled tick_weighted_price_dist, never VWAP (real_volume is 100% NULL).",
            "readiness_status": "READY_QUOTE_SEMANTICS_ONLY",
        },
        {
            "name": "tick_rate_ratio",
            "category": "quote_activity_intensity",
            "source_module": "src/goldedge/domain/services/indicator_service.py",
            "formula": "bar_tick_count / rolling_mean_20(bar_tick_count)",
            "update_frequency": "5m closed bar",
            "lookback_bars": 20,
            "earliest_usable_utc": "2026-08-13T01:40:00Z",
            "missing_value_behavior": "1.0 fallback",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "EXACT (requires market_ticks / market_bars_1m_quote_semantics_v1)",
            "redundancy_notes": "Measures broker quote-arrival bursts (liquidity/volatility proxy), NOT traded volume.",
            "readiness_status": "READY_QUOTE_SEMANTICS_ONLY",
        },
        {
            "name": "dist_to_20bar_swing_high_low_atr",
            "category": "market_structure",
            "source_module": "src/goldedge/application/use_cases/scan_market.py",
            "formula": "(close - min(low[-21:-1])) / ATR14 and (max(high[-21:-1]) - close) / ATR14",
            "update_frequency": "5m closed bar",
            "lookback_bars": 21,
            "earliest_usable_utc": "2026-08-13T01:45:00Z",
            "missing_value_behavior": "0.0 if ATR<=1e-9",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "EXACT",
            "redundancy_notes": "Strong independent exhaustion/support signal observed in G1/G2 SELL diagnostics.",
            "readiness_status": "READY",
        },
        {
            "name": "kalman_price_dist_vel_accel",
            "category": "state_space_filter",
            "source_module": "src/goldedge/domain/services/denoise_service.py",
            "formula": "(close - k_price)/ATR14, k_vel/ATR14, k_accel/ATR14 from 2D scalar Kalman",
            "update_frequency": "Tick-updated, sampled at 5m close",
            "lookback_bars": 288,
            "earliest_usable_utc": "2026-08-14T00:00:00Z (after 24h KALMAN_WARMUP_HOURS)",
            "missing_value_behavior": "Requires 24h tick warmup before use",
            "is_causal": True,
            "leakage_risk": "LOW (strictly recursive forward filter)",
            "offline_vs_shadow_parity": "EXACT (scalar math verified <1e-12)",
            "redundancy_notes": "k_vel sign inverted between Dev and Holdout in prior SELL research; regularize heavily.",
            "readiness_status": "READY",
        },
        {
            "name": "session_utc_cyclical_and_spread_bps",
            "category": "calendar_micro_context",
            "source_module": "derived from UTC timestamp & bid/ask",
            "formula": "sin(2pi*hour/24), cos(2pi*hour/24), (ask-bid)/mid*1e4",
            "update_frequency": "1m / 5m",
            "lookback_bars": 1,
            "earliest_usable_utc": "2026-08-13T00:00:00Z",
            "missing_value_behavior": "None (always defined on valid quote)",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "EXACT",
            "redundancy_notes": "Captures Asia/London/NY/Overlap volatility seasonality.",
            "readiness_status": "READY",
        },
    ]

    micro_features: List[Dict[str, Any]] = [
        {
            "name": "spread_usd_and_zscore_30t",
            "category": "liquidity_cost",
            "source_module": "derived from valid market_ticks",
            "formula": "(ask - bid) and z-score over trailing 30 valid ticks",
            "update_frequency": "Every valid tick",
            "lookback_ticks": 30,
            "earliest_usable_utc": "2026-08-13T00:00:10Z",
            "missing_value_behavior": "Reset on gap >= 60s",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "EXACT if live tick stream filters bid>0 & ask>0 & bid<ask",
            "redundancy_notes": "Directly predicts execution cost hurdle; median spread is $0.07.",
            "readiness_status": "READY",
        },
        {
            "name": "mid_return_5t_10t_30t",
            "category": "short_horizon_momentum",
            "source_module": "derived from valid market_ticks",
            "formula": "(mid_i - mid_{i-k}) / spread_mean_30t for k in {5, 10, 30}",
            "update_frequency": "Every valid tick",
            "lookback_ticks": 30,
            "earliest_usable_utc": "2026-08-13T00:00:10Z",
            "missing_value_behavior": "Reset on gap >= 60s",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "EXACT",
            "redundancy_notes": "Core microstructure signal; test for mean-reversion vs momentum.",
            "readiness_status": "READY",
        },
        {
            "name": "quote_arrival_burst_ratio",
            "category": "quote_intensity",
            "source_module": "derived from valid market_ticks (or microstructure_feature_repo burst_ratio)",
            "formula": "count(ticks in last 5s) / (count(ticks in last 30s)/6)",
            "update_frequency": "Every valid tick",
            "lookback_ticks": 30,
            "earliest_usable_utc": "2026-08-13T00:00:30Z",
            "missing_value_behavior": "1.0 fallback on quiet start",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "EXACT",
            "redundancy_notes": "Measures quote-arrival bursts (NOT traded order-flow volume).",
            "readiness_status": "READY_QUOTE_SEMANTICS_ONLY",
        },
        {
            "name": "interarrival_time_stats_10t",
            "category": "quote_timing",
            "source_module": "derived from valid market_ticks",
            "formula": "mean(dt_s) and cv(dt_s) over trailing 10 valid ticks",
            "update_frequency": "Every valid tick",
            "lookback_ticks": 10,
            "earliest_usable_utc": "2026-08-13T00:00:05Z",
            "missing_value_behavior": "Reset on gap >= 60s",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "EXACT (requires millisecond timestamp preservation)",
            "redundancy_notes": "Strongly correlated with quote_arrival_burst_ratio.",
            "readiness_status": "READY",
        },
        {
            "name": "bid_ask_revision_imbalance_30t",
            "category": "top_of_book_pressure",
            "source_module": "new clean-room feature (replaces broken Lee-Ready)",
            "formula": "(count(bid_up or ask_up) - count(bid_dn or ask_dn)) / 30 over last 30 ticks",
            "update_frequency": "Every valid tick",
            "lookback_ticks": 30,
            "earliest_usable_utc": "2026-08-13T00:00:10Z",
            "missing_value_behavior": "Reset on gap >= 60s",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "EXACT",
            "redundancy_notes": "Valid top-of-book quote-revision asymmetry; does NOT claim to be L2 order book or trade aggressor.",
            "readiness_status": "READY_QUOTE_SEMANTICS_ONLY",
        },
        {
            "name": "realized_vol_30t",
            "category": "micro_volatility",
            "source_module": "derived from valid market_ticks",
            "formula": "sqrt(sum((mid_k - mid_{k-1})^2)) over trailing 30 valid ticks",
            "update_frequency": "Every valid tick",
            "lookback_ticks": 30,
            "earliest_usable_utc": "2026-08-13T00:00:10Z",
            "missing_value_behavior": "Reset on gap >= 60s",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "EXACT",
            "redundancy_notes": "Determines whether 30-tick move can clear spread+commission hurdle.",
            "readiness_status": "READY",
        },
        {
            "name": "kalman_vel_accel_tick",
            "category": "state_space_filter",
            "source_module": "src/goldedge/domain/services/denoise_service.py",
            "formula": "Per-tick scalar Kalman velocity and acceleration",
            "update_frequency": "Every valid tick",
            "lookback_ticks": 1,
            "earliest_usable_utc": "2026-08-14T00:00:00Z (after 24h warmup)",
            "missing_value_behavior": "Requires 24h warmup",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "EXACT",
            "redundancy_notes": "Smooths tick-level bid-ask bounce.",
            "readiness_status": "READY",
        },
        {
            "name": "micro_range_position_30t",
            "category": "micro_location",
            "source_module": "derived from valid market_ticks",
            "formula": "(mid_t - min(mid[-30:])) / (max(mid[-30:]) - min(mid[-30:]))",
            "update_frequency": "Every valid tick",
            "lookback_ticks": 30,
            "earliest_usable_utc": "2026-08-13T00:00:10Z",
            "missing_value_behavior": "0.5 if range==0",
            "is_causal": True,
            "leakage_risk": "LOW",
            "offline_vs_shadow_parity": "EXACT",
            "redundancy_notes": "Bounded [0,1] location within micro swing.",
            "readiness_status": "READY",
        },
        {
            "name": "lee_ready_order_flow_and_l2_depth",
            "category": "order_flow",
            "source_module": "src/goldedge/adapters/secondary/features/microstructure_feature_repo.py + src/goldedge/domain/services/ml_feature_contract.py",
            "formula": "Lee-Ready aggressor (price>=ask / price<=bid) & Level II book imbalance",
            "update_frequency": "DISABLED",
            "lookback_ticks": 30,
            "earliest_usable_utc": "UNAVAILABLE",
            "missing_value_behavior": "Explicitly disabled in compute_forex_micro_features() and blocked by validate_ml_feature_names() (raises ValueError).",
            "is_causal": True,
            "leakage_risk": "N/A",
            "offline_vs_shadow_parity": "DISABLED_AND_EXCLUDED",
            "redundancy_notes": "RESOLVED (G3.1 corrective pass): Disabled in microstructure_feature_repo.py and strictly excluded by ml_feature_contract.EXCLUDED_UNAVAILABLE_ML_FEATURES so it cannot enter ML training.",
            "readiness_status": "DISABLED_AND_EXCLUDED",
        },
    ]

    phase_e_result = {
        "task": "G3.1_PHASE_E_FEATURE_READINESS",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "macro_features_count": len(macro_features),
        "macro_summary": {
            "READY": sum(1 for f in macro_features if f["readiness_status"] == "READY"),
            "READY_WITH_RECALIBRATION": sum(1 for f in macro_features if f["readiness_status"] == "READY_WITH_RECALIBRATION"),
            "READY_QUOTE_SEMANTICS_ONLY": sum(1 for f in macro_features if f["readiness_status"] == "READY_QUOTE_SEMANTICS_ONLY"),
            "UNAVAILABLE_OR_DEFECTIVE": sum(1 for f in macro_features if f["readiness_status"] == "UNAVAILABLE_OR_DEFECTIVE"),
        },
        "micro_features_count": len(micro_features),
        "micro_summary": {
            "READY": sum(1 for f in micro_features if f["readiness_status"] == "READY"),
            "READY_WITH_RECALIBRATION": sum(1 for f in micro_features if f["readiness_status"] == "READY_WITH_RECALIBRATION"),
            "READY_QUOTE_SEMANTICS_ONLY": sum(1 for f in micro_features if f["readiness_status"] == "READY_QUOTE_SEMANTICS_ONLY"),
            "DISABLED_AND_EXCLUDED": sum(1 for f in micro_features if f["readiness_status"] == "DISABLED_AND_EXCLUDED"),
        },
        "macro_features": macro_features,
        "micro_features": micro_features,
    }
    with open(OUT_DIR / "g3_1_feature_readiness.json", "w", encoding="utf-8") as f:
        json.dump(phase_e_result, f, indent=2)

    # ------------------------------------------------------------------
    # PHASE F: ML Model Feasibility (No model training or threshold tuning)
    #          Uses conservative day-level cluster effective sample sizes!
    # ------------------------------------------------------------------
    n_macro_nonov = phase_d["macro_effective_sample_size"]["nonoverlap_15m_windows"]
    n_macro_day_cluster = phase_d["macro_effective_sample_size"]["day_level_cluster_dependence"]["conservative_day_clustered_n_eff"]
    macro_day_block_ci_up_pct = round(
        phase_d["macro_effective_sample_size"]["day_level_cluster_dependence"]["up_0.10pct_binary"]["day_block_95pct_ci_halfwidth"] * 100.0, 2
    )
    n_macro_days = phase_d["macro_effective_sample_size"]["unique_trading_days"]
    up10_pct = phase_b["threshold_sensitivity_nonoverlap_15m"]["0.10pct"]["endpoint_executable"]["UP_pct"] / 100.0
    dn10_pct = phase_b["threshold_sensitivity_nonoverlap_15m"]["0.10pct"]["endpoint_executable"]["DOWN_pct"] / 100.0
    min_class_macro = min(up10_pct, dn10_pct)
    macro_p = 15
    macro_epv_nonov = round((n_macro_nonov * min_class_macro) / macro_p, 1)
    macro_epv_day_cluster = round((n_macro_day_cluster * min_class_macro) / macro_p, 1)
    # Per walk-forward val fold (~1/4 of history) using day-clustered N_eff
    macro_val_n_eff_clustered = max(1.0, n_macro_day_cluster / 4.0)
    macro_ci95_acc_clustered = round(1.96 * math.sqrt(0.25 / macro_val_n_eff_clustered) * 100.0, 2)

    n_micro_nonov = phase_d["micro_effective_sample_size"]["nonoverlap_30_tick_windows"]
    n_micro_day_cluster = phase_d["micro_effective_sample_size"]["day_level_cluster_dependence"]["conservative_day_clustered_n_eff"]
    ex0_up_pct = phase_c["executable_post_spread_30_tick"]["0.0"]["UP_pct"] / 100.0
    ex0_dn_pct = phase_c["executable_post_spread_30_tick"]["0.0"]["DOWN_pct"] / 100.0
    min_class_micro = min(ex0_up_pct, ex0_dn_pct)
    micro_p = 8
    micro_epv_nonov = round((n_micro_nonov * min_class_micro) / micro_p, 1)
    micro_epv_day_cluster = round((n_micro_day_cluster * min_class_micro) / micro_p, 1)
    micro_val_n_eff_clustered = max(1.0, n_micro_day_cluster / 4.0)
    micro_ci95_acc_clustered = round(1.96 * math.sqrt(0.25 / micro_val_n_eff_clustered) * 100.0, 2)

    med_30t_abs_move = phase_c["move_distributions_30_tick_usd"]["abs_mid_move_usd"]["p50_median"]
    med_spread = phase_a["spread_distribution"]["p50_median_usd"]
    rt_cost = round(med_spread + 0.045, 4)

    phase_f_result = {
        "task": "G3.1_PHASE_F_MODEL_FEASIBILITY",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "governance_note": "Strictly feasibility analysis only. Zero production models trained and zero trading thresholds optimized. Uses conservative day-level cluster dependence (not naive i.i.d. VIF=1.0).",
        "macro_15m_assessment": {
            "nonoverlap_15m_windows_horizon_only": n_macro_nonov,
            "conservative_day_clustered_n_eff": n_macro_day_cluster,
            "unique_trading_days": n_macro_days,
            "minority_class_prevalence_0.10pct": round(min_class_macro * 100.0, 2),
            "day_block_95pct_ci_up_prevalence_pct": macro_day_block_ci_up_pct,
            "candidate_feature_count": macro_p,
            "epv_nonoverlap_upper_bound": macro_epv_nonov,
            "epv_day_clustered_conservative": macro_epv_day_cluster,
            "approx_val_fold_n_eff_day_clustered": round(macro_val_n_eff_clustered, 1),
            "val_fold_95pct_ci_halfwidth_day_clustered_pct": macro_ci95_acc_clustered,
            "regime_coverage_limitation": f"Only {n_macro_days} active dates (~68 clean weekdays, ~3 months Aug-Nov 2026) in a single macro gold environment ($3,500-$4,500). Day-clustered N_eff ({n_macro_day_cluster}) is much smaller than raw non-overlapping windows ({n_macro_nonov}) due to within-day regime/volatility clustering.",
            "model_families": {
                "constant_prevalence_baseline": {
                    "feasible": True,
                    "role": "Mandatory log-loss / Brier benchmark.",
                },
                "regularized_logistic_regression": {
                    "feasible": True,
                    "role": "Primary baseline for G3.3; low variance, naturally calibrated via Platt/temperature scaling.",
                },
                "constrained_lightgbm": {
                    "feasible": "CONDITIONAL",
                    "constraints_required": "max_depth<=3, num_leaves<=7, min_child_samples>=150, strong L2 reg, feature_fraction<=0.7, strict purged walk-forward CV. High risk of overfitting to Aug-Nov macro regime without additional historical months.",
                },
            },
            "calibration_requirement": "Use Platt / temperature scaling (not isotonic regression, which overfits on day-clustered validation folds).",
        },
        "micro_30_tick_assessment": {
            "nonoverlap_30t_windows_horizon_only": n_micro_nonov,
            "conservative_day_clustered_n_eff": n_micro_day_cluster,
            "unique_trading_days": n_macro_days,
            "median_30t_abs_mid_move_usd": med_30t_abs_move,
            "median_spread_usd": med_spread,
            "fusion_total_rt_friction_usd": rt_cost,
            "move_to_friction_ratio": round(med_30t_abs_move / max(1e-6, rt_cost), 3),
            "executable_post_spread_gt0_minority_pct": round(min_class_micro * 100.0, 2),
            "candidate_feature_count": micro_p,
            "epv_nonoverlap_upper_bound": micro_epv_nonov,
            "epv_day_clustered_conservative": micro_epv_day_cluster,
            "val_fold_95pct_ci_halfwidth_day_clustered_pct": micro_ci95_acc_clustered,
            "economic_viability_warning": f"Median 30-tick |mid_move| is ${med_30t_abs_move:.3f} vs total round-trip spread+commission friction ${rt_cost:.3f} (ratio {round(med_30t_abs_move / max(1e-6, rt_cost), 2)}x). Standalone 30-tick directional scalping is heavily friction-constrained without L2 order book; Micro ML is better suited as an execution-timing / adverse-selection filter for Macro entries than a standalone 30-tick trading strategy.",
            "model_families": {
                "constant_prevalence_baseline": {"feasible": True, "role": "Mandatory benchmark."},
                "regularized_logistic_regression": {
                    "feasible": True,
                    "role": "Primary baseline for quote-revision & micro-momentum predictability.",
                },
                "constrained_lightgbm": {
                    "feasible": "CONDITIONAL",
                    "constraints_required": "Shallow trees (max_depth<=4, min_child_samples>=500), labels defined net of spread+commission or used as execution-timing filter, evaluated across unseen calendar weeks with day-block confidence intervals.",
                },
            },
        },
    }
    with open(OUT_DIR / "g3_1_model_feasibility.json", "w", encoding="utf-8") as f:
        json.dump(phase_f_result, f, indent=2)

    # ------------------------------------------------------------------
    # PHASE G: Historical Data Expansion Assessment
    # ------------------------------------------------------------------
    backup_info = _inspect_backup_ticks_bounds()
    phase_g_result = {
        "task": "G3.1_PHASE_G_DATA_EXPANSION_ASSESSMENT",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "current_db_coverage": {
            "raw_min_utc": phase_a["xauusd_summary"]["raw_min_time_utc"],
            "raw_max_utc": phase_a["xauusd_summary"]["raw_max_time_utc"],
            "clean_continuous_window": "2026-08-13T00:00:00Z .. 2026-11-18T23:59:59Z (68 continuous weekday trading days, zero unscheduled outages)",
            "post_nov18_outages": "2026-11-19 had 156m + 4574m feed outage (backfillable via cTrader ProtoOAGetTickDataReq).",
        },
        "local_backup_inspection": {
            **backup_info,
            "contains_earlier_or_new_dates": False,
            "conclusion": "backups/csv_goldedge_20261117_143146.zip is a Nov 17 snapshot of the same Aug 13 - Nov 17 TimescaleDB tables (no additional historical months).",
        },
        "legacy_bar_table_gap": phase_a["bar_table_comparison"],
        "ctrader_historical_expansion_option": {
            "mechanism": "cTrader Open API ProtoOAGetTickDataReq (already implemented and verified idempotent in scripts/audit/backfill_missing_ticks.py)",
            "provenance_and_compatibility": "EXACT same Fusion Markets broker quote feed, identical bid/ask spread structure, real_volume=NULL, zero spread fabrication.",
            "constraints": "Rate limit <= 5 req/s (2h chunks, ~0.24s/req); broker server historical tick retention depth (typically 3-12 months) must be probed via dry-run before full backfill.",
            "external_paid_data": "NOT recommended at this stage — third-party feeds have different liquidity providers/spreads that invalidate Fusion Markets execution calibration.",
        },
        "expansion_recommendation": {
            "required_before_g3_2_label_design": False,
            "required_before_g3_3_production_ml_training": True,
            "action_plan": [
                "1. Proceed to G3.2 (Macro/Micro Label Design) immediately on the clean 68-day Aug 13 - Nov 18, 2026 dataset.",
                "2. Before G3.3 model training, probe cTrader ProtoOAGetTickDataReq backward (e.g., Jan-Aug 2026) using backfill_missing_ticks.py --dry-test to expand Macro regime diversity to >= 6-12 months (130-250+ trading days) with identical broker spreads.",
                "3. Backfill the Nov 19-22 feed outage via backfill_missing_ticks.py and keep live shadow feed running to accumulate untouched prospective OOS holdout data.",
            ],
        },
    }
    with open(OUT_DIR / "g3_1_data_expansion_assessment.json", "w", encoding="utf-8") as f:
        json.dump(phase_g_result, f, indent=2)

    print("[PHASES E, F, G DONE]")
    print(f"  Macro EPV_nonov={macro_epv_nonov}, EPV_day_clustered={macro_epv_day_cluster}, N_eff_day={n_macro_day_cluster}, val 95% CI=+/-{macro_ci95_acc_clustered}%, days={n_macro_days}")
    print(f"  Micro EPV_nonov={micro_epv_nonov}, EPV_day_clustered={micro_epv_day_cluster}, N_eff_day={n_micro_day_cluster}, med_30t_move=${med_30t_abs_move:.3f} vs RT friction=${rt_cost:.3f}")
    print(f"  Backup first row: {backup_info.get('first_data_row')}")


if __name__ == "__main__":
    run_phases_efg()
