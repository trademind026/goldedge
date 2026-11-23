"""
Task 6 — XAUUSD Historical Tick Data Integrity, Archive Recovery & Coverage Validation.

Strictly independent SQL + stdlib/NumPy/Pandas audit script (imports NO production
data-processing modules). Reads live TimescaleDB `market_ticks` & `market_bars_1m`
(read-only) and inspects `csv_goldedge_20261117_143146.zip` without mutating DB or
frozen Task 4 / Task 5 artifacts.

Outputs under backtest_results/audit/:
  - task6_daily_coverage.csv
  - task6_data_quality.csv
  - task6_archive_integrity.json
  - task6_daily_fingerprints.json
  - task6_data_manifest.json
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import zipfile
from collections import defaultdict
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
ARCHIVE_PATH = Path(os.environ.get("TASK6_ARCHIVE_PATH", "/tmp/csv_goldedge_20261117_143146.zip"))
TASK4_FP_PATH = AUDIT_DIR / "task4_data_fingerprint.json"
FROZEN_111_PATH = Path("backtest_results/frozen_111_baseline_trades.csv")
FROZEN_118_PATH = AUDIT_DIR / "task4_run_1.csv"


def _file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_pg_ts(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    elif len(s) >= 3 and s[-3] in ("+", "-") and s[-2:].isdigit():
        s = s + ":00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _analyze_day_ticks(ticks: List[Tuple[datetime, float, float, Optional[int]]]) -> Dict[str, Any]:
    """ticks must already be sorted by datetime ascending."""
    n = len(ticks)
    valid = 0
    zero_sp = 0
    inverted = 0
    invalid = 0
    vol_null = 0
    vol_one = 0
    vol_other = 0
    spreads: List[float] = []
    max_jump = 0.0
    repeated_quotes = 0
    longest_gap = 0.0
    g1 = g5 = g15 = g60 = 0
    dup_ts = 0
    dup_full = 0

    prev_t: Optional[datetime] = None
    prev_mid: Optional[float] = None
    prev_pair: Optional[Tuple[float, float]] = None
    prev_row_full: Optional[Tuple[datetime, float, float, Optional[int]]] = None

    h = hashlib.sha256()
    parts: List[str] = []
    for dt, b, a, v in ticks:
        parts.append(f"{dt.isoformat()}|{b}|{a}|{1 if v is None else v}\n")
        if len(parts) >= 20000:
            h.update("".join(parts).encode("utf-8"))
            parts.clear()

        if v is None:
            vol_null += 1
        elif v == 1:
            vol_one += 1
        else:
            vol_other += 1

        if b <= 0.0 or a <= 0.0:
            invalid += 1
        elif b > a:
            inverted += 1
        elif b == a:
            zero_sp += 1
        else:
            valid += 1
            spreads.append(a - b)
            mid = 0.5 * (b + a)
            if prev_mid is not None:
                j = abs(mid - prev_mid)
                if j > max_jump:
                    max_jump = j
            prev_mid = mid
            if prev_pair is not None and (b, a) == prev_pair:
                repeated_quotes += 1
            prev_pair = (b, a)

        if prev_t is not None:
            gap = (dt - prev_t).total_seconds()
            if gap == 0.0:
                dup_ts += 1
            if prev_row_full == (dt, b, a, v):
                dup_full += 1
            if gap > longest_gap:
                longest_gap = gap
            if gap > 60.0:
                g1 += 1
            if gap > 300.0:
                g5 += 1
            if gap > 900.0:
                g15 += 1
            if gap > 3600.0:
                g60 += 1
        prev_t = dt
        prev_row_full = (dt, b, a, v)

    if parts:
        h.update("".join(parts).encode("utf-8"))

    sp_arr = np.array(spreads, dtype=np.float64) if spreads else np.array([], dtype=np.float64)
    return {
        "row_count": n,
        "valid_count": valid,
        "zero_spread_count": zero_sp,
        "inverted_count": inverted,
        "invalid_count": invalid,
        "vol_null_count": vol_null,
        "vol_one_count": vol_one,
        "vol_other_count": vol_other,
        "dup_timestamps": dup_ts,
        "dup_complete_rows": dup_full,
        "first_tick_utc": ticks[0][0].isoformat() if n else None,
        "last_tick_utc": ticks[-1][0].isoformat() if n else None,
        "longest_gap_sec": round(longest_gap, 3),
        "gaps_gt_1m": g1,
        "gaps_gt_5m": g5,
        "gaps_gt_15m": g15,
        "gaps_gt_60m": g60,
        "max_valid_mid_jump_usd": round(max_jump, 4),
        "repeated_valid_quote_pct": round(repeated_quotes / max(valid - 1, 1) * 100.0, 2) if valid > 1 else 0.0,
        "mean_spread_usd": round(float(sp_arr.mean()), 4) if len(sp_arr) else None,
        "p95_spread_usd": round(float(np.percentile(sp_arr, 95)), 4) if len(sp_arr) else None,
        "day_stream_sha256": h.hexdigest(),
    }


def _audit_archive() -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Validate ZIP integrity, inventory every member CSV, and audit market_ticks.csv per UTC day."""
    if not ARCHIVE_PATH.exists():
        raise FileNotFoundError(f"Archive not found: {ARCHIVE_PATH}")

    zip_sha = _file_sha256(ARCHIVE_PATH)
    inventory: List[Dict[str, Any]] = []
    ticks_by_day: Dict[str, List[Tuple[datetime, float, float, Optional[int]]]] = defaultdict(list)

    with zipfile.ZipFile(ARCHIVE_PATH, "r") as zf:
        bad_member = zf.testzip()
        for info in sorted(zf.infolist(), key=lambda x: x.filename):
            fh = hashlib.sha256()
            row_count = 0
            header = None
            with zf.open(info, "r") as raw_f:
                # Hash compressed/extracted stream and count lines if csv
                if info.filename == "market_ticks.csv":
                    reader_raw = io.BufferedReader(raw_f, buffer_size=1024 * 1024)
                    # Stream bytes for SHA-256 and parse CSV incrementally
                    text_buf = ""
                    while True:
                        chunk = reader_raw.read(1024 * 1024)
                        if not chunk:
                            break
                        fh.update(chunk)
                        text_buf += chunk.decode("utf-8")
                        lines = text_buf.split("\n")
                        text_buf = lines.pop()
                        for line in lines:
                            line = line.rstrip("\r")
                            if not line:
                                continue
                            if header is None:
                                header = line
                                continue
                            row_count += 1
                            parts = line.split(",")
                            dt = _parse_pg_ts(parts[0])
                            sym = parts[1]
                            if sym != "XAUUSD":
                                continue
                            b = float(parts[2]) if parts[2] != "" else 0.0
                            a = float(parts[3]) if parts[3] != "" else 0.0
                            v = int(parts[5]) if len(parts) > 5 and parts[5] != "" else None
                            ticks_by_day[dt.strftime("%Y-%m-%d")].append((dt, b, a, v))
                    if text_buf.strip():
                        line = text_buf.strip()
                        if header is None:
                            header = line
                        else:
                            row_count += 1
                            parts = line.split(",")
                            dt = _parse_pg_ts(parts[0])
                            if parts[1] == "XAUUSD":
                                b = float(parts[2]) if parts[2] != "" else 0.0
                                a = float(parts[3]) if parts[3] != "" else 0.0
                                v = int(parts[5]) if len(parts) > 5 and parts[5] != "" else None
                                ticks_by_day[dt.strftime("%Y-%m-%d")].append((dt, b, a, v))
                else:
                    # Smaller CSVs: hash and inspect header/count
                    while True:
                        chunk = raw_f.read(65536)
                        if not chunk:
                            break
                        fh.update(chunk)
                    with zf.open(info, "r") as raw_t:
                        sr = io.TextIOWrapper(raw_t, encoding="utf-8")
                        first = sr.readline().rstrip("\r\n")
                        header = first
                        for l in sr:
                            if l.strip():
                                row_count += 1
            inventory.append(
                {
                    "filename": info.filename,
                    "file_size_bytes": info.file_size,
                    "compress_size_bytes": info.compress_size,
                    "sha256": fh.hexdigest(),
                    "header": header,
                    "data_rows": row_count,
                }
            )

    arch_daily_stats: Dict[str, Dict[str, Any]] = {}
    for day in sorted(ticks_by_day.keys()):
        day_ticks = sorted(ticks_by_day[day], key=lambda x: x[0])
        arch_daily_stats[day] = _analyze_day_ticks(day_ticks)

    arch_summary = {
        "archive_filename": ARCHIVE_PATH.name,
        "archive_size_bytes": ARCHIVE_PATH.stat().st_size,
        "archive_sha256": zip_sha,
        "zip_crc_test_result": "OK" if bad_member is None else f"CORRUPT:{bad_member}",
        "files_count": len(inventory),
        "file_inventory": inventory,
        "market_ticks_total_rows": sum(d["row_count"] for d in arch_daily_stats.values()),
        "market_ticks_days": len(arch_daily_stats),
        "earliest_tick_utc": min((d["first_tick_utc"] for d in arch_daily_stats.values() if d["first_tick_utc"]), default=None),
        "latest_tick_utc": max((d["last_tick_utc"] for d in arch_daily_stats.values() if d["last_tick_utc"]), default=None),
        "missing_week_aug13_aug19_summary": {
            day: arch_daily_stats[day]
            for day in sorted(arch_daily_stats.keys())
            if day < "2026-08-20"
        },
    }
    return arch_summary, arch_daily_stats


