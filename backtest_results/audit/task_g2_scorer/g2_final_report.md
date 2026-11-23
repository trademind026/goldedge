# Task G2 — GoldEdge `TradeScorer` Root-Cause Analysis, Calibration & Independent Verification Report

**Task ID:** G2 — `TradeScorer` Calibration  
**Symbol & Environment:** `XAUUSD` Spot Gold CFD (`SHADOW_MODE = True`, `TRADING_MODE = "demo"`, `HWM_MAX_DRAWDOWN_PCT = 8.0`, broker orders `= 0`, broker trades `= 0`)  
**Overall Verdict (`§12`):** **`PARTIALLY VERIFIED`**
- **Engineering, Source Defect Fixes, Zero-Discrepancy Application Reconciliation & Safety Locks:** **`VERIFIED`** (`0` trade/price/cost/PnL/MTM-DD/HWM discrepancies across `H0`, `H3b`, `G2`, and all `2,482` candidate rows; PostgreSQL & REST API verified; `42/42` unit & regression tests passing across `6/6` suites; `npx tsc --noEmit` exit `0`).
- **Trading Profitability & Live/Demo Execution Readiness:** **`STATISTICALLY UNPROVEN — NOT PRODUCTION-READY`** (`G2` / `g2_calibrated` turns score calibration positive and outperforms `H0` in unprotected counterfactual replay, but fails Benjamini–Hochberg FDR significance `q_vs_H0 = 0.764–0.953`, loses `-$441.09` in `VAL`, and triggers the `8%` HWM kill switch on `2026-10-28`; `H3b` remains the strongest historical research benchmark and is research-only).

---

## 1. Reproducibility & Environment Fingerprint

| Item | Verified Value |
| :--- | :--- |
| **Git Base Revision (Pre-G2 Edits)** | `1bf6c45b3f5b790e3212c9b5b7a683d6fd5e3622` |
| **Frozen Baseline Artifacts (SHA-256)** | `frozen_111_baseline_trades.csv`: `73306E1529ADC48284BCE9AB6AC35122F33B9999D2464AF441867EB3BB6CF168`<br>`g1_original_70_baseline_trades.csv`: `27200EEA2C7FF4842FC586DD1131EE450247D0BCCBCF3E2F77ECA6952D2CCDC4` |
| **Dataset Span (`market_ticks`)** | `19,959,291` ticks (`2026-08-13T00:00:00.153000+00:00` .. `2026-11-19T11:45:39.744483+00:00`)<br>`daily_counts_sha256`: `2a8a46fe7aa8cb6fc5e57843e7bc3e0b6d643c4821e211589b22cd54a18b7561` |
| **Warmup & Split Windows (UTC)** | 4-day causal warmup prior to each split start:<br>- `DEV`: `2026-08-17` .. `2026-09-25` (`35` market days, unexposed)<br>- `VAL`: `2026-09-26` .. `2026-10-25` (`25` market days, unexposed)<br>- `HO_EXPOSED`: `2026-10-26` .. `2026-11-19` (`22` market days, **research-exposed**)<br>- `FULL`: `2026-08-17` .. `2026-11-19` (`82` market days) |
| **Execution & Cost Assumptions** | Profile `MEDIUM_BACKTEST` (`1.0%` risk/trade, `SL = 1.5x ATR`, `TP = 3.0x ATR`, `min_score = 0.60`, `block_high_volatility = True`, `block_buy_sideways = True`, `use_kalman_exit = True`, `session_close_utc = 15`, `early_fail = 15m < 0.4 ATR`), Fusion cTrader demo account `48439552` (`commission_profile = "fusion_ctrader_demo"`, `slippage_pips = 0.0533`, `apply_swap = True`, `max_hwm_drawdown_pct = 8.0`). |

---

## 2. Which Scoring Defects Were Confirmed? (Phase A Source Audit & Phase C)

Complete audit of `src/goldedge/domain/services/trade_scorer.py`, `indicator_service.py`, `denoise_service.py`, `regime_detector.py`, `signal_generator.py`, `run_backtest.py`, and `scan_market.py` confirmed **six structural defects** (deliverables `g2_scoring_formula_audit.json`, `g2_feature_saturation_report.json`):

