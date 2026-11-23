# Task 10 Final Report — XAUUSD Exhaustion-Chasing & TradeScorer Remediation Research

**Final Verdict:** `VERIFIED` *(Research, reproduction, testing, and independent raw-tick validation completed successfully. This verdict does **not** approve live trading.)*
**Safety & Immutability Status:** `SHADOW_MODE = True` | `TRADING_MODE = demo` | `market_orders = 0` | `market_trades = 0` | Zero edits to `src/goldedge/`, Task 4 baseline (`SHA-256: 73306E1529ADC48284BCE9AB6AC35122F33B9999D2464AF441867EB3BB6CF168`), `RECOVERED_FULL_HISTORY_BASELINE_V2`, or Task 8 HWM kill-switch.

---

## 1. Phase 1 — Baseline & Task 9 Verification

Verified in `task10_baseline_verification.json` and `task10_independent_reconciliation.json`:
- **Frozen SHA-256 Fingerprints:** Task 4 `frozen_111_baseline_trades.csv` and Task 6C V2 `w1_trades.csv`, `w2_trades.csv`, `w3_trades.csv`, `w3_journeys.json` all matched verbatim.
- **Independent Raw-Tick `H0` Reconciliation (zero `src/goldedge` imports):**
  - `W1` (`Aug 24–Nov 16`): `118 / 118` trades, **`0` mismatches**, `$0.000000` max P&L diff.
  - `W2` (`Aug 13–Nov 16`): `131 / 131` trades, **`0` mismatches**, `$0.000000` max P&L diff.
  - `W3` (`Aug 17–Nov 18`, warmup `Aug 13–16`): `143 / 143` trades, **`0` mismatches**, `$0.000000` max P&L diff.
- **Task 9 Cross-Check (`W3`):** `H4a` diff `$0.0041`, `H5` diff `$0.0049`, `H1` diff `$0.0034`, `H7` diff `$0.0008`.
- **Task 8 `8%` HWM Separation:** Kept strictly separate (`W3` halted `2026-10-16T07:39:25.668Z`, `78` trades, `+$146.64`, `MTM DD 8.01%`).

---

## 2. Phase 2 — Actual `TradeScorer` Component Audit

Inspected `src/goldedge/domain/services/trade_scorer.py`, `denoise_service.py`, `run_backtest.py`, and `scan_market.py` (without modifying code); full output in `task10_tradescorer_component_audit.json` and `task10_score_distribution.csv`:

- **Exact Formula (`w_ml = 0.0`, `w_quant = 1.0` in both backtest & live):**
  `final_score = round(clamp(clamp(0.35*S_vel + 0.20*S_er + 0.15*S_ent + 0.15*S_slope + 0.10*S_twp + 0.05*S_sess, 0, 1) + clamp(±2.0*kalman_accel, -0.08, +0.08), 0, 1) * tick_mult, 4)`
- **Symmetry & Causality:** BUY and SELL use exact mirror bands; all inputs (`kalman_vel`, `kalman_accel = x1 - prev_x1` up to first valid tick of entry minute, plus closed 5m indicators) are strictly causal.
- **Root Defects Driving Score Saturation & Exhaustion:**
  1. **`comp_entropy` Dead Weight (`15%`):** Uses `_score_band(ent, 2.50, 3.20, None, 3.60)` — because `bad_min=None`, ANY entropy `<= 3.20` scores `1.0000`. Across **143/143 executed trades (`100.0%`)** AND **2,159/2,159 candidate signals (`100.0%`)**, `comp_entropy = 1.0000` (`std = 0.0`), injecting a permanent `+0.15` free floor.
  2. **`comp_ema20_slope` Saturation (`15%`):** Pegged at `1.0000` on `86.01%` of trades (`>= 0.95` on `87.4%` of trades and `94.12%` of candidates; BUY mean `0.9961`), adding another `+0.145` near-constant floor (`fixed floor sum = 0.3322` with session).
  3. **Momentum Duplication & Inverted Correlation:** `comp_kaufman_er` (`68.5%` pegged at `1.0`) and `comp_kalman_vel` (`54.6%` pegged at `1.0`, `r_s = 0.7698` with final score) saturate simultaneously during late parabolic spikes. **All five non-constant components have negative Spearman correlation with realized P&L** (`-0.030` to `-0.137`).
  4. **Severe High-Score Breakdown (`task10_score_distribution.csv`):**
     - `0.60–0.80` (`n=78`): **`+$1,180.93`** (`BUY: -$9.80` across `n=23`, `SELL: +$1,190.72` across `n=55`).
     - `0.80–0.85` (`n=18`): **`-$384.62`** (`PF 0.496`, `BUY -$99.52`, `SELL -$285.11`).
     - `0.85–0.90` (`n=20`): `+$305.68` (`PF 1.417`).
     - **`0.90–1.00` (`n=27`): `-$890.51` (`37.04%` WR, `PF 0.353`; `BUY -$488.24` `PF 0.0035`, `SELL -$402.27` `PF 0.546`)!**

