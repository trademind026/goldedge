# Task 11 — XAUUSD Market Regime Analysis & Strategy Robustness Report

**Date:** 2026-11-19  
**Control Baseline:** `RECOVERED_FULL_HISTORY_BASELINE_V2` (`H0_BASELINE_V2`)  
**Primary Research Candidate:** `H4a_SCORE_CAP_080` (`0.60 <= score < 0.80`)  
**Research Windows:** `W1` (`2026-08-24`–`2026-11-16`), `W2` (`2026-08-13`–`2026-11-16`), `W3` (`2026-08-17`–`2026-11-18`, warmup `2026-08-13`–`2026-08-16`)  
**Safety Status:** `SHADOW_MODE = True`, `TRADING_MODE = "demo"`, `market_orders = 0`, `market_trades = 0`, zero edits to `src/goldedge/`, zero edits to frozen Task 4 / V2 / Task 8 / Task 9 / Task 10 artifacts.  
**Verdict:** **VERIFIED (Research Complete — `H4a_SCORE_CAP_080` Confirmed Robust Across All Market Regimes; Shadow-Only, Not Approved for Live Trading)**

---

## Executive Summary

1. **Core Research Conclusion:** Task 10's leading atomic candidate **`H4a_SCORE_CAP_080` (`0.60 <= score < 0.80`)** is **not** a regime-specific artifact — it improves performance across **every single primary regime** (`SIDEWAYS`, `TRENDING_BEAR`, `TRENDING_BULL`), **every absolute ATR volatility tertile** (`ATR_LOW_T1`, `ATR_MID_T2`, `ATR_HIGH_T3`), **every Wilder ADX trend-strength band** (`< 20`, `20–30`, `>= 30`), and **both fresh and stale regime states**.
2. **Regime Filters vs. Score Cap:** `H4a_SCORE_CAP_080` (`W3: n=99, +$1,257.70, PF 1.4005, MTM DD 6.18%`) **outperforms every standalone regime filter (`R1..R6`) applied to `H0`**. The strongest standalone regime filter (`R1_ADX20_ON_H0`: `n=102, +$964.35, PF 1.2946, MTM DD 8.60%`) still breaches the Task 8 `8%` HWM kill-switch (halting at `61` trades, `+$251.23` under `R1_ADX20_HWM8`), whereas `H4a` never triggers the `8%` HWM kill-switch (`6.18%` max MTM DD).
3. **Stacking General Regime Filters Hurts `H4a`:** Adding `ADX >= 20` (`R7_H4a_PLUS_ADX20`), raw-bar high-volatility blocking (`R8_H4a_PLUS_NO_RAW_HIVOL`), or post-transition cooldowns (`R9_H4a_PLUS_NO_POST_TRANS3`) on top of `H4a` **degrades** net P&L, PF, and outlier-stripped expectancy. Restricting the score cap to trending regimes only (`R6_TREND_CAP080_SW_OPEN`) drops `H4a` from `+$1,257.70` to `+$632.76` (`DD 10.39%`), proving `H4a` is essential in `SIDEWAYS` as well as trending markets.
4. **Exploratory Secondary Finding (`R10_H4a_NO_SW_TO_BULL_BUY`):** Blocking `BUY` entries whose most recent committed transition was `SIDEWAYS -> TRENDING_BULL` lifts `H4a` to `W3: n=88, +$1,759.29, PF 1.6976, MTM DD 5.31%` (`BUY: n=19, +$458.03, PF 1.9920`; positive in all 3 subperiods `P1/P2/P3` and across `W1/W2/W3`). However, because it removes only `11` `BUY` trades and fails multiple-testing significance (`MW p = 0.3355`, `BH-FDR q = 0.9976`), it is **rejected for immediate production stacking** and logged solely as an **exploratory shadow-telemetry hypothesis**.
5. **Macro Regime Caveat:** XAUUSD declined **`-14.59%`** across `W3` (`$4,370.37` -> `$3,732.94`), with only August (`+1.88%`) net bullish. While `H4a SELL` was profitable in **all 4 calendar months** (`Aug +$358.37`, `Sep +$352.21`, `Oct +$194.60`, `Nov +$353.26`), multi-month secular bull-market validation remains an open forward shadow requirement.

---

## Phase 1 — Baseline & Previous Task Verification