| Component | Weight | Original Mathematical Spec | Saturation (`ELIGIBLE_ALL`, n=453) | Confirmed Root Defect(s) | Independent `INDEP` IC (`rho` vs `diag_hyp_kal_r`, n=140) |
| :--- | :---: | :--- | :---: | :--- | :---: |
| **`shannon_entropy`** | `0.15` | `base=2` histogram over 10 bins on 30 × 5m `pct_change`; `_score_band(H, 2.50, 3.20, None, 3.60)` | **`100.0%` at `1.00`** (`std=0.0`, `1` unique val; `100%` across `H0_EXECUTED_68`) | **3 simultaneous bugs:** (1) theoretical max is `log2(10) = 3.321928 < bad_max=3.60` (bounded in `[0.695, 1.0]`); (2) `bad_min=None` clips structured trends to `1.0`; (3) `1m` return bin edges (`±0.00005..±0.0005`) applied to `5m` bars (`~sqrt(5)x` wider), forcing flat `+0.15` inflation everywhere. (`dev` quantiles `2.23..3.03`). | `-0.0382` (`p=0.654`); 5m norm `-0.0460` (`p=0.589`) |
| **`ema20_slope`** | `0.15` | `(ema20[-1] - ema20[-6]) / ema20[-6]` on 5m bars (`25m` horizon); monotonic `_interp(dir*s, 5e-5, 3e-4)` | **`94.26%` at `1.00`** (`mean=0.9865`; `85.29%` on `H0_EXECUTED_68`) | `1m` slope thresholds (`5e-5..3e-4`) applied to 5m (`25m`) bars where `SignalGeneratorService` already gates `|slope| > 2e-4` (unless `trr > 2.0`), pre-loading `[0.60, 1.0]` with no climax decay (`ELIGIBLE` min `0.000203`, `DEV` p50 `0.000737`, p95 `0.001441`). | `+0.0626` (`p=0.462`) |
| **`kalman_vel`** | `0.35` | 2D constant-velocity scalar Kalman `x1` on valid BID ticks (`q_p=1e-4, q_v=3e-5, r_base=1e-2, adapt=False`); `SIDEWAYS`: `[0.008,0.040]`, `TRENDING`: `[0.015,0.060]` | Bimodal: **`56.73%` at `0.00`**, **`27.81%` at `1.00`** (`H0_EXECUTED_68`: `58.82%` at `1.00`) | Unnormalized `$/tick` mixes quote arrival rate & volatility, AND `SIDEWAYS` halves bands (`0.008` vs `0.015`), boosting choppy sideways oscillations to `sub_mom_kalman=1.0` (`SIDEWAYS` `0.008..0.015`: `n=10`, `-0.3436 R`, `40.0%` SL). Over half of eligible signals open with `dir_kalman_vel <= 0` (`DEV` p50 `-0.000825`). | `+0.1244` (`p=0.143`) |
| **`tick_weighted_price_dist`** | `0.10` | `(close - TWP)/TWP`; BUY `[-0.0005, 0.003]`, SELL `[-0.003, 0.0005]` | `25.83%` at `1.00`, `36.64%` at `0.00` | **Effectively inverted** relative to empirical breakout edge: fixed fractional band saturates near `0` on strong breakouts (`dir_twp_dist > 0.0049` on `DEV`), giving `Spearman = -0.9660` between `sub_twp_dist` and `dir_twp_dist`, and `-0.1889` against forward outcome while raw `dir_twp_dist` is `+0.1855` (`p=0.02824`). Minor live mismatch in `ScanMarketUseCase:204` (`(close - twp)/close` vs `/ twp`). | Raw `+0.1855` (`p=0.028`); Subscore **`-0.1889`** |
| **`kaufman_er`** | `0.20` | 10-bar (`50m`) 5m close ER; `_score_band(er, 0.20, 0.60, 0.08, 0.90)` | `60.49%` at `1.00` (`mean=0.7864`) | Wide `1.0` plateau `[0.20, 0.60]` rewards mediocre `[0.20, 0.40]` efficiency (`n=46 INDEP`: `-0.2054 R`, `23.91%` SL) while correlating with `ema20_slope` and `dir_rsi_ext`. | `-0.0271` (`p=0.751`) |
| **`kalman_accel` & `utc_hour` / `trr`** | `±0.08` / `0.05` | Single-tick delta `clip(±2*ka, ±0.08)`; session score peak `1.0` in `12..16 UTC`; `trr_mult` `0.75/0.85` | `sub_session` `18.54%` at `1.00` | Single-tick acceleration `kalman_accel` is pure microstructure noise (`rho = -0.0607`, `p=0.476`); dead strategy branches (`kinematic_kalman_trend`, `statistical_mean_reversion`, `mean_reversion`) never trigger on active `5m` columns + `RegimeDetectorService`. | Accel `-0.0607`, Hour `+0.0221`, TRR `+0.0938` |