---

## 3. Phase 4 — Kalman Acceleration Scoring Inversion

Full results in `task10_acceleration_analysis.json`:
- **Monotonic Inversion Across Aligned Acceleration Bins (`W3`, `n=143`):**
  - `Decel_Mild (-0.02..0]` (`n=63`, pullback inside trend): **`66.7%` WR, `+$997.68`, `PF 1.540`** (`SELL +$1,021.68` `PF 1.800`; `BUY -$24.00` `PF 0.958`).
  - `Accel_Mod (0..0.015]` (`n=67`): **`50.8%` WR, `-$461.85`, `PF 0.824`** (`BUY -$408.88`, `SELL -$52.97`).
  - `Accel_High (0.015..0.03]` (`n=9`): **`22.2%` WR, `-$278.44`, `PF 0.473`** (`BUY 0/2 -$78.06`, `SELL 2/7 -$200.38`).
- **Why Removing `accel_adj` Alone Fails (`ACCEL_REMOVE`: `+$197.25`, `MTM DD 12.03%`):** Because `S_ent + S_slope + S_vel + S_er` already saturate at `0.85+`, zeroing or subtracting `-0.12` from `accel_adj` leaves exhausted entries above the `0.60` gate. Only **explicitly blocking** aligned acceleration when extended/straight-line (`ACCEL_WITH_ER_AND_EXT`: `+$1,468.57`, `PF 1.367`, `BUY +$196.28`, `SELL +$1,272.29`) or capping total saturated score (`H4a`: `score < 0.80`) prevents late entries.

---

## 4. Phases 3, 5, 6, 7 — Independent Hypothesis, Multi-Window, Cost-Stress & Outlier Results

Full tables in `task10_hypothesis_results.csv`, `task10_parameter_sensitivity.csv`, and `task10_statistical_validation.json`:

### A. W3 Main, Acceleration, Secondary, Combo, HWM-Protected & Cost-Stress Comparison

