# TASK G3.4 — GoldEdge Regularized Macro/Micro ML Training, Feature Ablation & Walk-Forward Verification

**Project:** GoldEdge — `XAUUSD` Spot Gold CFD  
**Task:** `G3.4` — Regularized ML Model Training, Feature Ablation & Independent Walk-Forward Verification  
**Previous:** `G3.3` — Leakage-Proof ML Training Pipeline & Historical Expansion (`VERIFIED`)  
**Next:** `G3.5` — Probability Calibration, Statistical Robustness & Model Selection  
**Generated (UTC):** `2026-11-23T09:51:24Z`

---

## 1. Executive Verdicts (§13 Mandatory Separation)

| Dimension | Verdict | Evidence-Based Summary |
| :--- | :---: | :--- |
| **Engineering & Leakage Control** | **`VERIFIED`** | Exact `6.2`-month G3.3 dataset SHA-256 verified (`36,833` Macro / `1,278,171` Micro rows); strict 4-way purged expanding walk-forward (`TRAIN -> INNER VAL -> CALIBRATION [untouched for G3.5] -> OOT TEST`); train-only preprocessing; `112` hyperparameter trials logged; `16` versioned `.joblib` artifacts saved; clean-room independent validator (zero `src/goldedge/` imports) `PASS` with `0` metric mismatches and `(0, 0)` duplicate predictions; `13/13` adversarial & shuffled-target negative-control tests `PASS`; `10/10` regression suites `PASS`. |
| **Macro (`15m` `net_class_010`) Predictive Signal** | **`NO_DEMONSTRATED_EDGE`** *(Directional)* | Pooled OOT log-loss beats train-prevalence `B1` (`0.87804 -> 0.84017`, day-block 95% CI `[-0.0454, -0.0238]`) across all 4 folds, **but** conditional directional `UP vs DOWN` ROC-AUC on moving bars (`n = 5,471`) is **`0.49591` (`B3`)** and **`0.50018` (`C1`)** (pure coin flip). `95.6%–98.7%` of argmax predictions collapse to `FLAT`; ablation proves ONLY `session_spread_activity` (`+0.00709`) and `volatility_atr_bb` (`+0.00370`) drive the log-loss gain. H3b quintile expectancy is negative across all `p_UP` quintiles and **inverted** across `p_DOWN` quintiles. **Confirmed volatility/movement-magnitude signal (`1 - P(FLAT)`), zero demonstrated directional alpha.** |
| **Micro (`30-tick` Entry Timing) Predictive Signal** | **`INCONCLUSIVE`** | Pooled OOT log-loss beats `B1` across all 4 folds (`0.97965 -> 0.96510`, day-block 95% CI `[-0.0158, -0.0135]`; consistent on `sell_timing_class` and `half_tick_buffer`), driven `~87%` by `spread` (`+0.01271` drop penalty; `NEUTRAL` OvR AUC `0.6389`). Conditional `ENTER_NOW vs WAIT_ADVERSE` AUC on non-neutral windows (`n = 487,654`) is **`0.50695` (`B3`)** and **`0.50604` (`C1`)**, yielding a `+$0.01277/oz` (`~0.13` pips) Q5-vs-Q1 BUY timing spread (`-$0.01109` in Q1 vs `+$0.00168` in Q5, peaking at `+$0.00534` in Q3) — statistically detectable at `N = 555k`, but very small relative to typical `\$0.07` spread. |

> [!IMPORTANT]
> **Trading Safety & Governance Locks Intact:** `SHADOW_MODE = True`, `TRADING_MODE = "demo"`, `market_orders = 0`, `market_trades = 0`, `HWM_MAX_DRAWDOWN_PCT = 8.0%`. No models were deployed, no H0/H3b strategy behavior was modified, and all `CALIBRATION` folds remain strictly untouched for `G3.5`.

---

## 2. Dataset Fingerprints, Architecture & Split Discipline

