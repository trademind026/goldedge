"""
TASK G3.5 — Secondary ML Adapter: Isolated Probability Calibration,
H3b Candidate Utility & Delayed-Execution Path Simulation.

Strict Hexagonal Architecture (GEMINI.md):
  - All sklearn/scipy/numpy/pandas calibration & simulation logic lives here in
    `src/goldedge/adapters/secondary/ml/`.
  - `src/goldedge/domain/` remains pure Python and untouched.
  - `RunBacktestUseCase` and frozen H0/H3b execution paths remain completely untouched.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import joblib
import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from src.goldedge.config.risk_profiles import get_profile
from src.goldedge.domain.models.signal import SignalDirection
from src.goldedge.domain.services.sizing_service import PositionSizingService

# Verified G3.2 Fusion Markets execution cost parameters
FUSION_COMM_RATE_PER_SIDE = 12.0 / 1_000_000.0  # $12 per $1M notional per side
FUSION_SLIPPAGE_USD_PER_SIDE = 0.00533          # 0.0533 pips = $0.00533/oz per side
CONTRACT_SIZE_OZ = 100.0                        # 1.00 lot = 100 oz XAUUSD


def _clip_renorm(probs: np.ndarray, eps: float = 1e-7) -> np.ndarray:
    p = np.asarray(probs, dtype=np.float64)
    p = np.clip(p, eps, 1.0 - eps)
    return p / np.sum(p, axis=1, keepdims=True)


def _safe_logits(probs: np.ndarray, eps: float = 1e-7) -> np.ndarray:
    p = _clip_renorm(probs, eps=eps)
    log_p = np.log(p)
    return log_p - np.mean(log_p, axis=1, keepdims=True)


def _softmax(logits: np.ndarray) -> np.ndarray:
    z = logits - np.max(logits, axis=1, keepdims=True)
    e = np.exp(z)
    return e / np.sum(e, axis=1, keepdims=True)


def per_sample_multiclass_logloss(y_true: np.ndarray, probs: np.ndarray, classes: Sequence[str]) -> np.ndarray:
    c2i = {str(c): i for i, c in enumerate(classes)}
    idx = np.array([c2i[str(v)] for v in y_true], dtype=np.int64)
    p = _clip_renorm(probs, eps=1e-15)
    return -np.log(p[np.arange(len(y_true)), idx])


def per_sample_binary_logloss(y_bin: np.ndarray, p_pos: np.ndarray) -> np.ndarray:
    y = np.asarray(y_bin, dtype=np.float64)
    p = np.clip(np.asarray(p_pos, dtype=np.float64), 1e-15, 1.0 - 1e-15)
    return -(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))


def day_block_mean_ci(values: np.ndarray, day_ids: np.ndarray) -> Dict[str, Any]:
    """Dependence-adjusted day-block mean & 95% CI, plus outlier-day removal check."""
    v = np.asarray(values, dtype=np.float64)
    uniq, inv = np.unique(day_ids, return_inverse=True)
    g = len(uniq)
    if g < 3:
        return {
            "n_days": int(g),
            "mean": round(float(np.mean(v)), 6) if len(v) else None,
            "se_day_block": None,
            "ci95_low": None,
            "ci95_high": None,
            "mean_ex_best_day": None,
            "mean_ex_worst_day": None,
        }
    cnt = np.bincount(inv).astype(np.float64)
    d_means = np.bincount(inv, weights=v) / np.maximum(1.0, cnt)
    mu = float(np.mean(d_means))
    se = float(np.std(d_means, ddof=1) / math.sqrt(g))
    best_i = int(np.argmax(d_means))
    worst_i = int(np.argmin(d_means))
    return {
        "n_days": int(g),
        "mean": round(mu, 6),
        "se_day_block": round(se, 6),
        "ci95_low": round(mu - 1.96 * se, 6),
        "ci95_high": round(mu + 1.96 * se, 6),
        "mean_ex_best_day": round(float(np.mean(np.delete(d_means, best_i))), 6),
        "mean_ex_worst_day": round(float(np.mean(np.delete(d_means, worst_i))), 6),
    }


def day_block_paired_diff_ci(
    a_vals: np.ndarray,
    b_vals: np.ndarray,
    day_ids: np.ndarray,
    lower_is_better: bool = True,
) -> Dict[str, Any]:
    """Paired day-block 95% CI on (a_vals - b_vals)."""
    diff = np.asarray(a_vals, dtype=np.float64) - np.asarray(b_vals, dtype=np.float64)
    uniq, inv = np.unique(day_ids, return_inverse=True)
    g = len(uniq)
    if g < 3:
        return {"n_days": int(g), "mean_diff": round(float(np.mean(diff)), 6), "significant_95pct": False}
    cnt = np.bincount(inv).astype(np.float64)
    d_means = np.bincount(inv, weights=diff) / np.maximum(1.0, cnt)
    mu = float(np.mean(d_means))
    se = float(np.std(d_means, ddof=1) / math.sqrt(g))
    lo, hi = mu - 1.96 * se, mu + 1.96 * se
    best_idx = int(np.argmin(d_means) if lower_is_better else np.argmax(d_means))
    mu_ex_best = float(np.mean(np.delete(d_means, best_idx)))
    sig = bool(hi < 0.0) if lower_is_better else bool(lo > 0.0)
    fragile = bool((mu < 0.0 and mu_ex_best >= 0.0) if lower_is_better else (mu > 0.0 and mu_ex_best <= 0.0))
    return {
        "n_days": int(g),
        "mean_diff_per_day": round(mu, 6),
        "se_day_block": round(se, 6),
        "ci95_low": round(lo, 6),
        "ci95_high": round(hi, 6),
        "significant_95pct": sig,
        "mean_diff_excluding_best_day": round(mu_ex_best, 6),
        "single_day_fragile": fragile,
    }


def paired_day_sign_permutation_p(
    a_vals: np.ndarray,
    b_vals: np.ndarray,
    day_ids: np.ndarray,
    n_perm: int = 2000,
    seed: int = 42,
) -> float:
    """Two-sided day-clustered sign-flip permutation p-value on (a_vals - b_vals)."""
    diff = np.asarray(a_vals, dtype=np.float64) - np.asarray(b_vals, dtype=np.float64)
    uniq, inv = np.unique(day_ids, return_inverse=True)
    g = len(uniq)
    if g < 3:
        return 1.0
    cnt = np.bincount(inv).astype(np.float64)
    d_means = np.bincount(inv, weights=diff) / np.maximum(1.0, cnt)
    obs = abs(float(np.mean(d_means)))
    rng = np.random.default_rng(seed)
    signs = rng.choice(np.array([-1.0, 1.0]), size=(n_perm, g))
    perm_means = np.abs(signs @ d_means) / g
    return round(float((np.sum(perm_means >= obs) + 1) / (n_perm + 1)), 5)


def benjamini_hochberg_fdr(p_values: Sequence[float]) -> List[float]:
    """Benjamini-Hochberg adjusted q-values preserving input order."""
    m = len(p_values)
    if m == 0:
        return []
    arr = np.asarray(p_values, dtype=np.float64)
    order = np.argsort(arr)
    ranked = arr[order]
    q = np.empty(m, dtype=np.float64)
    running_min = 1.0
    for i in range(m - 1, -1, -1):
        rank = i + 1
        val = ranked[i] * m / rank
        running_min = min(running_min, val)
        q[i] = min(1.0, running_min)
    out = np.empty(m, dtype=np.float64)
    out[order] = q
    return [round(float(x), 5) for x in out]


@dataclass(frozen=True)
class CalibratorMetadata:
    calibrator_id: str
    base_artifact_id: str
    base_artifact_sha256: str
    method: str  # RAW | TEMPERATURE | SIGMOID_OVR | ISOTONIC_OVR
    horizon: str
    target_name: str
    fold_id: int
    classes: Tuple[str, ...]
    cal_start_utc: str
    cal_end_utc: str
    cal_n_samples: int
    cal_unique_days: int
    internal_val_logloss: float
    created_at_utc: str


class MulticlassProbabilityCalibrator:
    """
    Leakage-proof post-hoc calibrator fitted strictly on the reserved CALIBRATION fold.
    Guarantees valid non-negative probabilities summing to 1.0.
    """

    def __init__(self, method: str, classes: Sequence[str]):
        self.method = str(method)
        self.classes = tuple(str(c) for c in classes)
        self.temperature_: float = 1.0
        self.ovr_models_: List[Any] = []
        self.is_fitted_: bool = False

    def fit(self, raw_probs: np.ndarray, y_true: np.ndarray) -> "MulticlassProbabilityCalibrator":
        p = _clip_renorm(raw_probs, eps=1e-7)
        c2i = {c: i for i, c in enumerate(self.classes)}
        y_idx = np.array([c2i[str(v)] for v in y_true], dtype=np.int64)

        if self.method == "RAW":
            self.is_fitted_ = True
            return self

        if self.method == "TEMPERATURE":
            logits = _safe_logits(p)

            def _nll(temp: float) -> float:
                cal_p = _softmax(logits / max(1e-3, temp))
                return float(-np.mean(np.log(np.clip(cal_p[np.arange(len(y_idx)), y_idx], 1e-15, 1.0))))

            res = minimize_scalar(_nll, bounds=(0.2, 5.0), method="bounded")
            self.temperature_ = float(res.x)
            self.is_fitted_ = True
            return self

        if self.method == "SIGMOID_OVR":
            self.ovr_models_ = []
            for ci in range(len(self.classes)):
                y_bin = (y_idx == ci).astype(int)
                pc = np.clip(p[:, ci], 1e-6, 1.0 - 1e-6)
                logit_c = np.log(pc / (1.0 - pc)).reshape(-1, 1)
                if len(np.unique(y_bin)) < 2:
                    self.ovr_models_.append(None)
                else:
                    lr = LogisticRegression(C=1e6, solver="lbfgs", max_iter=500)
                    lr.fit(logit_c, y_bin)
                    self.ovr_models_.append(lr)
            self.is_fitted_ = True
            return self

        if self.method == "ISOTONIC_OVR":
            self.ovr_models_ = []
            for ci in range(len(self.classes)):
                y_bin = (y_idx == ci).astype(int)
                pc = np.clip(p[:, ci], 1e-6, 1.0 - 1e-6)
                iso = IsotonicRegression(y_min=1e-5, y_max=1.0 - 1e-5, out_of_bounds="clip")
                iso.fit(pc, y_bin)
                self.ovr_models_.append(iso)
            self.is_fitted_ = True
            return self

        raise ValueError(f"Unsupported calibration method: {self.method}")

    def transform(self, raw_probs: np.ndarray) -> np.ndarray:
        if not self.is_fitted_:
            raise RuntimeError("Calibrator not fitted")
        p = _clip_renorm(raw_probs, eps=1e-7)
        if self.method == "RAW":
            return p
        if self.method == "TEMPERATURE":
            return _clip_renorm(_softmax(_safe_logits(p) / self.temperature_), eps=1e-7)
        if self.method == "SIGMOID_OVR":
            out = np.zeros_like(p)
            for ci, mdl in enumerate(self.ovr_models_):
                if mdl is None:
                    out[:, ci] = p[:, ci]
                else:
                    pc = np.clip(p[:, ci], 1e-6, 1.0 - 1e-6)
                    logit_c = np.log(pc / (1.0 - pc)).reshape(-1, 1)
                    out[:, ci] = mdl.predict_proba(logit_c)[:, 1]
            return _clip_renorm(out, eps=1e-7)
        if self.method == "ISOTONIC_OVR":
            out = np.zeros_like(p)
            for ci, iso in enumerate(self.ovr_models_):
                pc = np.clip(p[:, ci], 1e-6, 1.0 - 1e-6)
                out[:, ci] = iso.predict(pc)
            return _clip_renorm(out, eps=1e-7)
        raise ValueError(f"Unsupported calibration method: {self.method}")


@dataclass
class VersionedCalibrationArtifact:
    metadata: CalibratorMetadata
    calibrator: MulticlassProbabilityCalibrator

    def save(self, out_dir: Path) -> Tuple[Path, str]:
        out_dir.mkdir(parents=True, exist_ok=True)
        jpath = out_dir / f"{self.metadata.calibrator_id}.joblib"
        mpath = out_dir / f"{self.metadata.calibrator_id}.meta.json"
        joblib.dump(self, jpath, compress=3)
        h = hashlib.sha256(jpath.read_bytes()).hexdigest().upper()
        meta_dict = asdict(self.metadata)
        meta_dict["calibrator_sha256"] = h
        mpath.write_text(json.dumps(meta_dict, indent=2), encoding="utf-8")
        return jpath, h


def fit_select_calibrator_strict(
    cal_raw_probs: np.ndarray,
    cal_y: np.ndarray,
    cal_days: np.ndarray,
    classes: Sequence[str],
    allow_isotonic: bool = True,
    min_days_for_isotonic: int = 10,
) -> Tuple[MulticlassProbabilityCalibrator, Dict[str, Any]]:
    """
    Prespecified calibration selection rule using ONLY the reserved CALIBRATION fold:
      1. Chronologically split CALIBRATION days into first 60% (cal_fit) and last 40% (cal_sel).
      2. Evaluate candidate methods on cal_sel multiclass log-loss.
      3. Select lowest cal_sel log-loss method (prefer simpler RAW/TEMPERATURE on ties within 1e-4).
      4. Refit the winning method on the full CALIBRATION fold.
    """
    uniq_days = np.unique(cal_days)
    n_days = len(uniq_days)
    methods = ["RAW", "TEMPERATURE", "SIGMOID_OVR"]
    if allow_isotonic and n_days >= min_days_for_isotonic:
        methods.append("ISOTONIC_OVR")

    split_idx = max(1, int(math.floor(n_days * 0.60)))
    fit_days = set(uniq_days[:split_idx].tolist())
    m_fit = np.array([d in fit_days for d in cal_days], dtype=bool)
    m_sel = ~m_fit
    if m_sel.sum() == 0:
        m_fit[:] = True
        m_sel[:] = True

    eval_table: Dict[str, Dict[str, float]] = {}
    best_method = "RAW"
    best_ll = float("inf")

    for method in methods:
        cal = MulticlassProbabilityCalibrator(method=method, classes=classes)
        cal.fit(cal_raw_probs[m_fit], cal_y[m_fit])
        p_sel = cal.transform(cal_raw_probs[m_sel])
        ll_sel = float(np.mean(per_sample_multiclass_logloss(cal_y[m_sel], p_sel, classes)))
        eval_table[method] = {"cal_internal_sel_logloss": round(ll_sel, 6)}
        # Parsimony tie-break: require >= 1e-4 log-loss improvement to replace simpler method
        if ll_sel < best_ll - 1e-4:
            best_ll = ll_sel
            best_method = method

    final_cal = MulticlassProbabilityCalibrator(method=best_method, classes=classes)
    final_cal.fit(cal_raw_probs, cal_y)
    summary = {
        "cal_unique_days": int(n_days),
        "cal_fit_days": int(split_idx),
        "cal_sel_days": int(n_days - split_idx),
        "candidates_evaluated": eval_table,
        "selected_method": best_method,
        "selected_internal_logloss": round(best_ll, 6),
        "isotonic_allowed": bool(allow_isotonic and n_days >= min_days_for_isotonic),
    }
    return final_cal, summary


def evaluate_multiclass_calibration(
    y_true: np.ndarray,
    probs: np.ndarray,
    classes: Sequence[str],
    day_ids: np.ndarray,
    bin_counts: Tuple[int, ...] = (5, 10, 15),
) -> Dict[str, Any]:
    """Comprehensive multiclass calibration & ranking metrics."""
    p = _clip_renorm(probs, eps=1e-12)
    c2i = {str(c): i for i, c in enumerate(classes)}
    y_idx = np.array([c2i[str(v)] for v in y_true], dtype=np.int64)
    n = len(y_idx)
    k = len(classes)

    ll_vec = per_sample_multiclass_logloss(y_true, p, classes)
    y_onehot = np.eye(k, dtype=np.float64)[y_idx]
    brier_vec = np.sum((p - y_onehot) ** 2, axis=1)

    pred_i = np.argmax(p, axis=1)
    conf = np.max(p, axis=1)
    corr = (pred_i == y_idx).astype(np.float64)

    ece_by_bins: Dict[str, float] = {}
    rel_10: List[Dict[str, Any]] = []
    for nb in bin_counts:
        edges = np.linspace(0.0, 1.0, nb + 1)
        ece = 0.0
        bins_tmp = []
        for b in range(nb):
            m = (conf >= edges[b]) & (conf < edges[b + 1] if b < nb - 1 else conf <= edges[b + 1])
            cnt = int(np.sum(m))
            if cnt > 0:
                mp = float(np.mean(conf[m]))
                ma = float(np.mean(corr[m]))
                ece += (cnt / max(1, n)) * abs(ma - mp)
                bins_tmp.append({"bin": f"[{edges[b]:.2f},{edges[b+1]:.2f}]", "count": cnt, "mean_prob": round(mp, 4), "emp_acc": round(ma, 4)})
        ece_by_bins[f"ece_{nb}bin"] = round(ece, 5)
        if nb == 10:
            rel_10 = bins_tmp

    class_cond: Dict[str, Any] = {}
    roc_ovr: Dict[str, float] = {}
    pr_ovr: Dict[str, float] = {}
    for ci, cname in enumerate(classes):
        yb = (y_idx == ci).astype(int)
        pc = p[:, ci]
        roc_ovr[str(cname)] = round(float(roc_auc_score(yb, pc)), 5) if len(np.unique(yb)) == 2 else 0.5
        pr_ovr[str(cname)] = round(float(average_precision_score(yb, pc)), 5) if len(np.unique(yb)) == 2 else 0.0
        class_cond[str(cname)] = {
            "mean_pred_prob": round(float(np.mean(pc)), 5),
            "empirical_rate": round(float(np.mean(yb)), 5),
            "brier": round(float(brier_score_loss(yb, pc)), 5),
        }

    return {
        "n_samples": int(n),
        "unique_days": int(len(np.unique(day_ids))),
        "multiclass_log_loss": round(float(np.mean(ll_vec)), 5),
        "multiclass_brier": round(float(np.mean(brier_vec)), 5),
        "accuracy": round(float(np.mean(corr)), 5),
        "ece_sensitivity": ece_by_bins,
        "reliability_10bin": rel_10,
        "roc_auc_ovr": roc_ovr,
        "pr_auc_ovr": pr_ovr,
        "class_conditional": class_cond,
        "day_block_log_loss": day_block_mean_ci(ll_vec, day_ids),
    }


def evaluate_binary_event_calibration(
    y_bin: np.ndarray,
    p_pos: np.ndarray,
    day_ids: np.ndarray,
    bin_counts: Tuple[int, ...] = (5, 10, 15),
) -> Dict[str, Any]:
    """Binary event calibration (e.g., Macro P_MOVE = 1 - P_FLAT or conditional Micro)."""
    y = np.asarray(y_bin, dtype=np.int64)
    p = np.clip(np.asarray(p_pos, dtype=np.float64), 1e-7, 1.0 - 1e-7)
    n = len(y)
    ll_vec = per_sample_binary_logloss(y, p)
    brier_vec = (p - y) ** 2

    # Calibration slope & intercept via logistic regression on log-odds
    logit = np.log(p / (1.0 - p)).reshape(-1, 1)
    cal_slope, cal_intercept = None, None
    if len(np.unique(y)) == 2 and np.std(logit) > 1e-9:
        lr = LogisticRegression(C=1e6, solver="lbfgs", max_iter=500)
        lr.fit(logit, y)
        cal_slope = round(float(lr.coef_[0, 0]), 4)
        cal_intercept = round(float(lr.intercept_[0]), 4)

    ece_by_bins: Dict[str, float] = {}
    rel_10: List[Dict[str, Any]] = []
    for nb in bin_counts:
        edges = np.linspace(0.0, 1.0, nb + 1)
        ece = 0.0
        bins_tmp = []
        for b in range(nb):
            m = (p >= edges[b]) & (p < edges[b + 1] if b < nb - 1 else p <= edges[b + 1])
            cnt = int(np.sum(m))
            if cnt > 0:
                mp = float(np.mean(p[m]))
                ma = float(np.mean(y[m]))
                ece += (cnt / max(1, n)) * abs(ma - mp)
                bins_tmp.append({"bin": f"[{edges[b]:.2f},{edges[b+1]:.2f}]", "count": cnt, "mean_prob": round(mp, 4), "emp_rate": round(ma, 4)})
        ece_by_bins[f"ece_{nb}bin"] = round(ece, 5)
        if nb == 10:
            rel_10 = bins_tmp

    return {
        "n_samples": int(n),
        "positive_rate": round(float(np.mean(y)), 5),
        "mean_pred_prob": round(float(np.mean(p)), 5),
        "binary_log_loss": round(float(np.mean(ll_vec)), 5),
        "brier_score": round(float(np.mean(brier_vec)), 5),
        "roc_auc": round(float(roc_auc_score(y, p)), 5) if len(np.unique(y)) == 2 else 0.5,
        "pr_auc": round(float(average_precision_score(y, p)), 5) if len(np.unique(y)) == 2 else 0.0,
        "calibration_slope": cal_slope,
        "calibration_intercept": cal_intercept,
        "ece_sensitivity": ece_by_bins,
        "reliability_10bin": rel_10,
        "day_block_log_loss": day_block_mean_ci(ll_vec, day_ids),
    }


def simulate_h3b_trade_on_ticks(
    direction: str,
    entry_idx: int,
    tick_ts: np.ndarray,
    tick_bids: np.ndarray,
    tick_asks: np.ndarray,
    atr: float,
    equity: float = 10000.0,
    risk_pct: float = 0.5,
    comm_rate_per_side: float = FUSION_COMM_RATE_PER_SIDE,
    slippage_usd_per_side: float = FUSION_SLIPPAGE_USD_PER_SIDE,
    sl_atr_mult: float = 1.5,
    tp_atr_mult: float = 3.0,
    early_fail_minutes: int = 15,
    early_fail_min_mfe_atr: float = 0.4,
    session_close_utc: int = 15,
    enforce_session_cutoff: bool = True,
) -> Dict[str, Any]:
    """
    Canonical research H3b tick-level trade path simulator reconciled 1-to-1 with
    GoldEdge's frozen RunBacktestUseCase._OpenPosition mechanics:
      - Valid quote guard: skip b <= 0, a <= 0, or b >= a (inverted & zero-spread)
      - Executable entry: BUY at ask + slippage, SELL at bid - slippage
      - Position sizing via PositionSizingService.calculate_size with broker-rounded RT comm/lot
      - Hard SL (1.5x ATR) precedes TP Limit (3.0x ATR, exact tp fill with 0 exit slippage)
      - Breakeven at 1.0 ATR (+$0.20, reason="BREAKEVEN"), Trail Tier 1 at 1.6 ATR (1.10 ATR),
        Tier 2 at 2.5 ATR (0.75 ATR), TIME_SQUEEZE at bars_held / effective_max_hold >= 0.75
        (effective_max_hold = 60m in 07..19 UTC [hour < 20], 90m off-hours)
      - Minute-bar E1 early-fail at end of 15th completed post-entry 1m bar (MFE < 0.4 ATR)
      - Minute-bar TIMEOUT at bars_held >= effective_max_hold
      - Per-leg broker commission rounded to 2 decimals ($12 / $1M notional / side)
    """
    n = len(tick_ts)
    if entry_idx < 0 or entry_idx >= n or atr <= 0.0:
        return {"filled": False, "reason": "INVALID_ENTRY_INDEX"}

    e_ts = float(tick_ts[entry_idx])
    e_bid = float(tick_bids[entry_idx])
    e_ask = float(tick_asks[entry_idx])
    if e_bid <= 0.0 or e_ask <= 0.0 or e_bid >= e_ask or (e_ask - e_bid) > 15.0:
        return {"filled": False, "reason": "INVALID_ENTRY_QUOTE"}

    entry_dt = datetime.fromtimestamp(e_ts, tz=timezone.utc)
    if enforce_session_cutoff and entry_dt.hour >= session_close_utc:
        return {"filled": False, "reason": "PAST_SESSION_CUTOFF"}

    is_buy = str(direction).upper() == "BUY"
    raw_exec_entry = PositionSizingService.resolve_executable_entry(direction, e_bid, e_ask)
    exec_entry = raw_exec_entry + slippage_usd_per_side if is_buy else raw_exec_entry - slippage_usd_per_side

    # Exact broker-rounded RT commission per 1.00 lot (matching CommissionSchedule.estimate_round_trip_per_lot)
    one_lot_side_comm = round(1.0 * CONTRACT_SIZE_OZ * exec_entry * comm_rate_per_side, 2) if comm_rate_per_side > 0.0 else 0.0
    comm_per_lot_rt = round(2.0 * one_lot_side_comm, 2)

    sizing = PositionSizingService()
    prof = replace(get_profile("MEDIUM_BACKTEST"), risk_per_trade_pct=risk_pct, sl_atr_multiplier=sl_atr_mult, tp_atr_multiplier=tp_atr_mult)
    sig_dir = SignalDirection.BUY if is_buy else SignalDirection.SELL
    volume, init_sl, tp, rej = sizing.calculate_size(
        account_balance=equity,
        risk_profile=prof,
        entry_price=exec_entry,
        atr_value=atr,
        direction=sig_dir,
        symbol="XAUUSD",
        commission_per_lot=comm_per_lot_rt,
    )
    if volume is None or volume <= 0.0 or init_sl is None or tp is None:
        return {"filled": False, "reason": f"SIZING_REJECTED:{rej}"}

    entry_comm_usd = round(volume * CONTRACT_SIZE_OZ * exec_entry * comm_rate_per_side, 2) if comm_rate_per_side > 0.0 else 0.0
    est_rt_comm_usd = 2.0 * entry_comm_usd
    planned_risk_usd = abs(exec_entry - init_sl) * CONTRACT_SIZE_OZ * volume + est_rt_comm_usd
    if planned_risk_usd <= 1e-9:
        return {"filled": False, "reason": "ZERO_PLANNED_RISK"}

    effective_max_hold = 60 if (7 <= entry_dt.hour < 20) else 90
    entry_minute = int(e_ts // 60)

    cur_sl = float(init_sl)
    peak_price = float(exec_entry)
    trailing_active = False
    exit_reason_flag = ""

    def _ratchet(cand_px: float, bars_held: int) -> None:
        nonlocal cur_sl, peak_price, trailing_active, exit_reason_flag
        if (is_buy and cand_px > peak_price) or ((not is_buy) and cand_px < peak_price):
            peak_price = cand_px
        gain_abs = (peak_price - exec_entry) if is_buy else (exec_entry - peak_price)
        if gain_abs >= atr * 2.5:
            trailing_active = True
            t_sl = (peak_price - atr * 0.75) if is_buy else (peak_price + atr * 0.75)
            if (is_buy and t_sl > cur_sl) or ((not is_buy) and t_sl < cur_sl):
                cur_sl = t_sl
                exit_reason_flag = "TRAILING_SL"
        elif gain_abs >= atr * 1.6:
            trailing_active = True
            t_sl = (peak_price - atr * 1.10) if is_buy else (peak_price + atr * 1.10)
            if (is_buy and t_sl > cur_sl) or ((not is_buy) and t_sl < cur_sl):
                cur_sl = t_sl
                exit_reason_flag = "TRAILING_SL"
        elif gain_abs >= atr * 1.0:
            c_sl = (exec_entry + 0.20) if is_buy else (exec_entry - 0.20)
            if (is_buy and c_sl > cur_sl) or ((not is_buy) and c_sl < cur_sl):
                cur_sl = c_sl
                exit_reason_flag = "BREAKEVEN"

        if effective_max_hold > 0:
            hold_pct = bars_held / effective_max_hold
            if hold_pct >= 0.75:
                progress = min((hold_pct - 0.75) / 0.25, 1.0)
                if is_buy:
                    time_sl = cur_sl + progress * (cand_px - cur_sl)
                    if time_sl > cur_sl:
                        cur_sl = time_sl
                        exit_reason_flag = "TIME_SQUEEZE"
                else:
                    time_sl = cur_sl - progress * (cur_sl - cand_px)
                    if time_sl < cur_sl:
                        cur_sl = time_sl
                        exit_reason_flag = "TIME_SQUEEZE"

    # Group post-entry ticks by 1-minute bucket identically to RunBacktestUseCase
    post_mins = (tick_ts[entry_idx + 1:] // 60).astype(np.int64)
    exited = False
    exit_reason = "END_OF_BACKTEST"
    exit_idx = entry_idx
    exit_px = (e_bid - slippage_usd_per_side) if is_buy else (e_ask + slippage_usd_per_side)

    if len(post_mins) > 0:
        # Find contiguous minute boundaries in sorted tick array
        change_pos = np.flatnonzero(np.diff(post_mins) != 0) + 1
        starts = np.concatenate(([0], change_pos))
        ends = np.concatenate((change_pos, [len(post_mins)]))
        bars_held = 0

        for seg_s, seg_e in zip(starts, ends):
            m_val = int(post_mins[seg_s])
            is_entry_min = (m_val == entry_minute)
            if not is_entry_min:
                bars_held += 1
            evaluate_timeout = not is_entry_min

            last_valid_q: Optional[float] = None
            last_valid_idx: Optional[int] = None
            best_valid_q: Optional[float] = None

            for rel_i in range(int(seg_s), int(seg_e)):
                i = entry_idx + 1 + rel_i
                b_i = float(tick_bids[i])
                a_i = float(tick_asks[i])
                if b_i <= 0.0 or a_i <= 0.0 or b_i >= a_i:
                    continue
                exec_q = b_i if is_buy else a_i
                last_valid_q = exec_q
                last_valid_idx = i
                if best_valid_q is None or (is_buy and exec_q > best_valid_q) or ((not is_buy) and exec_q < best_valid_q):
                    best_valid_q = exec_q

                # 1. Hard/Trailing SL check BEFORE TP
                if (is_buy and b_i <= cur_sl) or ((not is_buy) and a_i >= cur_sl):
                    exit_px = (b_i - slippage_usd_per_side) if is_buy else (a_i + slippage_usd_per_side)
                    exit_reason = exit_reason_flag if exit_reason_flag else ("TRAILING_SL" if trailing_active else "SL")
                    exit_idx = i
                    exited = True
                    break

                # 2. TP Limit check (exact tp fill, ZERO exit slippage, matching RunBacktestUseCase)
                if (is_buy and b_i >= tp) or ((not is_buy) and a_i <= tp):
                    exit_px = float(tp)
                    exit_reason = "TARGET"
                    exit_idx = i
                    exited = True
                    break

                # 3. Ratchet trailing/BE/TIME_SQUEEZE on each valid tick
                _ratchet(exec_q, bars_held)

            if exited:
                break

            if evaluate_timeout and last_valid_q is not None and last_valid_idx is not None:
                # 4. E1 early-fail at exact end of 15th completed post-entry 1m bar
                if early_fail_minutes is not None and early_fail_min_mfe_atr is not None and bars_held == early_fail_minutes:
                    gain_abs = (peak_price - exec_entry) if is_buy else (exec_entry - peak_price)
                    mfe_atr = gain_abs / max(atr, 1e-9)
                    if mfe_atr < early_fail_min_mfe_atr:
                        exit_px = (last_valid_q - slippage_usd_per_side) if is_buy else (last_valid_q + slippage_usd_per_side)
                        exit_reason = f"EARLY_FAIL_{early_fail_minutes}M"
                        exit_idx = last_valid_idx
                        exited = True
                        break

                # 5. TIMEOUT at bars_held >= effective_max_hold
                if bars_held >= effective_max_hold:
                    exit_px = (last_valid_q - slippage_usd_per_side) if is_buy else (last_valid_q + slippage_usd_per_side)
                    exit_reason = "TIMEOUT"
                    exit_idx = last_valid_idx
                    exited = True
                    break

            if best_valid_q is not None:
                _ratchet(best_valid_q, bars_held)
                exit_idx = last_valid_idx if last_valid_idx is not None else exit_idx
                exit_px = (last_valid_q - slippage_usd_per_side) if is_buy else (last_valid_q + slippage_usd_per_side)

    gross_pnl_usd = ((exit_px - exec_entry) if is_buy else (exec_entry - exit_px)) * CONTRACT_SIZE_OZ * volume
    exit_comm_usd = round(volume * CONTRACT_SIZE_OZ * exit_px * comm_rate_per_side, 2) if comm_rate_per_side > 0.0 else 0.0
    comm_usd = round(entry_comm_usd + exit_comm_usd, 2)
    net_pnl_usd = gross_pnl_usd - comm_usd
    net_r = net_pnl_usd / planned_risk_usd
    best_fav = (peak_price - exec_entry) if is_buy else (exec_entry - peak_price)

    return {
        "filled": True,
        "reason": "FILLED",
        "direction": str(direction).upper(),
        "entry_ts": e_ts,
        "exit_ts": float(tick_ts[exit_idx]),
        "hold_sec": round(float(tick_ts[exit_idx] - e_ts), 3),
        "entry_bid": round(e_bid, 4),
        "entry_ask": round(e_ask, 4),
        "entry_spread_usd": round(e_ask - e_bid, 4),
        "exec_entry": round(exec_entry, 5),
        "exec_exit": round(exit_px, 5),
        "init_sl": round(float(init_sl), 5),
        "final_sl": round(float(cur_sl), 5),
        "tp": round(float(tp), 5),
        "volume": round(float(volume), 4),
        "planned_risk_usd": round(float(planned_risk_usd), 4),
        "gross_pnl_usd": round(float(gross_pnl_usd), 4),
        "comm_usd": round(float(comm_usd), 4),
        "net_pnl_usd": round(float(net_pnl_usd), 4),
        "net_r": round(float(net_r), 5),
        "mfe_atr": round(float(best_fav / atr), 4),
        "exit_reason": exit_reason,
    }
