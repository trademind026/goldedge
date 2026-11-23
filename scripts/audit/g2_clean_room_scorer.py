"""
Task G2 — Clean-Room Scorer, Full Candidate Cache & Causal Simulator.

STRICT RULE: ZERO imports from `src/goldedge/`. Uses pure helpers from
`g1_clean_room_engine.py` (which itself has zero `src/goldedge/` imports).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
import math
from pathlib import Path
import sys
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy.stats import entropy as scipy_entropy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import g1_clean_room_engine as g1

_LOG2_10 = math.log2(10.0)
_BINS_1M = [
    -np.inf,
    -0.000500,
    -0.000200,
    -0.000100,
    -0.000050,
    0.0,
    0.000050,
    0.000100,
    0.000200,
    0.000500,
    np.inf,
]
_BINS_5M = [
    -np.inf,
    -0.001120,
    -0.000450,
    -0.000220,
    -0.000110,
    0.0,
    0.000110,
    0.000220,
    0.000450,
    0.001120,
    np.inf,
]


def _score_band(v: float, g0: float, g1_v: float, b0: Optional[float], b1: Optional[float]) -> float:
    return g1._score_band(v, g0, g1_v, b0, b1)


def _interp(v: float, lo: float, hi: float) -> float:
    return g1._interp(v, lo, hi)


def _sess_score(h: int) -> float:
    return g1._sess_score(h)


def compute_entropy_bits(returns: pd.Series, bins: list[float], window: int = 30) -> float:
    if len(returns) < window:
        return 3.5
    recent = returns.iloc[-window:]
    counts, _ = np.histogram(recent, bins=bins)
    tot = counts.sum()
    if tot <= 0:
        return 3.5
    probs = counts / tot
    probs = probs[probs > 0]
    return float(scipy_entropy(probs, base=2))


@dataclass
class G2SpanCache:
    r_start_off: int
    k_vel_all: np.ndarray
    candidate_minutes: dict[int, dict[str, Any]]
    signal_minutes: dict[int, dict[str, Any]]
    bars_processed: int
    inverted_skipped: int
    zero_spread_skipped: int


def precompute_g2_span_cache(span: g1.PreparedSpan) -> G2SpanCache:
    """
    Causal precomputation over `span` matching post-E1 `g1_clean_room_engine` bit-for-bit.
    Captures EVERY in-session closed 5m candidate signal (even when HTF is neutral or >= 15 UTC)
    into `candidate_minutes`, plus exact app-equivalent active `signal_minutes` (htf_trend != 0).
    """
    kalman = g1.CleanKalman2D()
    agg_5m = g1.CleanBarAgg(5, 200)
    agg_1h = g1.CleanBarAgg(60, 100)
    regime_det = g1.CleanRegimeDetector()
    current_regime = "UNKNOWN"

    w_bids = span.w_bids
    w_asks = span.w_asks
    for ms in span.warmup_slices:
        for j in range(ms.s, ms.e):
            b = float(w_bids[j])
            a = float(w_asks[j])
            if b > 0.0 and a > 0.0 and b < a:
                kalman.update(b)
        if ms.candle is not None:
            agg_5m.update(ms.candle)
            agg_1h.update(ms.candle)

    r_start_off = span.replay_slices[0].s if span.replay_slices else 0
    r_end_off = span.replay_slices[-1].e if span.replay_slices else 0
    k_vel_all = np.zeros(max(0, r_end_off - r_start_off), dtype=np.float64)

    r_ts = span.r_ts_ns
    r_bids = span.r_bids
    r_asks = span.r_asks
    candidate_minutes: dict[int, dict[str, Any]] = {}
    signal_minutes: dict[int, dict[str, Any]] = {}
    bars_processed = 0
    inverted_skipped = 0
    zero_spread_skipped = 0

    def _sf(v: Any, dflt: float) -> float:
        try:
            f = float(v)
            return dflt if math.isnan(f) else f
        except Exception:
            return dflt

    for idx_m, ms in enumerate(span.replay_slices):
        inverted_skipped += ms.inverted_count
        zero_spread_skipped += ms.zero_spread_count
        s_i, e_i = ms.s, ms.e
        bids = r_bids[s_i:e_i]
        asks = r_asks[s_i:e_i]
        valid_mask = (bids > 0.0) & (asks > 0.0) & (bids < asks)
        valid_idxs = np.flatnonzero(valid_mask)

        en_i = int(valid_idxs[0]) if len(valid_idxs) > 0 else -1
        en_k = None
        for j in valid_idxs:
            ji = int(j)
            kp, kv = kalman.update(float(bids[ji]))
            k_vel_all[(s_i + ji) - r_start_off] = kv
            if ji == en_i:
                en_k = (kp, kv, kalman.accel())

        closed_5m = False
        df_ind = pd.DataFrame()
        if ms.candle is not None:
            closed_5m = agg_5m.update(ms.candle)
            agg_1h.update(ms.candle)
            bars_processed += 1
            if closed_5m and len(agg_5m.bars) >= 60:
                df_ind = g1.clean_compute_indicators(list(agg_5m.bars), compute_adx=True)
                if not df_ind.empty:
                    current_regime = regime_det.detect(df_ind)

        if en_i >= 0 and en_k is not None and closed_5m and len(agg_5m.bars) >= 60 and not df_ind.empty:
            htf_trend, htf_spread_pct = g1.clean_htf_trend([b["close"] for b in agg_1h.bars])
            sigs = g1.clean_generate_signals(df_ind, current_regime)
            if sigs:
                lr = df_ind.iloc[-1]
                cs_s = df_ind["close"]
                rets = cs_s.pct_change().dropna()
                c5_ts = agg_5m.bars[-1]["time"]
                atr_v = _sf(lr.get("atr"), 1.0)
                close_v = _sf(lr.get("close"), 0.0)
                open_v = _sf(lr.get("open"), close_v)
                high_v = _sf(lr.get("high"), close_v)
                low_v = _sf(lr.get("low"), close_v)
                ema20_v = _sf(lr.get("ema20"), 0.0)
                ema50_v = _sf(lr.get("ema50"), 0.0)
                adx_v = _sf(lr.get("adx"), np.nan) if "adx" in df_ind.columns else np.nan
                adx_prev3 = _sf(df_ind["adx"].iloc[-4], np.nan) if ("adx" in df_ind.columns and len(df_ind) >= 4) else np.nan
                adx_delta_3 = (adx_v - adx_prev3) if not (math.isnan(adx_v) or math.isnan(adx_prev3)) else np.nan
                atr_tail = df_ind["atr"].dropna().tail(20) if "atr" in df_ind.columns else pd.Series(dtype=float)
                atr_mean_20 = float(atr_tail.mean()) if len(atr_tail) > 0 else atr_v
                atr_ratio_20 = (atr_v / atr_mean_20) if atr_mean_20 > 1e-9 else 1.0

                ema20_dist_atr = ((close_v - ema20_v) / max(atr_v, 1e-9)) if ema20_v > 0 else 0.0
                ema_spread_pct = ((ema20_v - ema50_v) / ema50_v * 100.0) if ema50_v > 0 else 0.0
                bb_u = _sf(lr.get("bollinger_upper"), np.nan)
                bb_m = _sf(lr.get("bollinger_mid"), np.nan)
                bb_l = _sf(lr.get("bollinger_lower"), np.nan)
                bb_w = _sf(lr.get("bb_width"), np.nan)
                bb_pos = ((close_v - bb_l) / max(bb_u - bb_l, 1e-9)) if not (math.isnan(bb_u) or math.isnan(bb_l)) else np.nan
                swing20_hi = float(df_ind["high"].tail(20).max())
                swing20_lo = float(df_ind["low"].tail(20).min())
                entry_5m_tr = _sf(lr.get("tick_rate"), 0.0)

                ent_raw = g1.clean_shannon_entropy(rets) if len(cs_s) > 30 else 2.0
                ent_5m_bits = compute_entropy_bits(rets, _BINS_5M, window=30) if len(cs_s) > 30 else _LOG2_10
                ent_5m_norm = min(1.0, max(0.0, ent_5m_bits / _LOG2_10))
                k_vel_1m_atr = (en_k[1] * (max(entry_5m_tr, 1.0) / 5.0)) / max(atr_v, 0.50)

                eb = float(bids[en_i])
                ea = float(asks[en_i])
                entry_ns = int(r_ts[s_i + en_i])

                feat = {
                    "kalman_pos": en_k[0],
                    "kalman_vel": en_k[1],
                    "kalman_accel": en_k[2],
                    "shannon_entropy": ent_raw,
                    "shannon_entropy_raw_norm": min(1.0, max(0.0, ent_raw / _LOG2_10)),
                    "shannon_entropy_5m_bits": ent_5m_bits,
                    "shannon_entropy_5m_norm": ent_5m_norm,
                    "kalman_vel_1m_atr": k_vel_1m_atr,
                    "kalman_vel_per_tick_atr": en_k[1] / max(atr_v / 60.0, 0.01),
                    "kaufman_er": g1.clean_kaufman_er(cs_s) if len(cs_s) > 15 else 0.3,
                    "ema20_slope": _sf(lr.get("ema20_slope"), 0.0),
                    "tick_weighted_price_dist": _sf(lr.get("tick_weighted_price_dist"), 0.0),
                    "tick_rate_ratio": _sf(lr.get("tick_rate_ratio"), 1.0),
                    "rsi": _sf(lr.get("rsi"), 50.0),
                    "utc_hour": c5_ts.hour,
                    "minute_hour": ms.minute_ts.hour,
                    "atr": atr_v,
                    "atr_pct": (atr_v / close_v * 100.0) if close_v > 0 else 0.0,
                    "atr_ratio_20": atr_ratio_20,
                    "ema20": ema20_v,
                    "ema50": ema50_v,
                    "ema20_dist_atr": ema20_dist_atr,
                    "ema_spread_pct": ema_spread_pct,
                    "adx": adx_v,
                    "adx_delta_3": adx_delta_3,
                    "dmp": _sf(lr.get("dmp"), np.nan) if "dmp" in df_ind.columns else np.nan,
                    "dmn": _sf(lr.get("dmn"), np.nan) if "dmn" in df_ind.columns else np.nan,
                    "entry_5m_tick_rate": entry_5m_tr,
                    "n_valid_ticks": int(len(valid_idxs)),
                    "regime": current_regime,
                    "raw_regime": regime_det.last_raw,
                    "regime_lagged": (regime_det.last_raw != current_regime),
                    "bars_since_commit": regime_det.bars_since_commit,
                    "bb_pos": bb_pos,
                    "bb_width": bb_w,
                    "dist_20bar_high_atr": (swing20_hi - close_v) / max(atr_v, 1e-9),
                    "dist_20bar_low_atr": (close_v - swing20_lo) / max(atr_v, 1e-9),
                    "close": close_v,
                    "open": open_v,
                    "high": high_v,
                    "low": low_v,
                }

                pkt = {
                    "idx_m": idx_m,
                    "en_i": en_i,
                    "entry_ns": entry_ns,
                    "minute_ts": ms.minute_ts,
                    "closed_5m_ts": c5_ts,
                    "entry_bid": eb,
                    "entry_ask": ea,
                    "entry_spread_usd": round(ea - eb, 4),
                    "n_valid_ticks": int(len(valid_idxs)),
                    "regime": current_regime,
                    "raw_regime": regime_det.last_raw,
                    "regime_lagged": (regime_det.last_raw != current_regime),
                    "bars_since_commit": regime_det.bars_since_commit,
                    "htf_trend": htf_trend,
                    "htf_spread_pct": htf_spread_pct,
                    "sigs": sigs,
                    "feat": feat,
                    "atr": atr_v,
                    "ema20": ema20_v,
                    "ema50": ema50_v,
                    "adx": adx_v,
                    "dmp": feat["dmp"],
                    "dmn": feat["dmn"],
                    "ema20_dist_atr": ema20_dist_atr,
                    "bb_upper": bb_u,
                    "bb_mid": bb_m,
                    "bb_lower": bb_l,
                    "bb_pos": bb_pos,
                    "swing20_hi": swing20_hi,
                    "swing20_lo": swing20_lo,
                    "entry_5m_tick_rate": entry_5m_tr,
                    "entry_5m_is_feed_gap": bool(agg_5m.bars[-1].get("is_feed_gap", False)),
                }
                candidate_minutes[idx_m] = pkt
                if htf_trend != 0:
                    signal_minutes[idx_m] = pkt

    return G2SpanCache(
        r_start_off=r_start_off,
        k_vel_all=k_vel_all,
        candidate_minutes=candidate_minutes,
        signal_minutes=signal_minutes,
        bars_processed=bars_processed,
        inverted_skipped=inverted_skipped,
        zero_spread_skipped=zero_spread_skipped,
    )


def score_candidate(
    direction: str,
    regime: str,
    feat: dict[str, Any],
    mode: str = "legacy",
    params: Optional[dict[str, Any]] = None,
) -> tuple[float, dict[str, float]]:
    """
    Independent scorer library for G2 empirical audit & controlled experiments.
    All band ranges are anchored strictly to DEV empirical quantiles (`p5/p50/p75/p95`).

    Modes:
      - legacy: exact reproduction of `TradeScorer.compute_quant_score`
      - g2_a_entropy_fix: fixes 5m bin scaling & [0, log2(10)] domain so low disorder scores high
      - g2_b_slope_5m_fix: replaces 1m monotonic ema20_slope with DEV-anchored 5m bell + exhaustion decay
      - g2_c_kalman_norm_fix: replaces $/tick & sideways-halved kalman_vel with uniform ATR & tick-rate normalized 1m-equiv velocity
      - g2_d_exhaustion_replace_twp: replaces fixed-fraction TWP chasing with ATR-normalized EMA20 extension health
      - g2_d2_twp_breakout_fix: isolated fix replacing inverted sub_twp_dist with DEV-anchored breakout separation
      - g2_h_minimal_defect_only: surgical defect fixes only (A + C + D2) with legacy weights, accel & trr_mult
      - g2_e_calibrated_core: A + B + C + D2 + tighter ER, zero tick-accel noise, + EMA20 overextension dampener (`ext_mult`), legacy weights
      - g2_e_decorrelated: G2_E with balanced decorrelated weights (0.20, 0.15, 0.20, 0.15, 0.25, 0.05)
      - g2_f_regime_conditioned: G2_E_decorrelated with mild 0.90 penalty on SIDEWAYS or lagged regime transitions
    """
    p = params or {}
    if mode == "legacy":
        return g1.clean_score_detailed(direction, regime, feat)

    d_sign = 1.0 if direction == "BUY" else -1.0
    kv = float(feat["kalman_vel"])
    kv_1m_atr = float(feat["kalman_vel_1m_atr"])
    ka = float(feat["kalman_accel"])
    ent_raw = float(feat["shannon_entropy"])
    ent_5m_norm = float(feat["shannon_entropy_5m_norm"])
    er = float(feat["kaufman_er"])
    slp = float(feat["ema20_slope"])
    twd = float(feat["tick_weighted_price_dist"])
    ext_atr = float(feat["ema20_dist_atr"])
    trr = float(feat["tick_rate_ratio"])
    h = int(feat["utc_hour"])
    sw = "SIDEWAYS" in regime.upper()
    reg_lagged = bool(feat.get("regime_lagged", False))

    # Baseline subcomponents (mutated only by targeted hypotheses)
    if direction == "BUY":
        c_mom = _score_band(kv, 0.008, 0.040, 0.002, 0.080) if sw else _score_band(kv, 0.015, 0.060, 0.005, 0.120)
        c_slp = _interp(slp, 0.00005, 0.00030)
        c_twd = _score_band(twd, -0.0005, 0.003, -0.003, 0.006)
    else:
        c_mom = _score_band(kv, -0.040, -0.008, -0.080, -0.002) if sw else _score_band(kv, -0.060, -0.015, -0.120, -0.005)
        c_slp = _interp(slp, -0.00005, -0.00030)
        c_twd = _score_band(twd, -0.003, 0.0005, -0.006, 0.003)

    c_er = _score_band(er, 0.20, 0.60, 0.08, 0.90)
    c_ent = _score_band(ent_raw, 2.50, 3.20, None, 3.60)
    c_ses = _sess_score(h)

    core_family = ("g2_e_calibrated_core", "g2_e_decorrelated", "g2_f_regime_conditioned")
    use_a = mode in ("g2_a_entropy_fix", "g2_h_minimal_defect_only", *core_family)
    use_b = mode in ("g2_b_slope_5m_fix", *core_family)
    use_c = mode in ("g2_c_kalman_norm_fix", "g2_h_minimal_defect_only", *core_family)
    use_d = mode == "g2_d_exhaustion_replace_twp"
    use_d2 = mode in ("g2_d2_twp_breakout_fix", "g2_h_minimal_defect_only", *core_family)
    use_er_tight = mode in core_family
    use_ext_damp = mode in core_family

    if use_a:
        cleanliness = 1.0 - ent_5m_norm
        c_ent = _score_band(cleanliness, 0.18, 1.0, 0.06, None)
    if use_b:
        c_slp = _score_band(d_sign * slp, 0.00025, 0.00080, 0.00010, 0.00150)
    if use_c:
        v_dir_atr = d_sign * kv_1m_atr
        v_lo = float(p.get("kal_good_lo", 0.12))
        v_hi = float(p.get("kal_good_hi", 0.48))
        v_b0 = float(p.get("kal_bad_lo", -0.10))
        v_b1 = float(p.get("kal_bad_hi", 1.00))
        c_mom = _score_band(v_dir_atr, v_lo, v_hi, v_b0, v_b1)
    if use_d:
        d_ext = d_sign * ext_atr
        ext_hi = float(p.get("ext_good_hi", 1.80))
        ext_bad_hi = float(p.get("ext_bad_hi", 2.80))
        c_twd = _score_band(d_ext, 0.40, ext_hi, -0.30, ext_bad_hi)
    if use_d2:
        c_twd = _score_band(d_sign * twd, 0.0050, 0.0150, 0.0015, None)
    if use_er_tight:
        c_er = _score_band(er, 0.20, 0.55, 0.08, 0.85)

    if mode in ("g2_e_decorrelated", "g2_f_regime_conditioned"):
        w = (0.20, 0.15, 0.20, 0.15, 0.25, 0.05)
    elif "weights" in p:
        w = tuple(float(x) for x in p["weights"])
    else:
        w = (0.35, 0.20, 0.15, 0.15, 0.10, 0.05)

    base_s = w[0] * c_mom + w[1] * c_er + w[2] * c_ent + w[3] * c_slp + w[4] * c_twd + w[5] * c_ses
    qs = min(1.0, max(0.0, base_s))
    accel_adj = 0.0 if mode in core_family else max(-0.08, min(0.08, d_sign * ka * 2.0))
    qs = min(1.0, max(0.0, qs + accel_adj))

    trr_mult = 1.0
    if trr < 0.70:
        trr_mult = 0.75
    elif sw and trr < 1.10:
        trr_mult = 0.85

    ext_mult = 1.0
    if use_ext_damp:
        ext_mult = 0.55 + 0.45 * _score_band(d_sign * ext_atr, -1.0, 1.80, None, 3.00)

    reg_mult = 1.0
    if mode == "g2_f_regime_conditioned" and (sw or reg_lagged):
        reg_mult = float(p.get("regime_mult", 0.90))

    qs = round(min(1.0, max(0.0, qs * trr_mult * ext_mult * reg_mult)), 4)

    comp = {
        "sub_mom_kalman": round(c_mom, 4),
        "sub_er": round(c_er, 4),
        "sub_entropy": round(c_ent, 4),
        "sub_ema_slope": round(c_slp, 4),
        "sub_twp_or_ext": round(c_twd, 4),
        "sub_session": round(c_ses, 4),
        "base_score_pre_accel": round(base_s, 4),
        "accel_adj": round(accel_adj, 4),
        "trr_mult": round(trr_mult, 4),
        "ext_mult": round(ext_mult, 4),
        "reg_mult": round(reg_mult, 4),
    }
    return qs, comp


def run_g2_backtest(
    span: g1.PreparedSpan,
    cache: G2SpanCache,
    cfg: g1.CleanSimConfig,
    scoring_mode: str = "legacy",
    scoring_params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Fast causal tick-level simulation using precomputed G2SpanCache + arbitrary scorer mode.
    Matches `g1.run_clean_backtest(..., cache=...)` identically when `scoring_mode == "legacy"`.
    """
    r_ts = span.r_ts_ns
    r_bids = span.r_bids
    r_asks = span.r_asks

    open_pos: Optional[g1.CleanPos] = None
    closed_trades: list[dict[str, Any]] = []
    equity = float(cfg.initial_capital)
    day_start_eq = float(cfg.initial_capital)
    mtm = g1.CleanMtmTracker(cfg.initial_capital, g1.dt_to_ns(span.start_dt))
    ks = g1.CleanHwmKillSwitch(cfg.max_hwm_dd_pct, cfg.initial_capital) if cfg.max_hwm_dd_pct is not None else None

    daily_pnl = 0.0
    daily_trades = 0
    consec_losses = 0
    current_day: Optional[date] = None
    last_cd_bar: dict[tuple[str, str], int] = {}
    bar_idx = -1
    signals_seen = 0
    signals_passed = 0
    last_valid: Optional[tuple[float, float, int]] = None
    slip_off = g1._slip_offset(cfg)
    trade_counter = 0
    slice_start_ns = g1.dt_to_ns(cfg.slice_start_dt) if cfg.slice_start_dt is not None else None
    slice_end_ns = g1.dt_to_ns(cfg.slice_end_dt) if cfg.slice_end_dt is not None else None

    def _close_pos(pos: g1.CleanPos, reason: str, exit_px: float, exit_ns: int, exit_bid: float, exit_ask: float, exit_min_ticks: int) -> None:
        nonlocal equity, daily_pnl, consec_losses
        pos.apply_rollovers(exit_ns, cfg)
        c_ex = g1._side_comm(pos.lot, exit_px, cfg)
        pos.comm_exit = c_ex
        pos.comm_total = round(pos.entry_comm + c_ex, 6)
        gross = pos.gross_at(exit_px)
        net = gross - pos.comm_total + pos.swap_pnl
        eq_before = equity
        equity += net
        daily_pnl += net
        consec_losses = 0 if net > 0 else consec_losses + 1
        mtm.update(equity, exit_ns)
        if ks is not None:
            ks.evaluate(equity, exit_ns)

        realized_loss_pct = (net / eq_before * 100.0) if (eq_before > 0 and net < 0) else 0.0
        is_bull = pos.direction == "BUY"
        mae_price_diff = (pos.entry_price - pos.worst_price) if is_bull else (pos.worst_price - pos.entry_price)
        mfe_price_diff = (pos.peak_price - pos.entry_price) if is_bull else (pos.entry_price - pos.peak_price)
        mae_atr = max(0.0, mae_price_diff) / max(pos.atr, 1e-9)
        mfe_atr = max(0.0, mfe_price_diff) / max(pos.atr, 1e-9)
        r_mult = (net / pos.planned_risk_usd) if pos.planned_risk_usd > 0 else 0.0

        closed_trades.append({
            "trade_id": pos.trade_id,
            "entry_time": g1.ns_to_dt(pos.entry_ns),
            "exit_time": g1.ns_to_dt(exit_ns),
            "entry_ns": pos.entry_ns,
            "exit_ns": exit_ns,
            "hold_secs": (exit_ns - pos.entry_ns) / float(g1._NS_PER_SEC),
            "hold_bars": pos.bars_held,
            "direction": pos.direction,
            "strategy": pos.strategy,
            "regime": pos.regime,
            "score": pos.score,
            "entry_price": pos.entry_price,
            "exit_price": exit_px,
            "sl": pos.initial_sl,
            "final_sl": pos.sl,
            "tp": pos.tp,
            "atr": pos.atr,
            "qty": pos.lot,
            "gross_pnl": gross,
            "pnl": net,
            "commission_usd": pos.comm_total,
            "slippage_usd": pos.slip_usd,
            "swap_usd": pos.swap_cost,
            "planned_risk_usd": pos.planned_risk_usd,
            "planned_risk_pct": pos.planned_risk_pct,
            "realized_loss_pct": realized_loss_pct,
            "exit_reason": reason,
            "mfe_atr": round(mfe_atr, 4),
            "mae_atr": round(mae_atr, 4),
            "r_multiple": round(r_mult, 4),
            "entry_ctx": pos.entry_ctx,
        })

    for idx_m, ms in enumerate(span.replay_slices):
        m_py = ms.minute_ts
        m_ns = int(ms.minute_epoch_s) * g1._NS_PER_SEC
        if slice_end_ns is not None and m_ns > slice_end_ns and open_pos is None:
            break
        if slice_start_ns is not None and m_ns < slice_start_ns:
            continue

        bar_idx += 1
        m_day = m_py.date()
        if current_day != m_day:
            current_day = m_day
            daily_pnl = 0.0
            daily_trades = 0
            consec_losses = 0
            day_start_eq = max(equity, 1.0)

        if open_pos is None and idx_m not in cache.signal_minutes:
            continue

        s_i, e_i = ms.s, ms.e
        bids = r_bids[s_i:e_i]
        asks = r_asks[s_i:e_i]
        ts_arr = r_ts[s_i:e_i]
        valid_mask = (bids > 0.0) & (asks > 0.0) & (bids < asks)
        valid_idxs = np.flatnonzero(valid_mask)
        if len(valid_idxs) > 0:
            li = int(valid_idxs[-1])
            last_valid = (float(bids[li]), float(asks[li]), int(ts_arr[li]))

        def _scan_exit(pos: g1.CleanPos, start_i: int, eval_to: bool, cur_min_ticks: int) -> tuple[bool, str, float, int, float, float]:
            if start_i >= len(bids):
                return False, "", 0.0, len(bids) - 1, 0.0, 0.0
            is_bull = pos.direction == "BUY"
            eh = g1.ns_to_dt(pos.entry_ns).hour
            eff_mh = min(cfg.max_hold_bars, 60 if (7 <= eh < 20) else 90)
            if cfg.kalman_rate_norm:
                eff_rate = max(float(pos.prev_minute_valid_ticks), 60.0)
                apt = max(pos.atr / eff_rate, 0.01)
            else:
                apt = max(pos.atr / 60.0, 0.01)
            last_q: Optional[float] = None
            last_v_idx: Optional[int] = None
            last_vb, last_va = pos.entry_bid, pos.entry_ask
            best_q: Optional[float] = None
            last_best_ns = pos.entry_ns

            for k in range(start_i, len(bids)):
                b = float(bids[k])
                a = float(asks[k])
                if b <= 0.0 or a <= 0.0 or b >= a:
                    continue
                t_ns = int(ts_arr[k])
                kv = float(cache.k_vel_all[(s_i + k) - cache.r_start_off])
                pos.apply_rollovers(t_ns, cfg)
                exec_q = b if is_bull else a
                last_q = exec_q
                last_v_idx = k
                last_vb, last_va = b, a
                last_best_ns = t_ns

                if is_bull and exec_q < pos.worst_price:
                    pos.worst_price = exec_q
                    pos.worst_ns = t_ns
                elif (not is_bull) and exec_q > pos.worst_price:
                    pos.worst_price = exec_q
                    pos.worst_ns = t_ns

                if best_q is None or (is_bull and exec_q > best_q) or (not is_bull and exec_q < best_q):
                    best_q = exec_q
                vn = kv / apt

                if is_bull and b <= pos.sl:
                    rsn = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
                    theo = float(pos.sl)
                    fpx = b - slip_off
                    if slip_off > 0.0:
                        pos.slip_usd += slip_off * pos.lot * g1._CONTRACT_SIZE
                    gap_usd = pos.net_at(fpx, cfg) - pos.net_at(theo, cfg)
                    pos.theo_sl = theo
                    pos.last_sl_gap = gap_usd
                    mtm.update(equity + pos.net_at(fpx, cfg), t_ns, gap_usd=gap_usd)
                    return True, rsn, fpx, k, b, a
                if (not is_bull) and a >= pos.sl:
                    rsn = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
                    theo = float(pos.sl)
                    fpx = a + slip_off
                    if slip_off > 0.0:
                        pos.slip_usd += slip_off * pos.lot * g1._CONTRACT_SIZE
                    gap_usd = pos.net_at(fpx, cfg) - pos.net_at(theo, cfg)
                    pos.theo_sl = theo
                    pos.last_sl_gap = gap_usd
                    mtm.update(equity + pos.net_at(fpx, cfg), t_ns, gap_usd=gap_usd)
                    return True, rsn, fpx, k, b, a

                if is_bull and b >= pos.tp:
                    fpx = float(pos.tp)
                    mtm.update(equity + pos.net_at(fpx, cfg), t_ns)
                    return True, "TARGET", fpx, k, b, a
                if (not is_bull) and a <= pos.tp:
                    fpx = float(pos.tp)
                    mtm.update(equity + pos.net_at(fpx, cfg), t_ns)
                    return True, "TARGET", fpx, k, b, a

                if cfg.use_kalman_exit and pos.bars_held >= cfg.kalman_min_hold_bars:
                    if is_bull and vn <= -cfg.kalman_vel_thresh:
                        fpx = exec_q - slip_off
                        if slip_off > 0.0:
                            pos.slip_usd += slip_off * pos.lot * g1._CONTRACT_SIZE
                        mtm.update(equity + pos.net_at(fpx, cfg), t_ns)
                        return True, "KALMAN_VELOCITY_REVERSAL", fpx, k, b, a
                    elif (not is_bull) and vn >= cfg.kalman_vel_thresh:
                        fpx = exec_q + slip_off
                        if slip_off > 0.0:
                            pos.slip_usd += slip_off * pos.lot * g1._CONTRACT_SIZE
                        mtm.update(equity + pos.net_at(fpx, cfg), t_ns)
                        return True, "KALMAN_VELOCITY_REVERSAL", fpx, k, b, a
                    gain = (b - pos.entry_price) if is_bull else (pos.entry_price - a)
                    if gain >= pos.atr * 1.5 and ((is_bull and vn < -0.5) or ((not is_bull) and vn > 0.5)):
                        tsl = (b - pos.atr * 0.75) if is_bull else (a + pos.atr * 0.75)
                        if (is_bull and tsl > pos.sl) or ((not is_bull) and tsl < pos.sl):
                            pos.sl = tsl
                            pos.trailing_active = True
                            pos.exit_reason_flag = "KALMAN_TIGHTEN"

                raw_unr = pos.net_at(exec_q, cfg)
                mtm.update(equity + raw_unr, t_ns)
                if ks is not None and ks.evaluate(equity + raw_unr, t_ns):
                    fpx = (exec_q - slip_off) if is_bull else (exec_q + slip_off)
                    if slip_off > 0.0:
                        pos.slip_usd += slip_off * pos.lot * g1._CONTRACT_SIZE
                    mtm.update(equity + pos.net_at(fpx, cfg), t_ns)
                    return True, "HWM_KILL_SWITCH_8PCT", fpx, k, b, a
                pos.ratchet(exec_q, t_ns, eff_mh)

            pos.prev_minute_valid_ticks = cur_min_ticks

            if eval_to:
                ret_idx = last_v_idx if last_v_idx is not None else len(bids) - 1
                if cfg.early_fail_mins is not None and cfg.early_fail_min_mfe_atr is not None and pos.bars_held == cfg.early_fail_mins:
                    gain_abs = (pos.peak_price - pos.entry_price) if is_bull else (pos.entry_price - pos.peak_price)
                    if (gain_abs / max(pos.atr, 1e-9)) < cfg.early_fail_min_mfe_atr:
                        base_px = last_q if last_q is not None else pos.entry_price
                        ex_px = (base_px - slip_off) if is_bull else (base_px + slip_off)
                        if slip_off > 0.0 and last_q is not None:
                            pos.slip_usd += slip_off * pos.lot * g1._CONTRACT_SIZE
                        return True, f"EARLY_FAIL_{cfg.early_fail_mins}M", ex_px, ret_idx, last_vb, last_va
                if pos.bars_held >= eff_mh:
                    base_px = last_q if last_q is not None else pos.entry_price
                    ex_px = (base_px - slip_off) if is_bull else (base_px + slip_off)
                    if slip_off > 0.0 and last_q is not None:
                        pos.slip_usd += slip_off * pos.lot * g1._CONTRACT_SIZE
                    return True, "TIMEOUT", ex_px, ret_idx, last_vb, last_va
            if best_q is not None:
                pos.ratchet(best_q, last_best_ns, eff_mh)
            return False, "", 0.0, len(bids) - 1, last_vb, last_va

        if open_pos is not None:
            open_pos.bars_held += 1
            exited, rsn, epx, lidx, ex_b, ex_a = _scan_exit(open_pos, 0, True, len(valid_idxs))
            if exited:
                _close_pos(open_pos, rsn, epx, int(ts_arr[lidx]), ex_b, ex_a, len(valid_idxs))
                open_pos = None
            continue

        if slice_end_ns is not None and m_ns > slice_end_ns:
            continue

        if idx_m not in cache.signal_minutes:
            continue

        meta = cache.signal_minutes[idx_m]
        if cfg.session_close_utc is not None and m_py.hour >= cfg.session_close_utc:
            continue
        daily_loss_pct = abs(min(0.0, daily_pnl)) / max(day_start_eq, 1.0) * 100.0
        if daily_loss_pct >= cfg.max_daily_loss_pct or daily_trades >= cfg.max_trades_per_day or consec_losses >= cfg.max_consecutive_losses:
            continue

        en_i = meta["en_i"]
        current_regime = meta["regime"]
        sigs = meta["sigs"]
        feat = meta["feat"]
        atr_v = meta["atr"]
        signals_seen += len(sigs)

        scored: list[tuple[float, str, str, dict[str, float]]] = []
        for st_name, dir_val in sigs:
            sc_val, comp = score_candidate(dir_val, current_regime, feat, mode=scoring_mode, params=scoring_params)
            if sc_val >= cfg.min_score:
                scored.append((sc_val, st_name, dir_val, comp))
        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            continue

        sc, st, d, comp = scored[0]
        htf_trend = meta["htf_trend"]
        if (d == "BUY" and htf_trend != 1) or (d == "SELL" and htf_trend != -1):
            continue
        if (cfg.block_high_vol and current_regime == "HIGH_VOLATILITY") or (cfg.block_buy_sideways and d == "BUY" and current_regime == "SIDEWAYS") or (cfg.block_sell_sideways and d == "SELL" and current_regime == "SIDEWAYS"):
            continue
        if cfg.max_score is not None and sc >= cfg.max_score:
            continue
        if cfg.adx_min is not None:
            adx_v = float(feat.get("adx", np.nan))
            if math.isnan(adx_v) or adx_v < cfg.adx_min:
                continue
        if cfg.ema20_max_dist_atr is not None:
            ext_v = float(feat.get("ema20_dist_atr", 0.0))
            signed_dist = ext_v if d == "BUY" else -ext_v
            if signed_dist > cfg.ema20_max_dist_atr:
                continue
        if bar_idx - last_cd_bar.get((st, d), -9999) < cfg.cooldown_bars:
            continue
        if ks is not None and ks.halted:
            ks.trades_prevented += 1
            continue

        eb, ea = float(bids[en_i]), float(asks[en_i])
        raw_ep = ea if d == "BUY" else eb
        ep = (raw_ep + slip_off) if d == "BUY" else (raw_ep - slip_off)
        lot, sl_px, tp_px, _ = g1._size_position(equity, ep, atr_v, d, cfg)
        if lot is None:
            continue

        e_comm = g1._side_comm(lot, ep, cfg)
        est_rt_comm = 2.0 * e_comm
        e_slip = slip_off * lot * g1._CONTRACT_SIZE
        planned_risk_usd = abs(ep - sl_px) * lot * g1._CONTRACT_SIZE + est_rt_comm
        planned_risk_pct = (planned_risk_usd / equity * 100.0) if equity > 0 else 0.0

        trade_counter += 1
        entry_ns = int(ts_arr[en_i])
        open_pos = g1.CleanPos(
            trade_id=trade_counter,
            direction=d,
            strategy=st,
            regime=current_regime,
            score=sc,
            entry_ns=entry_ns,
            entry_bid=eb,
            entry_ask=ea,
            entry_price=ep,
            initial_sl=sl_px,
            sl=sl_px,
            tp=tp_px,
            atr=atr_v,
            lot=lot,
            planned_risk_usd=planned_risk_usd,
            planned_risk_pct=planned_risk_pct,
            entry_comm=e_comm,
            slip_usd=e_slip,
            peak_price=ep,
            peak_ns=entry_ns,
            worst_price=ep,
            worst_ns=entry_ns,
            last_swap_ns=entry_ns,
            entry_ctx={**feat, **comp},
            prev_minute_valid_ticks=len(valid_idxs),
        )
        init_unr = -(ea - eb) * lot * g1._CONTRACT_SIZE - est_rt_comm - e_slip
        mtm.update(equity + init_unr, entry_ns)
        last_cd_bar[(st, d)] = bar_idx
        daily_trades += 1
        signals_passed += 1

        if en_i + 1 < len(bids):
            exited, rsn, epx, lidx, ex_b, ex_a = _scan_exit(open_pos, en_i + 1, False, len(valid_idxs))
            if exited:
                _close_pos(open_pos, rsn, epx, int(ts_arr[lidx]), ex_b, ex_a, len(valid_idxs))
                open_pos = None

    if open_pos is not None and last_valid is not None:
        lb, la, lns = last_valid
        rc = lb if open_pos.direction == "BUY" else la
        epx = (rc - slip_off) if open_pos.direction == "BUY" else (rc + slip_off)
        if slip_off > 0.0:
            open_pos.slip_usd += slip_off * open_pos.lot * g1._CONTRACT_SIZE
        _close_pos(open_pos, "END_OF_BACKTEST", epx, lns, lb, la, open_pos.prev_minute_valid_ticks)

    pnls = [float(t["pnl"]) for t in closed_trades]
    n = len(closed_trades)
    wins = sum(1 for p in pnls if round(p, 2) > 0.0)
    losses = sum(1 for p in pnls if round(p, 2) < 0.0)
    total_pnl = sum(pnls)
    gross_win = sum(p for p in pnls if round(p, 2) > 0.0)
    gross_loss = sum(p for p in pnls if round(p, 2) < 0.0)
    pf = abs(gross_win / gross_loss) if gross_loss != 0.0 else (999.0 if gross_win > 0 else 0.0)

    ceq = cfg.initial_capital
    chwm = cfg.initial_capital
    closed_dd = 0.0
    closed_dd_pct = 0.0
    for p in pnls:
        ceq += p
        if ceq > chwm:
            chwm = ceq
        dd = ceq - chwm
        ddp = ((chwm - ceq) / chwm * 100.0) if chwm > 0 else 0.0
        if dd < closed_dd:
            closed_dd = dd
        if ddp > closed_dd_pct:
            closed_dd_pct = ddp

    hwm_summary = None
    if ks is not None:
        hwm_summary = {
            "halted": ks.halted,
            "trigger_time": g1.ns_to_dt(ks.trigger_ns).isoformat() if ks.trigger_ns else None,
            "trades_prevented": ks.trades_prevented,
            "max_seen_dd_pct": ks.max_seen_dd_pct,
        }

    return {
        "trades": n,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": (wins / n * 100.0) if n > 0 else 0.0,
        "pnl": total_pnl,
        "gross_pnl": sum(float(t["gross_pnl"]) for t in closed_trades),
        "commission_usd": sum(float(t["commission_usd"]) for t in closed_trades),
        "slippage_usd": sum(float(t["slippage_usd"]) for t in closed_trades),
        "swap_usd": sum(float(t["swap_usd"]) for t in closed_trades),
        "final_equity": equity,
        "pf": pf,
        "expectancy": (total_pnl / n) if n > 0 else 0.0,
        "expectancy_r": (sum(float(t["r_multiple"]) for t in closed_trades) / n) if n > 0 else 0.0,
        "closed_max_dd": closed_dd,
        "closed_max_dd_pct": closed_dd_pct,
        "mtm_max_dd": mtm.max_dd,
        "mtm_max_dd_pct": mtm.max_dd_pct,
        "hwm_kill_switch": hwm_summary,
        "trade_list": closed_trades,
    }


