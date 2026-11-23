# Task 9 Final Report — XAUUSD BUY vs SELL Strategy Performance Diagnosis

**Final Verdict:** `VERIFIED`
**Safety Status:** `SHADOW_MODE = True` | `TRADING_MODE = demo` | `market_orders = 0` | `market_trades = 0` | Zero edits to `src/goldedge/`, Task 4 baseline (`SHA-256: 73306E15...`), or `RECOVERED_FULL_HISTORY_BASELINE_V2` (`W3 SHA-256: 2021b78a...`).

---

## 1. Phase 1 Verification & BUY vs SELL Performance Breakdown

All frozen V1 and V2 dataset/trade SHA-256 fingerprints verified bit-exact. Independent raw-tick reproduction (`task9_trade_telemetry_extractor.py`, zero `src/goldedge` imports) matched **143 / 143 trades** and all trailing-SL journey steps (`0` mismatches, `max_pnl_diff = $0.000000`, `final_equity = $10,211.48`, `mtm_max_dd = 12.78%`). The Task 8 `8%` HWM kill-switch variant is reported separately (triggered `2026-10-16T07:39:25.668Z`) so post-halt trades are not silently excluded from diagnostic root-cause analysis.

| Variant & Subset | Trades | Wins / Losses | Win Rate | Gross P&L | Net P&L | PF | Expectancy (95% CI) | Avg Win / Avg Loss | Closed MaxDD | Tick-MTM MaxDD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **V2 Baseline — ALL** | 143 | 79 / 64 | 55.24% | +$423.23 | **+$211.48** | 1.0414 | +$1.48 (`[-13.60, +16.56]`) | $67.35 / -$79.84 | -$1,336.28 (`12.17%`) | -$1,409.22 (`12.78%`) |
| **V2 Baseline — BUY** | **41** | **20 / 21** | **48.78%** | **-$392.70** | **-$450.50** | **0.7142** | **-$10.99** (`[-37.66, +15.68]`) | **$56.29 / -$75.06** | -$732.07 | — |
| **V2 Baseline — SELL** | **102** | **59 / 43** | **57.84%** | **+$815.93** | **+$661.98** | **1.1874** | **+$6.49** (`[-11.91, +24.89]`) | **$71.10 / -$82.17** | -$986.49 | — |
| *Task 8 (`8%` HWM) — ALL* | *78* | *45 / 33* | *57.69%* | *+$264.83* | *+$146.64* | *1.0506* | *+$1.88 (`[-19.33, +23.09]`)* | *$67.66 / -$87.82* | *-$830.32 (`7.56%`)* | *-$883.65 (`8.01%`)* |
| *Task 8 — BUY / SELL* | *29 / 49* | *14W / 31W* | *48.3% / 63.3%* | *-$415.1 / +$679.9* | *-$456.61 / +$603.25* | *0.642 / 1.372* | *-$15.75 / +$12.31* | — | — | — |

---

## 2. Root Causes of BUY-Side Losses (`-$450.50`, `n=41`)

Four empirical mechanisms account for BUY's negative expectancy (`-$10.99/trade`):

1. **`07:00 UTC` London Pre-Open Fakeout Bleed (`-$378.38` = `84.0%` of BUY net loss):**
   - BUY at `07:00–07:59 UTC`: `n = 8`, `2W / 6L` (`25.0%` WR), **`-$378.38`** (`PF 0.240`, `-$47.30/trade`), losing heavily in both `P1` (`-$191.7`) and `P2` (`-$208.8`) and barely flat in `P3` (`+$22.2`).
   - By contrast, SELL at `07:00–07:59 UTC`: `n = 16`, `13W / 3L` (`81.25%` WR), **`+$512.78`** (`PF 5.003`, `+$32.05/trade`), profitable in **all three periods** (`P1 +$147.3`, `P2 +$152.3`, `P3 +$213.2`).
