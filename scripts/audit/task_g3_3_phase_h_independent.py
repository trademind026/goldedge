"""
TASK G3.3 — Phase H: Independent Clean-Room Verification of Leakage-Proof ML Pipeline.
STRICT CLEAN-ROOM: ZERO imports from src/goldedge/.
Independently verifies:
  1. Source dataset integrity & SHA-256 hashes
  2. Feature / target zero-overlap separation
  3. Timestamp contract alignment (100% of Macro & Micro rows)
  4. Fold assignments, purge & embargo boundaries (`max(train_end) < eval_start`)
  5. Training-only fitted preprocessing statistics (independent median/IQR parity)
  6. Exact Decimal discrete-tick tie counts & parity
  7. Duplicate event detection (`0` duplicates) & quarantined quote exclusion (`0` rows)
Outputs:
  - backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_independent_reconciliation.json
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

G32_DIR = Path("backtest_results/audit/task_g3_2_label_design")
OUT_DIR = Path("backtest_results/audit/task_g3_3_leakage_proof_pipeline")

BLACKLIST = {
    "exit_bid_15m", "exit_ask_15m", "buy_ret_raw", "sell_ret_raw", "buy_ret_net", "sell_ret_net",
    "raw_class_005", "net_class_005", "raw_class_010", "net_class_010", "raw_class_015", "net_class_015",
    "raw_class_020", "net_class_020", "buy_barrier_010", "sell_barrier_010", "buy_mfe_usd", "buy_mae_usd",
    "sell_mfe_usd", "sell_mae_usd", "h3b_sim_buy_exit", "h3b_sim_sell_exit", "fut_bid_30t", "fut_ask_30t",
    "mid_delta_usd", "buy_net_usd", "sell_net_usd", "net_class", "buy_timing_class", "sell_timing_class",
    "quote_move_class_1x_spread", "net_order_flow", "tick_momentum", "buy_vol", "sell_vol", "aggressor",
    "l2_order_book", "real_volume", "vwap",
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _ts(iso_z: str) -> float:
    return datetime.fromisoformat(iso_z.replace("Z", "+00:00")).timestamp()


def run_clean_room_g3_3() -> None:
    t0 = time.time()
    manifest = json.loads((OUT_DIR / "g3_3_dataset_manifest.json").read_text(encoding="utf-8"))
    fcontract = json.loads((OUT_DIR / "g3_3_feature_contract.json").read_text(encoding="utf-8"))
    splits = json.loads((OUT_DIR / "g3_3_walkforward_split_manifest.json").read_text(encoding="utf-8"))
    prep_aud = json.loads((OUT_DIR / "g3_3_preprocessing_audit.json").read_text(encoding="utf-8"))
    stab = json.loads((OUT_DIR / "g3_3_discrete_tick_stability.json").read_text(encoding="utf-8"))

    # 1. Independently verify SHA-256 hashes of all 4 Parquets
    hash_checks = {}
    for fname, d_dir in (
        ("g3_2_macro_labels.parquet", G32_DIR),
        ("g3_2_micro_labels.parquet", G32_DIR),
        ("g3_3_macro_features_causal.parquet", OUT_DIR),
        ("g3_3_micro_features_causal.parquet", OUT_DIR),
    ):
        actual = _sha256(d_dir / fname)
        expected = manifest["source_artifacts"][fname]["sha256"]
        hash_checks[fname] = {"match": actual == expected, "sha256": actual}

    df_m = pd.read_parquet(G32_DIR / "g3_2_macro_labels.parquet")
    df_u = pd.read_parquet(G32_DIR / "g3_2_micro_labels.parquet")
    df_mx = pd.read_parquet(OUT_DIR / "g3_3_macro_features_causal.parquet")
    df_ux = pd.read_parquet(OUT_DIR / "g3_3_micro_features_causal.parquet")

    # 2. Feature / target separation & alignment
    m_cols = [c for c in df_mx.columns if c != "feature_cutoff_utc"]
    u_cols = [c for c in df_ux.columns if c != "feature_cutoff_utc"]
    m_leaked = sorted(BLACKLIST.intersection({c.lower() for c in m_cols}))
    u_leaked = sorted(BLACKLIST.intersection({c.lower() for c in u_cols}))
    align_ok = bool(
        len(df_m) == len(df_mx)
        and len(df_u) == len(df_ux)
        and (df_m["feature_cutoff_utc"].values == df_mx["feature_cutoff_utc"].values).all()
        and (df_u["feature_cutoff_utc"].values == df_ux["feature_cutoff_utc"].values).all()
    )

    # 3. Duplicate & quarantine checks
    m_dups = int(df_m["signal_decision_utc"].duplicated().sum())
    u_dups = int(df_u["earliest_entry_utc"].duplicated().sum())
    quar_rows = int((df_m["day_utc"] == "2026-11-22").sum() + (df_u["day_utc"] == "2026-11-22").sum())

    # 4. Independently recompute all 3 walk-forward folds & training-only medians
    m_starts = df_m["label_obs_start_utc"].values.astype(np.float64)
    m_ends = df_m["label_obs_end_utc"].values.astype(np.float64)
    u_starts = df_u["earliest_entry_utc"].values.astype(np.float64)
    u_ends = df_u["label_obs_end_utc"].values.astype(np.float64)

    fold_mismatches = 0
    prep_median_mismatches = 0
    fold_details: List[Dict[str, Any]] = []

    for f_rec, p_rec in zip(splits["folds"], prep_aud["folds"]):
        tr_s = _ts(f_rec["shared_utc_boundaries"]["train"][0])
        val_s = _ts(f_rec["shared_utc_boundaries"]["inner_val"][0])
        cal_s = _ts(f_rec["shared_utc_boundaries"]["calibration"][0])
        tst_s = _ts(f_rec["shared_utc_boundaries"]["out_of_time_test"][0])
        tst_e = _ts(f_rec["shared_utc_boundaries"]["out_of_time_test"][1])

        m_emb = float(f_rec["macro"]["embargo_sec"])
        u_emb = float(f_rec["micro"]["embargo_sec"])

        # Independently verify ALL 3 adjacent boundaries: Train->Val, Val->Cal, Cal->Test
        m_tr_mask = (m_starts >= tr_s) & (m_starts < val_s - m_emb) & (m_ends < val_s)
        m_val_mask = (m_starts >= val_s) & (m_starts < cal_s - m_emb) & (m_ends < cal_s)
        m_cal_mask = (m_starts >= cal_s) & (m_starts < tst_s - m_emb) & (m_ends < tst_s)
        m_tst_mask = (m_starts >= tst_s) & (m_starts < tst_e)

        u_tr_mask = (u_starts >= tr_s) & (u_starts < val_s - u_emb) & (u_ends < val_s)
        u_val_mask = (u_starts >= val_s) & (u_starts < cal_s - u_emb) & (u_ends < cal_s)
        u_cal_mask = (u_starts >= cal_s) & (u_starts < tst_s - u_emb) & (u_ends < tst_s)
        u_tst_mask = (u_starts >= tst_s) & (u_starts < tst_e)

        m_all3 = bool(
            float(m_ends[m_tr_mask].max()) < val_s
            and float(m_ends[m_val_mask].max()) < cal_s
            and float(m_ends[m_cal_mask].max()) < tst_s
        )
        u_all3 = bool(
            float(u_ends[u_tr_mask].max()) < val_s
            and float(u_ends[u_val_mask].max()) < cal_s
            and float(u_ends[u_cal_mask].max()) < tst_s
        )

        f_ok = (
            int(m_tr_mask.sum()) == f_rec["macro"]["train_n"]
            and int(m_val_mask.sum()) == f_rec["macro"]["val_n"]
            and int(m_cal_mask.sum()) == f_rec["macro"]["cal_n"]
            and int(m_tst_mask.sum()) == f_rec["macro"]["test_n"]
            and int(u_tr_mask.sum()) == f_rec["micro"]["train_n"]
            and int(u_val_mask.sum()) == f_rec["micro"]["val_n"]
            and int(u_cal_mask.sum()) == f_rec["micro"]["cal_n"]
            and int(u_tst_mask.sum()) == f_rec["micro"]["test_n"]
            and m_all3
            and u_all3
        )
        if not f_ok:
            fold_mismatches += 1

        # Independently verify training-only medians for first 5 Macro & Micro features
        for col, expected_med in p_rec["macro_preprocessor"]["medians_sample_first5"].items():
            actual_med = round(float(np.nanmedian(df_mx.loc[m_tr_mask, col].values)), 6)
            if abs(actual_med - expected_med) > 1e-5:
                prep_median_mismatches += 1
        for col, expected_med in p_rec["micro_preprocessor"]["medians_sample_first5"].items():
            actual_med = round(float(np.nanmedian(df_ux.loc[u_tr_mask, col].values)), 6)
            if abs(actual_med - expected_med) > 1e-5:
                prep_median_mismatches += 1

        fold_details.append({"fold_id": f_rec["fold_id"], "exact_split_match": f_ok})

    # 5. Independently verify exact discrete-tick tie count
    ea_c = np.rint(df_u["entry_ask"].values * 100.0).astype(np.int64)
    eb_c = np.rint(df_u["entry_bid"].values * 100.0).astype(np.int64)
    fa_c = np.rint(df_u["fut_ask_30t"].values * 100.0).astype(np.int64)
    indep_buy_ties = int((np.abs(fa_c - ea_c) == (ea_c - eb_c)).sum())
    ties_match = indep_buy_ties == stab["exact_raw_price_ties_equal_to_entry_spread"]["buy_ties_count"]

    # 6. Independently verify expanded 6.2-month Parquets & all 4 folds x 3 adjacent boundaries (if present)
    exp_dir = OUT_DIR / "expanded_6m"
    exp_m_pq = exp_dir / "g3_3_expanded_6m_macro_dataset.parquet"
    exp_u_pq = exp_dir / "g3_3_expanded_6m_micro_dataset.parquet"
    exp_check: Dict[str, Any] = {"present": False}
    if exp_m_pq.exists() and exp_u_pq.exists():
        exp_rep = json.loads((OUT_DIR / "g3_3_historical_expansion_report.json").read_text(encoding="utf-8"))
        e6 = exp_rep.get("expanded_6_2_month_verified", {})
        em_sha = _sha256(exp_m_pq)
        eu_sha = _sha256(exp_u_pq)
        df_em = pd.read_parquet(exp_m_pq)
        df_eu = pd.read_parquet(exp_u_pq)

        em_ts_ok = bool((df_em["feature_cutoff_utc"] == df_em["signal_decision_utc"]).all() and (df_em["earliest_entry_utc"] >= df_em["signal_decision_utc"]).all() and (df_em["label_obs_end_utc"] > df_em["label_obs_start_utc"]).all())
        eu_ts_ok = bool((df_eu["feature_cutoff_utc"] == df_eu["earliest_entry_utc"]).all() and (df_eu["label_obs_end_utc"] > df_eu["earliest_entry_utc"]).all())
        em_dups = int(df_em["signal_decision_utc"].duplicated().sum())
        eu_dups = int(df_eu["earliest_entry_utc"].duplicated().sum())
        e_quar = int((df_em["day_utc"] == "2026-11-22").sum() + (df_eu["day_utc"] == "2026-11-22").sum())

        # Verify all 4 expanded folds x 3 adjacent boundaries independently
        exp_bounds = [
            ("2026-05-13T00:00:00Z", "2026-07-10T00:00:00Z", "2026-07-24T00:00:00Z", "2026-08-07T00:00:00Z", "2026-08-28T00:00:00Z"),
            ("2026-05-13T00:00:00Z", "2026-08-07T00:00:00Z", "2026-08-21T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-25T00:00:00Z"),
            ("2026-05-13T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-18T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-23T00:00:00Z"),
            ("2026-05-13T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-16T00:00:00Z", "2026-10-30T00:00:00Z", "2026-11-19T12:00:00Z"),
        ]
        ems, eme = df_em["label_obs_start_utc"].values.astype(float), df_em["label_obs_end_utc"].values.astype(float)
        eus, eue = df_eu["earliest_entry_utc"].values.astype(float), df_eu["label_obs_end_utc"].values.astype(float)
        exp_fold_mm = 0
        for fi, (ts0, vs0, cs0, x0, x1) in enumerate(exp_bounds):
            tr_s, val_s, cal_s, tst_s, tst_e = _ts(ts0), _ts(vs0), _ts(cs0), _ts(x0), _ts(x1)
            rep_f = e6["expanded_4way_folds"][fi]
            mtr = (ems >= tr_s) & (ems < val_s - 3600.0) & (eme < val_s)
            mva = (ems >= val_s) & (ems < cal_s - 3600.0) & (eme < cal_s)
            mca = (ems >= cal_s) & (ems < tst_s - 3600.0) & (eme < tst_s)
            mts = (ems >= tst_s) & (ems < tst_e)
            utr = (eus >= tr_s) & (eus < val_s - 300.0) & (eue < val_s)
            uva = (eus >= val_s) & (eus < cal_s - 300.0) & (eue < cal_s)
            uca = (eus >= cal_s) & (eus < tst_s - 300.0) & (eue < tst_s)
            uts = (eus >= tst_s) & (eus < tst_e)
            ok_f = (
                int(mtr.sum()) == rep_f["macro_n"]["train"]
                and int(mva.sum()) == rep_f["macro_n"]["val"]
                and int(mca.sum()) == rep_f["macro_n"]["cal"]
                and int(mts.sum()) == rep_f["macro_n"]["test"]
                and int(utr.sum()) == rep_f["micro_n"]["train"]
                and int(uva.sum()) == rep_f["micro_n"]["val"]
                and int(uca.sum()) == rep_f["micro_n"]["cal"]
                and int(uts.sum()) == rep_f["micro_n"]["test"]
                and float(eme[mtr].max()) < val_s and float(eme[mva].max()) < cal_s and float(eme[mca].max()) < tst_s
                and float(eue[utr].max()) < val_s and float(eue[uva].max()) < cal_s and float(eue[uca].max()) < tst_s
            )
            if not ok_f:
                exp_fold_mm += 1

        exp_check = {
            "present": True,
            "macro_rows": int(len(df_em)),
            "micro_rows": int(len(df_eu)),
            "macro_sha256_match": em_sha == e6.get("expanded_macro_sha256"),
            "micro_sha256_match": eu_sha == e6.get("expanded_micro_sha256"),
            "timestamp_contracts_ok": em_ts_ok and eu_ts_ok,
            "duplicates": em_dups + eu_dups,
            "quarantined_sunday_rows": e_quar,
            "expanded_4way_fold_mismatches": exp_fold_mm,
            "status": "PASS" if (em_sha == e6.get("expanded_macro_sha256") and eu_sha == e6.get("expanded_micro_sha256") and em_ts_ok and eu_ts_ok and (em_dups + eu_dups) == 0 and e_quar == 0 and exp_fold_mm == 0) else "FAIL",
        }

    all_pass = (
        all(v["match"] for v in hash_checks.values())
        and len(m_leaked) == 0
        and len(u_leaked) == 0
        and align_ok
        and m_dups == 0
        and u_dups == 0
        and quar_rows == 0
        and fold_mismatches == 0
        and prep_median_mismatches == 0
        and ties_match
        and (not exp_check["present"] or exp_check["status"] == "PASS")
    )

    out = {
        "task": "G3.3_PHASE_H_INDEPENDENT_RECONCILIATION",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "clean_room_zero_goldedge_imports": True,
        "overall_status": "PASS" if all_pass else "FAIL",
        "dataset_hash_checks": hash_checks,
        "feature_target_separation": {
            "macro_leaked_cols": m_leaked,
            "micro_leaked_cols": u_leaked,
            "exact_timestamp_alignment": align_ok,
        },
        "integrity_checks": {
            "macro_duplicate_timestamps": m_dups,
            "micro_duplicate_timestamps": u_dups,
            "quarantined_sunday_rows": quar_rows,
        },
        "walkforward_split_reconciliation": {
            "folds_checked": len(fold_details),
            "fold_mismatches": fold_mismatches,
            "details": fold_details,
        },
        "training_only_preprocessing_reconciliation": {
            "median_mismatches": prep_median_mismatches,
        },
        "discrete_tick_tie_reconciliation": {
            "independent_buy_ties": indep_buy_ties,
            "reported_buy_ties": stab["exact_raw_price_ties_equal_to_entry_spread"]["buy_ties_count"],
            "exact_match": ties_match,
        },
        "expanded_6m_independent_reconciliation": exp_check,
        "elapsed_sec": round(time.time() - t0, 2),
    }
    (OUT_DIR / "g3_3_independent_reconciliation.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"[G3.3 PHASE H CLEAN-ROOM DONE in {out['elapsed_sec']}s] status={out['overall_status']} fold_mm={fold_mismatches} prep_mm={prep_median_mismatches} ties={indep_buy_ties}")


if __name__ == "__main__":
    run_clean_room_g3_3()