All components are causal at decision time (`0` look-ahead). VIF across active predictors on `INDEP_ELIGIBLE (n=140)` shows strong extension collinearity (`dir_rsi_ext = 10.32`, `dir_ema20_dist_atr = 8.405`, `kaufman_er = 2.641`, `dir_ema20_slope = 2.625`, `adx = 2.342`, `dir_twp_dist = 1.771`, `dir_kalman_vel = 1.062`).

---

## 3. What Caused High-Score (`>= 0.80`) Failures? (Phases B & C)

Reconstructing the full candidate decision funnel across `2,482` in-session signal rows (`453` top-ranked pre-score/risk setups, `140` non-overlapping 30m independent setups) explained why `legacy_score` fails above `0.80`:

- **Exact `H0` Decision Funnel (`2,482` rows, `0` unexplained):** `SCORE_BELOW_MIN = 878`, `AFTER_15UTC = 517`, `H1_TREND_GATE = 515`, `REGIME_GATE = 270`, `POSITION_OPEN = 109`, `HWM_KILL_SWITCH_8PCT = 108`, `EXECUTED = 68`, `COOLDOWN_GATE = 9`, `OUTRANKED_AT_BAR = 4`, `DAILY_LIMIT = 4`.
- **Non-Monotonic Calibration (`g2_score_calibration.csv`):**
  - `INDEP_ELIGIBLE_ALL` (`n=140`, `rho = +0.0164` overall; **`rho = -0.1705`** for `>= 0.60`):
    - `[0.60, 0.70)` (`n=24`): `+0.2140 R` (`50.0%` WR, `8.33%` SL)
    - `[0.70, 0.80)` (`n=16`): `+0.1593 R` (`56.25%` WR, `18.75%` SL)
    - `[0.80, 0.90)` (`n=23`): **`-0.2937 R`** (`95% CI [-0.5313, -0.0417]`, `34.78%` WR, `26.09%` SL)
    - `[0.90, 1.00]` (`n=13`): **`-0.2523 R`** (`53.85%` WR, `30.77%` SL)
  - `H0_EXECUTED_68` (`rho = -0.0667`, `p = 0.589`):
    - `[0.60, 0.70)` (`n=25`): `+0.0996 R` (`52.0%` WR, `12.0%` SL)
    - `[0.70, 0.80)` (`n=15`): **`+0.2361 R`** (`95% CI [+0.0771, +0.4009]`, `80.0%` WR, `0.0%` SL)
    - `[0.80, 0.90)` (`n=18`): **`-0.1857 R`** (`44.44%` WR, `33.33%` SL)
    - `[0.90, 1.00]` (`n=10`): **`-0.1052 R`** (`50.0%` WR, `20.0%` SL)