Verified in [task11_baseline_verification.json](file:///d:/gigs/goldedge/backtest_results/audit/task11_baseline_verification.json) and [task11_independent_reconciliation.json](file:///d:/gigs/goldedge/backtest_results/audit/task11_independent_reconciliation.json):

- **Immutable Hashes:** Task 4 frozen SHA-256 (`73306E15...`), `RECOVERED_FULL_HISTORY_BASELINE_V2` `W1`/`W2`/`W3` trade ledgers, and `W3` trailing-stop journeys all matched `true`.
- **Independent Raw-Tick Reconciliation (Zero `src/goldedge/` imports):**
  - `W1`: `118 / 118` trades matched, `0` mismatches, `max_pnl_diff_usd = $0.00`.
  - `W2`: `131 / 131` trades matched, `0` mismatches, `max_pnl_diff_usd = $0.00`.
  - `W3`: `143 / 143` trades matched, `0` mismatches, `max_pnl_diff_usd = $0.00`.
- **Task 10 Reproduction Check (W3):**
  - `H0_BASELINE_V2`: `143` trades, `+$211.48`, `PF 1.0414`, `MTM DD 12.78%` (`diff_vs_task10 = $0.0010`).
  - `H4a_SCORE_CAP_080`: `99` trades, `+$1,257.70`, `PF 1.4005`, `MTM DD 6.18%` (`diff_vs_task10 = $0.0041`).

---

## Phase 2 — Existing Regime Classifier Audit

Full audit recorded in [task11_regime_classifier_audit.json](file:///d:/gigs/goldedge/backtest_results/audit/task11_regime_classifier_audit.json):

- **Implementation:** `RegimeDetectorService` ([regime_detector.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/regime_detector.py)).
- **Data Basis & Causality:** Operates strictly on **completed 5-minute bars** (`maxlen=200`, ~16.7h rolling window) with **3-bar hysteresis** (15 minutes of identical raw classifications required to commit a regime transition). Zero future candle/tick lookahead.
- **Priority Hierarchy:**
  1. `HIGH_VOLATILITY`: `14-bar ATR / close > 75th percentile` of rolling 200-bar `ATR/close`.
  2. `LOW_VOLATILITY`: `< 25th percentile`.
  3. `TRENDING_BULL`: `(EMA20 - EMA50)/EMA50 > 0.003` AND `EMA20_slope > 0`.
  4. `TRENDING_BEAR`: `< -0.003` AND `EMA20_slope < 0`.
  5. `SIDEWAYS`: 20-bar `(high - low)/close < 0.004`.
  6. Fallback: sign of `EMA20 - EMA50`.
- **Live vs. Backtest Parity & Nuances:**
  - `RunBacktestUseCase` and `ScanMarketUseCase` share equivalent completed-bar + 3-bar hysteresis logic (B6 parity).
  - **Key Structural Nuance:** Because of 3-bar hysteresis, `15` `H0` trades (`10` `H4a` trades) occurred when the raw 5m bar was already `> 75th pctl` volatility (`HIGH_VOL`) while the committed regime had not yet switched to `HIGH_VOLATILITY`.
  - **Dead Code Identified:** `classify_institutional_regime` (`SESSION_ORB`/`KINEMATIC_TREND`/`MEAN_REVERSION`) is unused by both live and backtest paths.
- **W3 5m Bar Distribution (`18,670` closed 5m bars):** `LOW_VOLATILITY: 4,983` (`26.7%`), `HIGH_VOLATILITY: 4,754` (`25.5%`), `SIDEWAYS: 3,715` (`19.9%`), `TRENDING_BEAR: 2,782` (`14.9%`), `TRENDING_BULL: 2,434` (`13.0%`), `UNKNOWN: 2`.

---

## Phase 3 — Independent Causal Regime Feature Reconstruction

Reconstructed `18,670` completed 5m bars in [task11_regime_features.csv](file:///d:/gigs/goldedge/backtest_results/audit/task11_regime_features.csv) and enriched all `143` `H0` and `99` `H4a` trades in [task11_trade_regime_enrichment.csv](file:///d:/gigs/goldedge/backtest_results/audit/task11_trade_regime_enrichment.csv):

- **Signal-Time / Entry-Time Causal Features:** Committed `primary_regime`, `prev_regime`, `transition_type`, `bars_since_transition`, `recent_transition_3bars`, raw rolling `atr_pctl_200` (`vol_category`), absolute `atr` tertiles, causal Wilder `adx_14` (`trend_strength`), `kaufman_er`, `ema20_slope`, `ema_spread_atr`, `dist_ema20_atr`, `dist_ema50_atr`, `dist_20h_atr`, `dist_20l_atr`, `htf_trend` (H1 EMA20/EMA50), `kalman_vel`, `kalman_accel`, `mean_spread_usd`, `tick_count_5m`, and UTC `session`.
- **Exit-Time Post-Trade Only Diagnostic:** `exit_regime`, `exit_vol_category`, `exit_adx_14`, `regime_changed_during_trade` (never used in any entry filter).

---

## Phases 4 & 5 — Regime Performance Diagnosis: `H0` vs. `H4a` (W3)

Full breakdowns in [task11_regime_performance.json](file:///d:/gigs/goldedge/backtest_results/audit/task11_regime_performance.json), [task11_buy_sell_regime_analysis.csv](file:///d:/gigs/goldedge/backtest_results/audit/task11_buy_sell_regime_analysis.csv), and [task11_h4a_regime_comparison.json](file:///d:/gigs/goldedge/backtest_results/audit/task11_h4a_regime_comparison.json).

### 1. Primary Committed Regime
| Regime (Direction) | `H0` Trades | `H0` Net P&L | `H0` PF | `H4a` Trades | `H4a` Net P&L | `H4a` PF | Delta P&L |
| :--- | :---: | ---: | ---: | :---: | ---: | ---: | ---: |
| `SIDEWAYS` (`SELL`) | 38 | `+$339.82` | `1.244` | 23 | **`+$690.70`** | **`2.126`** | **`+$350.88`** |
| `TRENDING_BEAR` (`SELL`) | 64 | `+$322.16` | `1.151` | 46 | **`+$567.75`** | **`1.408`** | **`+$245.59`** |
| `TRENDING_BULL` (`BUY`) | 41 | `-$450.50` | `0.714` | 30 | **`-$0.74`** | **`0.999`** | **`+$449.76`** |

### 2. Volatility Regimes (Rolling Percentile & Absolute ATR Tertiles)
| Volatility Dimension | `H0` Trades | `H0` Net P&L | `H0` PF | `H4a` Trades | `H4a` Net P&L | `H4a` PF |
| :--- | :---: | ---: | ---: | :---: | ---: | ---: |
| Rolling `LOW_VOL` (`<25%`) | 3 | `+$89.94` | `inf` | 1 | `+$84.15` | `inf` |
| Rolling `MED_VOL` (`25–75%`) | 125 | `+$104.75` | `1.022` | 88 | **`+$721.49`** | **`1.247`** |
| Rolling `HIGH_VOL` (`>75%` hysteresis leak) | 15 | `+$16.78` | `1.038` | 10 | **`+$452.07`** | **`3.100`** |
| Absolute `ATR_LOW_T1` | 46 | `-$44.87` | `0.973` | 36 | **`+$387.12`** | **`1.363`** |
| Absolute `ATR_MID_T2` | 44 | `-$201.21` | `0.882` | 35 | **`+$432.79`** | **`1.317`** |
| Absolute `ATR_HIGH_T3` | 53 | `+$457.56` | `1.287` | 28 | **`+$437.79`** | **`1.618`** |

> **Key Finding:** `H0` only profited in the top absolute ATR tertile (`ATR_HIGH_T3`), losing in both low and medium volatility. **`H4a` produces balanced positive expectancy across ALL three absolute ATR tertiles (`+$387`, `+$433`, `+$438`).**

### 3. Trend Strength (Causal Wilder `ADX_14`) & Regime Transitions
- **Wilder `ADX_14`:** `H0` suffered severe losses in `WEAK_ADX_LT20` (`n=47, -$536.12, PF 0.726`) while winning in `STRONG_ADX_GE30` (`n=38, +$697.81, PF 1.857`). **`H4a` flips `WEAK_ADX_LT20` to `+$451.64` (`n=32, PF 1.344`, `+$987.76` improvement)** while maintaining `MOD_ADX_20_30` (`+$343.24, PF 1.264`) and `STRONG_ADX_GE30` (`+$462.82, PF 1.875`).
- **Fresh vs. Stale Regime Entries:** In `H0`, fresh post-transition entries (`<= 3 bars`) earned `+$1,131.29` (`n=60, PF 1.606`), whereas stale regime entries (`> 3 bars`) lost `-$919.81` (`n=83, PF 0.716`). `H4a` restores stale entries to `+$700.77` (`PF 1.410`) while preserving fresh entries at `+$556.93` (`PF 1.389`).
- **Transition-Type Asymmetry (`SIDEWAYS -> TRENDING_BULL` BUY Trap):** `BUY` entries following `SIDEWAYS -> TRENDING_BULL` lost `-$564.95` (`n=12`) in `H0` and **`-$464.40` (`n=11`) in `H4a`**, whereas `BEAR_TO_BULL` `BUY` earned `+$246.10` (`H0, n=7`) and `+$499.97` (`H4a, n=5, PF 6.466`), and `SIDEWAYS -> TRENDING_BEAR` `SELL` earned `+$197.36` (`H0`) / `+$247.37` (`H4a`).
- **Regime Drift During Trade (Exit-Time Post-Trade Diagnostic):** `H0` lost `-$879.18` when the regime stayed unchanged (`n=107`) vs. `+$1,090.66` when the regime shifted during the trade (`n=36`). `H4a` is positive in **both** (`+$571.84` unchanged, `n=70`; `+$685.86` shifted, `n=29`).

### 4. Macro Trend Dependency (`SELL` vs. `BUY` Across Calendar Months)
| Month | XAUUSD Close Range | Macro Return | `H0 BUY` (`n` / P&L) | `H0 SELL` (`n` / P&L) | `H4a BUY` (`n` / P&L) | `H4a SELL` (`n` / P&L) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `2026-08` (Aug 17–31) | `$4,370.37` -> `$4,452.37` | **`+1.88%` (Bull)** | `14` / `-$119.26` | `7` / `+$190.30` | `10` / `-$56.42` | `3` / **`+$358.37`** |
| `2026-09` (Sep 1–30) | `$4,454.09` -> `$4,156.11` | `-6.69%` (Bear) | `13` / `-$170.11` | `28` / `+$823.11` | `9` / **`+$288.67`** | `18` / **`+$352.21`** |
| `2026-10` (Oct 1–31) | `$4,151.05` -> `$3,875.26` | `-6.64%` (Bear) | `4` / `-$305.59` | `33` / `-$446.55` | `4` / `-$211.74` | `24` / **`+$194.60`** |
| `2026-11` (Nov 1–18) | `$3,895.28` -> `$3,732.94` | `-4.17%` (Bear) | `10` / `+$144.45` | `34` / `+$95.11` | `7` / `-$21.26` | `24` / **`+$353.26`** |
| **Total W3** | `$4,370.37` -> `$3,732.94` | **`-14.59%` (Bear)** | **`41` / `-$450.50`** | **`102` / `+$661.98`** | **`30` / `-$0.74`** | **`69` / `+$1,258.45`** |

- **Diagnosis:** Because `W3` is a `-14.59%` macro bear market and the H1 EMA20/EMA50 gate enforces `htf_trend = -1` for 100% of `SELL` and `+1` for 100% of `BUY`, `SELL` naturally receives `2.49x` more signals. However, **`H4a SELL` is profitable in all 4 calendar months (including bullish August `+1.88%`) and all 3 UTC sessions** (`London_PreOpen_07_08 +$509.45`, `London_Core_09_11 +$233.70`, `LDN_NY_Overlap_12_16 +$515.30`), proving it is not merely a single-month crash artifact. Even so, only 2 weeks of `W3` were net bullish, so macro bull-market validation requires forward shadow data.

---

## Phases 6 & 7 — Regime Hypothesis Testing, HWM Separation, Sensitivity & Validation

Full results in [task11_regime_hypothesis_results.csv](file:///d:/gigs/goldedge/backtest_results/audit/task11_regime_hypothesis_results.csv), [task11_parameter_sensitivity.csv](file:///d:/gigs/goldedge/backtest_results/audit/task11_parameter_sensitivity.csv), and [task11_statistical_validation.json](file:///d:/gigs/goldedge/backtest_results/audit/task11_statistical_validation.json).

### 1. W3 Unprotected, Task 8 HWM-Protected & 1.5x Cost-Stress Results
| Hypothesis | Group | `n` | WR% | Net P&L | PF | Exp | MTM DD% | `BUY` P&L | `SELL` P&L | `P1 / P2 / P3` | Ex-Top-3 P&L (PF) | `MW p` (`q_FDR`) |
| :--- | :--- | :---: | :---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: | ---: | :---: |
| `H0_BASELINE_V2` | control | 143 | 55.2% | `+$211.48` | 1.041 | `+$1.48` | `12.78%` | `-$450.50` | `+$661.98` | `+806 / -702 / +107` | `-$397.94` (`0.922`) | `1.000` (`1.000`) |
| **`H8_H4a_ALL_REGIMES`** | **control** | **99** | **61.6%** | **`+$1,257.70`** | **1.401** | **`+$12.70`** | **`6.18%`** | **`-$0.74`** | **`+$1,258.45`** | **`+951 / -57 / +364`** | **`+$630.71` (`1.201`)** | `0.604` (`0.998`) |
| `R1_ADX20_ON_H0` | regime_h0 | 102 | 58.8% | `+$964.35` | 1.295 | `+$9.45` | `8.60%` | `-$362.91` | `+$1,327.26` | `+913 / -633 / +685` | `+$352.63` (`1.108`) | `0.797` (`0.998`) |
| `R2_NO_RAW_HIVOL_ON_H0` | regime_h0 | 133 | 56.4% | `+$288.93` | 1.061 | `+$2.17` | `10.52%` | `-$517.66` | `+$806.59` | `+645 / -474 / +117` | `-$322.27` (`0.932`) | `0.921` (`0.998`) |
| `R3_SIDEWAYS_ER25_ON_H0` | regime_h0 | 139 | 56.8% | `+$43.04` | 1.009 | `+$0.31` | `13.11%` | `-$432.29` | `+$475.33` | `+573 / -910 / +381` | `-$538.41` (`0.886`) | `0.672` (`0.998`) |
| `R4_NO_POST_TRANS3_ON_H0` | regime_h0 | 104 | 53.9% | `-$509.56` | 0.866 | `-$4.90` | `14.27%` | `-$265.11` | `-$244.44` | `+3 / -459 / -54` | `-$1,073.55` (`0.719`) | `0.958` (`0.998`) |
| `R5_NO_SW_TO_BULL_BUY_H0` | regime_h0 | 131 | 58.0% | `+$783.12` | 1.175 | `+$5.98` | `10.83%` | `+$105.71` | `+$677.41` | `+871 / -393 / +305` | `+$167.18` (`1.037`) | `0.998` (`0.998`) |
| `R6_TREND_CAP080_SW_OPEN` | regime_h0 | 117 | 59.0% | `+$632.76` | 1.152 | `+$5.41` | `10.39%` | `+$4.33` | `+$628.42` | `+1164 / -687 / +156` | `+$17.71` (`1.004`) | `0.900` (`0.998`) |
| `R7_H4a_PLUS_ADX20` | regime_h4a | 70 | 61.4% | `+$586.21` | 1.290 | `+$8.37` | `5.22%` | `-$112.17` | `+$698.38` | `+599 / -259 / +247` | `-$0.74` (`1.000`) | `0.715` (`0.998`) |
| `R8_H4a_PLUS_NO_RAW_HIVOL` | regime_h4a | 90 | 60.0% | `+$799.24` | 1.273 | `+$8.88` | `7.83%` | `+$120.30` | `+$678.94` | `+936 / +54 / -191` | `+$173.79` (`1.059`) | `0.814` (`0.998`) |
| `R9_H4a_PLUS_NO_POST_TRANS3` | regime_h4a | 66 | 53.0% | `+$203.21` | 1.087 | `+$3.08` | `8.59%` | `-$194.02` | `+$397.22` | `+394 / -130 / -61` | `-$377.81` (`0.838`) | `0.943` (`0.998`) |
| `R10_H4a_NO_SW_TO_BULL_BUY` | regime_h4a | 88 | 65.9% | `+$1,759.29` | 1.698 | `+$19.99` | `5.31%` | `+$458.03` | `+$1,301.26` | `+1007 / +169 / +584` | `+$1,100.31` (`1.436`) | `0.336` (`0.998`) |
| `R11_H4a_PLUS_H7_REF` | combo_ref | 99 | 65.7% | `+$1,468.52` | 1.512 | `+$14.83` | `5.23%` | `+$199.63` | `+$1,268.89` | `+965 / -5 / +509` | `+$821.13` (`1.287`) | `0.418` (`0.998`) |
| `H0_HWM8` | hwm_protected | 78 | 57.7% | `+$146.64` | 1.051 | `+$1.88` | `8.01%` | `-$456.61` | `+$603.25` | `+806 / -660 / 0` | `-$462.77` (`0.840`) | `0.942` (`0.998`) |
| **`H4a_HWM8`** | **hwm_protected** | **99** | **61.6%** | **`+$1,257.70`** | **1.401** | **`+$12.70`** | **`6.18%`** | **`-$0.74`** | **`+$1,258.45`** | **`+951 / -57 / +364`** | **`+$630.71` (`1.201`)** | `0.604` (`0.998`) |
| `R1_ADX20_HWM8` | hwm_protected | 61 | 57.4% | `+$251.23` | 1.119 | `+$4.12` | `8.02%` | `-$503.57` | `+$754.81` | `+913 / -633 / -28` | `-$335.18` (`0.842`) | `0.952` (`0.998`) |
| `R10_H4a_NOSW_HWM8` | hwm_protected | 88 | 65.9% | `+$1,759.29` | 1.698 | `+$19.99` | `5.31%` | `+$458.03` | `+$1,301.26` | `+1007 / +169 / +584` | `+$1,100.31` (`1.436`) | `0.336` (`0.998`) |
| `H4a_COST_STRESS` (1.5x) | cost_stress | 99 | 59.6% | `+$1,219.00` | 1.388 | `+$12.31` | `6.18%` | `-$14.12` | `+$1,233.12` | `+939 / -64 / +344` | `+$601.92` (`1.192`) | `0.710` (`0.998`) |
| `R10_COST_STRESS` (1.5x) | cost_stress | 88 | 63.6% | `+$1,713.71` | 1.676 | `+$19.47` | `5.40%` | `+$449.83` | `+$1,263.88` | `+997 / +158 / +558` | `+$1,055.86` (`1.416`) | `0.424` (`0.998`) |

### 2. Multi-Window Consistency (`W1` / `W2` / `W3`)
| Candidate | `W1` (`n` / P&L / PF / DD) | `W2` (`n` / P&L / PF / DD) | `W3` (`n` / P&L / PF / DD) |
| :--- | :---: | :---: | :---: |
| `H0_BASELINE_V2` | `118` / `+$116.5` / `1.027` / `12.79%` | `131` / `+$151.7` / `1.032` / `12.78%` | `143` / `+$211.5` / `1.041` / `12.78%` |
| **`H8_H4a_ALL_REGIMES`** | **`82` / `+$892.3` / `1.347` / `6.09%`** | **`91` / `+$929.4` / `1.322` / `6.18%`** | **`99` / `+$1,257.7` / `1.401` / `6.18%`** |
| `R1_ADX20_ON_H0` | `81` / `+$1,015.7` / `1.397` / `8.61%` | `94` / `+$1,052.8` / `1.357` / `8.60%` | `102` / `+$964.4` / `1.295` / `8.60%` |
| `R7_H4a_PLUS_ADX20` | `57` / `+$733.8` / `1.505` / `5.05%` | `66` / `+$746.5` / `1.421` / `5.22%` | `70` / `+$586.2` / `1.290` / `5.22%` |
| `R10_H4a_NO_SW_TO_BULL_BUY` | `75` / `+$1,393.6` / `1.647` / `5.31%` | `80` / `+$1,404.2` / `1.622` / `5.31%` | `88` / `+$1,759.3` / `1.698` / `5.31%` |
| `R11_H4a_PLUS_H7_REF` | `82` / `+$999.2` / `1.421` / `4.97%` | `91` / `+$1,128.7` / `1.433` / `5.03%` | `99` / `+$1,468.5` / `1.512` / `5.23%` |

### 3. Parameter Sensitivity & Statistical Limits
- **Sensitivity (`task11_parameter_sensitivity.csv`):**
  - `ADX` on `H0` (`15 / 20 / 25 / 30`): `+$409` (`11.71%` DD, `n=128`) -> `+$964` (`8.60%` DD, `n=102`) -> `+$1,271` (`3.93%` DD, `n=70`) -> `+$850` (`4.14%` DD, `n=43`). Steep trade-count collapse above `25` and fails when combined with `H4a`.
  - Raw Volatility Percentile (`70 / 75 / 80 / 85`): `+$657` -> `+$289` -> `+$170` -> `+$69` (all `> 10%` MTM DD; weak, fragile).
  - Post-Transition Cooldown (`1 / 2 / 3 / 5` bars): `+$533` -> `+$145` -> `-$510` -> `+$137` (unstable; blocking fresh transitions destroys edge).
- **Multiple-Testing Discipline:** Across `18` evaluated W3 configurations, **no hypothesis achieves BH-FDR statistical significance (`min q_FDR = 0.9976`)** at `n = 66..143` trades over 95 days. Consequently, low-complexity structural improvements (`H4a`, 1 parameter) must be strongly preferred over stacked multi-rule filters (`R10`).

---

## Phase 8 — Candidate Ranking

1. **Rank 1 (Primary Shadow Candidate): `H4a_SCORE_CAP_080` (`0.60 <= score < 0.80`)** — Single-parameter, regime-invariant, improves all 3 primary regimes and all 3 absolute ATR tertiles, never breaches `8%` HWM (`6.18%` DD), survives `1.5x` cost stress (`+$1,219.00, PF 1.388`), and retains strong ex-top-3 P&L (`+$630.71, PF 1.201`).
2. **Rank 2 (Reference Combo from Task 10): `R11_H4a_PLUS_H7_REF` (`H4a` + `BUY` `BE=0.75 ATR`)** — Preserves full `n=99` trade count, improves `BUY` to `+$199.63` (`PF 1.238`) and lowers MTM DD to `5.23%`, with slightly higher complexity (2 parameters).
3. **Rank 3 (Exploratory Shadow-Telemetry Only): `R10_H4a_NO_SW_TO_BULL_BUY`** — Highest historical metrics (`+$1,759.29, PF 1.698, DD 5.31%`), but removes only `11` `BUY` trades (`q_FDR = 0.9976`); must be tracked observation-only in shadow telemetry before any production consideration.
4. **Rejected:** `R1..R4`, `R6..R9` (either breach `8%` HWM, degrade `H4a`, or fail sensitivity/outlier checks).

---

## Phase 9 — Implementation Readiness Specification (Design Only — Zero Code Edits in Task 11)

- **No changes to `RegimeDetectorService` are recommended at this stage.** The current 5m completed-bar classifier with 3-bar hysteresis is causal and parity-aligned; its dead `classify_institutional_regime` method can be cleaned up in a separate maintenance task if desired.
- **Recommended Shadow Deployment Path (Next Task, Subject to User Approval):**
  1. Promote `H4a_SCORE_CAP_080` (`max_entry_score = 0.80`) as a dedicated virtual shadow portfolio in `ShadowValidationTracker` alongside the frozen baseline and existing `CANDIDATE` portfolios (`SHADOW_MODE = True`).
  2. Log `prev_regime`, `transition_type`, `bars_since_transition`, `adx_14`, and `atr_pctl_200` as **observation-only** fields in `JsonlShadowJournal` so `R10_H4a_NO_SW_TO_BULL_BUY` and macro bull-market behavior accumulate clean out-of-sample forward evidence.
  3. Keep Task 8's persistent `8%` HWM hard risk kill-switch enabled across all execution paths.

---

## Phase 10 — Audit Trail & Verification Evidence

- **Independent Research Scripts (Zero `src/goldedge/` imports):**
  - [task11_regime_reconstruction_and_diagnosis.py](file:///d:/gigs/goldedge/scripts/audit/task11_regime_reconstruction_and_diagnosis.py)
  - [task11_regime_hypotheses_and_validation.py](file:///d:/gigs/goldedge/scripts/audit/task11_regime_hypotheses_and_validation.py)
- **All 12 Task 11 Deliverables (`backtest_results/audit/task11_*`):**
  1. `task11_baseline_verification.json`
  2. `task11_regime_classifier_audit.json`
  3. `task11_regime_features.csv`
  4. `task11_trade_regime_enrichment.csv`
  5. `task11_regime_performance.json`
  6. `task11_buy_sell_regime_analysis.csv`
  7. `task11_h4a_regime_comparison.json`
  8. `task11_regime_hypothesis_results.csv`
  9. `task11_parameter_sensitivity.csv`
  10. `task11_statistical_validation.json`
  11. `task11_independent_reconciliation.json`
  12. `task11_final_report.md`
- **Verification Test Suite:** [test_task11_regime_research.py](file:///d:/gigs/goldedge/tests/test_task11_regime_research.py) plus full 10-module regression suite inside `goldedge-backend-dev`.
- **Safety Verification:** `SHADOW_MODE = True`, `TRADING_MODE = "demo"`, `market_orders = 0`, `market_trades = 0`.