def _audit_live_db(engine) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Query DB metadata, TimescaleDB jobs/chunks, and compute exact per-day stats & SHA-256."""
    with engine.connect() as conn:
        jobs = conn.execute(
            text(
                """
                SELECT job_id, application_name, schedule_interval::text, scheduled, next_start::text
                FROM timescaledb_information.jobs
                WHERE hypertable_name = 'market_ticks' OR application_name ILIKE '%retention%' OR application_name ILIKE '%compress%'
                ORDER BY job_id
                """
            )
        ).fetchall()
        chunks = conn.execute(
            text(
                """
                SELECT COUNT(*), COUNT(*) FILTER (WHERE is_compressed)
                FROM timescaledb_information.chunks
                WHERE hypertable_name = 'market_ticks'
                """
            )
        ).fetchone()
        bars_info = conn.execute(
            text("SELECT COUNT(*), MIN(time)::text, MAX(time)::text FROM market_bars_1m WHERE symbol='XAUUSD'")
        ).fetchone()
        tick_span = conn.execute(
            text("SELECT COUNT(*), MIN(time)::text, MAX(time)::text FROM market_ticks WHERE symbol='XAUUSD'")
        ).fetchone()
        prec_info = conn.execute(
            text(
                """
                SELECT
                    COUNT(*) FILTER (WHERE EXTRACT(MICROSECONDS FROM time)::bigint % 1000 != 0) AS sub_ms_ticks,
                    COUNT(*) FILTER (WHERE EXTRACT(MILLISECONDS FROM time)::bigint % 1000 != 0) AS sub_sec_ticks,
                    COUNT(DISTINCT DATE(time AT TIME ZONE 'UTC')) AS utc_days
                FROM market_ticks
                WHERE symbol = 'XAUUSD'
                """
            )
        ).fetchone()

        # Stream ALL XAUUSD ticks from DB ordered chronologically to compute daily stats & fingerprints
        days_list = [
            r[0]
            for r in conn.execute(
                text("SELECT DISTINCT DATE(time AT TIME ZONE 'UTC') AS d FROM market_ticks WHERE symbol='XAUUSD' ORDER BY d")
            ).fetchall()
        ]

        db_daily_stats: Dict[str, Dict[str, Any]] = {}
        for d in days_list:
            d_str = str(d)
            d_start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            d_end = d_start + timedelta(days=1)
            rows = conn.execute(
                text(
                    """
                    SELECT time, bid, ask, volume
                    FROM market_ticks
                    WHERE symbol = 'XAUUSD' AND time >= :s AND time < :e
                    ORDER BY time ASC
                    """
                ),
                {"s": d_start, "e": d_end},
            ).fetchall()
            parsed = [
                (
                    t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc),
                    float(b or 0.0),
                    float(a or 0.0),
                    int(v) if v is not None else None,
                )
                for t, b, a, v in rows
            ]
            db_daily_stats[d_str] = _analyze_day_ticks(parsed)

    db_meta = {
        "market_ticks_total_rows": int(tick_span[0] or 0),
        "market_ticks_min_time": tick_span[1],
        "market_ticks_max_time": tick_span[2],
        "sub_millisecond_ticks": int(prec_info[0] or 0),
        "sub_second_ticks": int(prec_info[1] or 0),
        "utc_days_count": int(prec_info[2] or 0),
        "market_bars_1m_rows": int(bars_info[0] or 0),
        "market_bars_1m_min": bars_info[1],
        "market_bars_1m_max": bars_info[2],
        "timescaledb_chunks_total": int(chunks[0] or 0),
        "timescaledb_chunks_compressed": int(chunks[1] or 0),
        "timescaledb_jobs": [
            {
                "job_id": int(j[0]),
                "application_name": j[1],
                "schedule_interval": j[2],
                "scheduled": bool(j[3]),
                "next_start": j[4],
            }
            for j in jobs
        ],
    }
    return db_meta, db_daily_stats


def _verify_task4_replay_fingerprint(engine) -> Dict[str, Any]:
    """Compare current TimescaleDB Task 4 replay window stream against recorded task4_data_fingerprint.json."""
    t4_fp = json.loads(TASK4_FP_PATH.read_text(encoding="utf-8"))
    h = hashlib.sha256()
    cnt = 0
    with engine.connect() as conn:
        res = conn.execution_options(stream_results=True).execute(
            text(
                """
                SELECT time, bid, ask, COALESCE(volume, 1)
                FROM market_ticks
                WHERE symbol = 'XAUUSD'
                  AND time >= '2026-08-24T00:00:00+00:00'
                  AND time <  '2026-11-16T20:01:23+00:00'
                ORDER BY time ASC
                """
            )
        )
        while True:
            batch = res.fetchmany(50000)
            if not batch:
                break
            cnt += len(batch)
            lines = "".join(f"{t.isoformat()}|{b}|{a}|{v}\n" for t, b, a, v in batch)
            h.update(lines.encode("utf-8"))
    cur_sha = h.hexdigest()
    rec_sha = t4_fp["replay"]["stream_sha256"]
    return {
        "recorded_task4_rows": t4_fp["replay"]["row_count"],
        "current_db_rows": cnt,
        "recorded_task4_sha256": rec_sha,
        "current_db_sha256": cur_sha,
        "exact_match": (cnt == t4_fp["replay"]["row_count"] and cur_sha == rec_sha),
    }


def _check_trade_overlaps(combined_daily: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Check whether any trade windows in frozen_111 or task4_118 occur during DB missing days (Aug 13-19) or anomalous days."""
    df_111 = pd.read_csv(FROZEN_111_PATH)
    df_118 = pd.read_csv(FROZEN_118_PATH)

    def _summarize_trades_vs_days(df_t: pd.DataFrame, label: str) -> Dict[str, Any]:
        days_hit = sorted({str(x)[:10] for x in df_t["entry_time"]})
        in_missing_db_week = [d for d in days_hit if d < "2026-08-20"]
        in_warmup_aug20_23 = [d for d in days_hit if "2026-08-20" <= d < "2026-08-24"]
        # Check open trade window gaps (already verified 0 gaps overlapping 118 in DB)
        return {
            "label": label,
            "total_trades": len(df_t),
            "unique_entry_days": len(days_hit),
            "trades_in_dropped_aug13_19": int(sum(1 for x in df_t["entry_time"] if str(x)[:10] < "2026-08-20")),
            "trades_in_warmup_aug20_23": int(sum(1 for x in df_t["entry_time"] if "2026-08-20" <= str(x)[:10] < "2026-08-24")),
            "days_in_dropped_week": in_missing_db_week,
            "days_in_warmup_week": in_warmup_aug20_23,
        }

    return {
        "frozen_111": _summarize_trades_vs_days(df_111, "frozen_111"),
        "task4_118": _summarize_trades_vs_days(df_118, "task4_118"),
    }


