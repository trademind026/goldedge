# TASK G3.3 — GoldEdge Leakage-Proof ML Training Pipeline, Historical Expansion & Independent Verification (Final Complete Pass)

**Project:** GoldEdge (`XAUUSD` Spot Gold CFD)  
**Phase:** G3 — Macro + Micro ML Development (Current: `G3.3 — Verified`, Previous: `G3.2`, Next: `G3.4 — Regularized ML Model Training & Comparison`)  
**Execution Mode:** STRICT SHADOW MODE (`SHADOW_MODE = True`, `TRADING_MODE = "demo"`, `HWM_MAX_DRAWDOWN_PCT = 8.0`, `market_orders = 0`, `market_trades = 0`)  
**Feature Schema:** `G3_3_FEATURE_SCHEMA_V1` | **Label Spec:** `G3_2_LABEL_SPEC_V1`  

---

## 1. Executive Verdicts

> [!IMPORTANT]
> - **Overall Engineering Verdict:** **`VERIFIED`** (`0` clean-room fold, preprocessing, boundary, and discrete-tick mismatches across BOTH the 3.2m core and expanded 6.2m datasets; `13/13` adversarial fault-injection tests `PASS`; `9/9` regression suites `PASS`; frozen H0/H3b & safety locks intact; zero forecasting models tuned or deployed).
> - **Historical Expansion Status:** **`HISTORY_EXPANSION_VERIFIED`** — Completed 100% (`290/290` 6-hour chunks) of the resumable cTrader `ProtoOAGetTickDataReq` staged backfill (`2026-05-13T00:00:00.098Z` – `2026-08-12T23:59:59.494Z`) into isolated `market_ticks_staged_expansion` (**`+18,403,826` valid ticks**, **`+90,373` 1m bars**, `66` weekdays, `0` invalid rows, `0` overlap with Aug–Nov core, median spread `\$0.07`). Combined via non-destructive union view `v_market_ticks_expanded_6m_v1` with the verified Aug–Nov core → **`6.2` continuous months (`134` clean weekdays, `187,235` 1m bars, `36,833` Macro labels, `1,278,171` Micro non-overlapping 30-tick labels)**.
> - **Macro Training Readiness:** **`MACRO_TRAINING_READY`** (for chronological walk-forward Logistic Regression & constrained LightGBM research on the verified 6.2-month dataset; final unseen deployment proof still requires prospective post-freeze forward shadow validation in `G3.7`).
> - **Micro Training Readiness:** **`MICRO_TRAINING_READY`** (for top-of-book quote-direction & ex-ante entry-timing research, excluding unavailable order-flow/L2 features).

---

## 2. Final Verification Pass — Resolution of Both Focused Checks