2. **Saturated / Late-Exhaustion Entry Scoring (`TradeScorer` Inversion):**
   - `BUY` with `score >= 0.75`: `n = 22`, `40.9%` WR, **`-$422.62` (`PF 0.5088`)** vs `score [0.60, 0.75)`: `n = 19`, `57.9%` WR, `-$27.88` (`PF 0.9611`).
   - `TradeScorer` adds up to `+0.08` when `kalman_accel` aligns with direction (`kalman_accel_aligned == 1`). On BUY, aligned acceleration trades (`n = 23`) lost **`-$487.08` (`39.1%` WR, `PF 0.512`)**, whereas decelerating/pullback acceleration (`n = 18`) made **`+$36.58` (`61.1%` WR, `PF 1.063`; `comp_accel_adj` Mann-Whitney `p = 0.0304`, Spearman `r_s = -0.262`)**.
3. **Late-Trend Extension Forced by `TRENDING_BULL`-Only Selection:**
   - All `41/41` BUY trades entered strictly in `TRENDING_BULL` via `bullish_momentum` (zero trades from `kinematic_kalman_trend`, `vwap_momentum_breakout`, or `session_orb`).
   - Average BUY entered **`+1.66 ATR` above `EMA20`**, **`+3.20 ATR` above `EMA50`**, **`+6.22 ATR` above `tick_weighted_price`**, after a **`+2.27 ATR` prior 1h run-up** and **`+5.57 ATR` prior 4h run-up**, within **`0.86 ATR` of the 20-bar high** (`<= 0.15 ATR` of 20-bar high: `n = 6`, `1W/5L`, **`-$273.84`**, `PF 0.226`).
4. **Post-Entry Profit Reversal & Smaller Payoff Asymmetry:**
   - BUY trades reached `+0.5 ATR` profit slightly **more** often than SELL (`73.17%` vs `69.61%`), and hit BE `+1.0 ATR` at a comparable rate (`51.22%` vs `54.90%`), yet **twice as many BUY trades reversed from `+0.5 ATR` unrealized profit into a net loss (`24.39%` vs `11.76%`, `p = 0.0598`)**.
   - Only **`21.95%`** of BUYs reached Trailing Tier 1 (`+1.6 ATR`) vs **`36.27%`** of SELLs (`p = 0.099`), capping BUY average winner at **`$56.29`** (`0.75R` vs `-$75.06` average loss) compared to **`$71.10`** on SELL. One BUY `BREAKEVEN` exit also suffered a `-$12.37` stop gap, leaving `BREAKEVEN` exits net negative on BUY (`-$4.73`, `PF 0.586`) vs positive on SELL (`+$10.22`, `PF 3.858`).

---

## 3. Explanation of SELL-Side Performance (`+$661.98`, `n=102`)

SELL outperformed BUY by **`+$1,112.48`** net (`PF 1.1874` vs `0.7142`) due to three structural advantages and one macro tail-wind:

1. **Both `SIDEWAYS` and `TRENDING_BEAR` Access (`+$339.82` + `+$322.16`):**
   - Unlike BUY (banned in `SIDEWAYS`), SELL operated in both `SIDEWAYS` (`n = 38`, `60.53%` WR, `+$339.82`, `PF 1.244`) and `TRENDING_BEAR` (`n = 64`, `56.25%` WR, `+$322.16`, `PF 1.1505`).
   - Furthermore, gold's net decline across `Aug 17 -> Nov 18` (`~$4,407` -> `~$3,890`–`$4,070`) produced `2.5x` as many `H1`-aligned SELL setups (`102` vs `41`).
2. **Strong `07:00 UTC` & `10:00`/`13:00 UTC` Session Edge:**
   - `07:00 UTC` SELL (`n = 16`, `81.25%` WR, `+$512.78`, `PF 5.003`), `10:00 UTC` (`n = 12`, `+$230.64`, `PF 1.952`), and `13:00 UTC` (`n = 6`, `+$402.98`, `PF 5.002`) drove virtually all SELL profits, offset by `11:00 UTC` (`-$266.61`) and `16:00 UTC` (`-$214.75`).
