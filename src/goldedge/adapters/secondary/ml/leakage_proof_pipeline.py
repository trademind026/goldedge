"""
Leakage-Proof ML Preprocessing & Evaluation Adapter (Task G3.3).

Hexagonal Architecture (secondary adapter): isolates numpy/pandas/sklearn
from domain contracts in `ml_training_contract.py`.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    balanced_accuracy_score,
    brier_score_loss,
    f1_score,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.preprocessing import RobustScaler

from ....domain.services.ml_training_contract import assert_zero_leakage_columns


class FoldIsolatedPreprocessor:
    """
    Phase D: Strictly fold-isolated preprocessing pipeline.
    Fits ONLY on the permitted training partition of a single fold:
      1. Zero-leakage column contract check (`assert_zero_leakage_columns`)
      2. Median imputation (`SimpleImputer`)
      3. Training-fold quantile outlier clipping (`[q_low, q_high]`)
      4. Robust scaling (`RobustScaler`)
    """

    def __init__(
        self,
        feature_cols: Sequence[str],
        clip_quantiles: Tuple[float, float] = (0.01, 0.99),
    ) -> None:
        self.feature_cols = assert_zero_leakage_columns(feature_cols)
        self.clip_quantiles = clip_quantiles
        self._imputer = SimpleImputer(strategy="median")
        self._scaler = RobustScaler()
        self._clip_low: Optional[np.ndarray] = None
        self._clip_high: Optional[np.ndarray] = None
        self._is_fitted = False
        self.audit_metadata: Dict[str, Any] = {}

    def fit(
        self,
        df_train: pd.DataFrame,
        fold_id: int,
        eval_start_ts: float,
        event_end_col: str = "label_obs_end_utc",
    ) -> "FoldIsolatedPreprocessor":
        assert_zero_leakage_columns(self.feature_cols)
        if event_end_col in df_train.columns:
            max_end = float(df_train[event_end_col].max())
            if max_end >= eval_start_ts:
                raise ValueError(
                    f"FOLD_PREPROCESS_LEAKAGE: max training {event_end_col} ({max_end}) >= eval_start ({eval_start_ts})"
                )
        else:
            max_end = float("nan")

        X = df_train[self.feature_cols].astype(np.float64).values
        X_imp = self._imputer.fit_transform(X)
        self._clip_low = np.nanquantile(X_imp, self.clip_quantiles[0], axis=0)
        self._clip_high = np.nanquantile(X_imp, self.clip_quantiles[1], axis=0)
        X_clip = np.clip(X_imp, self._clip_low, self._clip_high)
        self._scaler.fit(X_clip)
        self._is_fitted = True

        self.audit_metadata = {
            "fold_id": int(fold_id),
            "fit_n_samples": int(len(df_train)),
            "fit_max_event_end_utc": max_end,
            "eval_start_utc": float(eval_start_ts),
            "feature_count": len(self.feature_cols),
            "missing_counts_in_train": {
                col: int(df_train[col].isna().sum()) for col in self.feature_cols
            },
            "medians_sample_first5": {
                col: round(float(self._imputer.statistics_[i]), 6)
                for i, col in enumerate(self.feature_cols[:5])
            },
            "iqr_scale_sample_first5": {
                col: round(float(self._scaler.scale_[i]), 6)
                for i, col in enumerate(self.feature_cols[:5])
            },
        }
        return self

    def transform(self, df: pd.DataFrame) -> np.ndarray:
        if not self._is_fitted:
            raise RuntimeError("FoldIsolatedPreprocessor.transform called before fit()")
        X = df[self.feature_cols].astype(np.float64).values
        X_imp = self._imputer.transform(X)
        X_clip = np.clip(X_imp, self._clip_low, self._clip_high)
        return self._scaler.transform(X_clip)

    def fit_transform(
        self,
        df_train: pd.DataFrame,
        fold_id: int,
        eval_start_ts: float,
        event_end_col: str = "label_obs_end_utc",
    ) -> np.ndarray:
        self.fit(df_train, fold_id=fold_id, eval_start_ts=eval_start_ts, event_end_col=event_end_col)
        return self.transform(df_train)


def evaluate_classification_fold(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    classes: Sequence[str],
    day_ids: Sequence[str],
    realized_returns: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    """
    Phase F: Standardized Evaluation Contract for Macro & Micro walk-forward folds.
    Supports multiclass & binary classification, calibration reliability bins,
    day-block clustered uncertainty, and optional financial expectancy/PF/DD metrics.
    """
    y_true = np.asarray(y_true)
    y_prob = np.asarray(y_prob, dtype=np.float64)
    classes_list = list(classes)
    class_to_idx = {c: i for i, c in enumerate(classes_list)}
    y_idx = np.array([class_to_idx[c] for c in y_true], dtype=np.int64)
    pred_idx = np.argmax(y_prob, axis=1)
    y_pred = np.array([classes_list[i] for i in pred_idx])

    # Naive majority-class baseline
    vals, cnts = np.unique(y_idx, return_counts=True)
    maj_idx = int(vals[int(np.argmax(cnts))])
    naive_acc = float(np.mean(y_idx == maj_idx))

    acc = float(accuracy_score(y_idx, pred_idx))
    bal_acc = float(balanced_accuracy_score(y_idx, pred_idx))
    prec = float(precision_score(y_idx, pred_idx, average="macro", zero_division=0))
    rec = float(recall_score(y_idx, pred_idx, average="macro", zero_division=0))
    mf1 = float(f1_score(y_idx, pred_idx, average="macro", zero_division=0))
    ll = float(log_loss(y_idx, np.clip(y_prob, 1e-12, 1.0 - 1e-12), labels=list(range(len(classes_list)))))

    # Multiclass Brier score (mean squared error across one-hot class indicators)
    y_oh = np.eye(len(classes_list))[y_idx]
    brier = float(np.mean(np.sum((y_prob - y_oh) ** 2, axis=1)))

    # Per-class PR-AUC and ROC-AUC (one-vs-rest)
    pr_auc_by_class: Dict[str, Optional[float]] = {}
    roc_auc_by_class: Dict[str, Optional[float]] = {}
    for ci, cname in enumerate(classes_list):
        yb = (y_idx == ci).astype(int)
        if 0 < yb.sum() < len(yb):
            pr_auc_by_class[cname] = round(float(average_precision_score(yb, y_prob[:, ci])), 5)
            roc_auc_by_class[cname] = round(float(roc_auc_score(yb, y_prob[:, ci])), 5)
        else:
            pr_auc_by_class[cname] = None
            roc_auc_by_class[cname] = None

    # Day-block clustered 95% CI on accuracy
    day_arr = np.asarray(day_ids)
    uniq_d, inv = np.unique(day_arr, return_inverse=True)
    correct = (y_idx == pred_idx).astype(np.float64)
    if len(uniq_d) >= 3:
        d_cnt = np.bincount(inv).astype(np.float64)
        d_means = np.bincount(inv, weights=correct) / np.maximum(1.0, d_cnt)
        se_day = float(np.std(d_means, ddof=1) / math.sqrt(len(uniq_d)))
    else:
        se_day = float("nan")

    # 5-bin calibration reliability curve on top predicted probability
    max_p = np.max(y_prob, axis=1)
    cal_bins: List[Dict[str, Any]] = []
    edges = np.linspace(0.0, 1.0, 6)
    for b_i in range(5):
        m = (max_p >= edges[b_i]) & (max_p < edges[b_i + 1] if b_i < 4 else max_p <= edges[b_i + 1])
        if np.any(m):
            cal_bins.append(
                {
                    "bin": f"[{edges[b_i]:.1f},{edges[b_i+1]:.1f}]",
                    "count": int(np.sum(m)),
                    "mean_pred_prob": round(float(np.mean(max_p[m])), 4),
                    "empirical_acc": round(float(np.mean(correct[m])), 4),
                }
            )

    fin_metrics: Dict[str, Any] = {}
    if realized_returns is not None and len(realized_returns) == len(y_idx):
        rr = np.asarray(realized_returns, dtype=np.float64)
        rr = rr[np.isfinite(rr)]
        if len(rr) > 0:
            eq = np.cumsum(rr)
            hwm = np.maximum.accumulate(np.concatenate(([0.0], eq)))[1:]
            dd = float(np.min(eq - hwm))
            pos = float(np.sum(rr[rr > 0]))
            neg = float(-np.sum(rr[rr < 0]))
            fin_metrics = {
                "net_expectancy": round(float(np.mean(rr)), 6),
                "max_drawdown_sum": round(dd, 6),
                "profit_factor": round(pos / neg, 4) if neg > 1e-12 else None,
            }

    return {
        "n_samples": int(len(y_idx)),
        "unique_days": int(len(uniq_d)),
        "accuracy": round(acc, 5),
        "naive_majority_accuracy": round(naive_acc, 5),
        "balanced_accuracy": round(bal_acc, 5),
        "macro_precision": round(prec, 5),
        "macro_recall": round(rec, 5),
        "macro_f1": round(mf1, 5),
        "log_loss": round(ll, 5),
        "multiclass_brier": round(brier, 5),
        "pr_auc_ovr": pr_auc_by_class,
        "roc_auc_ovr": roc_auc_by_class,
        "day_block_acc_se": round(se_day, 5) if np.isfinite(se_day) else None,
        "day_block_acc_95pct_ci_halfwidth": round(1.96 * se_day, 5) if np.isfinite(se_day) else None,
        "calibration_bins": cal_bins,
        "financial_metrics": fin_metrics,
    }
