"""
Task 12 (Phases 1–6) — XAUUSD Session & Time-of-Day Baseline Verification,
Application Session Logic Audit, DST-Aware Feature Construction, H0 vs H4a
Hourly/Session/Weekday/BUY-SELL Analysis, and Intraday Market Behavior.

Zero imports from `src/goldedge/`.
Produces deliverables #1–#8 under `backtest_results/audit/task12_session_analysis/`:
  1. task12_baseline_verification.json
  2. task12_session_logic_audit.json
  3. task12_intraday_features.csv
  4. task12_trade_session_enrichment.csv
  5. task12_hourly_performance.csv
  6. task12_session_performance.json
  7. task12_buy_sell_session_analysis.csv
  8. task12_intraday_market_behavior.json
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / "backtest_results").exists() and Path("/app/backtest_results").exists():
    ROOT = Path("/app")

AUDIT_DIR = ROOT / "backtest_results" / "audit"
FROZEN_V2_DIR = AUDIT_DIR / "frozen_v2_recovered"
OUT_DIR = AUDIT_DIR / "task12_session_analysis"
OUT_DIR.mkdir(parents=True, exist_ok=True)

_LDN_TZ = ZoneInfo("Europe/London")
_NY_TZ = ZoneInfo("America/New_York")
_TYO_TZ = ZoneInfo("Asia/Tokyo")

# Relevant UK/US market holidays during 2026-08-13 .. 2026-11-18
_HOLIDAYS: dict[date, str] = {
    date(2026, 8, 31): "UK_SUMMER_BANK_HOLIDAY",
    date(2026, 9, 7): "US_LABOR_DAY",
    date(2026, 10, 12): "US_COLUMBUS_DAY",
    date(2026, 11, 11): "US_VETERANS_DAY",
}

_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _parse_utc(ts_str: str) -> datetime:
    dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def classify_dst_session(dt_utc: datetime) -> dict[str, Any]:
    """Strictly causal DST-aware session classification from canonical UTC timestamp.

    Uses independent IANA timezone rules:
      - Asia/Tokyo    (no DST): 09:00–18:00 JST = 00:00–09:00 UTC
      - Europe/London (BST UTC+1 until 2026-10-25 01:00Z, then GMT UTC+0): 08:00–16:30 local
      - America/New_York (EDT UTC-4 until 2026-11-01 06:00Z, then EST UTC-5): 08:00–17:00 local
    """
    u = dt_utc.astimezone(timezone.utc)
    ldn = u.astimezone(_LDN_TZ)
    ny = u.astimezone(_NY_TZ)
    tyo = u.astimezone(_TYO_TZ)

    ldn_mins = ldn.hour * 60 + ldn.minute + ldn.second / 60.0
    ny_mins = ny.hour * 60 + ny.minute + ny.second / 60.0
    tyo_mins = tyo.hour * 60 + tyo.minute + tyo.second / 60.0

    # Session windows in local minutes
    tyo_open, tyo_close = 9 * 60, 18 * 60         # 09:00–18:00 JST (00:00–09:00 UTC)
    ldn_open, ldn_close = 8 * 60, 16 * 60 + 30    # 08:00–16:30 London local
    ny_open, ny_close = 8 * 60, 17 * 60           # 08:00–17:00 NY local

    in_asian = tyo_open <= tyo_mins < tyo_close
    in_london = ldn_open <= ldn_mins < ldn_close
    in_ny = ny_open <= ny_mins < ny_close
    in_overlap = in_london and in_ny

    if in_overlap:
        primary_session = "LONDON_NY_OVERLAP"
    elif in_london:
        primary_session = "LONDON_PRE_OVERLAP"
    elif in_ny:
        primary_session = "NY_POST_LONDON"
    elif in_asian:
        primary_session = "ASIAN_ONLY"
    else:
        primary_session = "OFF_HOURS"

    mins_since_ldn_open = round(ldn_mins - ldn_open, 2)
    mins_until_ldn_close = round(ldn_close - ldn_mins, 2)
    mins_since_ny_open = round(ny_mins - ny_open, 2)
    mins_until_ny_close = round(ny_close - ny_mins, 2)

    is_first_30m_ldn = 0.0 <= mins_since_ldn_open < 30.0
    is_last_30m_ldn = 0.0 < mins_until_ldn_close <= 30.0
    is_first_30m_ny = 0.0 <= mins_since_ny_open < 30.0
    is_last_30m_ny = 0.0 < mins_until_ny_close <= 30.0
    is_first_30m_any = is_first_30m_ldn or is_first_30m_ny
    is_last_30m_any = is_last_30m_ldn or is_last_30m_ny

    # Session transition: within +-15 minutes of London open, NY open, London close, or Tokyo close
    is_transition = (
        abs(ldn_mins - ldn_open) <= 15.0
        or abs(ny_mins - ny_open) <= 15.0
        or abs(ldn_mins - ldn_close) <= 15.0
        or abs(tyo_mins - tyo_close) <= 15.0
    )

    uk_tz = ldn.tzname() or "UTC"
    us_tz = ny.tzname() or "UTC"
    if uk_tz == "BST" and us_tz == "EDT":
        dst_regime = "BOTH_DST_BST_EDT"
    elif uk_tz == "GMT" and us_tz == "EDT":
        dst_regime = "MISMATCH_GMT_EDT"
    else:
        dst_regime = "BOTH_STD_GMT_EST"

    hol = _HOLIDAYS.get(u.date(), "NONE")
    return {
        "utc_hour": u.hour,
        "utc_weekday_idx": u.weekday(),
        "utc_weekday": _WEEKDAY_NAMES[u.weekday()],
        "uk_tz": uk_tz,
        "us_tz": us_tz,
        "dst_regime": dst_regime,
        "in_asian": int(in_asian),
        "in_london": int(in_london),
        "in_ny": int(in_ny),
        "in_ldn_ny_overlap": int(in_overlap),
        "dst_session": primary_session,
        "mins_since_ldn_open": mins_since_ldn_open,
        "mins_until_ldn_close": mins_until_ldn_close,
        "mins_since_ny_open": mins_since_ny_open,
        "mins_until_ny_close": mins_until_ny_close,
        "is_first_30m_ldn": int(is_first_30m_ldn),
        "is_last_30m_ldn": int(is_last_30m_ldn),
        "is_first_30m_ny": int(is_first_30m_ny),
        "is_last_30m_ny": int(is_last_30m_ny),
        "is_first_30m_any": int(is_first_30m_any),
        "is_last_30m_any": int(is_last_30m_any),
        "is_session_transition": int(is_transition),
        "holiday_name": hol,
        "is_holiday": int(hol != "NONE"),
    }


def _group_metrics(df: pd.DataFrame) -> dict[str, Any]:
    n = len(df)
    if n == 0:
        return {
            "n": 0, "wins": 0, "losses": 0, "wr_pct": 0.0, "net_pnl": 0.0,
            "pf": 0.0, "exp": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
            "max_consec_losses": 0, "closed_dd": 0.0,
        }
    p = df["net_pnl_usd"].values.astype(float)
    w = p[p > 0]
    l = p[p <= 0]
    pf = float(w.sum() / abs(l.sum())) if len(l) and l.sum() < 0 else (999.0 if len(w) and w.sum() > 0 else 0.0)
    # Max consecutive losses within this slice
    mcl = cur = 0
    for v in p:
        if v <= 0:
            cur += 1
            mcl = max(mcl, cur)
        else:
            cur = 0
    ceq = np.cumsum(p)
    hwm = np.maximum.accumulate(np.insert(ceq, 0, 0.0))[1:]
    cdd = float((ceq - hwm).min())
    return {
        "n": int(n),
        "wins": int(len(w)),
        "losses": int(len(l)),
        "wr_pct": round(len(w) / n * 100.0, 2),
        "net_pnl": round(float(p.sum()), 2),
        "pf": round(pf, 4),
        "exp": round(float(p.mean()), 2),
        "avg_win": round(float(w.mean()), 2) if len(w) else 0.0,
        "avg_loss": round(float(l.mean()), 2) if len(l) else 0.0,
        "max_consec_losses": int(mcl),
        "closed_dd": round(cdd, 2),
    }


def main() -> None:
    # =========================================================================
    # PHASE 1: Baseline & Prior Artifact Verification
    # =========================================================================
    v2_man = json.loads((FROZEN_V2_DIR / "task6c_frozen_manifest.json").read_text(encoding="utf-8"))
    t11a_rec = json.loads((AUDIT_DIR / "task11a_candidate_reconciliation.json").read_text(encoding="utf-8"))
    t11a_val = json.loads((AUDIT_DIR / "task11a_validation_tests.json").read_text(encoding="utf-8"))
    settings_text = (ROOT / "src" / "goldedge" / "config" / "settings.py").read_text(encoding="utf-8")

    t4_hash = _sha256(ROOT / "backtest_results" / "frozen_111_baseline_trades.csv")
    t4_exp = v2_man["prior_frozen_task4_preserved"]["frozen_111_baseline_trades_sha256"].upper()
    w1_hash = _sha256(FROZEN_V2_DIR / "w1_trades.csv")
    w2_hash = _sha256(FROZEN_V2_DIR / "w2_trades.csv")
    w3_hash = _sha256(FROZEN_V2_DIR / "w3_trades.csv")

    hwm_match = bool(re.search(r"HWM_MAX_DRAWDOWN_PCT:\s*float\s*=\s*Field\(\s*8\.0\b", settings_text))
    shadow_ok = bool(re.search(r"SHADOW_MODE:\s*bool\s*=\s*Field\(\s*True\b", settings_text))
    demo_ok = bool(re.search(r'TRADING_MODE:\s*str\s*=\s*Field\(\s*"demo"', settings_text))

    baseline_ver = {
        "task4_frozen_trades_sha256": t4_hash,
        "task4_match": t4_hash == t4_exp,
        "v2_w1_trades_sha256": w1_hash,
        "v2_w1_match": w1_hash == v2_man["frozen_artifacts_sha256"]["w1_trades.csv"].upper(),
        "v2_w2_trades_sha256": w2_hash,
        "v2_w2_match": w2_hash == v2_man["frozen_artifacts_sha256"]["w2_trades.csv"].upper(),
        "v2_w3_trades_sha256": w3_hash,
        "v2_w3_match": w3_hash == v2_man["frozen_artifacts_sha256"]["w3_trades.csv"].upper(),
        "dataset_fingerprints": v2_man["dataset_fingerprints"],
        "task8_hwm_8pct_config_verified": hwm_match,
        "safety_locks": {"SHADOW_MODE_True": shadow_ok, "TRADING_MODE_demo": demo_ok},
        "task11a_verified_pairs": t11a_val["fully_reconciled_pairs"],
        "task11a_all_checks_passed": t11a_val["all_checks_passed"],
        "h0_task11a_ref": {w: t11a_rec[w]["H0_BASELINE_V2"] for w in ("W1", "W2", "W3")},
        "h4a_task11a_ref": {w: t11a_rec[w]["H8_H4a_ALL_REGIMES"] for w in ("W1", "W2", "W3")},
    }
    assert baseline_ver["task4_match"] and baseline_ver["v2_w1_match"] and baseline_ver["v2_w2_match"] and baseline_ver["v2_w3_match"], "Frozen hash mismatch!"
    assert t11a_val["all_checks_passed"] and hwm_match and shadow_ok and demo_ok, "Safety or Task 11A check failed!"
    (OUT_DIR / "task12_baseline_verification.json").write_text(json.dumps(baseline_ver, indent=2), encoding="utf-8")

    # =========================================================================
    # PHASE 2: Application Session Logic Audit (Read-Only)
    # =========================================================================
    session_audit = {
        "canonical_timestamp": "UTC (PostgreSQL market_ticks.time, pandas DatetimeTZDtype UTC)",
        "signal_generator_session_gate": {
            "source": "src/goldedge/domain/services/signal_generator.py (lines 13-23, 70-71)",
            "rule": "_SESSION_OPEN_UTC = 7, _SESSION_CLOSE_UTC = 17 -> 7 <= utc_hour < 17",
            "dst_aware": False,
            "weekday_restrictions": "None (trades Mon-Fri identically; weekend has no ticks)",
            "holiday_restrictions": "None (trades UK Summer Bank Holiday, US Labor Day, Columbus Day, Veterans Day)",
            "orb_hardcoded_utc": "07:00-08:30 UTC and 12:30-14:00 UTC (fixed UTC, line 345)",
            "dst_mismatch_finding": (
                "Comment assumes London opens 08:00 BST = 07:00 UTC year-round. After UK switches to GMT on "
                "2026-10-25 01:00Z, 07:00-07:59 UTC is pre-London/late Asia, while 16:00-16:30 London local "
                "remains inside 16:00 UTC. After US switches to EST on 2026-11-01 06:00Z, NY open moves from "
                "12:00 UTC to 13:00 UTC."
            ),
        },
        "trade_scorer_session_component": {
            "source": "src/goldedge/domain/services/trade_scorer.py (lines 117, 143, 257-274)",
            "weight": 0.05,
            "dst_aware": False,
            "fixed_utc_schedule": {
                "00-06 UTC": 0.0,
                "07-08 UTC": 0.4,
                "09-11 UTC": 0.7,
                "12-16 UTC": 1.0,
                "17-19 UTC": 0.7,
                "20-23 UTC": 0.2,
            },
            "dst_mismatch_finding": "Awards peak 1.0 overlap score at 12:00-12:59 UTC even after Nov 1 US EST shift when NY is not yet open.",
        },
        "backtest_and_live_hold_and_reset": {
            "effective_max_hold": "60m if 7 <= entry_time.hour < 20 else 90m (fixed UTC in run_backtest.py & shadow_tracker.py)",
            "daily_circuit_breaker_reset": "UTC midnight (00:00 UTC), independent of broker 17:00 NY rollover",
            "shadow_telemetry_flag": "scan_market.py logs in_15utc_window (cutoff_5m.hour < 15, fixed UTC) and Ridge hour_sin/hour_cos (observation-only)",
        },
        "broker_cost_rollover": {
            "source": "src/goldedge/domain/services/broker_cost_service.py (lines 176-257)",
            "dst_aware": True,
            "rule": "17:00 America/New_York (21:00Z EDT / 22:00Z EST), 3x Wednesday swap, skip Sat/Sun",
        },
        "task12_dst_calendar": {
            "asian": "09:00-18:00 Asia/Tokyo (00:00-09:00 UTC, no DST)",
            "london": "08:00-16:30 Europe/London (07:00-15:30Z BST -> 08:00-16:30Z GMT after 2026-10-25 01:00Z)",
            "new_york": "08:00-17:00 America/New_York (12:00-21:00Z EDT -> 13:00-22:00Z EST after 2026-11-01 06:00Z)",
            "overlap": "Intersection of active London and New York sessions (12:00-15:30Z BST/EDT; 12:00-16:30Z GMT/EDT; 13:00-16:30Z GMT/EST)",
            "holidays_flagged": {k.isoformat(): v for k, v in _HOLIDAYS.items()},
        },
    }
    (OUT_DIR / "task12_session_logic_audit.json").write_text(json.dumps(session_audit, indent=2), encoding="utf-8")

    # =========================================================================
    # PHASE 3: Build DST-Aware Intraday 5m Features & Enriched Trade Tables
    # =========================================================================
    f5m = pd.read_csv(AUDIT_DIR / "task11_regime_features.csv")
    sess_rows = [classify_dst_session(_parse_utc(t)) for t in f5m["bar_time"]]
    sess_df = pd.DataFrame(sess_rows)
    # Avoid duplicate utc_hour column
    sess_df = sess_df.drop(columns=["utc_hour"])
    f5m_out = pd.concat([f5m, sess_df], axis=1)
    f5m_out["quote_arrival_rate_per_min"] = (f5m_out["tick_count_5m"] / 5.0).round(2)
    f5m_out.to_csv(OUT_DIR / "task12_intraday_features.csv", index=False)

    # Load verified Task 11A trade ledgers for H0 and H4a across W1, W2, W3
    t11a_trades = pd.read_csv(AUDIT_DIR / "task11a_trade_level_differences.csv")
    t11a_sub = t11a_trades[t11a_trades["candidate"].isin(["H0_BASELINE_V2", "H8_H4a_ALL_REGIMES"])].copy()
    t11a_sub["variant"] = t11a_sub["candidate"].map({"H0_BASELINE_V2": "H0", "H8_H4a_ALL_REGIMES": "H4a"})
    t11a_sub["entry_spread_usd"] = (t11a_sub["entry_ask"] - t11a_sub["entry_bid"]).round(4)

    # Join causal 5m signal-time market features from task11_regime_features.csv (on signal_time)
    f5m_lookup = f5m_out.copy()
    f5m_lookup["bar_time_utc"] = f5m_lookup["bar_time"].map(lambda s: _parse_utc(s).isoformat())
    f5m_cols = [
        "bar_time_utc", "primary_regime", "vol_category", "atr", "atr_pctl_200",
        "adx_14", "kaufman_er", "ema20_slope", "dist_ema20_atr", "htf_trend",
        "kalman_vel", "kalman_accel", "mean_spread_usd", "tick_count_5m",
        "quote_arrival_rate_per_min",
    ]
    f5m_small = f5m_lookup[f5m_cols].drop_duplicates(subset=["bar_time_utc"])

    # Also join W3 exact entry score from task11_trade_regime_enrichment.csv where available
    t11_enr = pd.read_csv(AUDIT_DIR / "task11_trade_regime_enrichment.csv")
    t11_enr_key = t11_enr[["variant", "entry_time", "score", "strategy", "mae_atr", "mfe_atr", "holding_mins"]].copy()
    t11_enr_key["entry_time"] = t11_enr_key["entry_time"].map(lambda s: _parse_utc(s).isoformat())
    t11_enr_key = t11_enr_key.drop_duplicates(subset=["variant", "entry_time"])

    # Derive DST-aware session features strictly at canonical entry_time (causal!)
    t_sess = pd.DataFrame([classify_dst_session(_parse_utc(t)) for t in t11a_sub["entry_time"]])
    enriched = pd.concat([t11a_sub.reset_index(drop=True), t_sess.reset_index(drop=True)], axis=1)
    enriched["signal_time_utc"] = enriched["signal_time"].map(lambda s: _parse_utc(s).isoformat())
    enriched["entry_time_utc"] = enriched["entry_time"].map(lambda s: _parse_utc(s).isoformat())
    enriched = enriched.merge(f5m_small, left_on="signal_time_utc", right_on="bar_time_utc", how="left")
    enriched = enriched.merge(t11_enr_key, left_on=["variant", "entry_time_utc"], right_on=["variant", "entry_time"], how="left", suffixes=("", "_enr"))
    if "entry_time_enr" in enriched.columns:
        enriched = enriched.drop(columns=["entry_time_enr", "bar_time_utc"])
    enriched.to_csv(OUT_DIR / "task12_trade_session_enrichment.csv", index=False)

    # =========================================================================
    # PHASES 4 & 5: Hourly, Session, Weekday, BUY/SELL Breakdowns (H0 & H4a)
    # =========================================================================
    hourly_rows: list[dict[str, Any]] = []
    for wid in ("W1", "W2", "W3"):
        for var in ("H0", "H4a"):
            for d_filter in ("ALL", "BUY", "SELL"):
                sub = enriched[(enriched["window"] == wid) & (enriched["variant"] == var)]
                if d_filter != "ALL":
                    sub = sub[sub["direction"] == d_filter]
                for hr in range(7, 18):
                    m = _group_metrics(sub[sub["utc_hour"] == hr])
                    hourly_rows.append({"window": wid, "variant": var, "direction": d_filter, "utc_hour": hr, **m})
    hourly_df = pd.DataFrame(hourly_rows)
    hourly_df.to_csv(OUT_DIR / "task12_hourly_performance.csv", index=False)

    # BUY vs SELL session analysis CSV across DST sessions, weekdays, first/last 30m, transitions
    bs_rows: list[dict[str, Any]] = []
    dims = [
        ("dst_session", ["ASIAN_ONLY", "LONDON_PRE_OVERLAP", "LONDON_NY_OVERLAP", "NY_POST_LONDON"]),
        ("utc_weekday", ["Mon", "Tue", "Wed", "Thu", "Fri"]),
        ("dst_regime", ["BOTH_DST_BST_EDT", "MISMATCH_GMT_EDT", "BOTH_STD_GMT_EST"]),
        ("is_first_30m_any", [0, 1]),
        ("is_last_30m_any", [0, 1]),
        ("is_session_transition", [0, 1]),
        ("is_holiday", [0, 1]),
    ]
    for wid in ("W1", "W2", "W3"):
        for var in ("H0", "H4a"):
            for d_filter in ("ALL", "BUY", "SELL"):
                sub = enriched[(enriched["window"] == wid) & (enriched["variant"] == var)]
                if d_filter != "ALL":
                    sub = sub[sub["direction"] == d_filter]
                for col, vals in dims:
                    for val in vals:
                        m = _group_metrics(sub[sub[col] == val])
                        bs_rows.append({
                            "window": wid, "variant": var, "direction": d_filter,
                            "dimension": col, "bucket": str(val), **m,
                        })
    bs_df = pd.DataFrame(bs_rows)
    bs_df.to_csv(OUT_DIR / "task12_buy_sell_session_analysis.csv", index=False)

    # Structured session_performance.json + H4a vs H0 comparison
    sess_perf: dict[str, Any] = {}
    for wid in ("W1", "W2", "W3"):
        sess_perf[wid] = {}
        for col, vals in dims:
            sess_perf[wid][col] = {}
            for val in vals:
                h0_all = _group_metrics(enriched[(enriched["window"] == wid) & (enriched["variant"] == "H0") & (enriched[col] == val)])
                h4_all = _group_metrics(enriched[(enriched["window"] == wid) & (enriched["variant"] == "H4a") & (enriched[col] == val)])
                h0_buy = _group_metrics(enriched[(enriched["window"] == wid) & (enriched["variant"] == "H0") & (enriched["direction"] == "BUY") & (enriched[col] == val)])
                h4_buy = _group_metrics(enriched[(enriched["window"] == wid) & (enriched["variant"] == "H4a") & (enriched["direction"] == "BUY") & (enriched[col] == val)])
                h0_sell = _group_metrics(enriched[(enriched["window"] == wid) & (enriched["variant"] == "H0") & (enriched["direction"] == "SELL") & (enriched[col] == val)])
                h4_sell = _group_metrics(enriched[(enriched["window"] == wid) & (enriched["variant"] == "H4a") & (enriched["direction"] == "SELL") & (enriched[col] == val)])
                sess_perf[wid][col][str(val)] = {
                    "H0_ALL": h0_all, "H4a_ALL": h4_all,
                    "delta_pnl_H4a_vs_H0": round(h4_all["net_pnl"] - h0_all["net_pnl"], 2),
                    "delta_n_H4a_vs_H0": h4_all["n"] - h0_all["n"],
                    "H0_BUY": h0_buy, "H4a_BUY": h4_buy,
                    "H0_SELL": h0_sell, "H4a_SELL": h4_sell,
                }

    # Detailed W3 H4a vs H0 hourly delta (including removed winners / avoided losers by hour)
    w3_h0 = enriched[(enriched["window"] == "W3") & (enriched["variant"] == "H0")]
    w3_h4 = enriched[(enriched["window"] == "W3") & (enriched["variant"] == "H4a")]
    h4_keys = set(zip(w3_h4["entry_time_utc"], w3_h4["direction"]))
    h0_keys = set(zip(w3_h0["entry_time_utc"], w3_h0["direction"]))
    hourly_h4a_diff: dict[str, Any] = {}
    for hr in range(7, 18):
        s0 = w3_h0[w3_h0["utc_hour"] == hr]
        s4 = w3_h4[w3_h4["utc_hour"] == hr]
        rem_df = s0[~s0.apply(lambda r: (r["entry_time_utc"], r["direction"]) in h4_keys, axis=1)] if len(s0) > 0 else s0
        new_df = s4[~s4.apply(lambda r: (r["entry_time_utc"], r["direction"]) in h0_keys, axis=1)] if len(s4) > 0 else s4
        hourly_h4a_diff[f"UTC_{hr:02d}"] = {
            "H0": _group_metrics(s0),
            "H4a": _group_metrics(s4),
            "delta_pnl": round(float(s4["net_pnl_usd"].sum() - s0["net_pnl_usd"].sum()), 2),
            "removed_from_H0_n": int(len(rem_df)),
            "removed_winners": int((rem_df["net_pnl_usd"] > 0).sum()),
            "removed_losers": int((rem_df["net_pnl_usd"] <= 0).sum()),
            "removed_net_pnl": round(float(rem_df["net_pnl_usd"].sum()), 2),
            "new_in_H4a_n": int(len(new_df)),
            "new_net_pnl": round(float(new_df["net_pnl_usd"].sum()), 2),
        }
    sess_perf["W3_H4a_vs_H0_hourly_attribution"] = hourly_h4a_diff
    (OUT_DIR / "task12_session_performance.json").write_text(json.dumps(sess_perf, indent=2), encoding="utf-8")

    # =========================================================================
    # PHASE 6: Intraday Market Behavior (Quote-Based CFD Microstructure)
    # =========================================================================
    behavior: dict[str, Any] = {
        "data_note": "XAUUSD historical data is quote-based CFD tick data. quote_arrival_rate_per_min measures tick update frequency, NOT traded exchange volume.",
        "by_utc_hour_5m_bars": {},
        "by_dst_session_5m_bars": {},
        "by_utc_hour_w3_trades_H0": {},
        "by_utc_hour_w3_trades_H4a": {},
    }
    for hr in range(0, 24):
        sh = f5m_out[f5m_out["utc_hour"] == hr]
        if len(sh) == 0:
            continue
        behavior["by_utc_hour_5m_bars"][f"UTC_{hr:02d}"] = {
            "bars_5m": int(len(sh)),
            "mean_atr": round(float(sh["atr"].mean()), 4),
            "median_atr": round(float(sh["atr"].median()), 4),
            "mean_spread_usd": round(float(sh["mean_spread_usd"].mean()), 4),
            "p90_spread_usd": round(float(sh["mean_spread_usd"].quantile(0.90)), 4),
            "mean_quote_arrival_rate_per_min": round(float(sh["quote_arrival_rate_per_min"].mean()), 2),
            "mean_kaufman_er": round(float(sh["kaufman_er"].mean()), 4),
            "mean_adx_14": round(float(sh["adx_14"].mean()), 2),
            "mean_abs_kalman_vel": round(float(sh["kalman_vel"].abs().mean()), 5),
            "mean_abs_dist_ema20_atr": round(float(sh["dist_ema20_atr"].abs().mean()), 4),
            "regime_mix_pct": {k: round(v / len(sh) * 100.0, 1) for k, v in sh["primary_regime"].value_counts().to_dict().items()},
        }
    for sname in ["ASIAN_ONLY", "LONDON_PRE_OVERLAP", "LONDON_NY_OVERLAP", "NY_POST_LONDON", "OFF_HOURS"]:
        sh = f5m_out[f5m_out["dst_session"] == sname]
        if len(sh) == 0:
            continue
        behavior["by_dst_session_5m_bars"][sname] = {
            "bars_5m": int(len(sh)),
            "mean_atr": round(float(sh["atr"].mean()), 4),
            "mean_spread_usd": round(float(sh["mean_spread_usd"].mean()), 4),
            "mean_quote_arrival_rate_per_min": round(float(sh["quote_arrival_rate_per_min"].mean()), 2),
            "mean_kaufman_er": round(float(sh["kaufman_er"].mean()), 4),
            "mean_adx_14": round(float(sh["adx_14"].mean()), 2),
            "regime_mix_pct": {k: round(v / len(sh) * 100.0, 1) for k, v in sh["primary_regime"].value_counts().to_dict().items()},
        }
    for label, tdf in [("by_utc_hour_w3_trades_H0", w3_h0), ("by_utc_hour_w3_trades_H4a", w3_h4)]:
        for hr in range(7, 18):
            sh = tdf[tdf["utc_hour"] == hr]
            if len(sh) == 0:
                continue
            behavior[label][f"UTC_{hr:02d}"] = {
                "n": int(len(sh)),
                "net_pnl": round(float(sh["net_pnl_usd"].sum()), 2),
                "mean_atr": round(float(sh["atr"].mean()), 4),
                "mean_entry_spread_usd": round(float(sh["entry_spread_usd"].mean()), 4),
                "mean_quote_arrival_rate_per_min": round(float(sh["quote_arrival_rate_per_min"].mean()), 2),
                "mean_kaufman_er": round(float(sh["kaufman_er"].mean()), 4),
                "mean_adx_14": round(float(sh["adx_14"].mean()), 2),
                "mean_score": round(float(sh["score"].mean()), 4) if sh["score"].notna().any() else None,
                "buy_n": int((sh["direction"] == "BUY").sum()),
                "sell_n": int((sh["direction"] == "SELL").sum()),
            }
    (OUT_DIR / "task12_intraday_market_behavior.json").write_text(json.dumps(behavior, indent=2), encoding="utf-8")

    # Concise console summary
    print("[TASK 12 PART 1 COMPLETE — Phases 1-6]")
    print("\nW3 Hourly Comparison (H0 vs H4a):")
    for hr in range(7, 17):
        d = hourly_h4a_diff[f"UTC_{hr:02d}"]
        print(
            f"  UTC {hr:02d} | H0: n={d['H0']['n']:2d} pnl={d['H0']['net_pnl']:7.2f} pf={d['H0']['pf']:6.3f} "
            f"| H4a: n={d['H4a']['n']:2d} pnl={d['H4a']['net_pnl']:7.2f} pf={d['H4a']['pf']:6.3f} "
            f"| delta={d['delta_pnl']:+7.2f} (rem {d['removed_winners']}W/{d['removed_losers']}L={d['removed_net_pnl']:+7.2f})"
        )
    print("\nW3 DST-Aware Session Comparison (H0 vs H4a):")
    for sname in ["ASIAN_ONLY", "LONDON_PRE_OVERLAP", "LONDON_NY_OVERLAP", "NY_POST_LONDON"]:
        d = sess_perf["W3"]["dst_session"][sname]
        print(
            f"  {sname:20s} | H0: n={d['H0_ALL']['n']:2d} pnl={d['H0_ALL']['net_pnl']:7.2f} pf={d['H0_ALL']['pf']:6.3f} "
            f"| H4a: n={d['H4a_ALL']['n']:2d} pnl={d['H4a_ALL']['net_pnl']:7.2f} pf={d['H4a_ALL']['pf']:6.3f} "
            f"| delta={d['delta_pnl_H4a_vs_H0']:+7.2f}"
        )
    print("\nW3 Weekday Comparison (H0 vs H4a):")
    for wd in ["Mon", "Tue", "Wed", "Thu", "Fri"]:
        d = sess_perf["W3"]["utc_weekday"][wd]
        print(
            f"  {wd:4s} | H0: n={d['H0_ALL']['n']:2d} pnl={d['H0_ALL']['net_pnl']:7.2f} pf={d['H0_ALL']['pf']:6.3f} "
            f"| H4a: n={d['H4a_ALL']['n']:2d} pnl={d['H4a_ALL']['net_pnl']:7.2f} pf={d['H4a_ALL']['pf']:6.3f} "
            f"| delta={d['delta_pnl_H4a_vs_H0']:+7.2f}"
        )
    print("\nW3 DST Regime Comparison (H0 vs H4a):")
    for dr in ["BOTH_DST_BST_EDT", "MISMATCH_GMT_EDT", "BOTH_STD_GMT_EST"]:
        d = sess_perf["W3"]["dst_regime"][dr]
        print(
            f"  {dr:20s} | H0: n={d['H0_ALL']['n']:2d} pnl={d['H0_ALL']['net_pnl']:7.2f} pf={d['H0_ALL']['pf']:6.3f} "
            f"| H4a: n={d['H4a_ALL']['n']:2d} pnl={d['H4a_ALL']['net_pnl']:7.2f} pf={d['H4a_ALL']['pf']:6.3f}"
        )


if __name__ == "__main__":
    main()