- **Verified G3.3 Input Datasets (`6.2` continuous months, `2026-05-13 .. 2026-11-19`):**
  - Macro Parquet (`36,833` rows, `21` causal features): SHA-256 `2E9A9D9B7215A50B75B6464FD1E3A17AA887BF415B5A683586E0220B045189E3`
  - Micro Parquet (`1,278,171` rows, `10` causal features): SHA-256 `7B565DFAF77D2B23B987FF8EE64952B0875287D428EE2BE39B5DAF48C6778667`
- **OOF Prediction Parquets Generated:**
  - Macro OOF (`16,113` rows, `72` unique OOT days across 4 folds): SHA-256 `6F1B7EAC369BA74B1D9CE2C33E92A12F3337CC6389B597F4760C7C00DC5166F0`
  - Micro OOF (`555,308` rows, `72` unique OOT days across 4 folds): SHA-256 `C75D989A1BA0814A5B45EAEB34DBE0931439DD1AC0E5CE18C62A82D732029D05`
- **Strict Hexagonal Architecture (`GEMINI.md`):**
  - All ML model training, preprocessing (`SimpleImputer(strategy="median")` + `StandardScaler`), inner-validation selection, and versioned serialization live exclusively in secondary adapter [model_trainer.py](file:///d:/gigs/goldedge/src/goldedge/adapters/secondary/ml/model_trainer.py).
  - Pure-Python domain layer (`src/goldedge/domain/`) untouched and free of infrastructure imports.
- **Anti-Leakage Protocol:**
  - Every fold fits imputers, scalers, and models on `TRAIN` only; selects hyperparameters (`C in {0.01, 0.1, 1.0, 10.0}` for Logistic L2; constrained shallow grid `max_depth in {2, 3}`, `num_leaves in {4, 7}`, `min_child_samples in {150, 300}` Macro / `{500, 1000}` Micro, `reg_alpha = 1.0`, `reg_lambda = 5.0`, `colsample_bytree = 0.7`, `learning_rate = 0.05` with early stopping for LightGBM) exclusively on `INNER VAL` log-loss; leaves `CALIBRATION` untouched for `G3.5`; and evaluates once on purged `OUT-OF-TIME TEST`.
  - Total hyperparameter trials logged across all 4 folds and targets: **`112`**.

---

## 3. Macro (`15m` `net_class_010`) Walk-Forward Results

### 3.1 Pooled Out-of-Time Comparison (`N = 16,113` 5m rows / `5,336` non-overlapping 15m rows, `72` OOT days)

| Model | Eval Grid | Log-Loss | Multiclass Brier | Accuracy (Naive `0.6605`) | Bal. Acc | Macro F1 | OvR AUC (`UP` / `DOWN` / `FLAT`) | Cond. `UP vs DOWN` AUC (`n=5,471`) | Day-Block LL Diff vs `B1` (`95% CI`) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `B1_TRAIN_PREVALENCE` | Pooled 5m | `0.87804` | `0.50745` | `0.66046` | `0.33333` | `0.26517` | `0.498 / 0.483 / 0.499` | — | Baseline |
| `B2_CAUSAL_MOMENTUM` | Pooled 5m | `0.89326` | `0.51400` | `0.66046` | `0.33333` | `0.26517` | `0.493 / 0.488 / 0.549` | — | `+0.01635` (`[+0.0128, +0.0199]`) |
| `B3_LOGISTIC_L2` | Pooled 5m | `0.84192` | `0.48312` | `0.65996` | `0.34658` | `0.29923` | `0.625 / 0.623 / 0.660` | **`0.49591`** | **`-0.03325`** (`[-0.0422, -0.0243]`) |
| `C1_LIGHTGBM_CONSTRAINED` | Pooled 5m | `0.84017` | `0.48203` | `0.65922` | `0.34314` | `0.29172` | `0.627 / 0.625 / 0.663` | **`0.50018`** | **`-0.03458`** (`[-0.0454, -0.0238]`) |
| `B3_LOGISTIC_L2` | Non-overlap 15m | `0.84921` | `0.48826` | `0.65573` | `0.34392` | `0.29235` | `0.622 / 0.625 / 0.657` | — | Consistent with 5m grid |
| `C1_LIGHTGBM_CONSTRAINED` | Non-overlap 15m | `0.84580` | `0.48603` | `0.65424` | `0.33995` | `0.28422` | `0.624 / 0.629 / 0.664` | — | Consistent with 5m grid |

*Single-day fragility check:* Excluding the single best OOT day leaves Macro `C1` day-block log-loss diff at `-0.03146` (`B3`: `-0.03070`), confirming the volatility-regime log-loss gain is not driven by one outlier day.

### 3.2 Per-Fold Macro Walk-Forward Stability

| Fold | OOT Test Window (UTC) | `n_train / n_val / n_test` | `B1` LL | `B3` LL | `C1` LL | `C1 - B1` LL | `C1` `FLAT` Collapse % | `C1` Test − Train LL Gap |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `1` | `2026-08-07 .. 2026-08-28` | `11,188 / 2,708 / 4,080` | `0.89038` | `0.86160` | `0.86295` | `-0.02743` | `98.65%` | `+0.00175` |
| `2` | `2026-09-04 .. 2026-09-25` | `16,628 / 2,708 / 4,051` | `0.89951` | `0.86885` | `0.86506` | `-0.03445` | `97.63%` | `+0.02818` |
| `3` | `2026-10-02 .. 2026-10-23` | `22,068 / 2,679 / 4,078` | `0.80042` | `0.74797` | `0.74382` | `-0.05660` | `98.11%` | `-0.08572` |
| `4` | `2026-10-30 .. 2026-11-19` | `27,479 / 2,706 / 3,904` | `0.92392` | `0.89155` | `0.89116` | `-0.03276` | `96.29%` | `+0.04807` |

### 3.3 Macro Session & Regime Stability (`C1` vs `B1` Exact Log-Loss)

| Slice Type | Slice | `n` | `B1` LL | `B3` LL | `C1` LL | `C1 - B1` |
| :--- | :--- | ---: | :---: | :---: | :---: | :---: |
| **Session** | `ASIA_00_07` | `5,006` | `0.92248` | `0.90693` | `0.90492` | `-0.01756` |
| **Session** | `LONDON_07_12` | `3,595` | `0.80678` | `0.78106` | `0.77881` | `-0.02797` |
| **Session** | `OVERLAP_12_17` | `3,540` | `1.03718` | `0.99254` | `0.99039` | `-0.04680` |
| **Session** | `NY_17_20` | `2,103` | `0.80193` | `0.76563` | `0.76831` | `-0.03362` |
| **Session** | `OFF_20_24` | `1,869` | `0.68025` | `0.58545` | `0.58107` | `-0.09918` |
| **Regime** | `LOW_VOLATILITY` | `4,807` | `0.74804` | `0.69030` | `0.68781` | `-0.06023` |
| **Regime** | `HIGH_VOLATILITY` | `3,225` | `1.06040` | `1.02138` | `1.02019` | `-0.04021` |
| **Regime** | `SIDEWAYS` | `2,599` | `0.88488` | `0.85809` | `0.85696` | `-0.02792` |
| **Regime** | `TRENDING_BULL` | `2,647` | `0.88220` | `0.86115` | `0.85598` | `-0.02622` |
| **Regime** | `TRENDING_BEAR` | `2,835` | `0.88084` | `0.86210` | `0.86355` | `-0.01729` |

*Diagnostic interpretation:* The largest gains occur in `OFF_20_24` (`-0.0992`) and `LOW_VOLATILITY` (`-0.0602`), where the model simply assigns higher `P(FLAT)` because quiet sessions rarely move `>= 0.10%` in 15 minutes. Conditional on a `>= 0.10%` move actually occurring, directional accuracy is `50.0%` (`AUC = 0.50018`).

---

## 4. Micro (`30-tick` Entry Timing) Walk-Forward Results

### 4.1 Pooled Out-of-Time Comparison (`N = 555,308` non-overlapping windows, `72` OOT days)

| Target | Model | Log-Loss | Multiclass Brier | Accuracy | Bal. Acc | OvR AUC (`ENTER` / `WAIT` / `NEUT`) | Cond. `ENTER vs WAIT` AUC (`n=487,654`) | Day-Block LL Diff vs `B1` (`95% CI`) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `buy_timing_class` (strict `>`) | `B1_TRAIN_PREVALENCE` | `0.97965` | `0.59970` | `0.44189` | `0.33333` | `0.499 / 0.497 / 0.504` | — | Baseline |
| `buy_timing_class` (strict `>`) | `B2_CAUSAL_MOMENTUM` | `0.99105` | `0.60722` | `0.43716` | `0.33074` | `0.495 / 0.493 / 0.500` | — | `+0.01050` (`[+0.0095, +0.0115]`) |
| `buy_timing_class` (strict `>`) | `B3_LOGISTIC_L2` | `0.96658` | `0.59492` | `0.44432` | `0.34783` | `0.533 / 0.523 / 0.633` | **`0.50695`** | **`-0.01310`** (`[-0.0141, -0.0121]`) |
| `buy_timing_class` (strict `>`) | `C1_LIGHTGBM_CONSTRAINED` | `0.96510` | `0.59446` | `0.44443` | `0.34546` | `0.533 / 0.525 / 0.639` | **`0.50604`** | **`-0.01467`** (`[-0.0158, -0.0135]`) |
| `sell_timing_class` (strict `>`) | `B1_TRAIN_PREVALENCE` | `1.00096` | `0.61051` | `0.42802` | `0.33143` | `0.498 / 0.498 / 0.502` | — | Baseline |
| `sell_timing_class` (strict `>`) | `B3_LOGISTIC_L2` | `0.98927` | `0.60582` | `0.43622` | `0.34764` | `0.534 / 0.523 / 0.618` | — | **`-0.01155`** (`[-0.0125, -0.0106]`) |
| `sell_timing_class` (strict `>`) | `C1_LIGHTGBM_CONSTRAINED` | `0.98808` | `0.60537` | `0.43580` | `0.34548` | `0.535 / 0.525 / 0.623` | — | **`-0.01287`** (`[-0.0139, -0.0118]`) |
| `buy_timing_half_tick` (`+\$0.005`) | `B1_TRAIN_PREVALENCE` | `1.00096` | `0.61051` | `0.43306` | `0.33333` | `0.498 / 0.498 / 0.503` | — | Baseline |
| `buy_timing_half_tick` (`+\$0.005`) | `B3_LOGISTIC_L2` | `0.98898` | `0.60568` | `0.43605` | `0.34710` | `0.533 / 0.524 / 0.620` | — | **`-0.01196`** (`[-0.0128, -0.0111]`) |
| `buy_timing_half_tick` (`+\$0.005`) | `C1_LIGHTGBM_CONSTRAINED` | `0.98784` | `0.60527` | `0.43566` | `0.34476` | `0.533 / 0.526 / 0.625` | — | **`-0.01320`** (`[-0.0141, -0.0123]`) |

### 4.2 Per-Fold Micro (`buy_timing_class`) Walk-Forward & Session Stability

| Fold | `n_train / n_val / n_test` | `B1` LL | `B3` LL | `C1` LL | `C1 - B1` LL | `C1` Test − Train LL Gap |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `1` | `402,846 / 88,013 / 135,701` | `0.98791` | `0.97758` | `0.97667` | `-0.01124` | `+0.00603` |
| `2` | `577,548 / 88,352 / 139,223` | `0.97633` | `0.96381` | `0.96281` | `-0.01352` | `-0.01252` |
| `3` | `763,070 / 92,057 / 133,222` | `0.98569` | `0.97139` | `0.96936` | `-0.01633` | `-0.00489` |
| `4` | `951,314 / 87,717 / 147,162` | `0.96973` | `0.95470` | `0.95274` | `-0.01699` | `-0.01911` |

*Micro BUY Session Log-Loss (`C1 - B1`):* `ASIA_00_07` (`-0.01296`, `n=168,027`), `LONDON_07_12` (`-0.01258`, `n=111,091`), `OVERLAP_12_17` (`-0.01417`, `n=167,366`), `NY_17_20` (`-0.01409`, `n=71,368`), `OFF_20_24` (`-0.03015`, `n=37,456`).

*Discrete-Tick Sensitivity (Phase G):* Comparing frozen G3.2 `strict_gt` against symmetric `half_tick_buffer (+\$0.005)` preserves identical model ordering (`C1 < B3 < B1 < B2`), nearly identical OvR ROC-AUC (`0.533 / 0.526 / 0.625` vs `0.533 / 0.525 / 0.639`), and nearly identical day-block log-loss improvement (`-0.01320` vs `-0.01467`). Conclusions are **not** an artifact of floating-point boundary tie-breaking.

---

## 5. Controlled Feature Ablation & Causal Kalman Audit (Phase E)

| Horizon | Ablated / Added Feature Group | Pooled OOT LL | Delta LL (`Ablated - Base`) | Day-Block 95% CI | Conclusion |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **Macro (`C1`)** | *Base Full (`21` features)* | `0.840065` | `0.000000` | — | Reference |
| **Macro (`C1`)** | Drop `session_spread_activity` | `0.847156` | **`+0.007091`** | `[+0.00248, +0.01019]` | **Primary driver (session/activity volatility)** |
| **Macro (`C1`)** | Drop `volatility_atr_bb` | `0.843768` | **`+0.003703`** | `[+0.00197, +0.01019]` | **Secondary driver (ATR/BB volatility)** |
| **Macro (`C1`)** | Drop `structure_regime` | `0.840515` | `+0.000450` | `[-0.00050, +0.00141]` | Negligible / noise |
| **Macro (`C1`)** | Drop `momentum_rsi_ret` | `0.840034` | `-0.000031` | `[-0.00072, +0.00127]` | Zero contribution |
| **Macro (`C1`)** | Drop `trend_ema` | `0.839380` | `-0.000684` | `[-0.00231, +0.00023]` | Slightly hurts when included |
| **Macro (`C1`)** | **Add Causal Kalman (`vel`, `accel`)** | `0.840340` | **`+0.000275`** | `[-0.00016, +0.00187]` | **No improvement (slightly worse)** |
| **Micro (`C1`)** | *Base Full (`10` features)* | `0.965101` | `0.000000` | — | Reference |
| **Micro (`C1`)** | Drop `spread` (`usd`, `bps`) | `0.977815` | **`+0.012714`** | `[+0.01192, +0.01412]` | **Dominant driver (`~87%` of gain)** |
| **Micro (`C1`)** | Drop `session_time` (`hour_sin/cos`) | `0.965684` | `+0.000583` | `[+0.00046, +0.00086]` | Tiny positive increment |
| **Micro (`C1`)** | Drop `quote_timing_intensity` | `0.965549` | `+0.000448` | `[+0.00012, +0.00054]` | Tiny positive increment |
| **Micro (`C1`)** | Drop `prior_returns` (`5t/10t/30t`) | `0.965266` | `+0.000165` | `[+0.00002, +0.00028]` | Tiny positive increment |
| **Micro (`C1`)** | Drop `volatility` (`30t_usd`) | `0.965201` | `+0.000100` | `[+0.00010, +0.00050]` | Tiny positive increment |
| **Micro (`C1`)** | **Add Causal Kalman (`vel`, `accel`)** | `0.965192` | **`+0.000091`** | `[-0.00005, +0.00019]` | **No improvement (slightly worse)** |

---

## 6. Overfitting, Raw Probability Quality & H3b Economic Relevance (Phases F, H, I)

- **Overfitting & Feature Stability (Phase F):**
  - Train-to-test log-loss gaps are small and well-controlled (`-0.086 .. +0.048` Macro, `-0.019 .. +0.006` Micro), and `B3` Logistic L2 performs nearly identically to `C1` Constrained LightGBM (`0.8419` vs `0.8402` Macro; `0.9666` vs `0.9651` Micro), indicating no tree-memorization overfitting.
  - LightGBM gain-importance Spearman rank correlation between Fold 1 and Fold 4 is **`0.8464`** (Macro) and **`0.8545`** (Micro).
- **Raw Uncalibrated Probability Diagnostics (Phase I — `CALIBRATION` folds untouched for `G3.5`):**
  - 10-bin Expected Calibration Error (`ECE`): Macro `B3 = 0.0113`, `C1 = 0.00874`; Micro BUY `B3 = 0.00519`, `C1 = 0.00473`.
  - Class-conditional means closely match empirical rates (e.g., Macro `C1` `FLAT` mean `0.6614` vs empirical `0.6605`), but probability spreads for directional classes remain compressed (`p_UP` p10–p90 = `0.079–0.249`; Micro `p_ENTER_NOW` p10–p90 = `0.395–0.470`).
- **H3b Economic Relevance Quintiles (Phase H — Research-Only, NOT a `RunBacktestUseCase` portfolio backtest):**

| Model / Quintile | Macro `C1` `p_UP` → BUY `15m` Net (bps) / H3b Sim `R` | Macro `C1` `p_DOWN` → SELL `15m` Net (bps) / H3b Sim `R` | Micro `C1` `p_ENTER_NOW` → BUY Timing Adv (`\$/oz`) / Emp `ENTER_NOW %` |
| :--- | :---: | :---: | :---: |
| `Q1_LOW` | `-0.782 bps` / `-0.0905R` | `-0.111 bps` / `+0.0373R` | `-\$0.01109/oz` / `38.77%` |
| `Q2` | `-0.948 bps` / `-0.1012R` | `-0.070 bps` / `+0.0173R` | `-\$0.00311/oz` / `42.43%` |
| `Q3` | `-0.369 bps` / `-0.0774R` | `-0.096 bps` / `+0.0119R` | `+\$0.00534/oz` / `44.47%` |
| `Q4` | `-0.818 bps` / `-0.0330R` | `-0.433 bps` / `+0.0046R` | `+\$0.00333/oz` / `45.71%` |
| `Q5_HIGH` | `-0.177 bps` / `-0.0575R` | `-0.473 bps` / `-0.0112R` | `+\$0.00168/oz` / `46.76%` |

---

## 7. Independent Verification, Adversarial Tests & Regression Proof (Phase J)

- **Clean-Room Independent Validator** ([task_g3_4_phase_j_independent.py](file:///d:/gigs/goldedge/scripts/audit/task_g3_4_phase_j_independent.py)):
  - AST-verified **ZERO `src/goldedge/` imports**.
  - Independently re-verified dataset & OOF SHA-256 hashes, simplex constraints (`max_err <= 1e-6`), duplicate timestamps (`(0, 0)`), and all `8` model metric vectors (Macro + Micro `B1/B2/B3/C1`) with **`0` metric mismatches** (`max_diff = 0.0`) and frozen baseline checksums intact.
- **Adversarial & Shuffled-Target Negative Controls (`13/13 PASS` in [g3_4_adversarial_test_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_adversarial_test_results.json)):**
  - `01_shuffled_target_negative_control`: `PASS` (`chance_ll = 0.8273`, `lr_shuf = 0.8310`, `lgb_shuf = 0.8278` — zero phantom edge on shuffled labels).
  - `02..11`: Leaked global scaler, future features, wrong fold event-end, label-column leakage, schema mismatch, duplicates, probability ordering, incomplete label windows, exact price-tick threshold, and session-gap/quarantine all `PASS`.
  - `12_model_reload_prediction_parity`: `PASS` (`max_diff = 5.00e-07`).
  - `13_fixed_seed_reproducibility`: `PASS` (`max_diff = 0.00e+00`).
- **Full Regression Suite (`10/10 PASS` in [g3_4_regression_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_regression_results.json)):** `test_task_g3_4_validation.py`, `test_task_g3_3_validation.py`, `test_task_g3_2_validation.py`, `test_task_g3_1_validation.py`, `test_task_g2_validation.py`, `test_task_g1_validation.py`, `test_remediation_b1_b6.py`, `test_shadow_validation.py`, `test_task6c_validation.py`, `test_task8_hwm_kill_switch.py`.

---

## 8. Mandatory §16 Questions — Direct Answers & Recommendations for `G3.5`

1. **Do Macro models show directional predictive edge?** **No (`NO_DEMONSTRATED_EDGE`).** They accurately predict volatility/movement magnitude (`1 - P(FLAT)`), which lowers multiclass log-loss by `-0.038`, but conditional `UP vs DOWN` AUC is `0.500` and H3b directional quintiles are negative/inverted.
2. **Do Micro models show execution-timing predictive edge?** **`INCONCLUSIVE`.** Most log-loss gain (`~87%`) comes from spread predicting the `NEUTRAL` band; conditional `ENTER_NOW vs WAIT_ADVERSE` AUC is `0.5065` with a `+\$0.0128/oz` Q5-vs-Q1 spread — far smaller than typical `\$0.07` spread.
3. **Did causal Kalman features improve either model?** **No.** Adding `_kalman_vel_causal` and `_kalman_accel_causal` slightly degraded both Macro (`+0.000275` LL) and Micro (`+0.000091` LL).
4. **Is there evidence of overfitting or leakage?** **No.** Shuffled-target negative controls collapse to chance, clean-room reconciliation shows `0` mismatches, train-test gaps are small, and `B3` Logistic L2 matches `C1` LightGBM closely.
5. **What should `G3.5` focus on?**
   - **Do NOT deploy Macro as a directional classifier.** In `G3.5`, test whether (a) Macro's confirmed **volatility/activity probability (`1 - P(FLAT)`)** is useful as a calibrated regime/chop filter for H3b, and (b) whether calibrating Micro probabilities on the reserved `CALIBRATION` folds yields any positive net execution-timing utility after accounting for missed fills and spread.

---

## 9. Complete Deliverable Index (`16/16` Present)

All deliverables are stored in `backtest_results/audit/task_g3_4_ml_model_research/`:
1. [g3_4_training_manifest.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_training_manifest.json) (`16` versioned model SHA-256s, `112` trials)
2. [g3_4_model_configurations.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_model_configurations.json)
3. [g3_4_baseline_comparison.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_baseline_comparison.json)
4. [g3_4_macro_walkforward_metrics.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_macro_walkforward_metrics.json)
5. [g3_4_micro_walkforward_metrics.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_micro_walkforward_metrics.json)
6. [g3_4_macro_oof_predictions.parquet](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_macro_oof_predictions.parquet)
7. [g3_4_micro_oof_predictions.parquet](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_micro_oof_predictions.parquet)
8. [g3_4_feature_ablation.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_feature_ablation.json)
9. [g3_4_overfitting_diagnostics.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_overfitting_diagnostics.json)
10. [g3_4_probability_diagnostics.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_probability_diagnostics.json)
11. [g3_4_discrete_tick_sensitivity.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_discrete_tick_sensitivity.json)
12. [g3_4_h3b_economic_relevance.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_h3b_economic_relevance.json)
13. [g3_4_independent_reconciliation.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_independent_reconciliation.json)
14. [g3_4_adversarial_test_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_adversarial_test_results.json)
15. [g3_4_regression_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_regression_results.json)
16. [g3_4_final_report.md](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_final_report.md)
17. [g3_4_macro_raw_endpoint_control.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_macro_raw_endpoint_control.json) *(Issue 1 raw `0.10%` endpoint control)*
18. [g3_4_macro_raw_endpoint_oof.parquet](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_4_ml_model_research/g3_4_macro_raw_endpoint_oof.parquet)

---

## 10. Corrective Addendum — Issue 1 Raw Endpoint Control & Research-Exposure Limitations

### 10.1 Issue 1 Fix: Raw `0.10%` Endpoint-Direction Control (`raw_class_010` vs `net_class_010`)
- **Target Verification (`36,833` verified G3.3 rows):**
  - `raw_class_010`: `UP = 6,394 (17.36%)`, `DOWN = 6,887 (18.70%)`, `FLAT = 23,552 (63.94%)`.
  - `net_class_010`: `UP = 6,210 (16.86%)`, `DOWN = 6,689 (18.16%)`, `FLAT = 23,934 (64.98%)`.
  - **Row-level agreement:** **`98.963%`** (`36,451 / 36,833` identical; `382` borderline rows move from `UP`/`DOWN` to `FLAT` after execution friction).
- **Walk-Forward Side-by-Side Control (Exact same 4 purged folds, `N = 16,113` OOT rows, `72` OOT days, strictly inner-val selected, zero OOT retuning, `CALIBRATION` untouched; `0` duplicates, `max_simplex_err <= 1e-6`):**

| Metric (Pooled 5m OOT) | `raw_class_010` (Raw `0.10%` Endpoint) | `net_class_010` (Cost-Aware `0.10%`) | Interpretation |
| :--- | :---: | :---: | :--- |
| `B1_TRAIN_PREVALENCE` Log-Loss | `0.89155` | `0.87804` | Slightly higher entropy on raw (fewer `FLAT`) |
| `B3_LOGISTIC_L2` Log-Loss | `0.85591` | `0.84192` | Identical gain over `B1` (`-0.03335` vs `-0.03325` day-block) |
| `C1_LIGHTGBM_CONSTRAINED` Log-Loss | `0.85394` | `0.84017` | Identical gain over `B1` (`-0.03487` vs `-0.03458` day-block) |
| `B3` Cond. `UP vs DOWN` ROC-AUC | **`0.49720`** | **`0.49591`** | **Pure coin flip on both targets** |
| `C1` Cond. `UP vs DOWN` ROC-AUC | **`0.50033`** | **`0.50018`** | **Pure coin flip on both targets** |
| `C1` `FLAT` Argmax Collapse % | `97.33%` | `97.67%` | Nearly identical `FLAT` dominance |
| `C1` `p_UP` Q1..Q5 `15m` Net Return | `-0.714 .. -0.334 bps` (all `< 0`) | `-0.782 .. -0.177 bps` (all `< 0`) | No positive BUY quintile on either target |
| `C1` `p_DOWN` Q1..Q5 `15m` Net Return | `-0.213` (Q1) → `-0.422 bps` (Q5) | `-0.111` (Q1) → `-0.473 bps` (Q5) | Inverted on both targets |

**Conclusion:** Switching from `net_class_010` to `raw_class_010` changes **zero** substantive conclusions. Macro models capture a reproducible **activity/volatility signal (`1 - P(FLAT)`)**, not directional `UP vs DOWN` alpha.

### 10.2 Explicit Governance Acknowledgments (Issues 2 & 3 + Kalman Scope + `G3.5` Direction)
- **Micro Economic Weakness (Issue 2):** The `+\$0.01277/oz` Q5-vs-Q1 BUY timing spread is tiny relative to the `~\$0.07/oz` median spread, and non-monotonic (`Q3 = +\$0.00534/oz` > `Q5 = +\$0.00168/oz`), confirming no strong monotonic economic benefit yet.
- **Research-Exposure Limitation (Issue 3):** Because `112` hyperparameter trials and ablation comparisons have been inspected on historical OOT folds, those historical OOT metrics are **research-exposed** and cannot serve as final prospective confirmation (which requires frozen unseen forward shadow validation in `G3.7`).
- **Kalman Scope Clarification:** The `+0.000275` (Macro) / `+0.000091` (Micro) log-loss degradation applies **only** to causal Kalman *prediction features*, not GoldEdge's existing Kalman exit logic (which remains untouched).
- **Updated `G3.5` Research Mandate:** (1) Test calibrated Macro movement probability **`1 - P(FLAT)`** as a low-activity/chop filter for H3b (NOT as a directional BUY/SELL classifier), and (2) test calibrated Micro adverse-entry risk against actual execution decisions including missed opportunities and waiting cost.

