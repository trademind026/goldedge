"""
Task G2 — Clean-Room Reference Exporter for Phase F Application Reconciliation.

STRICT RULE: ZERO imports from `src/goldedge/`.
Run with `PYTHONPATH=/app:/app/scripts/audit` (excluding `/app/src`).
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
import g1_clean_room_engine as g1
import g2_clean_room_scorer as g2

FULL_LOAD_START = datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc)
FULL_LOAD_END = datetime(2026, 11, 19, 23, 59, 59, tzinfo=timezone.utc)
FULL_START = datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc)


def _export_res(res: dict[str, Any]) -> dict[str, Any]:
    trades_out = []
    for t in res["trade_list"]:
        trades_out.append({
            "entry_time": t["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),
            "exit_time": t["exit_time"].strftime("%Y-%m-%d %H:%M:%S"),
            "direction": t["direction"],
            "strategy": t["strategy"],
            "regime": t["regime"],
            "score": round(float(t["score"]), 4),
            "entry_price": float(t["entry_price"]),
            "exit_price": float(t["exit_price"]),
            "sl": float(t["sl"]),
            "target": float(t["tp"]),
            "qty": float(t["qty"]),
            "gross_pnl": float(t["gross_pnl"]),
            "pnl": float(t["pnl"]),
            "commission_usd": float(t["commission_usd"]),
            "slippage_usd": float(t["slippage_usd"]),
            "swap_usd": float(t["swap_usd"]),
            "exit_reason": t["exit_reason"],
        })
    return {
        "trades": int(res["trades"]),
        "wins": int(res["wins"]),
        "losses": int(res["losses"]),
        "pnl": float(res["pnl"]),
        "pf": float(res["pf"]),
        "final_equity": float(res["final_equity"]),
        "closed_max_dd": float(res["closed_max_dd"]),
        "closed_max_dd_pct": float(res["closed_max_dd_pct"]),
        "mtm_max_dd": float(res["mtm_max_dd"]),
        "mtm_max_dd_pct": float(res["mtm_max_dd_pct"]),
        "total_commission_usd": float(res["commission_usd"]),
        "total_slippage_usd": float(res["slippage_usd"]),
        "total_swap_usd": float(res["swap_usd"]),
        "hwm_kill_switch": res["hwm_kill_switch"],
        "trade_list": trades_out,
    }


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/g2_clean_room_refs.json")
    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*), MIN(time), MAX(time)
                FROM market_ticks
                WHERE symbol = %s AND time >= %s AND time < %s
                """,
                ("XAUUSD", FULL_LOAD_START, FULL_LOAD_END),
            )
            tot_cnt, min_t, max_t = cur.fetchone()
            cur.execute(
                """
                SELECT (time AT TIME ZONE 'UTC')::date AS d, COUNT(*)
                FROM market_ticks
                WHERE symbol = %s AND time >= %s AND time < %s
                GROUP BY 1 ORDER BY 1
                """,
                ("XAUUSD", FULL_LOAD_START, FULL_LOAD_END),
            )
            daily_rows = [(str(r[0]), int(r[1])) for r in cur.fetchall()]
        day_sha = hashlib.sha256(json.dumps(daily_rows).encode("utf-8")).hexdigest()
        raw_span = g1.load_prepared_span(conn, "XAUUSD", FULL_LOAD_START, FULL_LOAD_END, chunk_days=7)
    finally:
        conn.close()

    sp = g1.slice_prepared_span(raw_span, FULL_START, FULL_LOAD_END)
    cache = g2.precompute_g2_span_cache(sp)

    cfg_h0 = g1.CleanSimConfig(
        use_kalman_exit=True, session_close_utc=15, early_fail_mins=15,
        early_fail_min_mfe_atr=0.4, max_score=None, cost_mode="fusion_ctrader_demo", max_hwm_dd_pct=8.0,
    )
    cfg_h3b = g1.CleanSimConfig(
        use_kalman_exit=True, session_close_utc=15, early_fail_mins=15,
        early_fail_min_mfe_atr=0.4, max_score=0.80, cost_mode="fusion_ctrader_demo", max_hwm_dd_pct=8.0,
    )
    cfg_g2 = g1.CleanSimConfig(
        use_kalman_exit=True, session_close_utc=15, early_fail_mins=15,
        early_fail_min_mfe_atr=0.4, max_score=None, cost_mode="fusion_ctrader_demo", max_hwm_dd_pct=8.0,
    )

    res_h0 = g2.run_g2_backtest(sp, cache, cfg_h0, scoring_mode="legacy")
    res_h3b = g2.run_g2_backtest(sp, cache, cfg_h3b, scoring_mode="legacy")
    res_g2 = g2.run_g2_backtest(sp, cache, cfg_g2, scoring_mode="g2_e_decorrelated")

    cand_samples = []
    for idx_m in sorted(cache.candidate_minutes.keys()):
        pkt = cache.candidate_minutes[idx_m]
        m_py = pkt["minute_ts"]
        reg = pkt["regime"]
        htf = int(pkt["htf_trend"])
        feat = pkt["feat"]
        for st_name, dir_val in pkt["sigs"]:
            sc_leg, _ = g2.score_candidate(dir_val, reg, feat, mode="legacy")
            sc_g2, comp_g2 = g2.score_candidate(dir_val, reg, feat, mode="g2_e_decorrelated")
            in_sess_15 = (m_py.hour < 15)
            dir_ok = (dir_val == "BUY" and htf == 1) or (dir_val == "SELL" and htf == -1)
            reg_ok = not ((reg == "HIGH_VOLATILITY") or (dir_val == "BUY" and reg == "SIDEWAYS"))
            cand_samples.append({
                "timestamp_utc": m_py.isoformat(),
                "strategy": st_name,
                "direction": dir_val,
                "regime": reg,
                "close": float(feat["close"]),
                "is_eligible_setup": bool(in_sess_15 and htf != 0 and dir_ok and reg_ok),
                "feat": {
                    "kalman_vel": float(feat["kalman_vel"]),
                    "kalman_vel_1m_atr": float(feat["kalman_vel_1m_atr"]),
                    "kalman_accel": float(feat["kalman_accel"]),
                    "shannon_entropy": float(feat["shannon_entropy"]),
                    "shannon_entropy_5m_norm": float(feat["shannon_entropy_5m_norm"]),
                    "kaufman_er": float(feat["kaufman_er"]),
                    "ema20_slope": float(feat["ema20_slope"]),
                    "tick_weighted_price_dist": float(feat["tick_weighted_price_dist"]),
                    "tick_rate_ratio": float(feat["tick_rate_ratio"]),
                    "utc_hour": int(feat["utc_hour"]),
                    "atr": float(feat["atr"]),
                    "ema20_dist_atr": float(feat["ema20_dist_atr"]),
                    "entry_5m_tick_rate": float(feat["entry_5m_tick_rate"]),
                },
                "legacy_score": float(sc_leg),
                "g2_score": float(sc_g2),
                "g2_comp": comp_g2,
            })

    payload = {
        "dataset_fingerprint": {
            "symbol": "XAUUSD",
            "load_start": FULL_LOAD_START.isoformat(),
            "load_end": FULL_LOAD_END.isoformat(),
            "replay_start": FULL_START.isoformat(),
            "tick_count": int(tot_cnt),
            "min_time": min_t.isoformat() if min_t else None,
            "max_time": max_t.isoformat() if max_t else None,
            "daily_counts_sha256": day_sha,
        },
        "H0": _export_res(res_h0),
        "H3b": _export_res(res_h3b),
        "G2": _export_res(res_g2),
        "candidate_samples": cand_samples,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[G2 REF] Wrote {out_path} (H0={res_h0['trades']}, H3b={res_h3b['trades']}, G2={res_g2['trades']})", flush=True)


if __name__ == "__main__":
    main()
