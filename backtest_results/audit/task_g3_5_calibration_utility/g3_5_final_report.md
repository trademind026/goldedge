# TASK G3.5 — GoldEdge Probability Calibration, H3b Economic Utility & Statistical Robustness Research

**Project:** GoldEdge — `XAUUSD` Spot Gold CFD  
**Task:** `G3.5` — Probability Calibration, H3b Economic Utility & Statistical Robustness  
**Previous:** `G3.4` — Regularized Macro/Micro Model Research (`ENGINEERING VERIFIED`, Macro directional `NO_DEMONSTRATED_EDGE`, Micro `INCONCLUSIVE`)  
**Next:** **`G3.6` NOT AUTHORIZED** (No ML model demonstrated incremental value over frozen H3b or simple non-ML baselines)  
**Generated (UTC):** `2026-11-23T10:47:26Z`

---

## 1. Executive Verdicts (§14 Mandatory Separation)

| Dimension | Verdict | Evidence-Based Summary |
| :--- | :---: | :--- |
| **Engineering & Leakage Control** | **`VERIFIED`** | All `2` G3.3 datasets, `16` G3.4 models, and `16` G3.5 calibrators SHA-256 verified; strict `CALIBRATION`-only fitting & selection across all 4 purged folds; `0` causal timestamp violations on H3b candidate ledger; clean-room independent validator (AST-verified zero `src/goldedge/` imports) `PASS` with `0` metric mismatches; `14/14` adversarial fault-injection tests `PASS`; `11/11` regression suites `PASS`. |
| **Macro (`P_MOVE = 1 - P_FLAT`) Economic Value** | **`NO_INCREMENTAL_VALUE`** | Rejecting the lowest `20%` calibrated `P_MOVE` (thresholds set strictly on `CALIBRATION`) **significantly harmed** H3b: `C1` rejected **`13` wins vs `1` loss** (`-5.17 R`, day-block 95% CI `[-0.0760, -0.0070]`, BH-FDR `q = 0.00575`); `B3` rejected **`10` wins vs `1` loss** (`-5.83 R`, `q = 0.00575`). No Macro ML filter outperformed frozen H3b or simple `ATR`/`spread`/`combined` rules across normal, `1.5x`, or `2.0x` cost stresses. |
| **Micro Adverse-Entry Execution Value** | **`NO_INCREMENTAL_VALUE`** | Calibrated `ML_C1_CAL_WAIT10S` (`+5.08 R`, `p = 0.057`, BH-FDR `q = 0.187`) and `ML_C1_CAL_WAIT30T` (`+5.12 R`, `p = 0.075`, `q = 0.217`) fail multiple-testing correction, perform comparably to `RANDOM_MATCHED_WAIT10S` (`+2.92 R`), and trail unconditional non-ML waiting (`WAIT_5S/10S/15S/30T`, `+13.18 R` to `+21.81 R`) by `3x–4x`. Conditional `ENTER_NOW vs WAIT_ADVERSE` AUC remains `0.5061` with calibration slope `0.51`. |
| **H3b Meta-Labeling Feasibility** | **`META_LABEL_RESEARCH_NOT_FEASIBLE`** | Across the entire `6.2`-month dataset (`2026-05-17 .. 2026-11-19`), frozen H3b produced only **`215` opportunities across `87` unique days** (`BUY = 77`, `SELL = 138`; `109W / 106L`, `50.7%` WR, 95% CI `[38.4%, 63.0%]`; Kish day-clustered **`N_eff = 63.06`**). Splitting into 4-way purged folds leaves `< 120` training trades/fold — inadequate for tree meta-labeling without severe overfitting. |
| **`G3.6` Integration Authorization** | **`NOT AUTHORIZED`** | Per §11 & §15, neither Macro nor Micro ML supports actual GoldEdge integration. Frozen H0/H3b, `RunBacktestUseCase`, and existing Kalman exit logic remain untouched. |

> [!IMPORTANT]
> **Trading Safety & Governance Locks Intact:** `SHADOW_MODE = True`, `TRADING_MODE = "demo"`, `market_orders = 0`, `market_trades = 0`, `HWM_MAX_DRAWDOWN_PCT = 8.0%`. Historical OOT folds remain explicitly classified as **research-exposed / exploratory only**.

---

## 2. Phase A & B — Artifact Integrity & Isolated Probability Calibration

