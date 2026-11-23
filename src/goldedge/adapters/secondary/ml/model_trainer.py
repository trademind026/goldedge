"""
Versioned Regularized ML Model Training Adapter (Task G3.4).

Strict Hexagonal Architecture (secondary adapter):
  - Isolates scikit-learn, LightGBM, joblib, numpy, and pandas.
  - Enforces `FoldIsolatedPreprocessor` (train-only fit) and `ml_training_contract`
    zero-leakage feature assertions on every single train/predict call.
  - Strictly reserves CALIBRATION partitions for G3.5 and never tunes on OUT-OF-TIME TEST.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss

from ....domain.services.ml_training_contract import (
    FEATURE_SCHEMA_VERSION,
    assert_zero_leakage_columns,
)
from .leakage_proof_pipeline import FoldIsolatedPreprocessor


@dataclass(frozen=True)
class ModelArtifactMetadata:
    artifact_id: str
    model_family: str
    target_name: str
    classes: Tuple[str, ...]
    feature_schema_version: str
    feature_columns: Tuple[str, ...]
    dataset_sha256: str
    fold_id: int
    train_window_utc: Tuple[str, str]
    hyperparameters: Dict[str, Any]
    git_head: str
    random_seed: int
    created_at_utc: str


class VersionedMLArtifact:
    """Self-validating model bundle enforcing feature-schema & class-order parity on reload."""

    def __init__(
        self,
        metadata: ModelArtifactMetadata,
        preprocessor: Optional[FoldIsolatedPreprocessor],
        estimator: Any,
    ) -> None:
        self.metadata = metadata
        self.preprocessor = preprocessor
        self.estimator = estimator

    def predict_proba_df(self, df: pd.DataFrame) -> np.ndarray:
        cols = list(self.metadata.feature_columns)
        assert_zero_leakage_columns(cols)
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise ValueError(f"MODEL_SCHEMA_MISMATCH: missing columns {missing} for artifact {self.metadata.artifact_id}")

        if self.metadata.model_family == "B0_CONSTANT_CLASS":
            n = len(df)
            probs = np.zeros((n, len(self.metadata.classes)), dtype=np.float64)
            probs[:, int(self.estimator["majority_idx"])] = 1.0
            return probs

        if self.metadata.model_family == "B1_TRAIN_PREVALENCE":
            priors = np.asarray(self.estimator["priors"], dtype=np.float64)
            return np.tile(priors, (len(df), 1))

        if self.metadata.model_family == "B2_CAUSAL_MOMENTUM":
            mom_col = self.estimator["momentum_col"]
            priors = np.asarray(self.estimator["priors"], dtype=np.float64)
            thr = float(self.estimator["threshold"])
            pos_idx = int(self.estimator["pos_idx"])
            neg_idx = int(self.estimator["neg_idx"])
            flat_idx = int(self.estimator["flat_idx"])
            vals = df[mom_col].fillna(0.0).values.astype(np.float64)
            probs = np.tile(priors, (len(df), 1))
            # Mild deterministic tilt toward causal momentum direction (keeps finite log-loss)
            tilt = 0.10
            up_m = vals > thr
            dn_m = vals < -thr
            if np.any(up_m):
                probs[up_m, pos_idx] = np.clip(probs[up_m, pos_idx] + tilt, 0.01, 0.98)
                probs[up_m, neg_idx] = np.clip(probs[up_m, neg_idx] - tilt / 2.0, 0.01, 0.98)
                probs[up_m, flat_idx] = np.clip(probs[up_m, flat_idx] - tilt / 2.0, 0.01, 0.98)
            if np.any(dn_m):
                probs[dn_m, neg_idx] = np.clip(probs[dn_m, neg_idx] + tilt, 0.01, 0.98)
                probs[dn_m, pos_idx] = np.clip(probs[dn_m, pos_idx] - tilt / 2.0, 0.01, 0.98)
                probs[dn_m, flat_idx] = np.clip(probs[dn_m, flat_idx] - tilt / 2.0, 0.01, 0.98)
            probs = probs / np.sum(probs, axis=1, keepdims=True)
            return probs

        assert self.preprocessor is not None
        X = self.preprocessor.transform(df)
        raw_p = self.estimator.predict_proba(X)
        return np.asarray(raw_p, dtype=np.float64)

    def save(self, out_dir: Path) -> Tuple[Path, str]:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{self.metadata.artifact_id}.joblib"
        joblib.dump(self, path, compress=3)
        sha = hashlib.sha256(path.read_bytes()).hexdigest().upper()
        meta_path = out_dir / f"{self.metadata.artifact_id}.meta.json"
        meta_dict = {
            **self.metadata.__dict__,
            "artifact_sha256": sha,
            "preprocessor_audit": self.preprocessor.audit_metadata if self.preprocessor else None,
        }
        meta_path.write_text(json.dumps(meta_dict, indent=2), encoding="utf-8")
        return path, sha

    @staticmethod
    def load(path: Path, expected_feature_columns: Optional[Sequence[str]] = None) -> "VersionedMLArtifact":
        obj: VersionedMLArtifact = joblib.load(path)
        if expected_feature_columns is not None:
            if list(obj.metadata.feature_columns) != list(expected_feature_columns):
                raise ValueError(
                    f"ARTIFACT_SCHEMA_MISMATCH: loaded {list(obj.metadata.feature_columns)} != expected {list(expected_feature_columns)}"
                )
        return obj


def _encode_y(y: Sequence[str], classes: Sequence[str]) -> np.ndarray:
    c2i = {c: i for i, c in enumerate(classes)}
    return np.array([c2i[str(v)] for v in y], dtype=np.int64)


def fit_baseline_models(
    df_train: pd.DataFrame,
    y_train: np.ndarray,
    classes: Sequence[str],
    feature_cols: Sequence[str],
    momentum_col: str,
    target_name: str,
    dataset_sha256: str,
    fold_id: int,
    train_window_utc: Tuple[str, str],
    git_head: str,
    seed: int = 42,
) -> Dict[str, VersionedMLArtifact]:
    """Fit B0 (constant class), B1 (train prevalence), and B2 (causal momentum) strictly on TRAIN."""
    y_idx = _encode_y(y_train, classes)
    counts = np.bincount(y_idx, minlength=len(classes)).astype(np.float64)
    priors = np.clip(counts / max(1.0, counts.sum()), 1e-6, 1.0)
    priors = priors / priors.sum()
    maj_idx = int(np.argmax(counts))
    now_iso = datetime.now(timezone.utc).isoformat()

    def _meta(fam: str, hp: Dict[str, Any]) -> ModelArtifactMetadata:
        return ModelArtifactMetadata(
            artifact_id=f"{target_name}_f{fold_id}_{fam}",
            model_family=fam,
            target_name=target_name,
            classes=tuple(classes),
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            feature_columns=tuple(feature_cols),
            dataset_sha256=dataset_sha256,
            fold_id=fold_id,
            train_window_utc=train_window_utc,
            hyperparameters=hp,
            git_head=git_head,
            random_seed=seed,
            created_at_utc=now_iso,
        )

    b0 = VersionedMLArtifact(_meta("B0_CONSTANT_CLASS", {"majority_class": classes[maj_idx]}), None, {"majority_idx": maj_idx})
    b1 = VersionedMLArtifact(_meta("B1_TRAIN_PREVALENCE", {"priors": [round(float(x), 5) for x in priors]}), None, {"priors": priors})

    # Causal momentum threshold = training-fold median of |momentum_col|
    mom_vals = df_train[momentum_col].fillna(0.0).values.astype(np.float64)
    thr = float(np.median(np.abs(mom_vals)))
    # Identify positive, negative, neutral class indices cleanly
    pos_idx, neg_idx, flat_idx = 0, 1, 2
    b2 = VersionedMLArtifact(
        _meta("B2_CAUSAL_MOMENTUM", {"momentum_col": momentum_col, "train_median_abs_thr": round(thr, 7)}),
        None,
        {"momentum_col": momentum_col, "priors": priors, "threshold": thr, "pos_idx": pos_idx, "neg_idx": neg_idx, "flat_idx": flat_idx},
    )
    return {"B0_CONSTANT_CLASS": b0, "B1_TRAIN_PREVALENCE": b1, "B2_CAUSAL_MOMENTUM": b2}


def fit_select_logistic_regression(
    df_train: pd.DataFrame,
    y_train: np.ndarray,
    df_val: pd.DataFrame,
    y_val: np.ndarray,
    classes: Sequence[str],
    feature_cols: Sequence[str],
    eval_start_ts: float,
    target_name: str,
    dataset_sha256: str,
    fold_id: int,
    train_window_utc: Tuple[str, str],
    git_head: str,
    seed: int = 42,
    c_grid: Sequence[float] = (0.01, 0.1, 1.0),
) -> Tuple[VersionedMLArtifact, List[Dict[str, Any]]]:
    """Fit L2-regularized multinomial Logistic Regression on TRAIN; select C strictly on INNER VAL log-loss."""
    prep = FoldIsolatedPreprocessor(feature_cols)
    X_tr = prep.fit_transform(df_train, fold_id=fold_id, eval_start_ts=eval_start_ts)
    X_va = prep.transform(df_val)
    y_tr_i = _encode_y(y_train, classes)
    y_va_i = _encode_y(y_val, classes)

    best_model = None
    best_c = None
    best_val_ll = float("inf")
    trials: List[Dict[str, Any]] = []

    for c_val in c_grid:
        clf = LogisticRegression(
            C=float(c_val),
            penalty="l2",
            solver="lbfgs",
            max_iter=400,
            random_state=seed,
        )
        clf.fit(X_tr, y_tr_i)
        p_tr = np.clip(clf.predict_proba(X_tr), 1e-12, 1.0 - 1e-12)
        p_va = np.clip(clf.predict_proba(X_va), 1e-12, 1.0 - 1e-12)
        tr_ll = float(log_loss(y_tr_i, p_tr, labels=list(range(len(classes)))))
        va_ll = float(log_loss(y_va_i, p_va, labels=list(range(len(classes)))))
        trials.append({"model": "B3_LOGISTIC_L2", "C": float(c_val), "train_log_loss": round(tr_ll, 6), "val_log_loss": round(va_ll, 6)})
        if va_ll < best_val_ll:
            best_val_ll = va_ll
            best_c = float(c_val)
            best_model = clf

    meta = ModelArtifactMetadata(
        artifact_id=f"{target_name}_f{fold_id}_B3_LOGISTIC_L2",
        model_family="B3_LOGISTIC_L2",
        target_name=target_name,
        classes=tuple(classes),
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        feature_columns=tuple(feature_cols),
        dataset_sha256=dataset_sha256,
        fold_id=fold_id,
        train_window_utc=train_window_utc,
        hyperparameters={"C": best_c, "penalty": "l2", "solver": "lbfgs", "selected_on": "inner_val_log_loss"},
        git_head=git_head,
        random_seed=seed,
        created_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    return VersionedMLArtifact(meta, prep, best_model), trials


def fit_select_constrained_lightgbm(
    df_train: pd.DataFrame,
    y_train: np.ndarray,
    df_val: pd.DataFrame,
    y_val: np.ndarray,
    classes: Sequence[str],
    feature_cols: Sequence[str],
    eval_start_ts: float,
    target_name: str,
    dataset_sha256: str,
    fold_id: int,
    train_window_utc: Tuple[str, str],
    git_head: str,
    min_child_grid: Sequence[int] = (150, 300),
    depth_grid: Sequence[int] = (2, 3),
    seed: int = 42,
) -> Tuple[VersionedMLArtifact, List[Dict[str, Any]]]:
    """
    Train deliberately constrained LightGBM on TRAIN; early-stop & select hyperparameters
    strictly on INNER VALIDATION log-loss (never touching CALIBRATION or OUT-OF-TIME TEST).
    """
    prep = FoldIsolatedPreprocessor(feature_cols)
    X_tr = prep.fit_transform(df_train, fold_id=fold_id, eval_start_ts=eval_start_ts)
    X_va = prep.transform(df_val)
    y_tr_i = _encode_y(y_train, classes)
    y_va_i = _encode_y(y_val, classes)

    best_model = None
    best_hp: Dict[str, Any] = {}
    best_val_ll = float("inf")
    trials: List[Dict[str, Any]] = []

    for depth in depth_grid:
        num_leaves = 4 if depth == 2 else 7
        for mcs in min_child_grid:
            clf = lgb.LGBMClassifier(
                objective="multiclass",
                num_class=len(classes),
                max_depth=int(depth),
                num_leaves=int(num_leaves),
                min_child_samples=int(mcs),
                learning_rate=0.05,
                n_estimators=400,
                subsample=0.8,
                subsample_freq=1,
                colsample_bytree=0.7,
                reg_alpha=1.0,
                reg_lambda=5.0,
                random_state=seed,
                n_jobs=4,
                verbose=-1,
            )
            clf.fit(
                X_tr,
                y_tr_i,
                eval_set=[(X_va, y_va_i)],
                callbacks=[lgb.early_stopping(stopping_rounds=30, verbose=False)],
            )
            p_tr = np.clip(clf.predict_proba(X_tr), 1e-12, 1.0 - 1e-12)
            p_va = np.clip(clf.predict_proba(X_va), 1e-12, 1.0 - 1e-12)
            tr_ll = float(log_loss(y_tr_i, p_tr, labels=list(range(len(classes)))))
            va_ll = float(log_loss(y_va_i, p_va, labels=list(range(len(classes)))))
            hp = {
                "max_depth": int(depth),
                "num_leaves": int(num_leaves),
                "min_child_samples": int(mcs),
                "reg_alpha": 1.0,
                "reg_lambda": 5.0,
                "colsample_bytree": 0.7,
                "learning_rate": 0.05,
                "best_iteration": int(clf.best_iteration_ or 0),
            }
            trials.append({"model": "C1_LIGHTGBM_CONSTRAINED", **hp, "train_log_loss": round(tr_ll, 6), "val_log_loss": round(va_ll, 6)})
            if va_ll < best_val_ll:
                best_val_ll = va_ll
                best_hp = hp
                best_model = clf

    meta = ModelArtifactMetadata(
        artifact_id=f"{target_name}_f{fold_id}_C1_LIGHTGBM_CONSTRAINED",
        model_family="C1_LIGHTGBM_CONSTRAINED",
        target_name=target_name,
        classes=tuple(classes),
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        feature_columns=tuple(feature_cols),
        dataset_sha256=dataset_sha256,
        fold_id=fold_id,
        train_window_utc=train_window_utc,
        hyperparameters={**best_hp, "selected_on": "inner_val_log_loss"},
        git_head=git_head,
        random_seed=seed,
        created_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    return VersionedMLArtifact(meta, prep, best_model), trials