3. **Critical Caveat — SELL Also Suffers Severely From Exhaustion Chasing:**
   - **`kaufman_er` (FDR-Significant):** SELL losers had much higher Kaufman Efficiency Ratio than SELL winners (`0.4448` vs `0.3497`, Mann-Whitney **`p = 0.00945`**, BH-FDR **`q = 0.03998 < 0.05`**, Spearman `-0.185`). High straight-line ER before entry signals imminent mean-reversion snapback.
   - **Score Band Inversion:** `SELL` with `score [0.60, 0.75)` generated **`+$1,114.74`** (`n = 40`, `67.5%` WR, `PF 2.136`, positive in all 3 subperiods: `P1 +$505.3`, `P2 +$281.1`, `P3 +$328.4`, `p = 0.0345`), while `SELL` with `score >= 0.75` lost **`-$452.76`** (`n = 62`, `51.6%` WR, `PF 0.823`)!
   - **Period Decay:** SELL made `+$931.84` in `P1` (`PF 2.83`), lost `-$328.59` in `P2` (`PF 0.70`), and returned `+$58.72` in `P3` (`PF 1.03`).

---

## 4. Candidate Signal Gate Audit (`2,159` Signals Fired on W3)

- **`BLOCK_H1_COUNTER`:** Blocked `420` BUY signals vs `193` SELL signals (reflecting bearish macro H1 slope across Sep–Nov).
- **`BLOCK_BUY_SIDEWAYS`:** Blocked `35` BUY signals (`mean score 0.793`, `68.6%` green candles); Phase 7 simulation (`H2`) confirmed blocking them **saved** `+$235.21`!
- **`BLOCK_SCORE_LT_060`:** Blocked `562` BUY and `466` SELL signals where Kalman velocity opposed or stalled (`mean k_vel = -0.013` for blocked BUY, `+0.014` for blocked SELL).
- **`BLOCK_HIGH_VOL`:** Blocked `104` BUY and `189` SELL signals (`ATR > 75th pctl`).
- **Missing BUY Candle Symmetry (`_bullish_momentum`):** `100%` (`102/102`) of SELL entries closed `close < open`, whereas **`46.3%` (`19/41`)** of BUY entries occurred on red candles (`close <= open`). However, chronological OOS validation proved removing red candles actually *worsens* `P3` (`-$68.15`) because pullback entries on red candles avoid buying bar-high tops!

---

## 5. Phase 7 — Independent Causal Hypothesis Testing (`H1`–`H11` vs Unchanged `H0`)

All hypotheses were evaluated independently from raw `market_ticks` across `W3` (`Aug 17–Nov 18`), preserving open-slot dynamics, cooldowns, equity compounding, slippage (`0.0533p`), and Fusion demo commission (`$12/M`):