| Hypothesis | Group | `n` (`Ret/Rem/New`) | WR% | Net P&L | PF | Exp (`$/tr`) | Closed DD | MTM DD% | BUY P&L (`n`, PF) | SELL P&L (`n`, PF) | P1 / P2 / P3 (`$`) | Ex-Top-3 P&L (PF) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| **`H0_BASELINE_V2`** | main | 143 (`143/0/0`) | 55.2% | +$211.48 | 1.041 | +1.48 | -$1,336 | 12.78% | -$450.5 (`41`, 0.71) | +$662.0 (`102`, 1.19) | +806 / -702 / +107 | -$397.9 (`0.922`) |
| **`H4a_SCORE_CAP_080`** | main | **99** (`74/69/25`) | **61.6%** | **+$1,257.70** | **1.401** | **+12.70** | **-$649** | **6.18%** | **-$0.7** (`30`, 1.00) | **+$1,258.5** (`69`, 1.63) | **+951 / -57 / +364** | **+$630.7 (`1.201`)** |
| **`H5_ER_CAP_045`** | main | 105 (`87/56/18`) | 61.0% | +$896.08 | 1.266 | +8.53 | -$820 | 8.26% | +$67.5 (`29`, 1.07) | +$828.6 (`76`, 1.34) | +743 / -165 / +318 | +$281.0 (`1.083`) |
| **`H4a_PLUS_H5`** | main | 65 (`44/99/21`) | 64.6% | +$974.00 | 1.558 | +14.98 | -$406 | **4.21%** | -$44.5 (`21`, 0.93) | +$1,018.5 (`44`, 1.96) | **+409 / +69 / +496** | +$368.7 (`1.211`) |
| **`H4b_SCORE_CAP_075`** | main | 86 (`57/86/29`) | 59.3% | +$512.26 | 1.179 | +5.96 | -$814 | 7.76% | -$143.5 (`27`, 0.86) | +$655.8 (`59`, 1.36) | +685 / -283 / +110 | -$80.3 (`0.972`) |
| **`ACCEL_REMOVE`** | accel | 143 (`132/11/11`) | 55.9% | +$197.25 | 1.041 | +1.38 | -$1,243 | 12.03% | -$371.7 (`41`, 0.74) | +$569.0 (`102`, 1.17) | +683 / -692 / +207 | — |
| **`ACCEL_PENALIZE_EXTREME`** | accel | 143 (`132/11/11`) | 55.9% | +$197.25 | 1.041 | +1.38 | -$1,243 | 12.03% | -$371.7 (`41`, 0.74) | +$569.0 (`102`, 1.17) | +683 / -692 / +207 | -$403.3 (`0.917`) |
| **`ACCEL_MOD_BONUS_PEN_EXT`** | accel | 147 (`130/13/17`) | 55.8% | +$521.48 | 1.100 | +3.55 | -$1,374 | 12.97% | -$365.3 (`42`, 0.77) | +$886.8 (`105`, 1.24) | +1,029 / -844 / +336 | — |
| **`ACCEL_WITH_ER_AND_EXT`** | accel | **121** (`96/47/25`) | **63.6%** | **+$1,468.57** | **1.367** | +12.14 | -$950 | 8.65% | **+$196.3** (`34`, 1.16) | **+$1,272.3** (`87`, 1.46) | +1,179 / -522 / +812 | **+$816.8 (`1.204`)** |
| **`H1_BLOCK_BUY_07UTC`** | sec | 136 (`134/9/2`) | 57.4% | +$562.05 | 1.116 | +4.13 | -$1,055 | 10.10% | -$140.9 (`34`, 0.88) | +$703.0 (`102`, 1.19) | +933 / -497 / +126 | -$52.6 (`0.989`) |
| **`H7_BUY_FAST_BE_075_T1_12`** | sec | 143 (`142/1/1`) | 55.9% | +$452.79 | 1.093 | +3.17 | -$1,218 | 11.73% | -$235.1 (`41`, 0.82) | +$687.9 (`102`, 1.19) | +884 / -697 / +266 | -$161.6 (`0.967`) |
| **`H4a_PLUS_H7`** | combo | **99** (`74/69/25`) | **65.7%** | **+$1,468.52** | **1.512** | **+14.83** | **-$522** | **5.23%** | **+$199.6** (`30`, 1.24) | **+$1,268.9** (`69`, 1.63) | **+965 / -5 / +509** | **+$821.1 (`1.287`)** |
| **`H4a_PLUS_H1`** | combo | 93 (`68/75/25`) | 64.5% | +$1,554.11 | 1.534 | +16.71 | -$530 | 5.30% | +$310.2 (`24`, 1.37) | +$1,243.9 (`69`, 1.60) | +1,042 / +61 / +450 | +$906.7 (`1.312`) |
| *`H0_WITH_HWM_8PCT`* | *hwm* | *78* | *57.7%* | *+$146.64* | *1.051* | *+1.88* | *-$830* | *8.01%* | *-$456.6* | *+$603.3* | *+806 / -660 / 0* | *Halted Oct 16* |
| *`H4a_WITH_HWM_8PCT`* | *hwm* | *99* | *61.6%* | *+$1,257.70* | *1.401* | *+12.70* | *-$649* | *6.18%* | *-$0.7* | *+$1,258.5* | *+951 / -57 / +364* | *Never Halted* |
| *`H5_WITH_HWM_8PCT`* | *hwm* | *69* | *60.9%* | *+$232.60* | *1.103* | *+3.37* | *-$813* | *8.02%* | *-$56.4* | *+$289.0* | *+743 / -165 / -346* | *Halted @ 8.02%* |
| *`H4a_PLUS_H7_COST_STRESS`* | *stress* | *99* | *63.6%* | *+$1,429.70* | *1.499* | *+14.44* | *-$521* | *5.25%* | *+$191.7* | *+$1,238.0* | *+953 / -21 / +497* | *1.5x Slip + $15/M* |
| *`H4a_COST_STRESS`* | *stress* | *99* | *59.6%* | *+$1,219.00* | *1.388* | *+12.31* | *-$647* | *6.18%* | *-$14.1* | *+$1,233.1* | *+939 / -64 / +344* | *1.5x Slip + $15/M* |