- **Strict `CALIBRATION`-Only Selection:** Each fold's calibrator (`RAW`, `TEMPERATURE`, `SIGMOID_OVR`, `ISOTONIC_OVR`) was selected via a chronological `60/40` internal split inside the reserved `CALIBRATION` fold (with `1e-4` parsimony tie-break preferring simpler `RAW`/`TEMPERATURE`) and refit on the full `CALIBRATION` fold. `ISOTONIC_OVR` consistently overfit correlated calibration quotes (e.g., Macro `C1` OOT log-loss degraded `0.84017 -> 0.86238`, slope `0.6205`) and was rejected by the internal selector on all folds.
- **Did Calibration Improve Reliability?** Raw G3.4 `B3` and `C1` models were already well-calibrated (`ECE_10bin = 0.0087–0.0113` Macro, `0.0047–0.0052` Micro; Macro `C1` `P_MOVE` slope `0.9883`, intercept `-0.0025`). Post-hoc calibration on 12-day calibration partitions produced **no statistically significant log-loss improvement** over raw probabilities (Macro `C1` day-block `cal - raw` = `-0.00081`, 95% CI `[-0.00241, +0.00079]`; binary `P_MOVE` = `-0.00005`, CI `[-0.00040, +0.00030]`) and did not add directional ranking information.

| Horizon / Target | Variant | Log-Loss | Brier | `ECE` (`5 / 10 / 15` bins) | ROC-AUC (`P_MOVE` or Cond.) | Cal. Slope / Intercept |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **Macro Multiclass** | `B1_TRAIN_PREVALENCE` | `0.87804` | `0.50745` | `0.0278 / 0.0278 / 0.0278` | `0.49917` | — |
| **Macro Multiclass** | `B3_LOGISTIC_L2` (`raw` → `cal`) | `0.84192` → `0.84283` | `0.48312` → `0.48370` | `0.0113` → `0.0149` (`10b`) | — | — |
| **Macro Multiclass** | `C1_LIGHTGBM` (`raw` → `cal`) | `0.84017` → `0.84043` | `0.48203` → `0.48207` | `0.0087` → `0.0093` (`10b`) | — | — |
| **Macro Binary `P_MOVE`** | `C1_LIGHTGBM` (`raw` → `cal`) | `0.60274` → `0.60280` | `0.20763` → `0.20762` | `0.0098` → `0.0107` (`10b`) | `0.66331` → `0.66348` | `0.988 / -0.003` → `1.007 / +0.014` |
| **Macro Binary `P_MOVE`** | `C1_LIGHTGBM` (`ISOTONIC_OVR`) | `0.62045` | `0.20893` | `0.0100` (`10b`) | `0.66051` | `0.621 / -0.241` *(overfit)* |
| **Micro BUY Strict** | `C1_LIGHTGBM` (`raw` → `cal`) | `0.96510` → `0.96529` | `0.59446` → `0.59452` | `0.0047` → `0.0048` (`10b`) | — | — |
| **Micro Cond. `EN vs WA`** | `C1_LIGHTGBM` (`raw` → `cal`) | `0.69312` → `0.69312` | — | — | `0.50604` → `0.50607` | `0.512 / -0.004` → `0.510 / -0.004` |

---

## 3. Phase C — Macro `P_MOVE` H3b Utility vs. Simple Non-ML Baselines (`N = 112` OOT Candidates, `39` Days)

All filter thresholds (`q10/q20/q30` or `q70/q80/q90`) were frozen strictly on each fold's reserved `CALIBRATION` partition (`hour < 15 UTC`).

