"""
TASK G3.1 — Phase A: Complete TimescaleDB Historical Data Integrity Audit.
Clean-room standalone script: ZERO imports from src/goldedge/.
Outputs:
  - backtest_results/audit/task_g3_1_ml_feasibility/g3_1_historical_data_audit.json
  - backtest_results/audit/task_g3_1_ml_feasibility/g3_1_daily_coverage.csv
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import psycopg2

OUT_DIR = Path("backtest_results/audit/task_g3_1_ml_feasibility")
DB_URL = os.environ.get(
    "DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge"
).replace("+psycopg2", "")


def _git_rev() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


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


def _classify_gap(start_dt: datetime, end_dt: datetime, dur_min: float) -> str:
    s_wd = start_dt.weekday()  # Mon=0 .. Fri=4, Sat=5, Sun=6
    e_wd = end_dt.weekday()
    # Weekend market closure: starts Fri >= 20:00 UTC or Sat, ends Sun >= 20:00 UTC or Mon <= 02:00 UTC
    if (s_wd == 4 and start_dt.hour >= 20) or s_wd == 5:
        if e_wd in (6, 0) and dur_min >= 24 * 60:
            return "WEEKEND_CLOSURE"
    # Daily rollover maintenance: ~55-75 min around 20:55-23:05 UTC (DST shifts between 21:00 and 22:00 UTC)
    if 45.0 <= dur_min <= 85.0 and start_dt.hour in (20, 21, 22) and end_dt.hour in (21, 22, 23):
        return "DAILY_ROLLOVER"
    # Short rollover / liquidity lull around 21:00-23:00 UTC
    if 5.0 <= dur_min < 45.0 and start_dt.hour in (20, 21, 22):
        return "ROLLOVER_ADJACENT_LULL"
    return "INTRADAY_FEED_OUTAGE"


def run_phase_a() -> Dict[str, Any]:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = psycopg2.connect(DB_URL)
    conn.set_session(readonly=True, autocommit=True)

    result: Dict[str, Any] = {
        "task": "G3.1_PHASE_A_HISTORICAL_DATA_AUDIT",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_head": _git_rev(),
        "tables": {},
        "market_ticks_symbols": {},
        "xauusd_summary": {},
        "timestamp_semantics": {},
        "quote_validity": {},
        "spread_distribution": {},
        "price_jump_anomalies": {},
        "stale_identical_quotes": {},
        "session_quote_frequency": {},
        "monthly_quote_frequency": {},
        "hourly_quote_frequency": {},
        "gaps_summary": {},
        "intraday_outages_ge_5m": [],
        "rollover_and_weekend_gaps": [],
        "bar_table_comparison": {},
    }

    with conn.cursor() as cur:
        # 1. Inspect relevant tables, schemas, indexes, row counts
        target_tables = [
            "market_ticks",
            "market_bars_1m",
            "market_bars_1m_quote_semantics_v1",
            "v_market_ticks_quote_semantics_v1",
            "v_market_bars_1m_timeline_v1",
            "market_features",
            "market_signals",
            "market_orders",
            "market_trades",
            "backtest_runs",
        ]
        for tbl in target_tables:
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
                """,
                (tbl,),
            )
            cols = [{"name": r[0], "type": r[1], "nullable": r[2]} for r in cur.fetchall()]
            cur.execute(
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'public' AND tablename = %s
                """,
                (tbl,),
            )
            idxs = [{"name": r[0], "def": r[1]} for r in cur.fetchall()]
            cur.execute(f"SELECT count(*) FROM public.{tbl}")
            cnt = int(cur.fetchone()[0])
            min_t = max_t = None
            col_names = {c["name"] for c in cols}
            if "time" in col_names and cnt > 0:
                cur.execute(f"SELECT min(time), max(time) FROM public.{tbl}")
                r = cur.fetchone()
                min_t = r[0].isoformat() if r[0] else None
                max_t = r[1].isoformat() if r[1] else None
            result["tables"][tbl] = {
                "row_count": cnt,
                "min_time": min_t,
                "max_time": max_t,
                "columns": cols,
                "indexes": idxs,
            }

        # 2. Symbol breakdown in market_ticks
        cur.execute(
            """
            SELECT symbol, count(*), min(time), max(time)
            FROM market_ticks
            GROUP BY symbol
            ORDER BY count(*) DESC
            """
        )
        for sym, cnt, mn, mx in cur.fetchall():
            result["market_ticks_symbols"][sym] = {
                "row_count": int(cnt),
                "min_time": mn.isoformat() if mn else None,
                "max_time": mx.isoformat() if mx else None,
            }

        # 3. Single-pass SQL summary for XAUUSD validity, volume, timezone, precision
        cur.execute(
            """
            SELECT
                count(*) AS total_rows,
                count(*) FILTER (WHERE bid IS NOT NULL AND ask IS NOT NULL AND bid > 0 AND ask > 0 AND bid < ask) AS valid_rows,
                count(*) FILTER (WHERE bid IS NULL OR ask IS NULL) AS null_bid_or_ask,
                count(*) FILTER (WHERE bid <= 0 OR ask <= 0) AS nonpositive_price,
                count(*) FILTER (WHERE bid > ask AND bid > 0 AND ask > 0) AS inverted_quotes,
                count(*) FILTER (WHERE bid = ask AND bid > 0 AND ask > 0) AS zero_spread_quotes,
                count(*) FILTER (WHERE volume IS NOT NULL) AS non_null_volume,
                count(*) FILTER (WHERE volume IS NOT NULL AND volume > 0) AS positive_volume,
                min(time) AS raw_min_time,
                max(time) AS raw_max_time,
                min(time) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS valid_min_time,
                max(time) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS valid_max_time,
                count(*) FILTER (WHERE (EXTRACT(MICROSECONDS FROM time)::bigint % 1000) != 0) AS sub_ms_rows,
                count(*) FILTER (WHERE (EXTRACT(MICROSECONDS FROM time)::bigint % 1000000) = 0) AS whole_sec_rows,
                count(DISTINCT date_trunc('milliseconds', time)) AS distinct_ms_timestamps
            FROM market_ticks
            WHERE symbol = 'XAUUSD'
            """
        )
        r = cur.fetchone()
        total_rows = int(r[0])
        valid_rows = int(r[1])
        result["xauusd_summary"] = {
            "total_raw_ticks": total_rows,
            "total_valid_ticks": valid_rows,
            "total_invalid_ticks": total_rows - valid_rows,
            "valid_pct": round(100.0 * valid_rows / max(1, total_rows), 6),
            "raw_min_time_utc": r[8].isoformat() if r[8] else None,
            "raw_max_time_utc": r[9].isoformat() if r[9] else None,
            "valid_min_time_utc": r[10].isoformat() if r[10] else None,
            "valid_max_time_utc": r[11].isoformat() if r[11] else None,
            "non_null_volume_rows": int(r[6]),
            "positive_volume_rows": int(r[7]),
            "g2_reference_total_ticks": 19959291,
            "delta_vs_g2_reference": total_rows - 19959291,
        }
        result["quote_validity"] = {
            "null_bid_or_ask": int(r[2]),
            "nonpositive_price": int(r[3]),
            "inverted_bid_gt_ask": int(r[4]),
            "zero_spread_bid_eq_ask": int(r[5]),
            "valid_strict_bid_lt_ask": valid_rows,
        }
        result["timestamp_semantics"] = {
            "column_type": "timestamp with time zone (UTC stored)",
            "primary_key_prevents_exact_dups": "(symbol, time)",
            "sub_millisecond_rows": int(r[12]),
            "whole_second_rows": int(r[13]),
            "distinct_millisecond_timestamps": int(r[14]),
            "millisecond_collision_rows": total_rows - int(r[14]),
        }

        # 4. Inspect any non-XAUUSD rows or latest rows explaining +1 row vs G2
        cur.execute(
            """
            SELECT symbol, time, bid, ask, last, volume
            FROM market_ticks
            WHERE symbol != 'XAUUSD'
            ORDER BY time DESC
            LIMIT 20
            """
        )
        result["non_xauusd_sample"] = [
            {
                "symbol": row[0],
                "time": row[1].isoformat(),
                "bid": row[2],
                "ask": row[3],
                "last": row[4],
                "volume": row[5],
            }
            for row in cur.fetchall()
        ]

        # 5. Monthly & Hourly quote frequency (valid ticks)
        cur.execute(
            """
            SELECT
                to_char(time AT TIME ZONE 'UTC', 'YYYY-MM') AS ym,
                count(*) AS raw_cnt,
                count(*) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS valid_cnt,
                count(DISTINCT date_trunc('day', time AT TIME ZONE 'UTC')) AS active_days,
                count(DISTINCT time_bucket('1 minute', time)) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS valid_1m_bars,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY (ask - bid)) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS med_spread,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY (ask - bid)) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS p95_spread
            FROM market_ticks
            WHERE symbol = 'XAUUSD'
            GROUP BY ym
            ORDER BY ym
            """
        )
        for row in cur.fetchall():
            result["monthly_quote_frequency"][row[0]] = {
                "raw_ticks": int(row[1]),
                "valid_ticks": int(row[2]),
                "active_days": int(row[3]),
                "valid_1m_bars": int(row[4]),
                "ticks_per_valid_1m_bar": round(float(row[2]) / max(1, int(row[4])), 2),
                "median_spread_usd": round(float(row[5]), 5) if row[5] is not None else None,
                "p95_spread_usd": round(float(row[6]), 5) if row[6] is not None else None,
            }

        cur.execute(
            """
            SELECT
                EXTRACT(HOUR FROM time AT TIME ZONE 'UTC')::int AS hr,
                count(*) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS valid_cnt,
                count(DISTINCT time_bucket('1 minute', time)) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS valid_1m_bars,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY (ask - bid)) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS med_spread,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY (ask - bid)) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS p95_spread
            FROM market_ticks
            WHERE symbol = 'XAUUSD'
            GROUP BY hr
            ORDER BY hr
            """
        )
        sess_agg = defaultdict(lambda: {"valid_ticks": 0, "valid_1m_bars": 0})
        for hr, vcnt, bars, med_sp, p95_sp in cur.fetchall():
            sess = _session_of_hour(int(hr))
            result["hourly_quote_frequency"][f"{int(hr):02d}"] = {
                "session": sess,
                "valid_ticks": int(vcnt),
                "valid_1m_bars": int(bars),
                "ticks_per_min": round(float(vcnt) / max(1, int(bars)), 2),
                "median_spread_usd": round(float(med_sp), 5) if med_sp is not None else None,
                "p95_spread_usd": round(float(p95_sp), 5) if p95_sp is not None else None,
            }
            sess_agg[sess]["valid_ticks"] += int(vcnt)
            sess_agg[sess]["valid_1m_bars"] += int(bars)

        for sess, d in sess_agg.items():
            d["ticks_per_min"] = round(d["valid_ticks"] / max(1, d["valid_1m_bars"]), 2)
        result["session_quote_frequency"] = dict(sess_agg)

        # 6. Bar table comparison & exact 843-bar reconciliation + quarantine audit
        cur.execute(
            """
            SELECT symbol, time, reason, quarantined_at
            FROM market_ticks_quarantine
            ORDER BY time ASC
            """
        )
        quarantined_rows = [
            {
                "symbol": r[0],
                "time_utc": r[1].isoformat(),
                "reason": r[2],
                "quarantined_at_utc": r[3].isoformat(),
            }
            for r in cur.fetchall()
        ]
        cur.execute(
            """
            SELECT
                count(*) FILTER (WHERE is_quarantined) AS quarantined_cnt,
                count(*) FILTER (WHERE is_ml_eligible_quote) AS ml_eligible_cnt,
                max(time) FILTER (WHERE is_ml_eligible_quote) AS ml_eligible_max_time
            FROM v_market_ticks_quote_semantics_v1
            WHERE symbol = 'XAUUSD'
            """
        )
        q_cnt, ml_cnt, ml_max = cur.fetchone()
        result["quarantined_off_session_ticks"] = {
            "quarantined_count": int(q_cnt),
            "ml_eligible_valid_ticks": int(ml_cnt),
            "ml_eligible_max_time_utc": ml_max.isoformat() if ml_max else None,
            "root_cause": "Sunday 2026-11-22 16:00:02.669772Z quote (bid=3537.89, ask=3538.13) arrived hours before Fusion Markets' Sunday ~22:00-23:00 UTC XAUUSD session open because ctrader_live.py forwarded a SpotEvent reconnect snapshot with timestamp_ms=None, which LiveTickerService stamped with local UTC wall clock.",
            "quarantine_policy": "Non-destructive quarantine in market_ticks_quarantine + v_market_ticks_quote_semantics_v1.is_ml_eligible_quote=FALSE for Saturday UTC and Sunday < 21:00 UTC ticks; raw market_ticks preserved untouched.",
            "records": quarantined_rows,
        }

        cur.execute(
            """
            SELECT
                count(DISTINCT time_bucket('1 minute', time)) FILTER (WHERE is_valid_quote) AS all_valid_1m_buckets,
                count(DISTINCT time_bucket('1 minute', time)) FILTER (WHERE is_ml_eligible_quote) AS ml_eligible_1m_buckets,
                count(DISTINCT time_bucket('1 minute', time)) FILTER (
                    WHERE is_ml_eligible_quote
                      AND time >= '2026-08-20T00:00:00Z' AND time < '2026-11-16T20:02:00Z'
                ) AS approved_window_1m_buckets
            FROM v_market_ticks_quote_semantics_v1
            WHERE symbol = 'XAUUSD'
            """
        )
        all_1m, ml_1m, win_1m = cur.fetchone()

        cur.execute(
            """
            WITH clean_all AS (
                SELECT time_bucket('1 minute', time) AS t
                FROM market_ticks
                WHERE symbol = 'XAUUSD' AND bid > 0 AND ask > 0 AND bid < ask
                GROUP BY 1
            ),
            qs AS (
                SELECT time AS t
                FROM market_bars_1m_quote_semantics_v1
                WHERE symbol = 'XAUUSD'
            )
            SELECT
                to_char(clean_all.t AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day_utc,
                count(*) AS bar_count,
                min(clean_all.t) AS min_t,
                max(clean_all.t) AS max_t
            FROM clean_all
            LEFT JOIN qs ON clean_all.t = qs.t
            WHERE qs.t IS NULL
            GROUP BY 1
            ORDER BY 1
            """
        )
        diff_843_breakdown = [
            {
                "day_utc": r[0],
                "bars_only_in_market_ticks": int(r[1]),
                "min_utc": r[2].isoformat(),
                "max_utc": r[3].isoformat(),
                "reason": (
                    "QUARANTINED_OFF_SESSION_SUNDAY_SNAPSHOT"
                    if r[0] == "2026-11-22"
                    else "POST_QS_V1_MATERIALIZATION_CUTOFF_LIVE_TICKS_AFTER_2026_11_18_18_08Z"
                ),
            }
            for r in cur.fetchall()
        ]
        result["bar_table_comparison"] = {
            "raw_ticks_all_valid_1m_buckets": int(all_1m),
            "ml_eligible_1m_buckets_excl_quarantined": int(ml_1m),
            "raw_ticks_approved_window_1m_buckets": int(win_1m),
            "market_bars_1m_rows": result["tables"]["market_bars_1m"]["row_count"],
            "market_bars_1m_min": result["tables"]["market_bars_1m"]["min_time"],
            "market_bars_1m_max": result["tables"]["market_bars_1m"]["max_time"],
            "market_bars_1m_quote_semantics_v1_rows": result["tables"]["market_bars_1m_quote_semantics_v1"]["row_count"],
            "market_bars_1m_quote_semantics_v1_min": result["tables"]["market_bars_1m_quote_semantics_v1"]["min_time"],
            "market_bars_1m_quote_semantics_v1_max": result["tables"]["market_bars_1m_quote_semantics_v1"]["max_time"],
            "diff_all_valid_vs_qs_v1_bars": int(all_1m) - result["tables"]["market_bars_1m_quote_semantics_v1"]["row_count"],
            "diff_843_exact_breakdown": diff_843_breakdown,
            "unexplained_diff_bars": (int(all_1m) - result["tables"]["market_bars_1m_quote_semantics_v1"]["row_count"]) - sum(d["bars_only_in_market_ticks"] for d in diff_843_breakdown),
            "legacy_market_bars_1m_coverage_gap_bars": int(all_1m) - result["tables"]["market_bars_1m"]["row_count"],
        }

    # 7. Chunked day-by-day scan of valid XAUUSD ticks for exact spread quantiles,
    #    stale/identical consecutive quotes, out-of-order checks, price jump anomalies,
    #    exact gap classification, and daily coverage CSV.
    start_date = datetime(2026, 8, 9, 0, 0, 0, tzinfo=timezone.utc)
    end_date = datetime(2026, 11, 23, 0, 0, 0, tzinfo=timezone.utc)

    # Also find exact raw min/max dates dynamically
    raw_min = datetime.fromisoformat(result["xauusd_summary"]["raw_min_time_utc"])
    raw_max = datetime.fromisoformat(result["xauusd_summary"]["raw_max_time_utc"])
    cur_day = datetime(raw_min.year, raw_min.month, raw_min.day, tzinfo=timezone.utc)
    last_day = datetime(raw_max.year, raw_max.month, raw_max.day, tzinfo=timezone.utc)

    prev_ts_ns: int | None = None
    prev_bid: float | None = None
    prev_ask: float | None = None
    prev_mid: float | None = None

    out_of_order_count = 0
    identical_quote_count = 0
    max_identical_run = 0
    cur_identical_run = 0
    spread_gt_1 = 0
    spread_gt_2 = 0
    spread_gt_5 = 0
    spread_gt_10 = 0
    max_spread_obs = 0.0
    max_spread_ts = None
    min_spread_obs = 1e9

    jump_counts = {
        "gt_2_within_1s": 0,
        "gt_5_within_1s": 0,
        "gt_10_within_1s": 0,
        "gt_5_within_60s": 0,
        "gt_10_within_60s": 0,
        "gt_20_within_60s": 0,
    }
    top_jumps: List[Dict[str, Any]] = []
    gaps_ge_5m: List[Dict[str, Any]] = []
    gaps_1m_to_5m_count = 0

    # Reservoir / histogram for spread quantiles (exact via 0.01 rounding up to $50)
    spread_hist = np.zeros(5001, dtype=np.int64)
    spread_sum = 0.0
    spread_sq_sum = 0.0
    spread_n = 0

    daily_rows: List[Dict[str, Any]] = []

    with conn.cursor() as cur:
        d = cur_day
        while d <= last_day:
            nxt = d + timedelta(days=1)
            cur.execute(
                """
                SELECT
                    EXTRACT(EPOCH FROM time) * 1e9 AS ts_ns,
                    bid,
                    ask
                FROM market_ticks
                WHERE symbol = 'XAUUSD'
                  AND time >= %s AND time < %s
                ORDER BY time ASC
                """,
                (d, nxt),
            )
            rows = cur.fetchall()
            raw_cnt = len(rows)
            if raw_cnt == 0:
                wd = d.weekday()
                is_wknd = wd == 5 or (wd == 6)
                daily_rows.append(
                    {
                        "date": d.strftime("%Y-%m-%d"),
                        "day_of_week": d.strftime("%a"),
                        "is_weekend": int(wd in (5, 6)),
                        "raw_ticks": 0,
                        "valid_ticks": 0,
                        "invalid_ticks": 0,
                        "valid_1m_bars": 0,
                        "expected_trading_mins": 0 if wd == 5 else (60 if wd == 6 else (1260 if wd == 4 else 1380)),
                        "coverage_pct": 0.0,
                        "first_tick_utc": "",
                        "last_tick_utc": "",
                        "median_spread_usd": "",
                        "p95_spread_usd": "",
                        "max_spread_usd": "",
                        "intraday_gap_count_ge5m": 0,
                        "max_intraday_gap_min": 0.0,
                        "day_status": "WEEKEND_CLOSED" if wd == 5 else "NO_DATA",
                    }
                )
                d = nxt
                continue

            arr = np.array(rows, dtype=np.float64)
            ts_ns = arr[:, 0].astype(np.int64)
            bids = arr[:, 1]
            asks = arr[:, 2]

            valid_mask = (~np.isnan(bids)) & (~np.isnan(asks)) & (bids > 0.0) & (asks > 0.0) & (bids < asks)
            v_ts = ts_ns[valid_mask]
            v_bid = bids[valid_mask]
            v_ask = asks[valid_mask]
            v_cnt = int(len(v_ts))
            inv_cnt = raw_cnt - v_cnt

            day_intraday_gaps = 0
            day_max_intraday_gap = 0.0

            if v_cnt > 0:
                v_mid = 0.5 * (v_bid + v_ask)
                v_sp = v_ask - v_bid

                # Spread stats
                spread_n += v_cnt
                spread_sum += float(np.sum(v_sp))
                spread_sq_sum += float(np.sum(v_sp * v_sp))
                sp_idx = np.clip(np.rint(v_sp * 100.0).astype(np.int64), 0, 5000)
                np.add.at(spread_hist, sp_idx, 1)

                spread_gt_1 += int(np.sum(v_sp > 1.0))
                spread_gt_2 += int(np.sum(v_sp > 2.0))
                spread_gt_5 += int(np.sum(v_sp > 5.0))
                spread_gt_10 += int(np.sum(v_sp > 10.0))

                d_max_sp_i = int(np.argmax(v_sp))
                d_max_sp = float(v_sp[d_max_sp_i])
                d_min_sp = float(np.min(v_sp))
                if d_max_sp > max_spread_obs:
                    max_spread_obs = d_max_sp
                    max_spread_ts = datetime.fromtimestamp(v_ts[d_max_sp_i] / 1e9, tz=timezone.utc).isoformat()
                if d_min_sp < min_spread_obs:
                    min_spread_obs = d_min_sp

                # Prepend previous tick from prior day to catch boundary gaps/jumps/order
                if prev_ts_ns is not None:
                    ts_ext = np.concatenate(([prev_ts_ns], v_ts))
                    bid_ext = np.concatenate(([prev_bid], v_bid))
                    ask_ext = np.concatenate(([prev_ask], v_ask))
                    mid_ext = np.concatenate(([prev_mid], v_mid))
                else:
                    ts_ext = v_ts
                    bid_ext = v_bid
                    ask_ext = v_ask
                    mid_ext = v_mid

                dt_s = np.diff(ts_ext) / 1e9
                out_of_order_count += int(np.sum(dt_s < 0.0))

                # Identical consecutive (bid, ask)
                same = (bid_ext[1:] == bid_ext[:-1]) & (ask_ext[1:] == ask_ext[:-1])
                identical_quote_count += int(np.sum(same))
                if np.any(same):
                    # Run-length encoding for max identical run
                    padded = np.concatenate(([False], same, [False]))
                    starts = np.where(padded[1:] & ~padded[:-1])[0]
                    ends = np.where(~padded[1:] & padded[:-1])[0]
                    if len(starts) > 0:
                        local_max = int(np.max(ends - starts))
                        if local_max > max_identical_run:
                            max_identical_run = local_max

                # Price jumps (excluding gaps >= 300s)
                dmid = np.abs(np.diff(mid_ext))
                m1s = dt_s <= 1.0
                m60s = dt_s <= 60.0
                jump_counts["gt_2_within_1s"] += int(np.sum((dmid > 2.0) & m1s))
                jump_counts["gt_5_within_1s"] += int(np.sum((dmid > 5.0) & m1s))
                jump_counts["gt_10_within_1s"] += int(np.sum((dmid > 10.0) & m1s))
                jump_counts["gt_5_within_60s"] += int(np.sum((dmid > 5.0) & m60s))
                jump_counts["gt_10_within_60s"] += int(np.sum((dmid > 10.0) & m60s))
                jump_counts["gt_20_within_60s"] += int(np.sum((dmid > 20.0) & m60s))

                big_j = np.where((dmid > 10.0) & m60s)[0]
                for idx in big_j[:10]:
                    top_jumps.append(
                        {
                            "time_utc": datetime.fromtimestamp(ts_ext[idx + 1] / 1e9, tz=timezone.utc).isoformat(),
                            "dt_sec": round(float(dt_s[idx]), 4),
                            "prev_mid": round(float(mid_ext[idx]), 3),
                            "next_mid": round(float(mid_ext[idx + 1]), 3),
                            "abs_jump_usd": round(float(dmid[idx]), 3),
                        }
                    )

                # Gaps
                gaps_1m_to_5m_count += int(np.sum((dt_s >= 60.0) & (dt_s < 300.0)))
                gap_idxs = np.where(dt_s >= 300.0)[0]
                for idx in gap_idxs:
                    s_dt = datetime.fromtimestamp(ts_ext[idx] / 1e9, tz=timezone.utc)
                    e_dt = datetime.fromtimestamp(ts_ext[idx + 1] / 1e9, tz=timezone.utc)
                    dur_m = float(dt_s[idx]) / 60.0
                    g_type = _classify_gap(s_dt, e_dt, dur_m)
                    rec = {
                        "start_utc": s_dt.isoformat(),
                        "end_utc": e_dt.isoformat(),
                        "duration_min": round(dur_m, 2),
                        "classification": g_type,
                        "mid_before": round(float(mid_ext[idx]), 3),
                        "mid_after": round(float(mid_ext[idx + 1]), 3),
                        "price_gap_usd": round(float(mid_ext[idx + 1] - mid_ext[idx]), 3),
                    }
                    gaps_ge_5m.append(rec)
                    if g_type == "INTRADAY_FEED_OUTAGE":
                        day_intraday_gaps += 1
                        if dur_m > day_max_intraday_gap:
                            day_max_intraday_gap = dur_m

                prev_ts_ns = int(v_ts[-1])
                prev_bid = float(v_bid[-1])
                prev_ask = float(v_ask[-1])
                prev_mid = float(v_mid[-1])

                bars_1m = int(len(np.unique(v_ts // 60_000_000_000)))
                wd = d.weekday()
                exp_mins = 0 if wd == 5 else (120 if wd == 6 else (1260 if wd == 4 else 1380))
                cov_pct = round(100.0 * bars_1m / max(1, exp_mins), 2) if exp_mins > 0 else 0.0
                status = (
                    "WEEKEND_OPEN"
                    if wd == 6
                    else ("OK" if cov_pct >= 95.0 and day_max_intraday_gap < 15.0 else ("PARTIAL_OUTAGE" if cov_pct >= 50.0 else "SEVERE_OUTAGE"))
                )
                daily_rows.append(
                    {
                        "date": d.strftime("%Y-%m-%d"),
                        "day_of_week": d.strftime("%a"),
                        "is_weekend": int(wd in (5, 6)),
                        "raw_ticks": raw_cnt,
                        "valid_ticks": v_cnt,
                        "invalid_ticks": inv_cnt,
                        "valid_1m_bars": bars_1m,
                        "expected_trading_mins": exp_mins,
                        "coverage_pct": cov_pct,
                        "first_tick_utc": datetime.fromtimestamp(v_ts[0] / 1e9, tz=timezone.utc).strftime("%H:%M:%S"),
                        "last_tick_utc": datetime.fromtimestamp(v_ts[-1] / 1e9, tz=timezone.utc).strftime("%H:%M:%S"),
                        "median_spread_usd": round(float(np.median(v_sp)), 4),
                        "p95_spread_usd": round(float(np.percentile(v_sp, 95)), 4),
                        "max_spread_usd": round(d_max_sp, 4),
                        "intraday_gap_count_ge5m": day_intraday_gaps,
                        "max_intraday_gap_min": round(day_max_intraday_gap, 2),
                        "day_status": status,
                    }
                )
            d = nxt

    conn.close()

    # Exact quantiles from spread histogram
    cdf = np.cumsum(spread_hist) / max(1, spread_n)

    def _q(p: float) -> float:
        idx = int(np.searchsorted(cdf, p))
        return round(min(idx, 5000) / 100.0, 4)

    mean_sp = spread_sum / max(1, spread_n)
    std_sp = float(np.sqrt(max(0.0, spread_sq_sum / max(1, spread_n) - mean_sp * mean_sp)))

    result["spread_distribution"] = {
        "n_valid_quotes": spread_n,
        "min_usd": round(min_spread_obs, 5) if spread_n > 0 else None,
        "p01_usd": _q(0.01),
        "p05_usd": _q(0.05),
        "p25_usd": _q(0.25),
        "p50_median_usd": _q(0.50),
        "p75_usd": _q(0.75),
        "p95_usd": _q(0.95),
        "p99_usd": _q(0.99),
        "max_usd": round(max_spread_obs, 5),
        "max_spread_time_utc": max_spread_ts,
        "mean_usd": round(mean_sp, 5),
        "std_usd": round(std_sp, 5),
        "count_gt_1_usd": spread_gt_1,
        "count_gt_2_usd": spread_gt_2,
        "count_gt_5_usd": spread_gt_5,
        "count_gt_10_usd": spread_gt_10,
    }
    result["stale_identical_quotes"] = {
        "out_of_order_timestamps": out_of_order_count,
        "consecutive_identical_bid_ask_count": identical_quote_count,
        "consecutive_identical_pct": round(100.0 * identical_quote_count / max(1, spread_n), 4),
        "max_consecutive_identical_run": max_identical_run,
    }
    top_jumps.sort(key=lambda x: x["abs_jump_usd"], reverse=True)
    result["price_jump_anomalies"] = {
        "counts": jump_counts,
        "top_20_jumps_within_60s": top_jumps[:20],
    }

    intraday_outages = [g for g in gaps_ge_5m if g["classification"] == "INTRADAY_FEED_OUTAGE"]
    scheduled_gaps = [g for g in gaps_ge_5m if g["classification"] != "INTRADAY_FEED_OUTAGE"]
    result["gaps_summary"] = {
        "gaps_1m_to_5m_count": gaps_1m_to_5m_count,
        "total_gaps_ge_5m": len(gaps_ge_5m),
        "weekend_closures": sum(1 for g in gaps_ge_5m if g["classification"] == "WEEKEND_CLOSURE"),
        "daily_rollovers": sum(1 for g in gaps_ge_5m if g["classification"] == "DAILY_ROLLOVER"),
        "rollover_adjacent_lulls": sum(1 for g in gaps_ge_5m if g["classification"] == "ROLLOVER_ADJACENT_LULL"),
        "intraday_feed_outages_ge_5m": len(intraday_outages),
        "total_intraday_outage_minutes": round(sum(g["duration_min"] for g in intraday_outages), 2),
        "trading_days_total_weekdays": sum(1 for r in daily_rows if r["is_weekend"] == 0),
        "trading_days_ok": sum(1 for r in daily_rows if r["day_status"] == "OK"),
        "trading_days_partial_outage": sum(1 for r in daily_rows if r["day_status"] == "PARTIAL_OUTAGE"),
        "trading_days_severe_outage": sum(1 for r in daily_rows if r["day_status"] == "SEVERE_OUTAGE"),
        "trading_days_no_data_weekdays": sum(1 for r in daily_rows if r["is_weekend"] == 0 and r["day_status"] == "NO_DATA"),
    }
    result["intraday_outages_ge_5m"] = intraday_outages
    result["rollover_and_weekend_gaps"] = scheduled_gaps
    result["elapsed_sec"] = round(time.time() - t0, 2)

    # Save Deliverables 1 & 2
    out_json = OUT_DIR / "g3_1_historical_data_audit.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    out_csv = OUT_DIR / "g3_1_daily_coverage.csv"
    if daily_rows:
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(daily_rows[0].keys()))
            writer.writeheader()
            writer.writerows(daily_rows)

    print(f"[PHASE A DONE in {result['elapsed_sec']}s]")
    print("XAUUSD summary:", json.dumps(result["xauusd_summary"], indent=2))
    print("Validity:", json.dumps(result["quote_validity"], indent=2))
    print("Spread:", json.dumps(result["spread_distribution"], indent=2))
    print("Stale/Order:", json.dumps(result["stale_identical_quotes"], indent=2))
    print("Jumps:", json.dumps(result["price_jump_anomalies"]["counts"], indent=2))
    print("Gaps summary:", json.dumps(result["gaps_summary"], indent=2))
    print(f"Intraday outages >=5m ({len(intraday_outages)}):")
    for g in intraday_outages:
        print(" ", g)
    return result


if __name__ == "__main__":
    run_phase_a()
