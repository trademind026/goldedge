"""
Task 9 Deep-Dive Pattern & Subperiod Stability Inspector on Enriched W3 Trades.
Checks subperiod persistence (P1/P2/P3 and DEV/HOLDOUT), entry extension vs
momentum/score calibration, hourly/session patterns, and exit MFE/MAE structure.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent.parent
if not (ROOT / "backtest_results").exists() and Path("/app/backtest_results").exists():
    ROOT = Path("/app")
AUDIT_DIR = ROOT / "backtest_results" / "audit"


def _m(sub: pd.DataFrame) -> dict:
    n = len(sub)
    if n == 0:
        return {"n": 0, "w": 0, "l": 0, "wr": 0.0, "pnl": 0.0, "pf": 0.0, "exp": 0.0}
    p = sub["pnl"].values.astype(float)
    w = p[p > 0]
    l = p[p <= 0]
    pf = float(w.sum() / abs(l.sum())) if len(l) and l.sum() < 0 else (999.0 if len(w) and w.sum() > 0 else 0.0)
    return {
        "n": int(n),
        "w": int(len(w)),
        "l": int(len(l)),
        "wr": round(len(w) / n * 100.0, 1),
        "pnl": round(float(p.sum()), 2),
        "pf": round(pf, 3),
        "exp": round(float(p.mean()), 2),
    }


def _check_rule(name: str, sub: pd.DataFrame, universe: pd.DataFrame) -> dict:
    comp = universe.loc[~universe.index.isin(sub.index)]
    mw_p = float(stats.mannwhitneyu(sub["pnl"], comp["pnl"], alternative="two-sided").pvalue) if len(sub) >= 3 and len(comp) >= 3 else 1.0
    table = [
        [int((sub["pnl"] > 0).sum()), int((sub["pnl"] <= 0).sum())],
        [int((comp["pnl"] > 0).sum()), int((comp["pnl"] <= 0).sum())],
    ]
    _, fp = stats.fisher_exact(table) if len(sub) > 0 and len(comp) > 0 else (1.0, 1.0)
    return {
        "rule": name,
        "overall": _m(sub),
        "complement": _m(comp),
        "mw_p": round(mw_p, 5),
        "fisher_p": round(float(fp), 5),
        "P1": _m(sub[sub["subperiod"] == "P1_Aug17_Sep16"]),
        "P2": _m(sub[sub["subperiod"] == "P2_Sep17_Oct16"]),
        "P3": _m(sub[sub["subperiod"] == "P3_Oct17_Nov18"]),
        "DEV": _m(sub[sub["split_t4_dev_holdout"] == "DEV_Aug17_Oct16"]),
        "HO": _m(sub[sub["split_t4_dev_holdout"] == "HOLDOUT_Oct17_Nov18"]),
    }


def main() -> None:
    df = pd.read_csv(AUDIT_DIR / "task9_trades_enriched_w3.csv")
    buy = df[df["direction"] == "BUY"].copy()
    sell = df[df["direction"] == "SELL"].copy()

    rules = [
        ("ALL: score [0.60, 0.75)", df[df["score"] < 0.75], df),
        ("ALL: score [0.60, 0.80)", df[df["score"] < 0.80], df),
        ("ALL: kaufman_er <= 0.40", df[df["kaufman_er"] <= 0.40], df),
        ("ALL: kaufman_er <= 0.45", df[df["kaufman_er"] <= 0.45], df),
        ("ALL: kalman_accel_aligned == 0", df[df["kalman_accel_aligned"] == 0], df),
        ("ALL: utc_hour in 8..15", df[df["utc_hour"].between(8, 15)], df),
        ("ALL: utc_hour < 15", df[df["utc_hour"] < 15], df),
        ("BUY: score < 0.75", buy[buy["score"] < 0.75], buy),
        ("BUY: utc_hour >= 8", buy[buy["utc_hour"] >= 8], buy),
        ("BUY: utc_hour == 7", buy[buy["utc_hour"] == 7], buy),
        ("BUY: kaufman_er <= 0.45", buy[buy["kaufman_er"] <= 0.45], buy),
        ("BUY: kalman_accel_aligned == 0", buy[buy["kalman_accel_aligned"] == 0], buy),
        ("BUY: dist_ema20_atr <= 1.8", buy[buy["dist_ema20_atr"] <= 1.8], buy),
        ("BUY: ema_spread_atr <= 1.8", buy[buy["ema_spread_atr"] <= 1.8], buy),
        ("BUY: prev_30m_ret_atr <= 1.5", buy[buy["prev_30m_ret_atr"] <= 1.5], buy),
        ("BUY: mfe_15m_atr >= 0.4 (survived E1)", buy[buy["mfe_15m_atr"] >= 0.4], buy),
        ("SELL: score < 0.75", sell[sell["score"] < 0.75], sell),
        ("SELL: kaufman_er <= 0.40", sell[sell["kaufman_er"] <= 0.40], sell),
        ("SELL: kaufman_er <= 0.45", sell[sell["kaufman_er"] <= 0.45], sell),
        ("SELL: kalman_accel_aligned == 0", sell[sell["kalman_accel_aligned"] == 0], sell),
        ("SELL: rsi > 30", sell[sell["rsi"] > 30.0], sell),
        ("SELL: dist_10bar_high_atr <= 3.0", sell[sell["dist_10bar_high_atr"] <= 3.0], sell),
        ("SELL: atr_pctl_hist <= 60", sell[sell["atr_pctl_hist"] <= 60.0], sell),
        ("SELL: utc_hour < 16", sell[sell["utc_hour"] < 16], sell),
        ("SELL: utc_hour == 7", sell[sell["utc_hour"] == 7], sell),
        ("SELL: mfe_15m_atr >= 0.4 (survived E1)", sell[sell["mfe_15m_atr"] >= 0.4], sell),
    ]

    results = [_check_rule(n, s, u) for n, s, u in rules]
    for r in results:
        o = r["overall"]
        print(f"{r['rule']:38s} | n={o['n']:3d} wr={o['wr']:5.1f}% pnl={o['pnl']:8.2f} pf={o['pf']:5.3f} exp={o['exp']:6.2f} p={r['mw_p']:7.5f} | P1={r['P1']['pnl']:7.1f} P2={r['P2']['pnl']:7.1f} P3={r['P3']['pnl']:7.1f} | DEV={r['DEV']['pnl']:7.1f} HO={r['HO']['pnl']:7.1f}")

    # Print BUY trade ledger summary sorted by time to inspect the 41 BUY trades directly
    print("\n--- ALL 41 BUY TRADES ---")
    cols = ["trade_idx", "subperiod", "utc_hour", "score", "comp_kalman_vel", "kaufman_er", "kalman_accel_aligned", "bar5m_candle_aligned", "rsi", "dist_ema20_atr", "dist_20bar_high_atr", "mfe_atr", "mae_atr", "exit_reason", "pnl"]
    print(buy[cols].to_string(index=False))

    (AUDIT_DIR / "task9_deep_dive_rules.json").write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