| Filter Policy (`CAL`-Only Thresholds) | Retained / Rej (`Cov %`) | Rej `W / L` | Retained Mean `R` | Per-Orig-Opp Mean `R` | Total Net `R` (`Delta R`) | Cum-R MaxDD | Day-Block Paired `Delta R` (`95% CI`) | Day Perm `p` (`BH-FDR q`) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `FROZEN_H3B_UNFILTERED` | `112 / 0` (`100.0%`) | `0 / 0` | `+0.00690` | `+0.00690` | `+0.7725` (`0.000`) | `-10.91` | Baseline | `1.000` (`—`) |
| `ATR_ONLY_REJECT_LOW_20` | `100 / 12` (`89.3%`) | `8 / 4` | `-0.02846` | `-0.02541` | `-2.8459` (`-3.618`) | `-13.32` | `-0.0483` (`[-0.131, +0.035]`) | `0.288` (`0.642`) |
| `SPREAD_ONLY_REJECT_HIGH_20` | `85 / 27` (`75.9%`) | `13 / 14` | `+0.01246` | `+0.00946` | `+1.0593` (`+0.287`) | `-11.73` | `+0.0179` (`[-0.048, +0.084]`) | `0.670` (`1.000`) |
| `SIMPLE_ATR_SPREAD_COMBINED_20` | `89 / 23` (`79.5%`) | `13 / 10` | `-0.01286` | `-0.01022` | `-1.1447` (`-1.917`) | `-11.54` | `+0.0048` (`[-0.064, +0.074]`) | `0.916` (`1.000`) |
| **`MACRO_B3_CAL_PMOVE_REJECT_20`** | `101 / 11` (`90.2%`) | **`10 / 1`** | `-0.05006` | `-0.04514` | `-5.0561` (**`-5.829`**) | `-15.90` | `-0.0456` (`[-0.088, -0.003]`) | `0.0005` (`0.0058` *harm*) |
| **`MACRO_C1_CAL_PMOVE_REJECT_20`** | `98 / 14` (`87.5%`) | **`13 / 1`** | `-0.04485` | `-0.03925` | `-4.3957` (**`-5.168`**) | `-15.60` | `-0.0415` (`[-0.076, -0.007]`) | `0.0005` (`0.0058` *harm*) |
| `MACRO_C1_CAL_PMOVE_REJECT_30` | `84 / 28` (`75.0%`) | `21 / 7` | `-0.02826` | `-0.02119` | `-2.3734` (`-3.146`) | `-13.01` | `+0.0107` (`[-0.075, +0.096]`) | `0.811` (`1.000`) |

- **Why Macro `P_MOVE` Failed:** H3b already screens out dead bars via its existing score/regime/EMA gates. Rejecting the calmest `10%–25%` of remaining H3b signals systematically discarded **winning trades** (`13W / 1L` for `C1_20`, hurting all 4 folds `-0.01R` to `-2.14R`, both BUY `-0.03R` and SELL `-5.14R`, and `LONDON_07_12` `-5.17R`). Direct paired tests confirm `C1_PMOVE_20` offers zero incremental gain over `ATR_20` (`p = 0.884`) or `SIMPLE_COMBINED_20` (`p = 0.234`).
- **Cost Stress (`1.0x / 1.5x / 2.0x`):** Under `1.5x` moderate stress, frozen H3b drops to `-0.194 R` while `MACRO_C1_PMOVE_20` worsens to `-5.22 R` (`delta = -5.03 R`), confirming the negative result persists identically under stressed execution friction.

---

## 4. Phase D — Micro Adverse-Entry Risk & Execution Timing (`N = 112` OOT Candidates)

Every candidate was simulated on ordered raw bid/ask quotes from `v_market_ticks_expanded_6m_v1` with recalculated executable entry (`ask` BUY / `bid` SELL), Fusion Markets commission & slippage, recalculated `SL`/`TP`/lot-size/`E1`/timeout, session cutoff (`15:00 UTC`), and `> 0.5 ATR` signal invalidation.

| Execution Policy (`ALL`, `N = 112`) | Wait Rate | Missed Fills (`W / L`) | Mean Px Imp (`\$/oz`) | Policy Mean `R` (`Imm = -0.1162`) | Delta Total `R` | Day-Block `Delta R` (`95% CI`) | Day Perm `p` (`BH-FDR q`) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `WAIT_5S_ALL` *(unconditional)* | `100.0%` | `21` (`6 / 15`) | `+\$0.0593` | `+0.0359` | `+17.04` | `+0.1986` (`[+0.060, +0.337]`) | `0.0085` (`0.0402`) |
| `WAIT_10S_ALL` *(unconditional)* | `100.0%` | `23` (`8 / 15`) | `+\$0.1026` | `+0.0015` | `+13.18` | `+0.2078` (`[+0.054, +0.361]`) | `0.0105` (`0.0402`) |
| `WAIT_15S_ALL` *(unconditional)* | `100.0%` | `27` (`10 / 17`) | `+\$0.1128` | `+0.0785` | `+21.81` | `+0.2896` (`[+0.136, +0.443]`) | `0.0010` (`0.0077`) |
| `WAIT_30_TICKS_ALL` *(unconditional)* | `100.0%` | `23` (`8 / 15`) | `+\$0.0724` | `+0.0343` | `+16.86` | `+0.2101` (`[+0.060, +0.360]`) | `0.0095` (`0.0402`) |
| `SIMPLE_SPREAD_WAIT10S` | `15.2%` | `3` (`1 / 2`) | `+\$0.0700` | `-0.1215` | `-0.59` | `-0.0027` (`[-0.028, +0.023]`) | `0.8436` (`1.0000`) |
| `SIMPLE_MOMENTUM_WAIT10S` | `18.8%` | `1` (`1 / 0`) | `+\$0.0315` | `-0.1267` | `-1.17` | `-0.0054` (`[-0.024, +0.013]`) | `0.7701` (`1.0000`) |
| `RANDOM_MATCHED_WAIT10S` (`seed=42`) | `25.0%` | `6` (`2 / 4`) | `+\$0.0877` | `-0.0902` | `+2.92` | `+0.0304` (`[-0.028, +0.089]`) | `0.3288` (`0.6609`) |
| **`ML_C1_CAL_WAIT10S`** | `25.0%` | `7` (`2 / 5`) | `+\$0.2052` | `-0.0709` | `+5.08` | `+0.1115` (`[+0.001, +0.223]`) | `0.0570` (`0.1872`) |
| **`ML_C1_CAL_WAIT30T`** | `25.0%` | `6` (`2 / 4`) | `+\$0.1982` | `-0.0705` | `+5.12` | `+0.0873` (`[-0.012, +0.187]`) | `0.0755` (`0.2170`) |