### B. Multi-Window Robustness (`W1`, `W2`, `W3`)

| Candidate | W1 (`Aug 24–Nov 16`) | W2 (`Aug 13–Nov 16`) | W3 (`Aug 17–Nov 18`) |
|---|---|---|---|
| **`H0_BASELINE_V2`** | `n=118`, +$116.5, PF `1.027`, DD `12.79%` | `n=131`, +$151.7, PF `1.032`, DD `12.78%` | `n=143`, +$211.5, PF `1.041`, DD `12.78%` |
| **`H4a_SCORE_CAP_080`** | `n=82`, **+$892.3**, PF `1.347`, DD **`6.09%`** | `n=91`, **+$929.4**, PF `1.322`, DD **`6.18%`** | `n=99`, **+$1,257.7**, PF `1.401`, DD **`6.18%`** |
| **`H5_ER_CAP_045`** | `n=84`, +$320.3, PF `1.112`, DD `8.22%` | `n=96`, +$508.7, PF `1.161`, DD `8.26%` | `n=105`, +$896.1, PF `1.266`, DD `8.26%` |
| **`H4a_PLUS_H5`** | `n=55`, +$575.4, PF `1.368`, DD `4.29%` | `n=61`, +$491.4, PF `1.281`, DD `4.21%` | `n=65`, +$974.0, PF `1.558`, DD `4.21%` |
| **`ACCEL_WITH_ER_AND_EXT`** | `n=98`, +$1,160.8, PF `1.353`, DD `8.61%` | `n=110`, +$1,389.5, PF `1.383`, DD `8.65%` | `n=121`, +$1,468.6, PF `1.367`, DD `8.65%` |
| **`H4a_PLUS_H7`** | `n=82`, **+$999.2**, PF **`1.421`**, DD **`4.97%`** | `n=91`, **+$1,128.7**, PF **`1.433`**, DD **`5.03%`** | `n=99`, **+$1,468.5**, PF **`1.512`**, DD **`5.23%`** |
| **`H4a_PLUS_H1`** | `n=78`, +$1,075.3, PF `1.446`, DD `4.55%` | `n=85`, +$1,221.2, PF `1.462`, DD `4.54%` | `n=93`, +$1,554.1, PF `1.534`, DD `5.30%` |

### C. Parameter Sensitivity & Statistical Limitations
- **Score Cap Sensitivity (`W3`):** `0.75` (`+$512.3`, `DD 7.76%`), **`0.78` (`+$1,485.9`, `PF 1.499`, `DD 6.19%`)**, **`0.80` (`+$1,257.7`, `PF 1.401`, `DD 6.18%`)**, `0.82` (`+$694.5`, `PF 1.207`, `DD 7.12%`), `0.85` (`+$159.5`, `PF 1.040`, `DD 10.77%`), `0.90` (`+$286.6`, `PF 1.061`, `DD 11.70%`). Sharp cliff above `0.82` confirms exhaustion sits in `>= 0.80`, though local sensitivity between `0.80` and `0.82` means `0.80` must be validated on fresh forward data.
- **ER Cap Sensitivity (`W3`):** `0.40` (`+$1,294.5`, `DD 7.29%`), `0.45` (`+$896.1`, `DD 8.26%`), `0.50` (`+$493.3`, `DD 9.97%`), `0.55` (`+$556.3`, `DD 12.62%`).
- **Statistical Power & Multiple Comparisons:** No portfolio transition reaches BH-FDR `q < 0.05` on two-sided Mann-Whitney U (`mw_q = 0.968` across all candidates; lowest raw `mw_p = 0.320` for `H4a+H5`, `0.418` for `H4a+H7`, `0.604` for `H4a`). At observed effect sizes (`+$11–$13/trade`) and `$85–$90` per-trade SD, 80% power requires `~700–1,000+` trades per arm. Thus **evidence is economically coherent and multi-window/outlier/cost-stress consistent, but not yet statistically conclusive without fresh forward shadow trades**.