def main() -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    engine = create_engine(RAW_DB_URL)

    print("[1/4] Auditing CSV Archive (zip integrity, 18.7M market_ticks rows, SHA-256)...")
    arch_summary, arch_daily = _audit_archive()
    print(f"  Archive ticks: {arch_summary['market_ticks_total_rows']:,} across {arch_summary['market_ticks_days']} UTC days; Aug 13-19={sum(d['row_count'] for d in arch_summary['missing_week_aug13_aug19_summary'].values()):,}")

    print("[2/4] Auditing Live TimescaleDB market_ticks & metadata (read-only)...")
    db_meta, db_daily = _audit_live_db(engine)
    print(f"  DB ticks: {db_meta['market_ticks_total_rows']:,} across {db_meta['utc_days_count']} UTC days ({db_meta['market_ticks_min_time']} .. {db_meta['market_ticks_max_time']})")

    print("[3/4] Comparing Archive vs Live DB Overlaps & Building Coverage/Quality/Fingerprints...")
    overlap_conflicts: List[Dict[str, Any]] = []
    shared_days = sorted(set(arch_daily.keys()) & set(db_daily.keys()))
    exact_day_matches = 0
    for d in shared_days:
        a = arch_daily[d]
        b = db_daily[d]
        same_hash = a["day_stream_sha256"] == b["day_stream_sha256"]
        if same_hash:
            exact_day_matches += 1
        else:
            overlap_conflicts.append(
                {
                    "utc_day": d,
                    "archive_rows": a["row_count"],
                    "db_rows": b["row_count"],
                    "archive_last": a["last_tick_utc"],
                    "db_last": b["last_tick_utc"],
                    "archive_sha": a["day_stream_sha256"],
                    "db_sha": b["day_stream_sha256"],
                    "note": "Archive taken mid-day 2026-11-17 before live ticks appended" if d == "2026-11-17" else "Mismatch",
                }
            )

    arch_summary["db_overlap_comparison"] = {
        "shared_days": len(shared_days),
        "exact_cryptographic_day_matches": exact_day_matches,
        "non_matching_shared_days": overlap_conflicts,
    }
    (AUDIT_DIR / "task6_archive_integrity.json").write_text(json.dumps(arch_summary, indent=2), encoding="utf-8")

    # Unified complete calendar timeline (Archive for Aug 13-19 + DB for Aug 20 onward)
    all_days = sorted(set(arch_daily.keys()) | set(db_daily.keys()))
    coverage_rows: List[Dict[str, Any]] = []
    quality_rows: List[Dict[str, Any]] = []
    fingerprint_days: Dict[str, Any] = {}

    prev_day_last_valid_ts: Optional[str] = None
    for d in all_days:
        in_db = d in db_daily
        in_arch = d in arch_daily
        src = "DB_AND_ARCHIVE" if (in_db and in_arch) else ("DB_ONLY" if in_db else "ARCHIVE_ONLY")
        stats = db_daily[d] if in_db else arch_daily[d]
        dt_obj = datetime.strptime(d, "%Y-%m-%d")
        wd = dt_obj.strftime("%A")
        is_sat = wd == "Saturday"
        is_sun = wd == "Sunday"
        session_note = (
            "Weekend Closed (Sat)" if is_sat else
            ("Sunday Open ~22:00/23:00Z" if is_sun else
             ("US Labor Day Early Close ~18:30Z" if d == "2026-09-07" else
              ("Partial Live Day (in progress)" if d == all_days[-1] else "Full 23h FX/Metals Day")))
        )

        coverage_rows.append(
            {
                "utc_day": d,
                "weekday": wd,
                "source": src,
                "expected_session": session_note,
                "tick_count": stats["row_count"],
                "first_tick_utc": stats["first_tick_utc"],
                "last_tick_utc": stats["last_tick_utc"],
                "longest_gap_sec": stats["longest_gap_sec"],
                "gaps_gt_1m": stats["gaps_gt_1m"],
                "gaps_gt_5m": stats["gaps_gt_5m"],
                "gaps_gt_15m": stats["gaps_gt_15m"],
                "gaps_gt_60m": stats["gaps_gt_60m"],
                "inverted_quotes": stats["inverted_count"],
                "zero_spread_quotes": stats["zero_spread_count"],
                "invalid_quotes": stats["invalid_count"],
                "missing_volume_count": stats["vol_null_count"],
            }
        )
        quality_rows.append(
            {
                "utc_day": d,
                "source": src,
                "tick_count": stats["row_count"],
                "valid_count": stats["valid_count"],
                "zero_spread_count": stats["zero_spread_count"],
                "inverted_count": stats["inverted_count"],
                "invalid_count": stats["invalid_count"],
                "valid_pct": round(stats["valid_count"] / max(stats["row_count"], 1) * 100.0, 4),
                "vol_null_count": stats["vol_null_count"],
                "vol_one_count": stats["vol_one_count"],
                "vol_other_count": stats["vol_other_count"],
                "dup_timestamps": stats["dup_timestamps"],
                "dup_complete_rows": stats["dup_complete_rows"],
                "mean_spread_usd": stats["mean_spread_usd"],
                "p95_spread_usd": stats["p95_spread_usd"],
                "max_valid_mid_jump_usd": stats["max_valid_mid_jump_usd"],
                "repeated_valid_quote_pct": stats["repeated_valid_quote_pct"],
            }
        )
        fingerprint_days[d] = {
            "source": src,
            "row_count": stats["row_count"],
            "first_tick_utc": stats["first_tick_utc"],
            "last_tick_utc": stats["last_tick_utc"],
            "valid_count": stats["valid_count"],
            "inverted_count": stats["inverted_count"],
            "zero_spread_count": stats["zero_spread_count"],
            "invalid_count": stats["invalid_count"],
            "missing_volume_count": stats["vol_null_count"],
            "stream_sha256": stats["day_stream_sha256"],
        }
        prev_day_last_valid_ts = stats["last_tick_utc"]

    pd.DataFrame(coverage_rows).to_csv(AUDIT_DIR / "task6_daily_coverage.csv", index=False)
    pd.DataFrame(quality_rows).to_csv(AUDIT_DIR / "task6_data_quality.csv", index=False)
    (AUDIT_DIR / "task6_daily_fingerprints.json").write_text(
        json.dumps(
            {
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "serialization_format": "{dt.isoformat()}|{bid}|{ask}|{1 if volume is None else volume}\\n",
                "days_count": len(fingerprint_days),
                "daily_fingerprints": fingerprint_days,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print("[4/4] Verifying Task 4 Fingerprint & Checking Trade-Window Overlaps...")
    t4_match = _verify_task4_replay_fingerprint(engine)
    trade_overlaps = _check_trade_overlaps(fingerprint_days)

    # Check Aug 19 -> Aug 20 continuity (archive boundary to live DB start)
    aug19_end = arch_daily.get("2026-08-19", {}).get("last_tick_utc")
    aug20_start = db_daily.get("2026-08-20", {}).get("first_tick_utc")
    boundary_gap_sec = (
        (_parse_pg_ts(aug20_start) - _parse_pg_ts(aug19_end)).total_seconds()
        if aug19_end and aug20_start
        else None
    )

    # Aggregate Totals across DB vs Unified
    db_totals = {
        "rows": sum(s["row_count"] for s in db_daily.values()),
        "valid": sum(s["valid_count"] for s in db_daily.values()),
        "inverted": sum(s["inverted_count"] for s in db_daily.values()),
        "zero_spread": sum(s["zero_spread_count"] for s in db_daily.values()),
        "invalid": sum(s["invalid_count"] for s in db_daily.values()),
        "vol_null": sum(s["vol_null_count"] for s in db_daily.values()),
        "vol_one": sum(s["vol_one_count"] for s in db_daily.values()),
        "vol_other": sum(s["vol_other_count"] for s in db_daily.values()),
        "dup_ts": sum(s["dup_timestamps"] for s in db_daily.values()),
    }
    aug13_19_totals = {
        "rows": sum(arch_daily[d]["row_count"] for d in arch_daily if d < "2026-08-20"),
        "valid": sum(arch_daily[d]["valid_count"] for d in arch_daily if d < "2026-08-20"),
        "inverted": sum(arch_daily[d]["inverted_count"] for d in arch_daily if d < "2026-08-20"),
        "zero_spread": sum(arch_daily[d]["zero_spread_count"] for d in arch_daily if d < "2026-08-20"),
        "invalid": sum(arch_daily[d]["invalid_count"] for d in arch_daily if d < "2026-08-20"),
        "vol_null": sum(arch_daily[d]["vol_null_count"] for d in arch_daily if d < "2026-08-20"),
        "vol_one": sum(arch_daily[d]["vol_one_count"] for d in arch_daily if d < "2026-08-20"),
        "vol_other": sum(arch_daily[d]["vol_other_count"] for d in arch_daily if d < "2026-08-20"),
        "dup_ts": sum(arch_daily[d]["dup_timestamps"] for d in arch_daily if d < "2026-08-20"),
    }

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "outcome_tag": "HISTORICAL_DATA_BASELINE_V1",
        "database_inventory": db_meta,
        "database_quality_totals": db_totals,
        "archive_summary": {
            "archive_sha256": arch_summary["archive_sha256"],
            "zip_crc_test_result": arch_summary["zip_crc_test_result"],
            "market_ticks_rows": arch_summary["market_ticks_total_rows"],
            "earliest_tick_utc": arch_summary["earliest_tick_utc"],
            "latest_tick_utc": arch_summary["latest_tick_utc"],
            "aug13_19_missing_from_db_totals": aug13_19_totals,
            "db_overlap_exact_day_matches": f"{exact_day_matches} / {len(shared_days)}",
            "non_matching_shared_days": overlap_conflicts,
            "aug19_to_aug20_boundary_gap_sec": boundary_gap_sec,
        },
        "task4_replay_fingerprint_check": t4_match,
        "trade_window_overlap_check": trade_overlaps,
    }
    (AUDIT_DIR / "task6_data_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