- **Critical Diagnostic Interpretation:**
  1. Neither Micro ML timing policy survives BH-FDR (`q = 0.187` and `0.217`), and waiting on `25%` of entries yields `+5.08 R` vs. `+2.92 R` for random waiting (`seed=42`) — confirming **no demonstrated Micro ML edge**.
  2. The apparent `+13 R` to `+21 R` gain from unconditional waiting (`WAIT_5S/10S/15S/30T`) is an **exploratory non-ML observation**, driven largely by the `> 0.5 ATR` fast-move invalidation guard skipping `15–17` fast adverse spikes vs. `6–10` wins (and note that Phase D's tick-level `E1` check gives immediate mean `-0.1162 R` vs. `RunBacktestUseCase`'s minute-bar `E1` check `+0.0069 R`, so any future study of unconditional delayed entry / fast-spike invalidation would require a separately approved `RunBacktestUseCase` verification, NOT ML integration).

---

## 5. Phase E — H3b Meta-Labeling Feasibility (`6.2` Months: `2026-05-17 .. 2026-11-19`)

- **Total Frozen H3b Opportunities:** **`215`** across **`87` unique trading days** (`2.47` opportunities/active day; `0` overlapping concurrent trades).
- **Direction & Outcome Split:** `BUY = 77`, `SELL = 138`; `109` wins / `106` losses (`50.70%` WR, effective 95% CI `[38.36%, 63.04%]`); mean Net R = `+0.02725`, day-block mean Net R = `-0.04021` (`95% CI [-0.1907, +0.1103]`).
- **Regime Distribution:** `TRENDING_BEAR = 80`, `TRENDING_BULL = 75`, `SIDEWAYS = 58`, `LOW_VOLATILITY = 2`.
- **Day-Clustered Kish Effective Sample Size:** **`N_eff = 63.06`**.
- **Verdict:** **`META_LABEL_RESEARCH_NOT_FEASIBLE`** on the current `6.2`-month dataset. Purged 4-way splits leave `< 120` training trades per fold, far below the sample size required to train and validate a meta-labeler without severe overfitting.

---

## 6. Phase H — Independent Clean-Room Verification, Adversarial Tests & Regression

