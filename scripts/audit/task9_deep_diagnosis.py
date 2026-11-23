"""
Task 9 (Phases 2–6) — Comprehensive Statistical Diagnosis of BUY vs SELL Performance
on Immutable RECOVERED_FULL_HISTORY_BASELINE_V2 (W3: 143 trades, plus Task 8 n=78 view).

Outputs:
  - backtest_results/audit/task9_diagnosis_summary.json
  - backtest_results/audit/task9_feature_comparison.csv
  - backtest_results/audit/task9_segmented_stats.csv
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent.parent
if not (ROOT / "backtest_results").exists() and Path("/app/backtest_results").exists():
    ROOT = Path("/app")
AUDIT_DIR = ROOT / "backtest_results" / "audit"


def _metrics(df: pd.DataFrame) -> dict[str, Any]:
    n = int(len(df))
    if n == 0:
        return {"n": 0, "wins": 0, "losses": 0, "wr_pct": 0.0, "net_pnl": 0.0, "gross_pnl": 0.0, "pf": 0.0, "expectancy": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "closed_dd": 0.0, "std_pnl": 0.0, "ci95_exp_low": 0.0, "ci95_exp_high": 0.0}
    pnls = df["pnl"].values.astype(float)
    wins = pnls[pnls > 0.0]
    losses = pnls[pnls <= 0.0]
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() < 0 else (999.0 if len(wins) and wins.sum() > 0 else 0.0)
    # Closed DD on trade sequence
    ceq = np.cumsum(pnls) + 10000.0
    hwm = np.maximum.accumulate(np.insert(ceq, 0, 10000.0))[1:]
    dd = float((ceq - hwm).min())
    exp = float(pnls.mean())
    std = float(pnls.std(ddof=1)) if n > 1 else 0.0
    se = std / math.sqrt(n) if n > 1 else 0.0
    t_crit = float(stats.t.ppf(0.975, df=n - 1)) if n > 1 else 0.0
    return {
        "n": n,
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "wr_pct": round(float(len(wins) / n * 100.0), 2),
        "net_pnl": round(float(pnls.sum()), 2),
        "gross_pnl": round(float(df["gross_pnl"].sum()), 2) if "gross_pnl" in df.columns else round(float(pnls.sum()), 2),
        "pf": round(pf, 4),
        "expectancy": round(exp, 2),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "closed_dd": round(dd, 2),
        "std_pnl": round(std, 2),
        "ci95_exp_low": round(exp - t_crit * se, 2),
        "ci95_exp_high": round(exp + t_crit * se, 2),
    }


def _bh_fdr(pvals: list[float]) -> list[float]:
    m = len(pvals)
    if m == 0:
        return []
    indexed = sorted(enumerate(pvals), key=lambda x: x[1])
    out = [1.0] * m
    prev = 1.0
    for rank_rev, (idx, p) in enumerate(reversed(indexed)):
        rank = m - rank_rev
        q = min(prev, (p * m) / rank)
        out[idx] = round(min(1.0, q), 6)
        prev = q
    return out


def main() -> None:
    df = pd.read_csv(AUDIT_DIR / "task9_trades_enriched_w3.csv")
    cand = pd.read_csv(AUDIT_DIR / "task9_candidate_signals_w3.csv")
    t8_df = pd.read_csv(AUDIT_DIR / "task8_app_w3_hwm_trades.csv")

    buy = df[df["direction"] == "BUY"].copy()
    sell = df[df["direction"] == "SELL"].copy()

    # 1) Overview + Task 8 Protected View
    overview = {
        "V2_W3_ALL": _metrics(df),
        "V2_W3_BUY": _metrics(buy),
        "V2_W3_SELL": _metrics(sell),
        "TASK8_W3_ALL": _metrics(t8_df),
        "TASK8_W3_BUY": _metrics(t8_df[t8_df["direction"] == "BUY"]),
        "TASK8_W3_SELL": _metrics(t8_df[t8_df["direction"] == "SELL"]),
    }

    # 2) Feature Comparison: BUY vs SELL & Winners vs Losers within BUY and SELL
    features = [
        "score", "comp_kalman_vel", "comp_kaufman_er", "comp_entropy",
        "comp_ema20_slope", "comp_twp_dist", "comp_session", "base_score_pre_accel",
        "comp_accel_adj", "comp_tick_mult",
        "kalman_vel", "kalman_accel", "kalman_aligned", "kalman_accel_aligned", "kalman_dist_atr",
        "ema20_slope", "ema_spread_atr", "dist_ema20_atr", "dist_ema50_atr", "dist_twp_atr",
        "htf_ema_spread_atr", "rsi", "atr", "atr_pctl_hist", "kaufman_er", "shannon_entropy",
        "entry_spread_usd", "quote_arrival_1m", "tick_rate_ratio",
        "bar5m_candle_aligned", "bar5m_body_atr", "bar5m_upper_wick_atr", "bar5m_lower_wick_atr", "bar5m_close_pos",
        "dist_10bar_high_atr", "dist_10bar_low_atr", "dist_20bar_high_atr", "dist_20bar_low_atr",
        "prev_30m_ret_atr", "prev_1h_ret_atr", "prev_4h_ret_atr",
        "holding_mins", "mae_atr", "mfe_atr", "time_to_mfe_mins", "time_to_mae_mins",
        "mfe_before_mae", "mfe_15m_atr", "mae_15m_atr", "be_reached", "t1_reached", "t2_reached",
        "was_profitable_05atr", "reversed_from_05atr_profit", "reversed_from_10atr_profit",
    ]
    feat_rows = []
    for f in features:
        bw = buy[buy["pnl"] > 0][f].dropna()
        bl = buy[buy["pnl"] <= 0][f].dropna()
        sw = sell[sell["pnl"] > 0][f].dropna()
        sl = sell[sell["pnl"] <= 0][f].dropna()
        # Mann-Whitney U between Win vs Loss inside BUY and SELL
        p_b = float(stats.mannwhitneyu(bw, bl, alternative="two-sided").pvalue) if len(bw) >= 3 and len(bl) >= 3 and (bw.std() > 0 or bl.std() > 0) else 1.0
        p_s = float(stats.mannwhitneyu(sw, sl, alternative="two-sided").pvalue) if len(sw) >= 3 and len(sl) >= 3 and (sw.std() > 0 or sl.std() > 0) else 1.0
        p_bs = float(stats.mannwhitneyu(buy[f].dropna(), sell[f].dropna(), alternative="two-sided").pvalue) if (buy[f].std() > 0 or sell[f].std() > 0) else 1.0
        corr_b = float(stats.spearmanr(buy[f], buy["pnl"]).statistic) if buy[f].std() > 0 else 0.0
        corr_s = float(stats.spearmanr(sell[f], sell["pnl"]).statistic) if sell[f].std() > 0 else 0.0
        feat_rows.append({
            "feature": f,
            "buy_mean": round(float(buy[f].mean()), 5),
            "buy_win_mean": round(float(bw.mean()), 5) if len(bw) else None,
            "buy_loss_mean": round(float(bl.mean()), 5) if len(bl) else None,
            "buy_win_loss_p": round(p_b, 5),
            "buy_spearman_pnl": round(corr_b, 4) if not np.isnan(corr_b) else 0.0,
            "sell_mean": round(float(sell[f].mean()), 5),
            "sell_win_mean": round(float(sw.mean()), 5) if len(sw) else None,
            "sell_loss_mean": round(float(sl.mean()), 5) if len(sl) else None,
            "sell_win_loss_p": round(p_s, 5),
            "sell_spearman_pnl": round(corr_s, 4) if not np.isnan(corr_s) else 0.0,
            "buy_vs_sell_p": round(p_bs, 5),
        })
    feat_df = pd.DataFrame(feat_rows)
    feat_df["buy_win_loss_q_fdr"] = _bh_fdr(feat_df["buy_win_loss_p"].tolist())
    feat_df["sell_win_loss_q_fdr"] = _bh_fdr(feat_df["sell_win_loss_p"].tolist())
    feat_df.to_csv(AUDIT_DIR / "task9_feature_comparison.csv", index=False)

    # 3) Multi-dimensional Segmentation (Phase 3, 4, 5, 6)
    seg_rows = []
    def _add_seg(group_col: str, subset_name: str, sub_df: pd.DataFrame):
        m = _metrics(sub_df)
        m["group_by"] = group_col
        m["segment"] = str(subset_name)
        seg_rows.append(m)

    for col in ["regime", "session", "utc_hour", "strategy", "exit_reason", "subperiod", "split_t4_dev_holdout", "month"]:
        for val, grp in df.groupby(col, sort=True):
            _add_seg(f"ALL:{col}", val, grp)
        for val, grp in buy.groupby(col, sort=True):
            _add_seg(f"BUY:{col}", val, grp)
        for val, grp in sell.groupby(col, sort=True):
            _add_seg(f"SELL:{col}", val, grp)

    # Custom diagnostic slices for BUY & SELL
    slices = [
        ("BUY", "bar5m_candle_aligned==1", buy[buy["bar5m_candle_aligned"] == 1]),
        ("BUY", "bar5m_candle_aligned==0 (RED CANDLE BUY)", buy[buy["bar5m_candle_aligned"] == 0]),
        ("BUY", "kalman_aligned==1 (vel>0)", buy[buy["kalman_aligned"] == 1]),
        ("BUY", "kalman_aligned==0 (vel<=0)", buy[buy["kalman_aligned"] == 0]),
        ("BUY", "kalman_accel_aligned==1", buy[buy["kalman_accel_aligned"] == 1]),
        ("BUY", "kalman_accel_aligned==0", buy[buy["kalman_accel_aligned"] == 0]),
        ("BUY", "score_0.60_to_0.75", buy[(buy["score"] >= 0.60) & (buy["score"] < 0.75)]),
        ("BUY", "score_0.75_to_1.00", buy[buy["score"] >= 0.75]),
        ("BUY", "dist_20bar_high_atr <= 0.15 (chasing high)", buy[buy["dist_20bar_high_atr"] <= 0.15]),
        ("BUY", "dist_20bar_high_atr > 0.15 (room below high)", buy[buy["dist_20bar_high_atr"] > 0.15]),
        ("BUY", "dist_ema20_atr >= 1.5 (extended from ema20)", buy[buy["dist_ema20_atr"] >= 1.5]),
        ("BUY", "dist_ema20_atr < 1.5", buy[buy["dist_ema20_atr"] < 1.5]),
        ("BUY", "rsi >= 70 (overbought chase)", buy[buy["rsi"] >= 70.0]),
        ("BUY", "rsi < 70", buy[buy["rsi"] < 70.0]),
        ("BUY", "htf_ema_spread_atr <= 1.0 (weak H1)", buy[buy["htf_ema_spread_atr"] <= 1.0]),
        ("BUY", "htf_ema_spread_atr > 1.0 (strong H1)", buy[buy["htf_ema_spread_atr"] > 1.0]),
        ("BUY", "spread >= $0.25", buy[buy["entry_spread_usd"] >= 0.25]),
        ("BUY", "spread < $0.25", buy[buy["entry_spread_usd"] < 0.25]),
        ("SELL", "kalman_aligned==1 (vel<0)", sell[sell["kalman_aligned"] == 1]),
        ("SELL", "kalman_aligned==0 (vel>=0)", sell[sell["kalman_aligned"] == 0]),
        ("SELL", "kalman_accel_aligned==1", sell[sell["kalman_accel_aligned"] == 1]),
        ("SELL", "kalman_accel_aligned==0", sell[sell["kalman_accel_aligned"] == 0]),
        ("SELL", "score_0.60_to_0.75", sell[(sell["score"] >= 0.60) & (sell["score"] < 0.75)]),
        ("SELL", "score_0.75_to_1.00", sell[sell["score"] >= 0.75]),
        ("SELL", "dist_20bar_low_atr <= 0.15 (selling low)", sell[sell["dist_20bar_low_atr"] <= 0.15]),
        ("SELL", "dist_20bar_low_atr > 0.15", sell[sell["dist_20bar_low_atr"] > 0.15]),
        ("SELL", "rsi <= 30 (oversold chase)", sell[sell["rsi"] <= 30.0]),
        ("SELL", "rsi > 30", sell[sell["rsi"] > 30.0]),
        ("SELL", "utc_hour == 9", sell[sell["utc_hour"] == 9]),
        ("SELL", "utc_hour != 9", sell[sell["utc_hour"] != 9]),
    ]
    for side, sname, sub in slices:
        _add_seg(f"SLICE:{side}", sname, sub)

    seg_df = pd.DataFrame(seg_rows)
    seg_df.to_csv(AUDIT_DIR / "task9_segmented_stats.csv", index=False)

    # 4) Candidate signal gate breakdown
    gate_summary = cand.groupby(["direction", "gate_status"], as_index=False).agg(
        count=("score", "count"),
        mean_score=("score", "mean"),
        mean_kvel=("kalman_vel", "mean"),
        mean_er=("kaufman_er", "mean"),
        mean_rsi=("rsi", "mean"),
        pct_candle_aligned=("bar5m_candle_aligned", "mean"),
    )

    # 5) Subperiod stability table for top candidate patterns
    subperiod_tests = {}
    candidate_rules = [
        ("BUY_candle_aligned", buy[buy["bar5m_candle_aligned"] == 1], buy),
        ("BUY_red_candle", buy[buy["bar5m_candle_aligned"] == 0], buy),
        ("BUY_kalman_aligned", buy[buy["kalman_aligned"] == 1], buy),
        ("BUY_kalman_misaligned", buy[buy["kalman_aligned"] == 0], buy),
        ("BUY_score_ge_075", buy[buy["score"] >= 0.75], buy),
        ("BUY_LDN_NY_Overlap", buy[buy["session"] == "LDN_NY_Overlap_12_16"], buy),
        ("BUY_PreOpen_and_London", buy[buy["session"] != "LDN_NY_Overlap_12_16"], buy),
        ("BUY_not_overbought_rsi70", buy[buy["rsi"] < 70.0], buy),
        ("BUY_room_below_20h_015", buy[buy["dist_20bar_high_atr"] > 0.15], buy),
        ("SELL_SIDEWAYS", sell[sell["regime"] == "SIDEWAYS"], sell),
        ("SELL_TRENDING_BEAR", sell[sell["regime"] == "TRENDING_BEAR"], sell),
        ("SELL_not_hr09", sell[sell["utc_hour"] != 9], sell),
        ("SELL_kalman_aligned", sell[sell["kalman_aligned"] == 1], sell),
    ]
    for rname, sub, base_side in candidate_rules:
        per_p = {}
        for pcol in ["P1_Aug17_Sep16", "P2_Sep17_Oct16", "P3_Oct17_Nov18"]:
            per_p[pcol] = _metrics(sub[sub["subperiod"] == pcol])
        per_split = {}
        for scol in ["DEV_Aug17_Oct16", "HOLDOUT_Oct17_Nov18"]:
            per_split[scol] = _metrics(sub[sub["split_t4_dev_holdout"] == scol])
        # Fisher test of WR inside vs outside
        comp = base_side.loc[~base_side.index.isin(sub.index)]
        table = [
            [int((sub["pnl"] > 0).sum()), int((sub["pnl"] <= 0).sum())],
            [int((comp["pnl"] > 0).sum()), int((comp["pnl"] <= 0).sum())],
        ]
        _, p_fish = stats.fisher_exact(table) if (len(sub) > 0 and len(comp) > 0) else (1.0, 1.0)
        p_mw = float(stats.mannwhitneyu(sub["pnl"], comp["pnl"], alternative="two-sided").pvalue) if (len(sub) >= 3 and len(comp) >= 3) else 1.0
        subperiod_tests[rname] = {
            "overall": _metrics(sub),
            "complement": _metrics(comp),
            "fisher_wr_p": round(float(p_fish), 5),
            "mw_pnl_p": round(p_mw, 5),
            "subperiods": per_p,
            "dev_vs_holdout": per_split,
        }

    # FDR adjust rule p-values
    rnames = list(subperiod_tests.keys())
    qs = _bh_fdr([subperiod_tests[k]["mw_pnl_p"] for k in rnames])
    for k, qv in zip(rnames, qs):
        subperiod_tests[k]["mw_pnl_q_fdr"] = qv

    # Exit behavior stats
    exit_summary = {
        "BUY_exit_profile": {
            "mfe_before_mae_pct": round(float(buy["mfe_before_mae"].mean() * 100.0), 2),
            "be_reached_pct": round(float(buy["be_reached"].mean() * 100.0), 2),
            "t1_reached_pct": round(float(buy["t1_reached"].mean() * 100.0), 2),
            "t2_reached_pct": round(float(buy["t2_reached"].mean() * 100.0), 2),
            "was_profitable_05atr_pct": round(float(buy["was_profitable_05atr"].mean() * 100.0), 2),
            "reversed_from_05atr_profit_cnt": int(buy["reversed_from_05atr_profit"].sum()),
            "reversed_from_10atr_profit_cnt": int(buy["reversed_from_10atr_profit"].sum()),
            "mean_mfe_atr": round(float(buy["mfe_atr"].mean()), 4),
            "mean_mae_atr": round(float(buy["mae_atr"].mean()), 4),
            "mean_hold_mins": round(float(buy["holding_mins"].mean()), 2),
            "mean_mfe_15m_atr": round(float(buy["mfe_15m_atr"].mean()), 4),
            "sl_15m_early_fail_count": int(((buy["mfe_15m_atr"] < 0.4) & (buy["pnl"] < 0)).sum()),
        },
        "SELL_exit_profile": {
            "mfe_before_mae_pct": round(float(sell["mfe_before_mae"].mean() * 100.0), 2),
            "be_reached_pct": round(float(sell["be_reached"].mean() * 100.0), 2),
            "t1_reached_pct": round(float(sell["t1_reached"].mean() * 100.0), 2),
            "t2_reached_pct": round(float(sell["t2_reached"].mean() * 100.0), 2),
            "was_profitable_05atr_pct": round(float(sell["was_profitable_05atr"].mean() * 100.0), 2),
            "reversed_from_05atr_profit_cnt": int(sell["reversed_from_05atr_profit"].sum()),
            "reversed_from_10atr_profit_cnt": int(sell["reversed_from_10atr_profit"].sum()),
            "mean_mfe_atr": round(float(sell["mfe_atr"].mean()), 4),
            "mean_mae_atr": round(float(sell["mae_atr"].mean()), 4),
            "mean_hold_mins": round(float(sell["holding_mins"].mean()), 2),
            "mean_mfe_15m_atr": round(float(sell["mfe_15m_atr"].mean()), 4),
            "sl_15m_early_fail_count": int(((sell["mfe_15m_atr"] < 0.4) & (sell["pnl"] < 0)).sum()),
        },
    }

    out = {
        "overview": overview,
        "exit_summary": exit_summary,
        "candidate_gate_summary": gate_summary.to_dict(orient="records"),
        "top_buy_win_loss_features": feat_df.sort_values("buy_win_loss_p").head(15).to_dict(orient="records"),
        "top_sell_win_loss_features": feat_df.sort_values("sell_win_loss_p").head(15).to_dict(orient="records"),
        "top_buy_vs_sell_diff_features": feat_df.sort_values("buy_vs_sell_p").head(15).to_dict(orient="records"),
        "subperiod_tests": subperiod_tests,
    }
    (AUDIT_DIR / "task9_diagnosis_summary.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