- **Failure Mechanism (`g2_exhaustion_diagnosis.csv`):**
  Because `sub_entropy` supplies a free `+0.15`, `sub_ema_slope` adds `+0.148`, and `sub_er` averages `+0.94` in high-score bars (`ELIGIBLE_GE_080`: `sub_mom_kalman = 0.982`, `sub_er = 0.939`, `sub_entropy = 1.000`, `sub_ema_slope = 0.988`), crossing `>= 0.80` simply selects **high instantaneous tick velocity** (`sub_mom_kalman -> 1.0`), disproportionately concentrating three toxic states:
  1. **Choppy `SIDEWAYS` spikes** boosted by halved `SIDEWAYS` Kalman thresholds: `SIDEWAYS & >= 0.80` (`n=17 INDEP`: `-0.3385 R`, `29.41%` SL; `n=35 ELIG`: `-0.2611 R`, `28.57%` SL vs `SIDEWAYS 0.60–0.80`: `n=12 INDEP`, `+0.5463 R`).
  2. **Stretched / climax entries (`>= 0.80` + extension):** `EMA20_EXT_GT_2_0_ATR & GE_080` (`n=7 INDEP`: **`14.29%` WR, `57.14%` SL, `-0.6637 R`**), `RSI_EXTREME & GE_080` (`n=14 INDEP`: `28.57%` WR, `35.71%` SL, `-0.4185 R`), `KALMAN_ACCELERATING & GE_080` (`n=15 INDEP`: `26.67%` WR, `-0.4246 R`).
  3. **Transition / session vulnerability:** `REGIME_LAGGED & GE_080` (`n=10`: `-0.3350 R`) and `H_09_UTC & GE_080` (`n=8`: `12.5%` WR, `37.5%` SL, `-0.6100 R`).

---

## 4. Which Fixes Were Implemented & Tested? (Phase D–F)

Nine predeclared `G2` hypotheses anchored strictly to `DEV` empirical quantiles (`p5/p50/p75/p95`) were evaluated against frozen `H0` and `H3b`. The cleanest candidate (**`G2_E_DECORRELATED`**, exposed opt-in as **`scorer_mode="g2_calibrated"`**) was wired behind an isolated default-OFF flag (`scorer_mode="legacy"` default everywhere so `H0` and `H3b` stay bit-for-bit untouched):

- [denoise_service.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/denoise_service.py): Added `DenoiseService.compute_rolling_entropy_5m_norm(returns, window=30)` using `5m`-scaled bins (`±0.00011..±0.00112`) normalized by `log2(10)` into `[0.0, 1.0]`.
- [trade_scorer.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/trade_scorer.py): Added `VALID_SCORER_MODES = ("legacy", "g2_calibrated")` in pure Python (zero `pandas`/`numpy`), implementing:
  - Normalized 1m-equiv ATR Kalman velocity `_score_band(dir * kv_1m_atr, 0.12, 0.48, -0.10, 1.00)` uniformly across all regimes (eliminates `SIDEWAYS` halving defect).
  - Entropy cleanliness `_score_band(1 - ent_5m_norm, 0.18, 1.0, 0.06, None)` (`ELIGIBLE_ALL` saturation cut `100.0% -> 14.79%`).
  - Climax-decay 5m EMA20 slope `_score_band(dir * slp, 0.00025, 0.00080, 0.00010, 0.00150)`.
  - Tighter Kaufman ER `_score_band(er, 0.20, 0.55, 0.08, 0.85)`.
  - Aligned breakout TWP distance `_score_band(dir * twd, 0.0050, 0.0150, 0.0015, None)`.
  - Zero single-tick `kalman_accel` adjustment (`0.0`).
  - Decorrelated weights `(0.20, 0.15, 0.20, 0.15, 0.25, 0.05)` + smooth EMA20 overextension dampener `ext_mult = 0.55 + 0.45 * _score_band(dir * ext_atr, -1.0, 1.80, None, 3.00)`.