| Hypothesis | `n` (`Ret / Rem / New`) | WR% | Net P&L | PF | Exp (`$/tr`) | Closed DD | MTM DD% | BUY P&L (`n`, PF) | SELL P&L (`n`, PF) | P1 / P2 / P3 (`$`) | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| **`H0_BASELINE_V2`** | 143 (`143/0/0`) | 55.2% | +$211.48 | 1.041 | +1.48 | -$1,336 | 12.78% | -$450.5 (`41`, 0.71) | +$662.0 (`102`, 1.19) | +806 / -702 / +107 | Control |
| **`H4a_SCORE_CAP_080`** | **99** (`74/69/25`) | **61.6%** | **+$1,257.70** | **1.401** | **+12.70** | **-$649** | **6.18%** | **-$0.7** (`30`, 1.00) | **+$1,258.5** (`69`, 1.63) | **+951 / -57 / +364** | **Supported (#1 Atomic)** |
| **`H5_CAP_ER_045`** | **105** (`87/56/18`) | **61.0%** | **+$896.08** | **1.266** | **+8.53** | -$820 | 8.26% | **+$67.5** (`29`, 1.07) | **+$828.6** (`76`, 1.34) | **+743 / -165 / +318** | **Supported (#2 Atomic)** |
| **`H9_BUY_NO_HIGH_CHASE_015`** | 142 (`137/6/5`) | 57.0% | +$708.06 | 1.141 | +4.99 | -$1,355 | 12.55% | +$37.6 (`40`, 1.03) | +$670.5 (`102`, 1.18) | +1,147 / -618 / +179 | Caution (`n=6` rem fragile) |
| **`H1_BLOCK_BUY_07UTC`** | **136** (`134/9/2`) | **57.4%** | **+$562.05** | **1.116** | **+4.13** | **-$1,055** | **10.10%** | **-$140.9** (`34`, 0.88) | +$703.0 (`102`, 1.19) | **+933 / -497 / +126** | **Supported (#3 Atomic)** |
| **`H4b_SCORE_CAP_075`** | 86 (`57/86/29`) | 59.3% | +$512.26 | 1.179 | +5.96 | -$814 | 7.76% | -$143.5 (`27`, 0.86) | +$655.8 (`59`, 1.36) | +685 / -283 / +110 | Supported (over-prunes `0.75-0.80`) |
| **`H7_BUY_FAST_BE_075_T1_12`** | **143** (`142/1/1`) | 55.9% | **+$452.79** | 1.093 | +3.17 | -$1,218 | 11.73% | -$235.1 (`41`, 0.82) | +$687.9 (`102`, 1.19) | **+884 / -697 / +266** | **Supported (#4 Exit)** |
| **`H6_SESSION_CLOSE_15UTC`** | 110 (`110/33/0`) | 57.3% | +$450.66 | 1.110 | +4.10 | -$1,210 | 11.62% | -$372.2 (`30`, 0.70) | +$822.8 (`80`, 1.29) | +1,182 / -549 / **-182** | **Rejected (fails OOS P3)** |
| **`H8_EARLY_FAIL_E1_15M`** | 146 (`141/2/5`) | 53.4% | +$324.20 | 1.064 | +2.22 | -$1,172 | 11.34% | -$84.7 (`42`, 0.94) | +$408.9 (`104`, 1.11) | +903 / -537 / **-42** | **Rejected standalone** (hurts SELL & P3) |
| **`H3_BUY_BULL_CANDLE`** | 134 (`122/21/12`) | 56.7% | +$264.00 | 1.054 | +1.97 | -$1,198 | 11.79% | -$437.4 (`32`, 0.68) | +$701.4 (`102`, 1.20) | +930 / -597 / **-68** | **Rejected (fails OOS P3)** |
| **`H2_UNBLOCK_BUY_SIDEWAYS`** | 159 (`137/6/22`) | 52.2% | **-$23.73** | **0.996** | -0.15 | -$1,456 | **14.22%** | **-$681.2** (`57`, 0.69) | +$657.5 (`102`, 1.19) | +691 / -798 / +82 | **Rejected (strongly harmful)** |
| **`H10_V2_WITH_HWM_8PCT`** | 78 (`78/65/0`) | 57.7% | +$146.64 | 1.051 | +1.88 | -$830 | **8.01%** | -$456.6 (`29`, 0.64) | +$603.3 (`49`, 1.37) | +806 / -660 / 0 | Risk-Protected V2 Baseline |
| **`H11_COMBO_CANDIDATE`** | **97** (`68/75/29`) | **59.8%** | **+$1,291.89** | **1.463** | **+13.32** | **-$410** | **4.33%** | **+$138.5** (`27`, 1.15) | **+$1,153.4** (`70`, 1.63) | **+808 / +14 / +470** | *Exploratory Combo* (needs OOS shadow) |

---

## 6. Statistical Limitations & Multiple-Comparison Discipline

1. **Trade-Level Return Variance & Power:** With per-trade standard deviation `$84–$94`, no whole-portfolio hypothesis transition reached BH-FDR `q < 0.05` against `H0` on two-sided Mann-Whitney U (`mw_q_fdr = 0.968` across `H1–H11`; minimum raw `mw_p = 0.558` for `H11` and `0.604` for `H4a`). A `+$12/trade` edge with `$88` SD requires `~300+` trades to pass `alpha = 0.05` on trade-level tests alone.
2. **Which Underlying Signals *Did* Pass Sub-Analysis Statistical Checks?**
   - **Kaufman ER exhaustion on SELL:** Passed BH-FDR across all 55 tested features (`p = 0.00945`, **`q = 0.03998 < 0.05`**).
   - **Score Band Inversion (`[0.60, 0.75)` vs `>= 0.75`):** `p = 0.0290` combined (`p = 0.0345` SELL), positive in `P1`, `P2`, and `P3`.
   - **Kalman acceleration chasing on BUY (`comp_accel_adj`):** `p = 0.0304` (FDR `q = 0.133`), inverted directionally (`kalman_accel_aligned == 1` lost `-$487.08`).
3. **Small-`n` Caution:** Single-hour filters (`BUY 07 UTC`, `n=8` removed) and tight geometry cutoffs (`dist_20bar_high <= 0.15 ATR`, `n=6` removed) carry high sample uncertainty and must be validated on fresh forward data alongside structural fixes (`score < 0.80` / `ER <= 0.45`).

---

## 7. Recommendations Ranked by Evidence for Task 10

1. **Priority 1 — Fix Exhaustion-Chasing Scoring (`H4a: score < 0.80` or Penalize Saturated `kaufman_er > 0.45` & `kalman_accel` Spike `H5`):**
   - Strongest, cleanest single-rule improvement across BOTH BUY and SELL (`+$1,257.70`, `PF 1.401`, **cuts Tick-MTM MaxDD from `12.78%` to `6.18%`**, positive/strong in `P1`, `P2` nearly flat `-$57`, and `P3/HOLDOUT +$364`). Avoids overfitting because it corrects an empirically inverted weighting in `TradeScorer` rather than carving out narrow hours.
2. **Priority 2 — Asymmetric BUY Session & Early-Lock Adjustments (`H1: Block BUY 07 UTC` + `H7: BUY BE 0.75 / T1 1.2`):**
   - `H1` removes `84%` of BUY's losses (`+$211 -> +$562`) while preserving SELL's best hour (`07:00 UTC +$512.78`), and `H7` captures BUY's `73%` `+0.5 ATR` excursions before they reverse (`+$211 -> +$453`, positive across `P1`, `P2` improvement, and `P3 +$266`).
3. **Priority 3 — Test Combined Candidate Under Task 8 8% HWM Protection on Fresh Shadow Data Before Live Order Approval:**
   - Validate a lean Task 10 candidate (e.g., `H4a` exhaustion cap + `H1` + `H7`, or `H11_COMBO_CANDIDATE` which achieved `+$1,291.89`, `PF 1.463`, `MTM DD 4.33%`, positive in all 3 subperiods and both BUY/SELL) in `RunBacktestUseCase` across `W1`, `W2`, `W3`, and collect fresh shadow forward trades with `SHADOW_MODE = True`.

---

## 8. Phase 8 Audit & Retest Evidence

- **Unit & Integration Suite:** `Ran 55 tests in 4.677s — OK` (`0` failures, `0` errors, `0` skipped) across `test_remediation_b1_b6`, `test_shadow_validation`, `test_backtest_history`, `test_task7_remediation`, `test_task7b_quote_semantics`, `test_task6c_validation`, `test_task8_hwm_kill_switch`, and `test_task9_diagnostics`.
- **Audit Artifacts Written:**
  - `backtest_results/audit/task9_phase1_verification.json`
  - `backtest_results/audit/task9_trades_enriched_w3.csv` (`143` trades, reconciled `0` mismatches)
  - `backtest_results/audit/task9_candidate_signals_w3.csv` (`2,159` candidate signals)
  - `backtest_results/audit/task9_diagnosis_summary.json` & `task9_feature_comparison.csv` & `task9_segmented_stats.csv`
  - `backtest_results/audit/task9_hypothesis_results.json` & `task9_hypothesis_results.csv`