- **Clean-Room Validator** ([task_g3_5_phase_h_independent.py](file:///d:/gigs/goldedge/scripts/audit/task_g3_5_phase_h_independent.py)): AST-verified **ZERO `src/goldedge/` imports**; `PASS` with **`0` metric mismatches**, `0` duplicate timestamps, `0` causal violations, `max_simplex_err = 0.0`, all `16` calibrator hashes verified, and frozen `111`-trade & `70`-trade SHA-256s intact.
- **Adversarial Tests (`14/14 PASS` in [g3_5_adversarial_test_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5_calibration_utility/g3_5_adversarial_test_results.json)):** All 14 fault injections (future labels, class alignment/simplex, artifact hashes, invalid/inverted quotes, out-of-order timestamps, future Micro label features, BUY/SELL polarity, missed fills/session cutoff, commission/slippage, no double-counting, delayed SL/TP recalculation, zero partition overlap, `CAL`-only calibration selection, and `E1` exit path) passed.
- **Full Regression Suite (`11/11 PASS` in [g3_5_regression_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5_calibration_utility/g3_5_regression_results.json)):** `test_task_g3_5_validation.py` through `test_task8_hwm_kill_switch.py`.

---

## 7. Mandatory §14 Final Questions — Direct Answers

1. **Did calibration improve probability reliability?** No statistically significant gain; raw G3.4 models were already well-calibrated (`ECE < 0.012`), and `ISOTONIC_OVR` overfit correlated quotes.
2. **Does Macro improve H3b beyond ATR, spread and session filters?** **No (`NO_INCREMENTAL_VALUE`).** Macro `P_MOVE` significantly **hurt** H3b (`-5.17 R`, `13W / 1L` rejected, `q = 0.0058`).
3. **Does Micro improve execution after missed opportunities and costs?** **No (`NO_INCREMENTAL_VALUE`).** Micro ML timing failed BH-FDR (`q = 0.187`) and was outperformed `3x–4x` by unconditional non-ML waiting.
4. **How many independent H3b opportunities were evaluated?** `112` OOT opportunities across `39` unique OOT days (Phases C & D), and `215` total `6.2`-month opportunities across `87` unique days (`N_eff = 63.06`, Phase E).
5. **Are any ML gains stable across trading days and market regimes?** **No.** Macro `P_MOVE` lost money across all 4 folds, both directions, and all active regimes.
6. **Do ML gains survive cost stress and multiple-testing adjustments?** **No.** Zero ML policies survived BH-FDR (`0/23` positive ML survivors) or `1.5x`/`2.0x` cost stress.
7. **Were any leakage or calculation defects found and repaired?** Zero leakage defects; one test-path defect in `test_task_g3_5_validation.py` (`ROOT` resolution inside `/tmp`) was repaired and re-verified (`11/11 PASS`).
8. **Do results justify a separately approved `G3.6` ML implementation?** **No — `G3.6` is NOT authorized.**
9. **What precisely failed and what should be investigated next?** Both general-bar Macro (`AUC ~ 0.50` direction; `P_MOVE` rejects clean H3b winners) and 30-tick Micro (`AUC ~ 0.506`, fails FDR vs. unconditional waiting) failed to add incremental value to H3b. Recommended next steps (for separate approval): (a) continue frozen shadow accumulation (`G3.7`) to build a larger prospective H3b sample, (b) evaluate the **non-ML** observation from Phase D (whether a simple `5s–15s` confirmation delay / `> 0.5 ATR` fast-spike invalidation guard holds up in actual `RunBacktestUseCase` without ML), or (c) backfill `12–24` months of cTrader ticks before revisiting low-DOF H3b meta-labeling.

---

## 8. Deliverable Index (`11/11` Mandatory + Supporting Ledgers & Calibrators)

Stored in `backtest_results/audit/task_g3_5_calibration_utility/`:
1. [g3_5_calibration_integrity.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5_calibration_utility/g3_5_calibration_integrity.json)
2. [g3_5_probability_calibration.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5_calibration_utility/g3_5_probability_calibration.json)
3. [g3_5_macro_h3b_utility.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5_calibration_utility/g3_5_macro_h3b_utility.json)
4. [g3_5_micro_execution_utility.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5_calibration_utility/g3_5_micro_execution_utility.json)
5. [g3_5_meta_label_feasibility.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5_calibration_utility/g3_5_meta_label_feasibility.json)
6. [g3_5_robustness_report.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5_calibration_utility/g3_5_robustness_report.json)
7. [g3_5_experiment_governance.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5_calibration_utility/g3_5_experiment_governance.json)
8. [g3_5_independent_reconciliation.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5_calibration_utility/g3_5_independent_reconciliation.json)
9. [g3_5_adversarial_test_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5_calibration_utility/g3_5_adversarial_test_results.json)
10. [g3_5_regression_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5_calibration_utility/g3_5_regression_results.json)
11. [g3_5_final_report.md](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5_calibration_utility/g3_5_final_report.md)

Supporting artifacts: `16` versioned calibrators (`calibrators/*.joblib` + `.meta.json`), `g3_5_macro_calibrated_{cal,oof}.parquet`, `g3_5_micro_calibrated_{cal,oof}.parquet`, `g3_5_h3b_all_6m_trades.parquet`, `g3_5_h3b_candidate_ledger.parquet`, and `g3_5_micro_execution_ledger.parquet`.