---

## 5. Phases 8 & 9 — Candidate Ranking & Application Implementation Readiness Spec

### Final Candidate Ranking
1. **Leading Atomic Candidate — `H4a_SCORE_CAP_080` (`0.60 <= score < 0.80`):** Best single-parameter risk-adjusted improvement across `W1/W2/W3`, stays `< 6.2%` MTM DD (never triggers 8% HWM stop), survives `1.5x` cost stress (`+$1,219`) and top-3 outlier removal (`+$630.7`, `PF 1.201`), zero BUY/SELL asymmetry risk.
2. **Leading Two-Rule Candidate — `H4a_PLUS_H7` (`0.60 <= score < 0.80` + `BUY BE 0.75 / T1 1.2/0.8`):** Lifts BUY from flat `-$0.74` to `+$199.63` (`PF 1.238`), cuts MTM DD to `4.97%–5.23%` across `W1/W2/W3`, `+$821.1` ex-top-3 outliers, without relying on the small-`n` (`n=8`) `07:00 UTC` hour filter.
3. **Runner-Up Low-DD Filter — `H4a_PLUS_H5`:** Lowest DD (`4.21%`) and positive in `P1/P2/P3`, but drops sample size to `n=65`.
4. **Deferred / Rejected as Standalone:** `ACCEL_REMOVE` / `ACCEL_PENALIZE_EXTREME` (ineffective without fixing score floor), `H5` standalone (breaches 8% HWM stop), `H1` / `H7` standalone (fail ex-top-3 outlier check).

### Phase 9 Application Readiness Specification *(NOT Implemented in Task 10)*
For the future approved implementation task (Tasks 27–32):
- **Primary Parameter:** Add optional `max_score_threshold: Optional[float] = 0.80` on `RiskProfile` (default `None` to keep V2 baseline reproducible when disabled).
- **Exact Rule:** In `TradeScorer.filter_and_rank(...)` / `RunBacktestUseCase` / `ScanMarketUseCase`, reject candidate signals where `final_score >= max_score_threshold` with explicit rejection reason `EXHAUSTION_SCORE_CAP` (`score >= 0.80`), logging `{symbol, direction, score, kaufman_er, kalman_vel, kalman_accel, reason}` in shadow telemetry without altering spread, commission, slippage, sizing, cooldowns, or Task 8 HWM behavior.
- **Secondary Follow-Up (Optional in Task 11–12 / Implementation Phase):** Evaluate `H4a_PLUS_H7` (`buy_be_atr=0.75`, `buy_t1_trigger_atr=1.2`, `buy_t1_dist_atr=0.8`) and `TradeScorer` structural cleanup (fixing `comp_entropy` `bad_min=None` floor) after Task 11–12 regime/session studies complete.

---

## 6. Phase 10 — Final Verification & Audit

- **Regression Test Suite:** Verified in `goldedge-backend-dev` across all 9 modules (`test_remediation_b1_b6`, `test_shadow_validation`, `test_backtest_history`, `test_task7_remediation`, `test_task7b_quote_semantics`, `test_task6c_validation`, `test_task8_hwm_kill_switch`, `test_task9_diagnostics`, `test_task10_research`).
- **Deliverables Verified under `backtest_results/audit/`:** All 12 required files (`task10_baseline_verification.json` through `task10_final_report.md`) saved and validated.
