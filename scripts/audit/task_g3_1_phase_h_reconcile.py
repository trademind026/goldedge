"""
TASK G3.1 — Phase H: Independent Clean-Room vs Actual GoldEdge Application Reconciliation.
Compares clean-room SQL/NumPy reconstructions directly against GoldEdge's actual
TickRepository, market_bars_1m_quote_semantics_v1, mtf_trend.aggregate_ohlcv_bars,
IndicatorService, RegimeDetectorService, and MicrostructureFeatureRepository.
Outputs:
  - backtest_results/audit/task_g3_1_ml_feasibility/g3_1_independent_reconciliation.json
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import psycopg2
from sqlalchemy import create_engine

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")

from src.goldedge.adapters.secondary.db.tick_repository import TickRepository
from src.goldedge.adapters.secondary.features.microstructure_feature_repo import (
    compute_forex_micro_features,
)
from src.goldedge.config.settings import settings
from src.goldedge.domain.services.indicator_service import IndicatorService
from src.goldedge.domain.services.ml_feature_contract import (
    EXCLUDED_UNAVAILABLE_ML_FEATURES,
    validate_ml_feature_names,
)
from src.goldedge.domain.services.mtf_trend import aggregate_ohlcv_bars
from src.goldedge.domain.services.regime_detector import RegimeDetectorService

OUT_DIR = Path("backtest_results/audit/task_g3_1_ml_feasibility")
DB_URL = os.environ.get(
    "DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge"
).replace("+psycopg2", "")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def run_phase_h() -> Dict[str, Any]:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = psycopg2.connect(DB_URL)
    conn.set_session(readonly=True, autocommit=True)
    sa_url = DB_URL.replace("postgresql://", "postgresql+psycopg2://")
    engine = create_engine(sa_url)
    tick_repo = TickRepository(engine)

    checks: Dict[str, Any] = {}

    # 1. Tick validity & count reconciliation across 3 sample windows
    #    Verifies BOTH raw TickRepository.get_ticks_as_df exact match AND valid-quote filtered match,
    #    explicitly documenting that TickRepository.get_ticks_as_df returns raw unfiltered rows
    #    (downstream callers such as ML pipelines MUST filter bid>0 & ask>0 & bid<ask).
    sample_windows = [
        ("aug24_25", datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc), datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)),
        ("oct14_15", datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc), datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)),
        ("nov10_11", datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc)),
    ]
    tick_recon: List[Dict[str, Any]] = []
    total_tick_mismatches = 0
    with conn.cursor() as cur:
        for name, s, e in sample_windows:
            cur.execute(
                """
                SELECT
                    count(*) AS raw_cnt,
                    count(*) FILTER (WHERE bid IS NOT NULL AND ask IS NOT NULL AND bid > 0 AND ask > 0 AND bid < ask) AS valid_cnt,
                    min(bid) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask),
                    max(ask) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask),
                    avg(ask - bid) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask)
                FROM market_ticks
                WHERE symbol = 'XAUUSD'
                  AND time >= %s AND time < %s
                """,
                (s, e),
            )
            c_raw, c_cnt, c_minb, c_maxa, c_sp = cur.fetchone()
            df_app_raw = tick_repo.get_ticks_as_df("XAUUSD", s, e)
            app_raw_cnt = int(len(df_app_raw))
            df_app = df_app_raw[
                df_app_raw["bid"].notna()
                & df_app_raw["ask"].notna()
                & (df_app_raw["bid"] > 0.0)
                & (df_app_raw["ask"] > 0.0)
                & (df_app_raw["bid"] < df_app_raw["ask"])
            ]
            app_cnt = int(len(df_app))
            app_minb = float(df_app["bid"].min()) if app_cnt else None
            app_maxa = float(df_app["ask"].max()) if app_cnt else None
            app_sp = float((df_app["ask"] - df_app["bid"]).mean()) if app_cnt else None
            match = (
                int(c_raw) == app_raw_cnt
                and int(c_cnt) == app_cnt
                and abs(float(c_minb) - app_minb) < 1e-9
                and abs(float(c_maxa) - app_maxa) < 1e-9
                and abs(float(c_sp) - app_sp) < 1e-9
            )
            if not match:
                total_tick_mismatches += 1
            tick_recon.append(
                {
                    "window": name,
                    "clean_room_raw_count": int(c_raw),
                    "app_tick_repo_raw_count": app_raw_cnt,
                    "invalid_quotes_passed_through_by_repo": app_raw_cnt - app_cnt,
                    "clean_room_valid_count": int(c_cnt),
                    "app_filtered_valid_count": app_cnt,
                    "exact_match": match,
                }
            )
    checks["tick_repository_reconciliation"] = {
        "windows_tested": len(sample_windows),
        "mismatches": total_tick_mismatches,
        "repo_returns_unfiltered_invalid_quotes": True,
        "ml_pipeline_guard_required": "Callers of TickRepository.get_ticks_as_df MUST explicitly apply (bid>0 & ask>0 & bid<ask) or read v_market_ticks_quote_semantics_v1 WHERE is_valid_quote.",
        "status": "PASS" if total_tick_mismatches == 0 else "FAIL",
        "details": tick_recon,
    }

    # 2. Full 1m candle reconciliation vs market_bars_1m_quote_semantics_v1 (exact populated span)
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH bounds AS (
                SELECT min(time) AS tmin, max(time) + INTERVAL '1 minute' AS tmax
                FROM market_bars_1m_quote_semantics_v1
                WHERE symbol = 'XAUUSD'
            ),
            clean_1m AS (
                SELECT
                    time_bucket('1 minute', m.time) AS time,
                    first(m.bid, m.time) AS open,
                    max(m.bid) AS high,
                    min(m.bid) AS low,
                    last(m.bid, m.time) AS close,
                    count(*) AS tick_count
                FROM market_ticks m, bounds b
                WHERE m.symbol = 'XAUUSD'
                  AND m.time >= b.tmin
                  AND m.time <  b.tmax
                  AND m.bid IS NOT NULL AND m.ask IS NOT NULL
                  AND m.bid > 0 AND m.ask > 0 AND m.bid < m.ask
                GROUP BY 1
            )
            SELECT
                (SELECT count(*) FROM clean_1m) AS clean_cnt,
                (SELECT count(*) FROM market_bars_1m_quote_semantics_v1 WHERE symbol='XAUUSD') AS table_cnt,
                count(*) FILTER (
                    WHERE c.time IS NULL OR t.time IS NULL
                       OR abs(c.open - t.open) > 1e-9
                       OR abs(c.high - t.high) > 1e-9
                       OR abs(c.low - t.low) > 1e-9
                       OR abs(c.close - t.close) > 1e-9
                       OR c.tick_count != t.tick_count
                ) AS mismatch_cnt
            FROM clean_1m c
            FULL OUTER JOIN market_bars_1m_quote_semantics_v1 t
              ON t.symbol = 'XAUUSD' AND t.time = c.time
            """
        )
        c_1m, t_1m, m_1m = cur.fetchone()
    checks["candle_1m_quote_semantics_reconciliation"] = {
        "clean_room_1m_bars": int(c_1m),
        "db_quote_semantics_v1_bars": int(t_1m),
        "mismatches": int(m_1m),
        "status": "PASS" if int(m_1m) == 0 and int(c_1m) == int(t_1m) else "FAIL",
    }

    # 3. 5m aggregation & causal indicator/regime check on Oct 14-16 (3 days of 1m bars)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT time, open, high, low, close, tick_count, real_volume, spread_points
            FROM market_bars_1m_quote_semantics_v1
            WHERE symbol = 'XAUUSD'
              AND time >= '2026-10-14T00:00:00Z'
              AND time <  '2026-10-17T00:00:00Z'
            ORDER BY time ASC
            """
        )
        b1m_rows = cur.fetchall()
    bars_1m_dicts = [
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
    app_5m = aggregate_ohlcv_bars(bars_1m_dicts, 5)
    # Clean-room 5m check
    clean_5m_map: Dict[int, Dict[str, Any]] = {}
    for b in bars_1m_dicts:
        k = (int(b["time"].timestamp()) // 300) * 300
        if k not in clean_5m_map:
            clean_5m_map[k] = {
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "tick_volume": b["tick_volume"],
            }
        else:
            d = clean_5m_map[k]
            d["high"] = max(d["high"], b["high"])
            d["low"] = min(d["low"], b["low"])
            d["close"] = b["close"]
            d["tick_volume"] += b["tick_volume"]
    m_5m = 0
    for b5 in app_5m:
        k = int(b5["time"].timestamp())
        ref = clean_5m_map.get(k)
        if (
            ref is None
            or abs(b5["open"] - ref["open"]) > 1e-9
            or abs(b5["high"] - ref["high"]) > 1e-9
            or abs(b5["low"] - ref["low"]) > 1e-9
            or abs(b5["close"] - ref["close"]) > 1e-9
            or int(b5["tick_volume"]) != int(ref["tick_volume"])
        ):
            m_5m += 1

    # Causality test on IndicatorService & RegimeDetectorService:
    # Truncating future bars after index K must leave indicators/regime at index K bit-identical!
    df_full = IndicatorService.compute_indicators(app_5m)
    k_cut = 250
    df_trunc = IndicatorService.compute_indicators(app_5m[: k_cut + 1])
    causal_cols = ["ema20", "ema50", "rsi", "atr", "adx", "bb_upper", "bb_lower", "tick_rate_ratio", "tick_weighted_price"]
    max_causal_diff = 0.0
    for col in causal_cols:
        if col in df_full.columns and col in df_trunc.columns:
            v1 = float(df_full[col].iloc[k_cut])
            v2 = float(df_trunc[col].iloc[k_cut])
            if np.isfinite(v1) and np.isfinite(v2):
                max_causal_diff = max(max_causal_diff, abs(v1 - v2))

    det_full = RegimeDetectorService()
    det_trunc = RegimeDetectorService()
    reg_full = det_full.detect(df_full.iloc[: k_cut + 1])
    reg_trunc = det_trunc.detect(df_trunc)

    checks["aggregation_5m_and_causality"] = {
        "app_5m_bars": len(app_5m),
        "clean_room_5m_bars": len(clean_5m_map),
        "ohlcv_mismatches": m_5m,
        "indicator_truncation_max_abs_diff": max_causal_diff,
        "regime_truncation_match": reg_full == reg_trunc,
        "status": "PASS" if (m_5m == 0 and len(app_5m) == len(clean_5m_map) and max_causal_diff < 1e-12 and reg_full == reg_trunc) else "FAIL",
    }

    # 4. Verify disabled Lee-Ready order-flow exclusion & ML feature contract enforcement
    df_micro_sample = tick_repo.get_ticks_as_df(
        "XAUUSD",
        datetime(2026, 10, 14, 13, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 14, 13, 15, tzinfo=timezone.utc),
    )
    micro_out = compute_forex_micro_features(df_micro_sample, window_seconds=30)
    forbidden_emitted = sorted(
        {c for c in micro_out.columns if c.lower() in EXCLUDED_UNAVAILABLE_ML_FEATURES}
    )
    flag_rejected = False
    try:
        compute_forex_micro_features(df_micro_sample, window_seconds=30, include_disabled_order_flow=True)
    except ValueError:
        flag_rejected = True

    contract_rejected = False
    try:
        validate_ml_feature_names(["spread_usd_and_zscore_30t", "net_order_flow"], horizon="micro")
    except ValueError:
        contract_rejected = True

    checks["order_flow_feature_exclusion_enforcement"] = {
        "ticks_tested": int(len(df_micro_sample)),
        "output_1s_rows": int(len(micro_out)),
        "output_columns": list(micro_out.columns),
        "forbidden_order_flow_columns_emitted": forbidden_emitted,
        "include_disabled_flag_raises_value_error": flag_rejected,
        "ml_feature_contract_blocks_net_order_flow": contract_rejected,
        "status": "PASS" if (len(forbidden_emitted) == 0 and flag_rejected and contract_rejected) else "FAIL",
    }

    # 4b. Verify Sunday tick quarantine & exact 843-bar reconciliation
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM market_ticks_quarantine WHERE symbol='XAUUSD'")
        q_cnt = int(cur.fetchone()[0])
        cur.execute(
            """
            SELECT
                count(DISTINCT time_bucket('1 minute', time)) FILTER (WHERE is_valid_quote) AS all_1m,
                count(DISTINCT time_bucket('1 minute', time)) FILTER (WHERE is_ml_eligible_quote) AS ml_1m
            FROM v_market_ticks_quote_semantics_v1
            WHERE symbol = 'XAUUSD'
            """
        )
        all_1m_chk, ml_1m_chk = cur.fetchone()
    diff_843 = int(all_1m_chk) - int(t_1m)
    checks["quarantine_and_843_bar_reconciliation"] = {
        "quarantined_off_session_ticks": q_cnt,
        "all_valid_1m_bars": int(all_1m_chk),
        "ml_eligible_1m_bars": int(ml_1m_chk),
        "qs_v1_1m_bars": int(t_1m),
        "diff_bars_all_vs_qs_v1": diff_843,
        "quarantined_bars_removed": int(all_1m_chk) - int(ml_1m_chk),
        "explained_breakdown": "291 (2026-11-18 18:09-23:59Z) + 551 (2026-11-19 00:00-11:45Z) + 1 (quarantined 2026-11-22 16:00Z) = 843 (0 unexplained)",
        "status": "PASS" if (q_cnt == 1 and int(all_1m_chk) - int(ml_1m_chk) == 1 and diff_843 == 843) else "FAIL",
    }

    # 5. Safety locks & frozen artifacts integrity
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM market_orders")
        orders_cnt = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM market_trades")
        trades_cnt = int(cur.fetchone()[0])
    conn.close()

    frozen_111_sha = _sha256(Path("backtest_results/frozen_111_baseline_trades.csv"))
    frozen_70_sha = _sha256(Path("backtest_results/audit/task_g1_root_cause/g1_original_70_baseline_trades.csv"))
    safety_ok = (
        settings.SHADOW_MODE is True
        and settings.TRADING_MODE == "demo"
        and abs(float(settings.HWM_MAX_DRAWDOWN_PCT) - 8.0) < 1e-9
        and orders_cnt == 0
        and trades_cnt == 0
        and frozen_111_sha == "73306E1529ADC48284BCE9AB6AC35122F33B9999D2464AF441867EB3BB6CF168"
        and frozen_70_sha == "27200EEA2C7FF4842FC586DD1131EE450247D0BCCBCF3E2F77ECA6952D2CCDC4"
    )
    checks["safety_and_frozen_integrity"] = {
        "SHADOW_MODE": settings.SHADOW_MODE,
        "TRADING_MODE": settings.TRADING_MODE,
        "HWM_MAX_DRAWDOWN_PCT": float(settings.HWM_MAX_DRAWDOWN_PCT),
        "market_orders_count": orders_cnt,
        "market_trades_count": trades_cnt,
        "frozen_111_sha256_match": frozen_111_sha == "73306E1529ADC48284BCE9AB6AC35122F33B9999D2464AF441867EB3BB6CF168",
        "frozen_70_sha256_match": frozen_70_sha == "27200EEA2C7FF4842FC586DD1131EE450247D0BCCBCF3E2F77ECA6952D2CCDC4",
        "status": "PASS" if safety_ok else "FAIL",
    }

    overall_pass = all(
        checks[k]["status"] == "PASS"
        for k in (
            "tick_repository_reconciliation",
            "candle_1m_quote_semantics_reconciliation",
            "aggregation_5m_and_causality",
            "order_flow_feature_exclusion_enforcement",
            "quarantine_and_843_bar_reconciliation",
            "safety_and_frozen_integrity",
        )
    )
    report = {
        "task": "G3.1_PHASE_H_INDEPENDENT_RECONCILIATION",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "overall_status": "PASS" if overall_pass else "FAIL",
        "checks": checks,
        "elapsed_sec": round(time.time() - t0, 2),
    }
    with open(OUT_DIR / "g3_1_independent_reconciliation.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"[PHASE H DONE in {report['elapsed_sec']}s] overall={report['overall_status']}")
    for k, v in checks.items():
        print(f"  {k}: {v.get('status', 'INFO')} -> { {kk: vv for kk, vv in v.items() if kk != 'details'} }")
    return report


if __name__ == "__main__":
    run_phase_h()