def compute_diagnostic_outcome(
    span: g1.PreparedSpan,
    cache: G2SpanCache,
    idx_m: int,
    en_i: int,
    direction: str,
    atr_v: float,
    cfg_template: g1.CleanSimConfig,
) -> dict[str, Any]:
    """
    Research-only isolated forward labeler starting STRICTLY after candidate tick (never visible to any scorer).
    Evaluates causal bid/ask 15m & 60m excursions and standalone Fusion-cost PnL & R-multiple
    under both Kalman-exit ON (`diag_hyp_kal_*`) and Standard-exit (`diag_hyp_std_*`).
    """
    r_ts = span.r_ts_ns
    r_bids = span.r_bids
    r_asks = span.r_asks
    ms0 = span.replay_slices[idx_m]
    eb0 = float(r_bids[ms0.s + en_i])
    ea0 = float(r_asks[ms0.s + en_i])
    entry_ns = int(r_ts[ms0.s + en_i])
    is_bull = direction == "BUY"
    slip_off = g1._slip_offset(cfg_template)
    raw_ep = ea0 if is_bull else eb0
    ep = (raw_ep + slip_off) if is_bull else (raw_ep - slip_off)

    lot, sl_px, tp_px, _ = g1._size_position(10000.0, ep, atr_v, direction, cfg_template)
    if lot is None:
        return {}
    e_comm = g1._side_comm(lot, ep, cfg_template)
    planned_risk_usd = abs(ep - sl_px) * lot * g1._CONTRACT_SIZE + 2.0 * e_comm
    eh = g1.ns_to_dt(entry_ns).hour
    eff_mh = min(cfg_template.max_hold_bars, 60) if (7 <= eh < 20) else min(cfg_template.max_hold_bars, 90)
    apt = max(atr_v / 60.0, 0.01)

    t15_ns = entry_ns + 15 * 60 * g1._NS_PER_SEC
    t60_ns = entry_ns + 60 * 60 * g1._NS_PER_SEC
    max_fav_15 = 0.0
    max_adv_15 = 0.0
    max_fav_60 = 0.0
    max_adv_60 = 0.0

    def _sim_mode(use_k: bool) -> tuple[float, float, str, int]:
        pos = g1.CleanPos(
            trade_id=0, direction=direction, strategy="diag", regime="", score=0.0,
            entry_ns=entry_ns, entry_bid=eb0, entry_ask=ea0, entry_price=ep,
            initial_sl=sl_px, sl=sl_px, tp=tp_px, atr=atr_v, lot=lot,
            planned_risk_usd=planned_risk_usd, planned_risk_pct=1.0,
            entry_comm=e_comm, slip_usd=slip_off * lot * g1._CONTRACT_SIZE,
            peak_price=ep, peak_ns=entry_ns, worst_price=ep, worst_ns=entry_ns,
            last_swap_ns=entry_ns, prev_minute_valid_ticks=0,
        )
        m_cur = idx_m
        start_k = en_i + 1
        bars_held = 0
        while m_cur < len(span.replay_slices) and m_cur <= idx_m + eff_mh + 5:
            ms = span.replay_slices[m_cur]
            s_i, e_i = ms.s, ms.e
            last_q: Optional[float] = None
            last_ns_m = entry_ns
            best_q: Optional[float] = None
            last_best_ns = entry_ns
            for k in range(start_k, e_i - s_i):
                b = float(r_bids[s_i + k])
                a = float(r_asks[s_i + k])
                if b <= 0.0 or a <= 0.0 or b >= a:
                    continue
                t_ns = int(r_ts[s_i + k])
                pos.apply_rollovers(t_ns, cfg_template)
                exec_q = b if is_bull else a
                last_q = exec_q
                last_ns_m = t_ns
                if best_q is None or (is_bull and exec_q > best_q) or (not is_bull and exec_q < best_q):
                    best_q = exec_q
                    last_best_ns = t_ns
                if is_bull and b <= pos.sl:
                    rsn = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
                    fpx = b - slip_off
                    return _finish(pos, fpx, t_ns, rsn, m_cur - idx_m)
                if (not is_bull) and a >= pos.sl:
                    rsn = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
                    fpx = a + slip_off
                    return _finish(pos, fpx, t_ns, rsn, m_cur - idx_m)
                if is_bull and b >= pos.tp:
                    return _finish(pos, float(pos.tp), t_ns, "TARGET", m_cur - idx_m)
                if (not is_bull) and a <= pos.tp:
                    return _finish(pos, float(pos.tp), t_ns, "TARGET", m_cur - idx_m)
                if use_k:
                    kv = float(cache.k_vel_all[(s_i + k) - cache.r_start_off])
                    vn = kv / apt
                    if (is_bull and vn <= -cfg_template.kalman_vel_thresh) or ((not is_bull) and vn >= cfg_template.kalman_vel_thresh):
                        fpx = (exec_q - slip_off) if is_bull else (exec_q + slip_off)
                        return _finish(pos, fpx, t_ns, "KALMAN_VELOCITY_REVERSAL", m_cur - idx_m)
                    gain = (b - pos.entry_price) if is_bull else (pos.entry_price - a)
                    if gain >= pos.atr * 1.5 and ((is_bull and vn < -0.5) or ((not is_bull) and vn > 0.5)):
                        tsl = (b - pos.atr * 0.75) if is_bull else (a + pos.atr * 0.75)
                        if (is_bull and tsl > pos.sl) or ((not is_bull) and tsl < pos.sl):
                            pos.sl = tsl
                            pos.trailing_active = True
                            pos.exit_reason_flag = "KALMAN_TIGHTEN"
                pos.ratchet(exec_q, t_ns, eff_mh)
            if m_cur > idx_m:
                if cfg_template.early_fail_mins is not None and cfg_template.early_fail_min_mfe_atr is not None and bars_held == cfg_template.early_fail_mins:
                    gain_abs = (pos.peak_price - pos.entry_price) if is_bull else (pos.entry_price - pos.peak_price)
                    if (gain_abs / max(pos.atr, 1e-9)) < cfg_template.early_fail_min_mfe_atr and last_q is not None:
                        ex_px = (last_q - slip_off) if is_bull else (last_q + slip_off)
                        return _finish(pos, ex_px, last_ns_m, f"EARLY_FAIL_{cfg_template.early_fail_mins}M", m_cur - idx_m)
                if bars_held >= eff_mh and last_q is not None:
                    ex_px = (last_q - slip_off) if is_bull else (last_q + slip_off)
                    return _finish(pos, ex_px, last_ns_m, "TIMEOUT", m_cur - idx_m)
            if best_q is not None:
                pos.ratchet(best_q, last_best_ns, eff_mh)
            m_cur += 1
            start_k = 0
            bars_held += 1
            pos.bars_held += 1
        return 0.0, 0.0, "END_OF_SPAN", m_cur - idx_m

    def _finish(pos: g1.CleanPos, exit_px: float, exit_ns: int, reason: str, bars: int) -> tuple[float, float, str, int]:
        pos.apply_rollovers(exit_ns, cfg_template)
        c_ex = g1._side_comm(pos.lot, exit_px, cfg_template)
        net = pos.gross_at(exit_px) - (pos.entry_comm + c_ex) + pos.swap_pnl
        r_m = (net / planned_risk_usd) if planned_risk_usd > 0 else 0.0
        return round(net, 4), round(r_m, 4), reason, bars

    # Pure fixed-horizon 15m & 60m causal MFE/MAE from bid/ask ticks
    m_cur = idx_m
    start_k = en_i + 1
    while m_cur < len(span.replay_slices):
        ms = span.replay_slices[m_cur]
        s_i, e_i = ms.s, ms.e
        for k in range(start_k, e_i - s_i):
            b = float(r_bids[s_i + k])
            a = float(r_asks[s_i + k])
            if b <= 0.0 or a <= 0.0 or b >= a:
                continue
            t_ns = int(r_ts[s_i + k])
            if t_ns > t60_ns:
                break
            q = b if is_bull else a
            fav = ((q - ep) if is_bull else (ep - q)) / max(atr_v, 1e-9)
            adv = ((ep - q) if is_bull else (q - ep)) / max(atr_v, 1e-9)
            if fav > max_fav_60:
                max_fav_60 = fav
            if adv > max_adv_60:
                max_adv_60 = adv
            if t_ns <= t15_ns:
                if fav > max_fav_15:
                    max_fav_15 = fav
                if adv > max_adv_15:
                    max_adv_15 = adv
        if ms.minute_epoch_s * g1._NS_PER_SEC > t60_ns + 60 * g1._NS_PER_SEC:
            break
        m_cur += 1
        start_k = 0

    kpnl, kr, krsn, kbars = _sim_mode(True)
    spnl, sr, srsn, sbars = _sim_mode(False)

    return {
        "diag_mfe_15m_atr": round(max_fav_15, 4),
        "diag_mae_15m_atr": round(max_adv_15, 4),
        "diag_mfe_60m_atr": round(max_fav_60, 4),
        "diag_mae_60m_atr": round(max_adv_60, 4),
        "diag_hit_1atr_60m": bool(max_fav_60 >= 1.0),
        "diag_hyp_kal_pnl": kpnl,
        "diag_hyp_kal_r": kr,
        "diag_hyp_kal_reason": krsn,
        "diag_hyp_kal_bars": int(kbars),
        "diag_hyp_kal_win": bool(round(kpnl, 2) > 0.0),
        "diag_hyp_kal_sl": bool(krsn == "SL"),
        "diag_hyp_std_pnl": spnl,
        "diag_hyp_std_r": sr,
        "diag_hyp_std_reason": srsn,
        "diag_hyp_std_bars": int(sbars),
    }