| Focused Check | Resolution Implemented & Independently Verified |
| :--- | :--- |
| **Check 1 — Complete 6+ Month Historical Backfill & Rebuild** | Executed 100% (`290/290` chunks) of resumable [task_g3_3_staged_backfill.py](file:///d:/gigs/goldedge/scripts/audit/task_g3_3_staged_backfill.py) into isolated `market_ticks_staged_expansion` (`0` edits to raw `market_ticks` or frozen H0/H3b), audited coverage (`18,403,826` valid ticks, `90,373` 1m bars, `0` invalid, `0` overlap, median spread `\$0.07`, p95 `\$0.15`), created non-destructive union view `v_market_ticks_expanded_6m_v1` (`187,235` 1m bars total), and rebuilt expanded 6.2-month datasets via [task_g3_3_post_backfill_rebuild.py](file:///d:/gigs/goldedge/scripts/audit/task_g3_3_post_backfill_rebuild.py): `expanded_6m/g3_3_expanded_6m_macro_dataset.parquet` (`36,833` rows, `12,193` non-overlapping 15m, SHA-256 `2E9A9D9B7215...`) and `expanded_6m/g3_3_expanded_6m_micro_dataset.parquet` (`1,278,171` rows, SHA-256 `7B565DFAF77D...`). Independently verified in clean-room (`status = PASS`, `0` hash/timestamp/duplicate/quarantine/fold mismatches). |
| **Check 2 — Strict Purging & Embargo Across All 3 Adjacent Split Boundaries** | Implemented `assign_purged_4way_walkforward_fold(...)` in pure-Python [ml_training_contract.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/ml_training_contract.py#L228-L314) enforcing exact per-row `event_end` purging AND embargo (`60m` Macro / `300s` Micro) across **all three adjacent boundaries**: `Train → Inner Val`, `Inner Val → Calibration`, and `Calibration → Out-of-Time Test`. Independently verified `0` mismatches across all 3 core folds AND all 4 expanded 6.2-month folds in clean-room [task_g3_3_phase_h_independent.py](file:///d:/gigs/goldedge/scripts/audit/task_g3_3_phase_h_independent.py) (`fold_mm = 0`, `expanded_4way_fold_mismatches = 0`). |

---

## 3. Historical Expansion & Expanded 6.2-Month Dataset Summary

Full report: [g3_3_historical_expansion_report.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_historical_expansion_report.json).

| Metric | Newly Staged (`May 13 – Aug 12`) | Verified Core (`Aug 13 – Nov 19`) | Combined `6.2-Month` Union |
| :--- | ---: | ---: | ---: |
| **Valid XAUUSD Ticks** | `18,403,826` (`0` invalid) | `19,947,246` (`1` Sunday quarantined) | **`38,351,072`** |
| **Completed 1m Bars** | `90,373` | `96,862` | **`187,235`** (`0` overlap) |
| **Weekday Trading Days** | `66` | `68` (clean core) | **`134`** |
| **Spread (Median / Mean / p95)** | `\$0.07 / \$0.086 / \$0.15` | `\$0.07 / \$0.074 / \$0.12` | Broker-consistent Fusion demo |
| **Macro 5m Labels (`15m` Non-Overlap)** | `+17,829` (`+5,901`) | `19,004` (`6,292`) | **`36,833` (`12,193`)** |
| **Micro Non-Overlap 30-Tick Labels** | `+613,365` | `664,806` | **`1,278,171`** |

- **Expanded 6.2-Month Parquets (Fresh SHA-256):**
  - `expanded_6m/g3_3_expanded_6m_macro_dataset.parquet`: `2E9A9D9B7215A50B75B6464FD1E3A17AA887BF415B5A683586E0220B045189E3`
  - `expanded_6m/g3_3_expanded_6m_micro_dataset.parquet`: `7B565DFAF77D2B23B987FF8EE64952B0875287D428EE2BE39B5DAF48C6778667`

---

## 4. Pipeline Contracts, Discrete-Tick Stability, Clean-Room & Adversarial Results

- **Leakage Blacklist ([g3_3_feature_contract.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_feature_contract.json)):** `71` forbidden target, future-quote, future-barrier, H3b-outcome, and unavailable order-flow/L2/volume columns; `0` overlap with `21` Macro or `10` Micro causal features; 100% timestamp ordering verified.
- **Fold-Isolated Preprocessing ([g3_3_preprocessing_audit.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_preprocessing_audit.json)):** `SimpleImputer(median)` + `[1%, 99%]` clip + `RobustScaler` fit strictly on `X_train` per fold (`0` NaNs after transform, `0` clean-room median mismatches).
- **Discrete-Tick Boundary Stability ([g3_3_discrete_tick_stability.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_discrete_tick_stability.json)):** Verified `\$0.01` broker tick size; `0 / 664,806` float-vs-integer/Decimal parity mismatches (`0 / 2,000` Python `Decimal` cross-check); `11,888` BUY (`1.788%`) and `11,754` SELL (`1.768%`) exact ties uniformly spread across days; symmetric `half_tick_buffer (+ \$0.005)` variant yields identical `13.89%` `NEUTRAL` for both BUY and SELL and is included in the expanded Micro Parquet alongside frozen G3.2 `strict_gt`.
- **Clean-Room & Adversarial Verification:**
  - [g3_3_independent_reconciliation.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_independent_reconciliation.json) (ZERO `src/goldedge/` imports): **`PASS`** (`0` hash, fold, preprocessing, tie, duplicate, or expanded-6m mismatches).
  - [g3_3_adversarial_test_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_adversarial_test_results.json): **`13 / 13` PASS** (including all 3 adjacent split boundary leakage tests).
  - [g3_3_regression_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_regression_results.json): **`9 / 9` suites PASS**; `SHADOW_MODE = True`, `TRADING_MODE = "demo"`, `HWM = 8.0%`, `market_orders = 0`, `market_trades = 0`, frozen H0/H3b & SHA-256 intact.

---

## 5. Deliverables Index (`backtest_results/audit/task_g3_3_leakage_proof_pipeline/`)

1. [g3_3_historical_expansion_report.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_historical_expansion_report.json) (`HISTORY_EXPANSION_VERIFIED`)
2. [g3_3_dataset_manifest.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_dataset_manifest.json)
3. [g3_3_feature_contract.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_feature_contract.json)
4. [g3_3_walkforward_split_manifest.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_walkforward_split_manifest.json)
5. [g3_3_preprocessing_audit.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_preprocessing_audit.json)
6. [g3_3_discrete_tick_stability.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_discrete_tick_stability.json)
7. [g3_3_dependence_report.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_dependence_report.json)
8. [g3_3_evaluation_contract.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_evaluation_contract.json)
9. [g3_3_holdout_governance.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_holdout_governance.json)
10. [g3_3_independent_reconciliation.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_independent_reconciliation.json)
11. [g3_3_adversarial_test_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_adversarial_test_results.json)
12. [g3_3_regression_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_regression_results.json)
13. [g3_3_final_report.md](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_3_leakage_proof_pipeline/g3_3_final_report.md)

**Ready for Next Step:** Proceed to **Task G3.4 — Regularized Logistic Regression & LightGBM Training, Feature Ablation & Walk-Forward Comparison** on the verified `6.2-month` dataset.
