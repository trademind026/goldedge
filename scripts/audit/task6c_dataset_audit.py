"""
Task 6C (Phase 1) — Dataset Integrity, Completeness, Idempotency & Hash Verification Audit
covering full recovered XAUUSD history (2026-08-13 through 2026-11-18).
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import pandas as pd
import psycopg2

ROOT = Path(__file__).resolve().parent.parent.parent
if not (ROOT / "backtest_results").exists() and Path("/app/backtest_results").exists():
    ROOT = Path("/app")
AUDIT_DIR = ROOT / "backtest_results" / "audit"
AUDIT_DIR.mkdir(parents=True, exist_ok=True)


def _file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _stream_sha256(conn, start_iso: str, end_iso: str, end_inclusive: bool = False) -> tuple[int, str, str]:
    op = "<=" if end_inclusive else "<"
    h_clean = hashlib.sha256()
    h_t4 = hashlib.sha256()
    cnt = 0
    with conn.cursor(name="task6c_stream_hash") as cur:
        cur.itersize = 50000
        cur.execute(
            f"""
            SELECT time, bid, ask, last, volume, flags
            FROM market_ticks
            WHERE symbol = 'XAUUSD'
              AND time >= %s AND time {op} %s
            ORDER BY time ASC
            """,
            (start_iso, end_iso),
        )
        for t, bid, ask, last, vol, flags in cur:
            cnt += 1
            t_utc = t.astimezone(timezone.utc)
            h_clean.update(f"{t_utc.isoformat()}|{bid}|{ask}|{last}|{vol}|{flags}\n".encode("utf-8"))
            h_t4.update(f"{t.isoformat()}|{bid}|{ask}|{1 if vol is None else vol}\n".encode("utf-8"))
    return cnt, h_clean.hexdigest(), h_t4.hexdigest()


def main():
    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total_rows,
                    COUNT(*) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS valid_rows,
                    COUNT(*) FILTER (WHERE bid > ask) AS inverted_rows,
                    COUNT(*) FILTER (WHERE bid = ask) AS zero_spread_rows,
                    COUNT(*) FILTER (WHERE bid <= 0 OR ask <= 0 OR bid IS NULL OR ask IS NULL) AS non_positive_rows,
                    COUNT(volume) AS non_null_volume_rows,
                    MIN(time) AS min_time,
                    MAX(time) AS max_time
                FROM market_ticks
                WHERE symbol = 'XAUUSD'
                  AND time >= '2026-08-13T00:00:00Z'
                  AND time <= '2026-11-18T18:09:38.022225Z'
            """)
            ov = cur.fetchone()

            cur.execute("""
                WITH dup AS (
                    SELECT time, COUNT(*) c
                    FROM market_ticks
                    WHERE symbol = 'XAUUSD'
                      AND time >= '2026-08-13T00:00:00Z'
                      AND time <= '2026-11-18T18:09:38.022225Z'
                    GROUP BY time HAVING COUNT(*) > 1
                )
                SELECT COUNT(*) FROM dup
            """)
            dup_cnt = cur.fetchone()[0]

            # Per-day stats
            cur.execute("""
                SELECT
                    (time AT TIME ZONE 'UTC')::date AS utc_day,
                    COUNT(*) AS total_ticks,
                    COUNT(*) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS valid_ticks,
                    COUNT(*) FILTER (WHERE bid > ask) AS inverted_ticks,
                    COUNT(*) FILTER (WHERE bid = ask) AS zero_spread_ticks,
                    COUNT(volume) AS non_null_vol,
                    MIN(time) AS first_tick,
                    MAX(time) AS last_tick
                FROM market_ticks
                WHERE symbol = 'XAUUSD'
                  AND time >= '2026-08-13T00:00:00Z'
                  AND time <= '2026-11-18T18:09:38.022225Z'
                GROUP BY 1
                ORDER BY 1
            """)
            day_rows = cur.fetchall()

            # Check ALL gaps > 600s within full history
            cur.execute("""
                WITH ordered AS (
                    SELECT time, LAG(time) OVER (ORDER BY time) AS prev_t
                    FROM market_ticks
                    WHERE symbol = 'XAUUSD'
                      AND time >= '2026-08-13T00:00:00Z'
                      AND time <= '2026-11-18T18:09:38.022225Z'
                )
                SELECT
                    prev_t,
                    time,
                    ROUND(EXTRACT(EPOCH FROM (time - prev_t))::numeric, 1) AS gap_secs,
                    EXTRACT(ISODOW FROM prev_t)::int AS prev_isodow
                FROM ordered
                WHERE EXTRACT(EPOCH FROM (time - prev_t)) > 600
                ORDER BY prev_t
            """)
            gaps = cur.fetchall()

        # Verify exact Task 4 warmup & replay hashes untouched!
        t4_warmup_cnt, t4_warmup_clean_sha, t4_warmup_t4_sha = _stream_sha256(conn, "2026-08-20T00:00:00Z", "2026-08-24T00:00:00Z", end_inclusive=False)
        t4_replay_cnt, t4_replay_clean_sha, t4_replay_t4_sha = _stream_sha256(conn, "2026-08-24T00:00:00Z", "2026-11-16T20:01:23Z", end_inclusive=False)
        w2_cnt, w2_sha, _ = _stream_sha256(conn, "2026-08-13T00:00:00Z", "2026-11-16T20:01:23Z", end_inclusive=False)
        w3_cnt, w3_sha, _ = _stream_sha256(conn, "2026-08-17T00:00:00Z", "2026-11-18T18:00:00Z", end_inclusive=False)
        w3_warmup_cnt, w3_warmup_sha, _ = _stream_sha256(conn, "2026-08-13T00:00:00Z", "2026-08-17T00:00:00Z", end_inclusive=False)

        t4_fp = json.loads((AUDIT_DIR / "task4_data_fingerprint.json").read_text(encoding="utf-8"))

        daily_df = pd.DataFrame(
            day_rows,
            columns=["utc_day", "total_ticks", "valid_ticks", "inverted_ticks", "zero_spread_ticks", "non_null_vol", "first_tick", "last_tick"],
        )
        daily_df.to_csv(AUDIT_DIR / "task6c_daily_integrity.csv", index=False)

        gap_list = []
        unexplained_weekday_gaps = []
        for pt, ct, gs, dow in gaps:
            gs_f = float(gs)
            pt_u = pt.astimezone(timezone.utc)
            ct_u = ct.astimezone(timezone.utc)
            in_est_rollover = (pt_u >= datetime(2026, 11, 1, tzinfo=timezone.utc) and pt_u.hour == 22 and ct_u.hour == 22)
            in_edt_rollover = (pt_u < datetime(2026, 11, 1, tzinfo=timezone.utc) and pt_u.hour == 21 and ct_u.hour == 21)
            if dow == 5 and gs_f > 100000:
                kind = "WEEKEND_CLOSE"
            elif 3500 <= gs_f <= 4200:
                kind = "DAILY_ROLLOVER_BREAK"
            elif in_est_rollover or in_edt_rollover:
                kind = "DAILY_ROLLOVER_KEEPALIVE_GAP"
            elif str(pt.date()) == "2026-09-07":
                kind = "US_LABOR_DAY_EARLY_CLOSE"
            else:
                kind = "UNEXPLAINED_INTRADAY_GAP"
                unexplained_weekday_gaps.append({"prev": pt.isoformat(), "curr": ct.isoformat(), "secs": gs_f})
            gap_list.append({
                "prev_time_utc": pt.isoformat(),
                "next_time_utc": ct.isoformat(),
                "gap_secs": gs_f,
                "classification": kind,
            })
        pd.DataFrame(gap_list).to_csv(AUDIT_DIR / "task6c_session_breaks_and_gaps.csv", index=False)

        report = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "recovered_span": {
                "start_utc": ov[6].astimezone(timezone.utc).isoformat(),
                "end_utc": ov[7].astimezone(timezone.utc).isoformat(),
                "total_rows": int(ov[0]),
                "valid_rows": int(ov[1]),
                "inverted_rows": int(ov[2]),
                "zero_spread_rows": int(ov[3]),
                "non_positive_rows": int(ov[4]),
                "non_null_volume_rows": int(ov[5]),
                "duplicate_timestamps": int(dup_cnt),
                "trading_days": int(len(daily_df)),
            },
            "task4_preservation": {
                "expected_warmup_rows": t4_fp["warmup"]["row_count"],
                "actual_warmup_rows": t4_warmup_cnt,
                "expected_warmup_sha256": t4_fp["warmup"]["stream_sha256"],
                "actual_warmup_legacy_t4_sha256": t4_warmup_t4_sha,
                "actual_warmup_clean_sha256": t4_warmup_clean_sha,
                "warmup_exact_match": (t4_warmup_cnt == t4_fp["warmup"]["row_count"] and t4_warmup_t4_sha == t4_fp["warmup"]["stream_sha256"]),
                "expected_replay_rows": t4_fp["replay"]["row_count"],
                "actual_replay_rows": t4_replay_cnt,
                "expected_replay_sha256": t4_fp["replay"]["stream_sha256"],
                "actual_replay_legacy_t4_sha256": t4_replay_t4_sha,
                "actual_replay_clean_sha256": t4_replay_clean_sha,
                "replay_exact_match": (t4_replay_cnt == t4_fp["replay"]["row_count"] and t4_replay_t4_sha == t4_fp["replay"]["stream_sha256"]),
            },
            "frozen_ledger_hashes": {
                "frozen_111_baseline_trades_sha256": _file_sha256(ROOT / "backtest_results" / "frozen_111_baseline_trades.csv"),
                "task4_run_1_sha256": _file_sha256(AUDIT_DIR / "task4_run_1.csv"),
                "task4_zero_cost_ref_sha256": _file_sha256(AUDIT_DIR / "task4_zero_cost_ref.csv"),
            },
            "window_fingerprints": {
                "W1_task4_20260824_20261116": {"rows": t4_replay_cnt, "stream_sha256": t4_replay_clean_sha, "legacy_t4_sha256": t4_replay_t4_sha},
                "W2_recovered_20260813_20261116": {"rows": w2_cnt, "stream_sha256": w2_sha},
                "W3_warmup_20260813_20260817": {"rows": w3_warmup_cnt, "stream_sha256": w3_warmup_sha},
                "W3_recovered_20260817_20261118": {"rows": w3_cnt, "stream_sha256": w3_sha},
            },
            "gaps_over_600s_total": len(gap_list),
            "unexplained_intraday_gaps_over_600s": unexplained_weekday_gaps,
        }
        (AUDIT_DIR / "task6c_dataset_integrity.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
