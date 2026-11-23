"""
Task 11 (Phases 1–5) — Baseline Verification, Regime Classifier Audit,
Independent Causal Regime Reconstruction, Regime Performance Diagnosis (H0),
and H4a Regime Robustness Comparison.

Zero imports from `src/goldedge`. Connects directly to TimescaleDB `market_ticks`
via `psycopg2`. Produces deliverables #1–#7:
  - task11_baseline_verification.json
  - task11_regime_classifier_audit.json
  - task11_regime_features.csv
  - task11_trade_regime_enrichment.csv
  - task11_regime_performance.json
  - task11_buy_sell_regime_analysis.csv
  - task11_h4a_regime_comparison.json
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Optional

import numpy as np
import pandas as pd
import psycopg2

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import task6c_independent_validator as t6c

ROOT = t6c.ROOT
AUDIT_DIR = t6c.AUDIT_DIR
FROZEN_V2_DIR = AUDIT_DIR / "frozen_v2_recovered"


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _compute_wilder_adx14(df: pd.DataFrame) -> float:
    """Strictly causal Wilder 14-period ADX on completed 5m bars."""
    if len(df) < 30:
        return 0.0
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    n = len(h)
    tr = np.zeros(n)
    pdm = np.zeros(n)
    ndm = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        up = h[i] - h[i - 1]
        dn = l[i - 1] - l[i]
        pdm[i] = up if (up > dn and up > 0.0) else 0.0
        ndm[i] = dn if (dn > up and dn > 0.0) else 0.0
    p = 14
    atr_s = tr[1 : p + 1].sum()
    p_s = pdm[1 : p + 1].sum()
    n_s = ndm[1 : p + 1].sum()
    dxs = []
    for i in range(p + 1, n):
        atr_s = atr_s - (atr_s / p) + tr[i]
        p_s = p_s - (p_s / p) + pdm[i]
        n_s = n_s - (n_s / p) + ndm[i]
        if atr_s > 0.0:
            pdi = 100.0 * p_s / atr_s
            ndi = 100.0 * n_s / atr_s
            denom = pdi + ndi
            dx = 100.0 * abs(pdi - ndi) / denom if denom > 0.0 else 0.0
            dxs.append(dx)
    if len(dxs) < p:
        return round(float(np.mean(dxs)), 2) if dxs else 0.0
    adx = float(np.mean(dxs[:p]))
    for dx in dxs[p:]:
        adx = ((adx * (p - 1)) + dx) / p
    return round(adx, 2)


def _classify_transition(prev_r: str, cur_r: str) -> str:
    if prev_r == cur_r or prev_r == "UNKNOWN":
        return "NONE"
    trends = {"TRENDING_BULL", "TRENDING_BEAR"}
    vols = {"HIGH_VOLATILITY", "LOW_VOLATILITY"}
    if prev_r in trends and cur_r == "SIDEWAYS":
        return "TREND_TO_SIDEWAYS"
    if prev_r == "SIDEWAYS" and cur_r in trends:
        return "SIDEWAYS_TO_TREND"
    if prev_r == "TRENDING_BULL" and cur_r == "TRENDING_BEAR":
        return "BULL_TO_BEAR"
    if prev_r == "TRENDING_BEAR" and cur_r == "TRENDING_BULL":
        return "BEAR_TO_BULL"
    if prev_r in vols or cur_r in vols:
        return "VOLATILITY_SHIFT"
    return "OTHER"


def _vol_cat(pctl: float) -> str:
    if pctl > 75.0:
        return "HIGH_VOL"
    if pctl < 25.0:
        return "LOW_VOL"
    return "MED_VOL"


def _adx_cat(adx: float) -> str:
    if adx < 20.0:
        return "WEAK_ADX_LT20"
    if adx < 30.0:
        return "MOD_ADX_20_30"
    return "STRONG_ADX_GE30"


def _subperiod(dt: datetime) -> str:
    d = t6c._as_utc(dt)
    if d < datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc):
        return "P1"
    if d < datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc):
        return "P2"
    return "P3"


def _metrics_df(df: pd.DataFrame) -> dict[str, Any]:
    n = int(len(df))
    if n == 0:
        return {
            "n": 0, "wins": 0, "losses": 0, "wr_pct": 0.0, "gross_pnl": 0.0, "net_pnl": 0.0,
            "pf": 0.0, "expectancy": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
            "mean_mae_atr": 0.0, "mean_mfe_atr": 0.0, "mean_hold_mins": 0.0,
            "closed_dd": 0.0, "costs_usd": 0.0,
        }
    p = df["pnl"].values.astype(float)
    w = p[p > 0]
    l = p[p <= 0]
    pf = float(w.sum() / abs(l.sum())) if len(l) and l.sum() < 0 else (999.0 if len(w) and w.sum() > 0 else 0.0)
    ceq = np.cumsum(p) + 10000.0
    hwm = np.maximum.accumulate(np.insert(ceq, 0, 10000.0))[1:]
    c_dd = float((ceq - hwm).min())
    return {
        "n": n,
        "wins": int(len(w)),
        "losses": int(len(l)),
        "wr_pct": round(len(w) / n * 100.0, 2),
        "gross_pnl": round(float(df["gross_pnl"].sum()), 2),
        "net_pnl": round(float(p.sum()), 2),
        "pf": round(pf, 4),
        "expectancy": round(float(p.mean()), 2),
        "avg_win": round(float(w.mean()), 2) if len(w) else 0.0,
        "avg_loss": round(float(l.mean()), 2) if len(l) else 0.0,
        "mean_mae_atr": round(float(df["mae_atr"].mean()), 4),
        "mean_mfe_atr": round(float(df["mfe_atr"].mean()), 4),
        "mean_hold_mins": round(float(df["holding_mins"].mean()), 2),
        "closed_dd": round(c_dd, 2),
        "costs_usd": round(float((df["commission_usd"] + df["slippage_usd"]).sum()), 2),
    }


class _DualPortfolio:
    def __init__(self, name: str, max_score: float):
        self.name = name
        self.max_score = max_score
        self.open_pos: Optional[t6c.IndPos] = None
        self.open_meta: dict[str, Any] = {}
        self.trades: list[dict[str, Any]] = []
        self.equity = 10000.0
        self.day_start_eq = 10000.0
        self.mtm = t6c.IndMtmTracker(10000.0)
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.consec_losses = 0
        self.current_day: Optional[date] = None
        self.last_cd_bar: dict[tuple[str, str], int] = {}

    def new_day(self, d: date) -> None:
        if self.current_day != d:
            self.current_day = d
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.consec_losses = 0
            self.day_start_eq = max(self.equity, 1.0)

    def close_pos(self, pos: t6c.IndPos, reason: str, exit_px: float, exit_ts: datetime, cur_reg_snap: dict[str, Any]) -> None:
        pos.apply_rollovers(exit_ts)
        c_ex = t6c._ind_side_comm(pos.lot, exit_px)
        pos.comm_exit = c_ex
        pos.comm_total = round(pos.entry_comm + c_ex, 2)
        gross = pos.gross_at(exit_px)
        net = gross - pos.comm_total + pos.swap_pnl
        self.equity += net
        self.daily_pnl += net
        self.consec_losses = 0 if net > 0 else self.consec_losses + 1
        self.mtm.update(self.equity)

        is_bull = pos.direction == "BUY"
        mae_diff = (pos.entry_price - pos.worst_price) if is_bull else (pos.worst_price - pos.entry_price)
        mfe_diff = (pos.peak_price - pos.entry_price) if is_bull else (pos.entry_price - pos.peak_price)
        e_utc = t6c._as_utc(pos.entry_time)
        x_utc = t6c._as_utc(exit_ts)

        row = dict(self.open_meta)
        row.update({
            "variant": self.name,
            "trade_idx": len(self.trades) + 1,
            "entry_time": e_utc.isoformat(),
            "exit_time": x_utc.isoformat(),
            "subperiod": _subperiod(e_utc),
            "holding_mins": round((x_utc - e_utc).total_seconds() / 60.0, 2),
            "entry_price": pos.entry_price,
            "exit_price": exit_px,
            "qty": pos.lot,
            "gross_pnl": gross,
            "pnl": net,
            "commission_usd": pos.comm_total,
            "slippage_usd": pos.slip_usd,
            "exit_reason": reason,
            "mae_atr": round(max(0.0, mae_diff) / max(pos.atr, 1e-9), 4),
            "mfe_atr": round(max(0.0, mfe_diff) / max(pos.atr, 1e-9), 4),
            # Exit-time regime (recorded strictly post-trade for drift analysis only!)
            "exit_regime": cur_reg_snap.get("primary_regime", "UNKNOWN"),
            "exit_vol_category": cur_reg_snap.get("vol_category", "MED_VOL"),
            "exit_adx_14": cur_reg_snap.get("adx_14", 0.0),
            "regime_changed_during_trade": int(cur_reg_snap.get("primary_regime", "UNKNOWN") != row.get("primary_regime")),
        })
        self.trades.append(row)

    def scan_exit(self, bids: np.ndarray, asks: np.ndarray, ttimes: list[datetime], start_i: int, eval_to: bool) -> Optional[tuple[str, float, datetime]]:
        pos = self.open_pos
        if pos is None:
            return None
        is_bull = pos.direction == "BUY"
        e_hr = pos.entry_time.hour
        eff_mh = 60 if 7 <= e_hr < 20 else 90
        best_q = None
        last_q = None
        for i in range(start_i, len(bids)):
            b = float(bids[i])
            a = float(asks[i])
            ts_i = ttimes[i]
            if b <= 0.0 or a <= 0.0 or b >= a:
                continue
            pos.apply_rollovers(ts_i)
            eq_q = b if is_bull else a
            last_q = eq_q
            if is_bull and eq_q < pos.worst_price:
                pos.worst_price = eq_q
            elif (not is_bull) and eq_q > pos.worst_price:
                pos.worst_price = eq_q
            if best_q is None or (is_bull and eq_q > best_q) or (not is_bull and eq_q < best_q):
                best_q = eq_q
            if is_bull and b <= pos.sl:
                rsn = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
                fpx = b - t6c._SLIP_OFFSET
                pos.slip_usd += t6c._SLIP_OFFSET * pos.lot * t6c._CONTRACT_SIZE
                self.mtm.update(self.equity + pos.net_at(fpx))
                return rsn, fpx, ts_i
            if (not is_bull) and a >= pos.sl:
                rsn = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
                fpx = a + t6c._SLIP_OFFSET
                pos.slip_usd += t6c._SLIP_OFFSET * pos.lot * t6c._CONTRACT_SIZE
                self.mtm.update(self.equity + pos.net_at(fpx))
                return rsn, fpx, ts_i
            if is_bull and b >= pos.tp:
                fpx = float(pos.tp)
                self.mtm.update(self.equity + pos.net_at(fpx))
                return "TARGET", fpx, ts_i
            if (not is_bull) and a <= pos.tp:
                fpx = float(pos.tp)
                self.mtm.update(self.equity + pos.net_at(fpx))
                return "TARGET", fpx, ts_i
            self.mtm.update(self.equity + pos.net_at(eq_q))
            pos.ratchet(eq_q, eff_mh)
        if eval_to and pos.bars_held >= eff_mh:
            bpx = last_q if last_q is not None else pos.entry_price
            epx = (bpx - t6c._SLIP_OFFSET) if is_bull else (bpx + t6c._SLIP_OFFSET)
            if last_q is not None:
                pos.slip_usd += t6c._SLIP_OFFSET * pos.lot * t6c._CONTRACT_SIZE
            return "TIMEOUT", epx, ttimes[-1]
        if best_q is not None:
            pos.ratchet(best_q, eff_mh)
        return None


def main() -> None:
    # Phase 1: Hash & Previous Evidence Verification
    manifest = json.loads((FROZEN_V2_DIR / "task6c_frozen_manifest.json").read_text(encoding="utf-8"))
    t4 = manifest["prior_frozen_task4_preserved"]
    app_sum = manifest["application_summary"]
    t10_h = {r["hypothesis"]: r for r in json.loads((AUDIT_DIR / "task10_hypothesis_results.json").read_text(encoding="utf-8"))}
    t10_rec = json.loads((AUDIT_DIR / "task10_independent_reconciliation.json").read_text(encoding="utf-8"))

    hash_verif = {
        "task4_match": _sha256(ROOT / "backtest_results" / "frozen_111_baseline_trades.csv") == t4["frozen_111_baseline_trades_sha256"],
        "v2_w1_match": _sha256(FROZEN_V2_DIR / "w1_trades.csv") == app_sum["W1"]["trades_csv_sha256"],
        "v2_w2_match": _sha256(FROZEN_V2_DIR / "w2_trades.csv") == app_sum["W2"]["trades_csv_sha256"],
        "v2_w3_match": _sha256(FROZEN_V2_DIR / "w3_trades.csv") == app_sum["W3"]["trades_csv_sha256"],
        "v2_w3_journeys_match": _sha256(FROZEN_V2_DIR / "w3_journeys.json") == app_sum["W3"]["journeys_json_sha256"],
    }
    if not all(hash_verif.values()):
        raise RuntimeError(f"Frozen hash mismatch: {hash_verif}")

    # Phase 3: Stream W3 raw ticks once, reconstruct causal 5m regime features + run H0 & H4a
    db_url = os.environ.get("DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge").replace("+psycopg2", "")
    conn = psycopg2.connect(db_url)
    start_dt = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc)
    warmup_start = start_dt - timedelta(days=4)

    def _fetch(s_dt: datetime, e_dt: datetime) -> pd.DataFrame:
        chunks = []
        cs = s_dt
        while cs < e_dt:
            ce = min(cs + timedelta(days=7), e_dt)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT time, bid, ask FROM market_ticks WHERE symbol = 'XAUUSD' AND time >= %s AND time < %s ORDER BY time ASC",
                    (cs, ce),
                )
                rows = cur.fetchall()
            if rows:
                chunks.append(pd.DataFrame(rows, columns=["time", "bid", "ask"]))
            cs = ce
        if not chunks:
            return pd.DataFrame(columns=["time", "bid", "ask"])
        out = pd.concat(chunks, ignore_index=True)
        out["time"] = pd.to_datetime(out["time"], utc=True)
        return out

    kalman = t6c.IndKalman2D()
    agg_5m = t6c.IndBarAggregator(5, 200)
    agg_1h = t6c.IndBarAggregator(60, 100)
    regime_det = t6c.IndRegimeDetector()
    current_regime = "UNKNOWN"
    prev_committed_regime = "UNKNOWN"
    last_transition_type = "NONE"
    bars_since_transition = 999
    bar5m_rows: list[dict[str, Any]] = []
    transition_counts: dict[str, int] = {}
    cur_reg_snap: dict[str, Any] = {"primary_regime": "UNKNOWN", "vol_category": "MED_VOL", "adx_14": 0.0}

    pfs = [_DualPortfolio("H0", 1.01), _DualPortfolio("H4a", 0.80)]
    bar_idx = -1
    last_valid: Optional[tuple[float, float, datetime]] = None

    try:
        w_df = _fetch(warmup_start, start_dt)
        if not w_df.empty:
            w_df["minute"] = w_df["time"].dt.floor("min")
            for mts, grp in w_df.groupby("minute", sort=True):
                wb = grp["bid"].values.astype(np.float64)
                wa = grp["ask"].values.astype(np.float64)
                vm = (wb > 0.0) & (wa > 0.0) & (wb < wa)
                vi = np.flatnonzero(vm)
                if len(vi) == 0:
                    continue
                for j in vi:
                    kalman.update(float(wb[j]))
                vb = wb[vi]
                wc = {"time": mts.to_pydatetime(), "open": float(vb[0]), "high": float(vb.max()), "low": float(vb.min()), "close": float(vb[-1]), "tick_count": int(len(vi))}
                agg_5m.update(wc)
                agg_1h.update(wc)
        del w_df

        end_ex = end_dt + timedelta(microseconds=1)
        cs = start_dt
        while cs < end_ex:
            ce = min(cs + timedelta(days=7), end_ex)
            print(f"[TASK 11 W3] Streaming {cs.strftime('%Y-%m-%d')} -> {ce.strftime('%Y-%m-%d')} (H0={len(pfs[0].trades)}, H4a={len(pfs[1].trades)})...", flush=True)
            t_df = _fetch(cs, ce)
            cs = ce
            if t_df.empty:
                continue
            t_df["minute"] = t_df["time"].dt.floor("min")
            for mts, grp in t_df.groupby("minute", sort=True):
                bar_idx += 1
                m_py = mts.to_pydatetime()
                d_py = m_py.date()
                bids = grp["bid"].values.astype(np.float64)
                asks = grp["ask"].values.astype(np.float64)
                ttimes = [pd.Timestamp(x).to_pydatetime() for x in grp["time"].values]
                vm = (bids > 0.0) & (asks > 0.0) & (bids < asks)
                vi = np.flatnonzero(vm)
                if len(vi) > 0:
                    lv = int(vi[-1])
                    last_valid = (float(bids[lv]), float(asks[lv]), ttimes[lv])

                closed_5m = False
                df_ind = pd.DataFrame()
                if len(vi) > 0:
                    vb = bids[vi]
                    va = asks[vi]
                    mean_spr = float((va - vb).mean())
                    candle = {"time": m_py, "open": float(vb[0]), "high": float(vb.max()), "low": float(vb.min()), "close": float(vb[-1]), "tick_count": int(len(vi))}
                    closed_5m = agg_5m.update(candle)
                    agg_1h.update(candle)
                    if closed_5m and len(agg_5m.bars) >= 60:
                        df_ind = t6c._ind_compute_indicators(list(agg_5m.bars))
                        if not df_ind.empty:
                            new_reg = regime_det.detect(df_ind)
                            if new_reg != current_regime:
                                ttype = _classify_transition(current_regime, new_reg)
                                prev_committed_regime = current_regime
                                current_regime = new_reg
                                last_transition_type = ttype
                                bars_since_transition = 0
                                tr_key = f"{prev_committed_regime}->{current_regime}"
                                transition_counts[tr_key] = transition_counts.get(tr_key, 0) + 1
                            else:
                                bars_since_transition += 1

                            # Compute independent causal regime features on this closed 5m bar
                            lr = df_ind.iloc[-1]
                            tr_s = pd.concat([
                                df_ind["high"] - df_ind["low"],
                                (df_ind["high"] - df_ind["close"].shift(1)).abs(),
                                (df_ind["low"] - df_ind["close"].shift(1)).abs(),
                            ], axis=1).max(axis=1)
                            atr_pct_s = (tr_s.rolling(14).mean() / df_ind["close"]).dropna()
                            cur_atr_pct = float(atr_pct_s.iloc[-1]) if len(atr_pct_s) else 0.0
                            atr_pctl = round(float((atr_pct_s <= cur_atr_pct).mean() * 100.0), 2) if len(atr_pct_s) > 1 else 50.0
                            vcat = _vol_cat(atr_pctl)
                            adx_v = _compute_wilder_adx14(df_ind)
                            er_v = round(t6c._ind_kaufman_er(df_ind["close"]), 4)
                            atr_v = float(lr["atr"]) if pd.notna(lr.get("atr")) else 1.0
                            close_v = float(lr["close"])
                            e20_v = float(lr["ema20"]) if pd.notna(lr.get("ema20")) else close_v
                            e50_v = float(lr["ema50"]) if pd.notna(lr.get("ema50")) else close_v
                            slp_v = float(lr["ema20_slope"]) if pd.notna(lr.get("ema20_slope")) else 0.0
                            h20 = float(df_ind["high"].tail(20).max())
                            l20 = float(df_ind["low"].tail(20).min())
                            htf = t6c._ind_htf_trend([b["close"] for b in agg_1h.bars])
                            c5_bar = agg_5m.bars[-1]

                            cur_reg_snap = {
                                "bar_time": c5_bar["time"].isoformat(),
                                "utc_hour": int(c5_bar["time"].hour),
                                "close": round(close_v, 3),
                                "primary_regime": current_regime,
                                "prev_regime": prev_committed_regime,
                                "transition_type": last_transition_type,
                                "bars_since_transition": bars_since_transition,
                                "vol_category": vcat,
                                "atr": round(atr_v, 4),
                                "atr_pct": round(cur_atr_pct, 6),
                                "atr_pctl_200": atr_pctl,
                                "adx_14": adx_v,
                                "trend_strength": _adx_cat(adx_v),
                                "kaufman_er": er_v,
                                "ema20_slope": round(slp_v, 7),
                                "ema_spread_atr": round((e20_v - e50_v) / max(atr_v, 1e-9), 4),
                                "dist_ema20_atr": round((close_v - e20_v) / max(atr_v, 1e-9), 4),
                                "dist_ema50_atr": round((close_v - e50_v) / max(atr_v, 1e-9), 4),
                                "dist_20h_atr": round((h20 - close_v) / max(atr_v, 1e-9), 4),
                                "dist_20l_atr": round((close_v - l20) / max(atr_v, 1e-9), 4),
                                "htf_trend": htf,
                                "kalman_vel": round(kalman.x1, 6),
                                "kalman_accel": round(kalman.accel(), 6),
                                "mean_spread_usd": round(mean_spr, 4),
                                "tick_count_5m": int(c5_bar["tick_count"]),
                            }
                            bar5m_rows.append(cur_reg_snap)

                entered_or_open = [False, False]
                for p_idx, pf in enumerate(pfs):
                    pf.new_day(d_py)
                    if pf.open_pos is not None:
                        entered_or_open[p_idx] = True
                        pf.open_pos.bars_held += 1
                        ex = pf.scan_exit(bids, asks, ttimes, 0, True)
                        if ex is not None:
                            rsn, epx, ex_ts = ex
                            pf.close_pos(pf.open_pos, rsn, epx, ex_ts, cur_reg_snap)
                            pf.open_pos = None

                if len(vi) == 0:
                    continue
                en_i = int(vi[0])
                for j in range(en_i + 1):
                    if vm[j]:
                        kalman.update(float(bids[j]))
                k_snap_x1 = kalman.x1
                k_snap_acc = kalman.accel()
                for j in range(en_i + 1, len(bids)):
                    if vm[j]:
                        kalman.update(float(bids[j]))

                if not closed_5m or len(agg_5m.bars) < 60 or df_ind.empty:
                    continue
                htf = t6c._ind_htf_trend([b["close"] for b in agg_1h.bars])
                if htf == 0:
                    continue
                sigs = t6c._ind_generate_signals(df_ind, current_regime)
                if not sigs:
                    continue

                lr = df_ind.iloc[-1]
                cs_s = df_ind["close"]
                c5_ts = agg_5m.bars[-1]["time"]
                ent_v = t6c._ind_shannon_entropy(cs_s.pct_change().dropna()) if len(cs_s) > 30 else 2.0
                er_v = t6c._ind_kaufman_er(cs_s) if len(cs_s) > 15 else 0.3
                feat = {
                    "kalman_vel": k_snap_x1,
                    "kalman_accel": k_snap_acc,
                    "shannon_entropy": ent_v,
                    "kaufman_er": er_v,
                    "ema20_slope": float(lr["ema20_slope"]) if pd.notna(lr.get("ema20_slope")) else 0.0,
                    "tick_weighted_price_dist": float(lr["tick_weighted_price_dist"]) if pd.notna(lr.get("tick_weighted_price_dist")) else 0.0,
                    "tick_rate_ratio": float(lr["tick_rate_ratio"]) if pd.notna(lr.get("tick_rate_ratio")) else 1.0,
                    "utc_hour": c5_ts.hour,
                }
                atr_v = float(df_ind["atr"].iloc[-1]) if pd.notna(df_ind["atr"].iloc[-1]) else 1.0
                eb, ea = float(bids[en_i]), float(asks[en_i])

                scored = [(t6c._ind_score(d, current_regime, feat), st, d) for st, d in sigs]
                scored.sort(key=lambda x: x[0], reverse=True)

                for p_idx, pf in enumerate(pfs):
                    if entered_or_open[p_idx]:
                        continue
                    if abs(min(0.0, pf.daily_pnl)) / max(pf.day_start_eq, 1.0) * 100.0 >= 3.0 or pf.daily_trades >= 8 or pf.consec_losses >= 3:
                        continue
                    if current_regime == "HIGH_VOLATILITY":
                        continue

                    chosen = None
                    for sc, st, d in scored:
                        if not (0.60 <= sc < pf.max_score):
                            continue
                        if (d == "BUY" and htf != 1) or (d == "SELL" and htf != -1):
                            continue
                        if d == "BUY" and current_regime == "SIDEWAYS":
                            continue
                        if bar_idx - pf.last_cd_bar.get((st, d), -9999) < 15:
                            continue
                        chosen = (sc, st, d)
                        break

                    if chosen is None:
                        continue
                    sc, st, d = chosen
                    raw_ep = ea if d == "BUY" else eb
                    ep = (raw_ep + t6c._SLIP_OFFSET) if d == "BUY" else (raw_ep - t6c._SLIP_OFFSET)
                    lot, sl_px, tp_px = t6c._ind_size(pf.equity, ep, atr_v, d)
                    if lot is None:
                        continue

                    e_comm = t6c._ind_side_comm(lot, ep)
                    e_slip = t6c._SLIP_OFFSET * lot * t6c._CONTRACT_SIZE
                    pf.open_pos = t6c.IndPos(
                        direction=d, strategy=st, entry_time=ttimes[en_i],
                        entry_bid=eb, entry_ask=ea, entry_price=ep,
                        initial_sl=sl_px, sl=sl_px, tp=tp_px, atr=atr_v, lot=lot,
                        entry_comm=e_comm, slip_usd=e_slip,
                        peak_price=ep, worst_price=ep, last_swap_ts=ttimes[en_i],
                    )
                    pf.open_meta = dict(cur_reg_snap)
                    pf.open_meta.update({
                        "signal_time": c5_ts.isoformat(),
                        "direction": d,
                        "strategy": st,
                        "score": sc,
                        "entry_bid": eb,
                        "entry_ask": ea,
                        "entry_spread_usd": round(ea - eb, 4),
                        "entry_kalman_vel": round(k_snap_x1, 6),
                        "entry_kalman_accel": round(k_snap_acc, 6),
                        "aligned_ext_ema20_atr": round(cur_reg_snap["dist_ema20_atr"] if d == "BUY" else -cur_reg_snap["dist_ema20_atr"], 4),
                        "recent_transition_3bars": int(cur_reg_snap["bars_since_transition"] <= 3),
                        "session": "London_PreOpen_07_08" if c5_ts.hour < 9 else ("London_Core_09_11" if c5_ts.hour < 12 else "LDN_NY_Overlap_12_16"),
                    })
                    pf.mtm.update(pf.equity - (ea - eb) * lot * t6c._CONTRACT_SIZE - 2.0 * e_comm - e_slip)
                    pf.last_cd_bar[(st, d)] = bar_idx
                    pf.daily_trades += 1

                    if en_i + 1 < len(bids):
                        ex = pf.scan_exit(bids, asks, ttimes, en_i + 1, False)
                        if ex is not None:
                            rsn, epx, ex_ts = ex
                            pf.close_pos(pf.open_pos, rsn, epx, ex_ts, cur_reg_snap)
                            pf.open_pos = None

        if last_valid is not None:
            lb, la, lt = last_valid
            for pf in pfs:
                if pf.open_pos is not None:
                    rc = lb if pf.open_pos.direction == "BUY" else la
                    epx = (rc - t6c._SLIP_OFFSET) if pf.open_pos.direction == "BUY" else (rc + t6c._SLIP_OFFSET)
                    pf.open_pos.slip_usd += t6c._SLIP_OFFSET * pf.open_pos.lot * t6c._CONTRACT_SIZE
                    pf.close_pos(pf.open_pos, "END_OF_BACKTEST", epx, lt, cur_reg_snap)
                    pf.open_pos = None
    finally:
        conn.close()

    # Save Deliverable #1: Baseline Verification
    h0_pf, h4a_pf = pfs[0], pfs[1]
    base_verif = {
        "frozen_hashes": hash_verif,
        "w3_reproduction": {
            "H0": {"n": len(h0_pf.trades), "net_pnl": round(h0_pf.equity - 10000.0, 2), "mtm_dd_pct": round(h0_pf.mtm.max_dd_pct, 2), "diff_vs_task10": round(abs((h0_pf.equity - 10000.0) - t10_h["H0_BASELINE_V2"]["overall"]["net_pnl"]), 4)},
            "H4a": {"n": len(h4a_pf.trades), "net_pnl": round(h4a_pf.equity - 10000.0, 2), "mtm_dd_pct": round(h4a_pf.mtm.max_dd_pct, 2), "diff_vs_task10": round(abs((h4a_pf.equity - 10000.0) - t10_h["H4a_SCORE_CAP_080"]["overall"]["net_pnl"]), 4)},
        },
        "task10_reconciliation_ref": t10_rec,
        "bar5m_count_reconstructed": len(bar5m_rows),
    }
    (AUDIT_DIR / "task11_baseline_verification.json").write_text(json.dumps(base_verif, indent=2), encoding="utf-8")

    # Save Deliverable #3: Regime Features 5m CSV
    bars_df = pd.DataFrame(bar5m_rows)
    bars_df.to_csv(AUDIT_DIR / "task11_regime_features.csv", index=False)

    # Save Deliverable #2: Regime Classifier Audit
    reg_dist = bars_df["primary_regime"].value_counts().to_dict()
    vol_dist = bars_df["vol_category"].value_counts().to_dict()
    adx_dist = bars_df["trend_strength"].value_counts().to_dict()
    tr_type_dist = bars_df["transition_type"].value_counts().to_dict()
    classifier_audit = {
        "implementation_source": "src/goldedge/domain/services/regime_detector.py (RegimeDetectorService)",
        "timeframe": "Completed 5-minute bars (rolling 200-bar window = ~16.7h)",
        "priority_thresholds": {
            "1_HIGH_VOLATILITY": "14-bar ATR/close > 75th percentile of rolling 200-bar ATR/close",
            "2_LOW_VOLATILITY": "14-bar ATR/close < 25th percentile of rolling 200-bar ATR/close",
            "3_TRENDING_BULL": "ema_diff=(ema20-ema50)/ema50 > 0.003 AND ema20_slope > 0",
            "4_TRENDING_BEAR": "ema_diff < -0.003 AND ema20_slope < 0",
            "5_SIDEWAYS": "20-bar (high-low)/close < 0.004",
            "6_FALLBACK": "ema_diff > 0 -> TRENDING_BULL; ema_diff < 0 -> TRENDING_BEAR; else SIDEWAYS",
        },
        "hysteresis": "3 consecutive identical closed 5m raw classifications (15 minutes) required to switch committed regime",
        "completed_candles_only": True,
        "live_vs_backtest_parity": "Equivalent logic and 3-bar hysteresis (B6 remediation). ScanMarketUseCase replays recent completed bars into a fresh RegimeDetectorService per scan; relative 200-bar volatility percentiles can shift intraday as rolling window advances.",
        "unused_static_method": "classify_institutional_regime (SESSION_ORB/KINEMATIC_TREND/MEAN_REVERSION) is not called by RunBacktestUseCase or ScanMarketUseCase.",
        "w3_5m_bar_distributions": {
            "total_closed_5m_bars": int(len(bars_df)),
            "primary_regime_counts": {k: int(v) for k, v in reg_dist.items()},
            "vol_category_counts": {k: int(v) for k, v in vol_dist.items()},
            "adx_trend_strength_counts": {k: int(v) for k, v in adx_dist.items()},
            "transition_family_counts": {k: int(v) for k, v in tr_type_dist.items()},
            "top_regime_transitions": dict(sorted(transition_counts.items(), key=lambda x: x[1], reverse=True)[:15]),
        },
    }
    (AUDIT_DIR / "task11_regime_classifier_audit.json").write_text(json.dumps(classifier_audit, indent=2), encoding="utf-8")

    # Save Deliverable #4: Trade Regime Enrichment CSV (H0 + H4a)
    enr_df = pd.DataFrame(h0_pf.trades + h4a_pf.trades)
    # Add derived bins for Phase 4 & 5
    enr_df["score_band"] = pd.cut(enr_df["score"], bins=[0.60, 0.70, 0.80, 0.90, 1.01], labels=["0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90-1.00"], right=False).astype(str)
    enr_df["er_band"] = pd.cut(enr_df["kaufman_er"], bins=[-0.01, 0.30, 0.45, 0.60, 1.01], labels=["ER_LE_030", "ER_030_045", "ER_045_060", "ER_GT_060"]).astype(str)
    enr_df["ext_band"] = pd.cut(enr_df["aligned_ext_ema20_atr"], bins=[-10.0, 1.0, 1.5, 2.5, 20.0], labels=["EXT_LE_1.0", "EXT_1.0_1.5", "EXT_1.5_2.5", "EXT_GT_2.5"]).astype(str)
    enr_df["atr_abs_band"] = pd.qcut(enr_df["atr"], q=3, labels=["ATR_LOW_T1", "ATR_MID_T2", "ATR_HIGH_T3"]).astype(str)
    enr_df["mom_band"] = pd.qcut(enr_df["entry_kalman_vel"].abs(), q=3, labels=["VEL_LOW_T1", "VEL_MID_T2", "VEL_HIGH_T3"]).astype(str)
    enr_df.to_csv(AUDIT_DIR / "task11_trade_regime_enrichment.csv", index=False)

    # Save Deliverables #5, #6, #7: Regime Performance (H0 & H4a) + BUY/SELL CSV + H4a Comparison
    h0_df = enr_df[enr_df["variant"] == "H0"].copy()
    h4a_df = enr_df[enr_df["variant"] == "H4a"].copy()

    dims = ["primary_regime", "vol_category", "atr_abs_band", "trend_strength", "mom_band", "transition_type", "recent_transition_3bars", "regime_changed_during_trade", "session", "score_band", "er_band", "ext_band", "subperiod"]
    perf_json: dict[str, Any] = {"H0": {}, "H4a": {}}
    bs_rows = []
    for vname, vdf in [("H0", h0_df), ("H4a", h4a_df)]:
        for dim in dims:
            perf_json[vname][dim] = {}
            for val, grp in vdf.groupby(dim, sort=True):
                sval = str(val)
                perf_json[vname][dim][sval] = {
                    "ALL": _metrics_df(grp),
                    "BUY": _metrics_df(grp[grp["direction"] == "BUY"]),
                    "SELL": _metrics_df(grp[grp["direction"] == "SELL"]),
                }
                for side_n, sdf in [("ALL", grp), ("BUY", grp[grp["direction"] == "BUY"]), ("SELL", grp[grp["direction"] == "SELL"])]:
                    m = _metrics_df(sdf)
                    m.update({"variant": vname, "dimension": dim, "segment": sval, "direction": side_n})
                    bs_rows.append(m)

    (AUDIT_DIR / "task11_regime_performance.json").write_text(json.dumps(perf_json, indent=2), encoding="utf-8")
    pd.DataFrame(bs_rows).to_csv(AUDIT_DIR / "task11_buy_sell_regime_analysis.csv", index=False)

    # H4a vs H0 side-by-side comparison across key regime dimensions
    h4a_comp: dict[str, Any] = {}
    for dim in ["primary_regime", "vol_category", "atr_abs_band", "trend_strength", "recent_transition_3bars", "subperiod"]:
        h4a_comp[dim] = {}
        all_vals = sorted(set(h0_df[dim].astype(str)).union(set(h4a_df[dim].astype(str))))
        for val in all_vals:
            m0 = _metrics_df(h0_df[h0_df[dim].astype(str) == val])
            m4 = _metrics_df(h4a_df[h4a_df[dim].astype(str) == val])
            b0 = _metrics_df(h0_df[(h0_df[dim].astype(str) == val) & (h0_df["direction"] == "BUY")])
            b4 = _metrics_df(h4a_df[(h4a_df[dim].astype(str) == val) & (h4a_df["direction"] == "BUY")])
            s0 = _metrics_df(h0_df[(h0_df[dim].astype(str) == val) & (h0_df["direction"] == "SELL")])
            s4 = _metrics_df(h4a_df[(h4a_df[dim].astype(str) == val) & (h4a_df["direction"] == "SELL")])
            h4a_comp[dim][val] = {
                "H0_ALL": m0, "H4a_ALL": m4,
                "delta_n": m4["n"] - m0["n"],
                "delta_pnl": round(m4["net_pnl"] - m0["net_pnl"], 2),
                "H0_BUY": b0, "H4a_BUY": b4,
                "H0_SELL": s0, "H4a_SELL": s4,
            }
    (AUDIT_DIR / "task11_h4a_regime_comparison.json").write_text(json.dumps(h4a_comp, indent=2), encoding="utf-8")

    print("\n[TASK 11 PART 1 COMPLETE]")
    print("Verification:", json.dumps(base_verif, indent=2))
    print("\nH0 vs H4a by Primary Regime & Volatility & ADX:")
    for dim in ["primary_regime", "vol_category", "trend_strength", "recent_transition_3bars"]:
        print(f"--- {dim} ---")
        for val, d in h4a_comp[dim].items():
            print(f"  {val:20s} | H0: n={d['H0_ALL']['n']:3d} pnl={d['H0_ALL']['net_pnl']:7.1f} pf={d['H0_ALL']['pf']:.3f} | H4a: n={d['H4a_ALL']['n']:3d} pnl={d['H4a_ALL']['net_pnl']:7.1f} pf={d['H4a_ALL']['pf']:.3f} | dPnL={d['delta_pnl']:+7.1f}")


if __name__ == "__main__":
    main()