- [run_backtest.py](file:///d:/gigs/goldedge/src/goldedge/application/use_cases/run_backtest.py), [backtest.py](file:///d:/gigs/goldedge/src/goldedge/adapters/primary/rest_api/routes/backtest.py), [api.ts](file:///d:/gigs/goldedge/dashboard/lib/api.ts): Wired `scorer_mode: str = "legacy"` end-to-end with `400` validation and PostgreSQL `metrics` persistence; left `ScanMarketUseCase` and live/shadow signal pipeline unchanged.
- [test_remediation_b1_b6.py](file:///d:/gigs/goldedge/tests/test_remediation_b1_b6.py), [test_task6c_validation.py](file:///d:/gigs/goldedge/tests/test_task6c_validation.py), [test_task8_hwm_kill_switch.py](file:///d:/gigs/goldedge/tests/test_task8_hwm_kill_switch.py): Added `export_artifacts=False` to prevent tests from overwriting shared `backtest_results/` artifacts (`§9`).

---

## 5. Research Comparison Across Baselines & Predeclared G2 Hypotheses (Phase D–E)

From `g2_research_comparison.json` (Fusion cTrader demo costs, Kalman exit ON, `15:00 UTC + E1`, `8%` HWM ON unless noted as counterfactual `NO_HWM`):

| Config | FULL HWM-ON (Trades, PnL, PF, DD, Rho, `q_H0`) | Counterfactual FULL NO_HWM (non-tradable) | `DEV` (`08-17..09-25`) | `VAL` (`09-26..10-25`) | `HO_EXPOSED` (`10-26..11-19`) | Cost `2.0x` FULL | Ex-Top-2 Days (HWM-ON / NO-HWM) |
| :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: |
| **`H0`** | `68 tr`, **`+$112.74`**, PF `1.06`, `8.00%` (Oct 22), `rho=-0.067` | `126 tr`, `+$397.22`, PF `1.10`, `9.14%` | `+$689.53` | `-$588.98` | `+$354.05` | `+$60.67` | `-$677.9` / `-$393.4` |
| **`H3b`** | `90 tr`, **`+$1,584.43`**, **PF `1.67`**, **`4.16%`** (No halt), `rho=+0.096` | **`90 tr`, `+$1,584.43`**, PF `1.67`, `4.16%` | `+$699.56` | **`+$227.74`** | **`+$602.65`** | **`+$1,427.60`** | **`+$816.4` / `+$816.4`** |
| `G2_A_ENTROPY_FIX` | `105 tr`, `+$253.96`, PF `1.08`, `7.49%`, `rho=-0.124`, `q=0.953` | `105 tr`, `+$253.96`, `7.49%` | `+$670.66` | `-$515.04` | `+$124.49` | `+$81.72` | `-$388.3` / `-$388.3` |
| `G2_B_SLOPE_5M_FIX` | `65 tr`, `+$245.46`, PF `1.13`, `8.01%`, `rho=-0.062`, `q=0.953` | `122 tr`, `+$183.59`, `10.88%` | `+$875.95` | `-$706.83` | `+$102.91` | `+$172.74` | `-$544.6` / `-$606.4` |
| `G2_C_KALMAN_NORM_FIX` | `72 tr`, `+$281.75`, PF `1.14`, `8.01%`, `rho=+0.086`, `q=0.953` | `144 tr`, `+$17.16`, `12.43%` | `+$693.80` | `-$680.82` | `+$112.39` | `+$193.79` | `-$545.3` / `-$809.9` |
| `G2_D_EXHAUSTION_REPLACE_TWP` | `72 tr`, `+$99.49`, PF `1.05`, `8.01%`, `rho=-0.006`, `q=0.971` | `136 tr`, `-$116.61`, `11.68%` | `+$496.11` | `-$557.25` | `-$29.85` | `+$51.54` | `-$776.8` / `-$992.9` |
| `G2_D2_TWP_BREAKOUT_FIX` | `74 tr`, `-$198.88`, PF `0.91`, `8.00%`, `rho=-0.019`, `q=0.953` | `131 tr`, `+$6.34`, `10.98%` | `+$179.51` | `-$567.96` | `+$423.84` | `-$256.44` | `-$820.8` / `-$734.0` |
| `G2_H_MINIMAL_DEFECT_ONLY` | `66 tr`, `+$199.10`, PF `1.09`, `8.00%`, `rho=+0.044`, `q=0.953` | `127 tr`, `-$284.28`, `14.60%` | `+$601.50` | `-$656.22` | `-$124.79` | `+$132.91` | `-$680.7` / `-$1,164.0` |
| `G2_E_CALIBRATED_CORE` | `67 tr`, `+$1.18`, PF `1.00`, `8.02%`, `rho=+0.196`, `q=0.953` | `111 tr`, `+$196.13`, `9.67%` | `+$715.77` | `-$627.16` | `+$161.57` | `-$64.41` | `-$672.9` / `-$623.5` |
| **`G2_E_DECORRELATED` (`g2_calibrated`)** | `68 tr`, `+$338.91`, PF `1.17`, `8.02%` (Oct 28), **`rho=+0.163`**, `q=0.953` | **`104 tr`, `+$866.71`, PF `1.29`, `8.47%`** (`P(>0)=0.821`) | **`+$987.92`** | `-$441.09` | `+$332.43` | `+$282.92` | `-$508.9` / **`+$18.9` (`PF 1.01`)** |
| `G2_F_REGIME_CONDITIONED` | `62 tr`, `+$362.86`, PF `1.21`, `8.01%` (Oct 28), **`rho=+0.254`**, `q=0.953` | `94 tr`, `+$648.76`, PF `1.24`, `8.68%` | `+$932.82` | `-$387.76` | `+$117.38` | `+$300.06` | `-$484.9` / `-$199.0` |

### Answers to Questions 4, 5, and 7 (`§11`)
- **Does `H3b` remain the strongest candidate?** **Yes.** `H3b` (`max_score_threshold=0.80`) is the only configuration that remains profitable in `VAL` (`+$227.74`, PF `1.75`) as well as `DEV` (`+$699.56`) and `HO_EXPOSED` (`+$602.65`), avoids the `8%` HWM kill switch (`4.16%` tick-MTM DD), stays strongly positive ex-top-2 days (`+$816.40`, PF `1.35`), and achieves `96.0%` bootstrap probability of positive total P&L (`[-$191.78, +$3,544.55]`). Because `H3b` is an un-derived hard ceiling found during G1 research, it remains **research-only**.
- **Does `G2` (`g2_calibrated`) improve calibration and out-of-sample performance?**
  - **Calibration improved monotonically:** `INDEP_ELIGIBLE` rank correlation rose from `+0.0164` (`H0`) to `+0.1654` (`p=0.05088`), and `FULL` executed rank correlation flipped from `-0.0667` (`H0`) to **`+0.1634`** (`tertile spread +0.1447 R`), with monotonic score-band expectancy: `[0.60, 0.70)` = `-0.107 R` (`n=33`), `[0.70, 0.80)` = `+0.152 R` (`n=23`), `[0.80, 1.00]` = **`+0.306 R`** (`n=12`) — reversing legacy's negative `>= 0.80` tail.
  - **Performance vs `H0`:** Beats `H0` on `FULL` (`+$338.91 PF 1.17` vs `+$112.74 PF 1.06`), `DEV` (`+$987.92` vs `+$689.53`), `VAL` (`-$441.09` vs `-$588.98`), `2.0x` cost stress (`+$282.92` vs `+$60.67`), and counterfactual `NO_HWM` (`+$866.71 PF 1.29, 8.47% DD` vs `+$397.22 PF 1.10, 9.14% DD`; positive across `BUY +$505.0`, `SELL +$361.7`, `SIDEWAYS +$335.1`, `BEAR +$26.6`, `BULL +$505.0`, and ex-top-2 days `+$18.9 PF 1.01`). Daily block bootstrap `P(>0) = 0.653` (HWM ON) / `0.821` (HWM OFF).
  - **Why it is still NOT statistically significant:** Daily-block bootstrap `95% CI` spans `[-$1,067.43, +$2,001.14]` (HWM ON) and `[-$941.17, +$2,859.53]` (HWM OFF), BH-FDR `q_vs_H0 = 0.95293` (ON) / `0.76441` (OFF), `VAL` loses `-$441.09`, and `8%` HWM still triggers on `2026-10-28`.
- **Which hypotheses failed and why?**
  - **Single-defect fixes (`G2_A`, `G2_B`, `G2_C`, `G2_D`, `G2_D2`, `G2_H`):** Fixing one feature while leaving `35%` Kalman weight or noisy features unadjusted shifts individual cutoffs without creating multi-feature ranking edge; under `NO_HWM` `G2_D`, `G2_D2`, `G2_H` degrade (`-$116.61`, `+$6.34`, `-$284.28`, up to `14.60%` DD), because `TWP` breakout weighting at old Kalman weights admits aggressive October trend-reversal entries (`SL = 13..15`).
  - **`G2_E_CALIBRATED_CORE` (`+$1.18`, `PF 1.00`) vs `G2_E_DECORRELATED` (`+$338.91` / `NO_HWM +$866.71`):** Keeping the legacy `35%` Kalman weight over-relied on noisy tick velocity (`FULL NO_HWM` only `+$196.13`), whereas redistributing weight from Kalman (`35% -> 20%`) to orthogonal 5m entropy (`20%`) + aligned TWP (`25%`) improved net robustness and calibration.
  - **`G2_F_REGIME_CONDITIONED` (`rho = +0.2542 p=0.0462`, `FULL +$362.86`):** Achieved the highest executed rank correlation (`>=0.80` `n=7` at `+0.711 R`), but suppressed `HO_EXPOSED` (`+$117.38` vs `+$332.43`) and cut sample to `62` trades with higher sensitivity to regime hysteresis lags.
  - **Root cause across all G2 candidates:** The six existing entry features individually have near-zero predictive power over future trade R (`INDEP max univariate |rho| = 0.1855`). Better weighting & calibration eliminates mathematical pathology and flips rank ordering positive, but cannot manufacture strong out-of-sample profit from weak underlying entry signals.

---

## 6. Actual Application Reconciliation & Regression (Phases F & G)

### Phase F (`g2_application_reconciliation.json`) — Does the actual application match independent research?
**Yes — 100% (`0` discrepancies across all scenarios and all `2,482` candidate rows):**

| Verification Check | Actual Application (`RunBacktestUseCase` / `TradeScorer` / API) | Clean-Room Independent Engine (`g2_clean_room_scorer.py`) | Max Abs Diff / Mismatches |
| :--- | :--- | :--- | :---: |
| **Candidate Score Parity (`n=2,482`, `453` eligible)** | `TradeScorer` (`legacy` & `g2_calibrated`) | Subprocess without `src/` in `PYTHONPATH` | `legacy=0.0`, `g2=0.0`, `sub=0.0` (`0` mismatches) |
| **`H0` (`scorer_mode="legacy"`, `cap=None`)** | `68 tr` (`38W/30L`), `+$112.7398`, PF `1.057`, Closed DD `-$816.3817` (`7.4698%`), MTM DD `-$879.9116` (`8.0045%`), HWM `2026-10-22T09:12:13.436000+00:00` | `68 tr` (`38W/30L`), `+$112.7398`, PF `1.057`, Closed DD `-$816.3817`, MTM DD `-$879.9116` (`8.0045%`), HWM `2026-10-22T09:12:13.436000+00:00` | **`0` mismatches** (`score=0.0`, `pnl=0.0`, `px=0.0`, `qty=0.0`) |
| **`H3b` (`scorer_mode="legacy"`, `cap=0.80`)** | `90 tr` (`50W/40L`), `+$1,584.4266`, PF `1.6691`, Closed DD `-$322.5905`, MTM DD `-$455.6683` (`4.1562%`), HWM `False` | `90 tr` (`50W/40L`), `+$1,584.4266`, PF `1.6691`, Closed DD `-$322.5905`, MTM DD `-$455.6683` (`4.1562%`), HWM `False` | **`0` mismatches** (`score=0.0`, `pnl=0.0`, `px=0.0`, `qty=0.0`) |
| **`G2` (`scorer_mode="g2_calibrated"`, `cap=None`)** | `68 tr` (`35W/33L`), `+$338.9147`, PF `1.1746`, Closed DD `-$837.456`, MTM DD `-$900.9859` (`8.016%`), HWM `2026-10-28T12:46:14.506000+00:00` | `68 tr` (`35W/33L`), `+$338.9147`, PF `1.1746`, Closed DD `-$837.456`, MTM DD `-$900.9859` (`8.016%`), HWM `2026-10-28T12:46:14.506000+00:00` | **`0` mismatches** (`score=0.0`, `pnl=0.0`, `px=0.0`, `qty=0.0`) |
| **PostgreSQL + Flask REST API** | Persisted `run_id="g2_verified_full_20261122"` (`trades=68`, `pnl=338.9147`, `scorer_mode="g2_calibrated"`), invalid `scorer_mode` returns HTTP `400` | Direct SQL + Flask `test_request_context` + live `localhost:5000` after restart | Verified (`200` / `400`, `verified=true`) |

### Phase G (`g2_regression_results.json`) — Regression & Safety Verification
- **Unit & Integration Suites (`6/6` passed, `42/42` tests, `0` skipped, `0` failures inside restarted `goldedge-backend-dev`):**
  - `test_task_g2_validation.py`: `6/6` passed (`29.75s`) — AST zero-external-lib check on `trade_scorer.py`, `legacy` exactness, `g2_calibrated` unit defect fixes, no-lookahead invariance, `>7`-day chunk continuity, API validation, clean-room AST isolation, frozen SHA-256 preservation, safety locks.
  - `test_task_g1_validation.py`: `6/6` passed (`0.35s`)
  - `test_remediation_b1_b6.py`: `4/4` passed (`0.28s`)
  - `test_shadow_validation.py`: `8/8` passed (`0.47s`)
  - `test_task6c_validation.py`: `3/3` passed (`3.20s`)
  - `test_task8_hwm_kill_switch.py`: `15/15` passed (`1.06s`)
- **Frontend Check (`goldedge-frontend-dev`):** `npx tsc --noEmit` exited `0`.
- **Live Safety State Verified:** `SHADOW_MODE = True`, `TRADING_MODE = "demo"`, `HWM_MAX_DRAWDOWN_PCT = 8.0`, PostgreSQL `market_orders = 0`, `market_trades = 0`, shadow journal `backtest_results/shadow_live_rollovers.jsonl` (`366` lines), live restarted API `GET /api/backtest/history/g2_verified_full_20261122` -> HTTP `200`, `POST /api/backtest/run` invalid `scorer_mode` -> HTTP `400`.

---

## 7. Remaining Limitations & Forward Validation Protocol (`§10` & `§11`)

1. **Statistical Limitations:**
   - The `2026-10-26..11-19` window (`HO_EXPOSED`) was previously inspected during G1 research and cannot serve as an independent OOS proof.
   - `VAL` sample size (`n=16..26` executed trades across variants) is small, yielding wide daily-block bootstrap confidence intervals across all configurations (`[-$1,067, +$2,001]` for `G2` HWM-ON).
   - No `G2` scorer variant passes BH-FDR multiple-testing correction against `H0` (`q >= 0.764`), nor survives the October drawdown without hitting `8%` HWM.
2. **Forward Validation Freeze Protocol (`§10`):**
   - `H0` (`scorer_mode="legacy", max_score_threshold=None`), `H3b` (`scorer_mode="legacy", max_score_threshold=0.80`), and `G2` (`scorer_mode="g2_calibrated", max_score_threshold=None`) remain frozen at commit/post-G2 state with `SHADOW_MODE = True` (zero broker orders permitted).
   - **Next Task (`Task G3`):** Log passive unseen shadow evaluations of `H0`, `H3b`, and `G2` until at least **`30–50` non-overlapping eligible forward trades** accumulate, evaluate forward score monotonicity & regime stability with dependence-aware CIs, and research orthogonal entry/regime filters (e.g., `SELL` market-structure/time dynamics) rather than re-weighting weak existing features alone.

---

## 8. Deliverables Inventory (`backtest_results/audit/task_g2_scorer/`)
1. `g2_scoring_formula_audit.json`
2. `g2_candidate_decisions.csv`
3. `g2_score_calibration.csv`
4. `g2_feature_saturation_report.json`
5. `g2_exhaustion_diagnosis.csv`
6. `g2_research_comparison.json`
7. `g2_application_reconciliation.json`
8. `g2_regression_results.json`
9. `g2_final_report.md`
