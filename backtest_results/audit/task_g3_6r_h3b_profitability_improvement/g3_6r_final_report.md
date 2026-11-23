# TASK G3.6R — GoldEdge H3b Backtest Profitability Improvement, Controlled Experiments & Actual Application Verification

**Project:** GoldEdge — `XAUUSD` Spot Gold CFD  
**Task:** `G3.6R` — Actual-Application H3b Backtest Profitability Improvement  
**Previous:** `G3.5R` — H3b Baseline/E1 Execution Reconciliation (`VERIFIED`, `EXACTLY_RECONCILED`)  
**Scope:** Historical backtest development ONLY (`SHADOW_MODE = True`, `market_orders = 0`, `market_trades = 0`, `HWM = 8.0%`; no shadow/live changes, no ML integration).  
**Generated (UTC):** `2026-11-23T11:40:48Z`

---

## 1. Executive Verdicts (§9 Mandatory Separation)

| Dimension | Verdict | Evidence-Based Summary |
| :--- | :---: | :--- |
| **Engineering Verdict** | **`VERIFIED`** | Complete 215-trade Phase A attribution & drawdown audit; `3` prespecified non-ML causal interventions implemented in `RunBacktestUseCase` behind isolated default-OFF opt-in flags (`max_kaufman_er=None`, `be_trigger_atr=1.0`, `min_score_threshold=0.60`); all `5` full `6.2`-month actual application backtests executed (`H0`, `H3b`, `Change A/B/C`); `6/6` adversarial checks `PASS`; `13/13` regression suites `PASS`. |
| **Profitability Improvement Verdict** | **`EXPLORATORY_ONLY`** | All `3` interventions improved full `6.2`-month actual-application Net Profit, Net R, PF, and Tick-MTM DD and survived `1.5x` & `2.0x` cost stress — led by **`Change A (max_kaufman_er=0.50)`** (`+\$263.64 -> +\$596.17`, `PF 1.072 -> 1.224`, `MTM DD 5.64% -> 3.20%`, `80.0%` coverage) and **`Change C (min_score=0.68)`** (`+\$512.53`, `PF 1.172`, `MTM DD 4.50%`, improved in BOTH subperiods). **However**, paired daily tests across `102` active days remain underpowered (`Change A`: `p = 0.361`, `BH-FDR q = 0.447`; `Change C`: `p = 0.447`, `q = 0.447`) and the May–Nov history is **research-exposed**. Therefore, gains are strictly **`EXPLORATORY_ONLY`**, pending fresh unseen prospective validation (`G3.7`) or `12–24m` backfill. |
| **Application Reconciliation** | **`EXACTLY_RECONCILED`** | Clean-room independent execution auditor (AST-verified zero `src/goldedge/` imports) replayed every trade from raw PostgreSQL ticks (`v_market_ticks_expanded_6m_v1`) across all **3 runs (`564/564` trades total, `0` mismatches)**: **`H3B_FROZEN`** (`215/215`, `+\$263.64` vs `+\$263.64`, DD `-11.1722 R`), **`H3B_CHANGE_A_ER50`** (`172/172`, `+\$596.17` vs `+\$596.17`, DD `-6.4839 R`), and **`H3B_CHANGE_C_SCORE68`** (`177/177`, `+\$512.53` vs `+\$512.53`, DD `-9.1453 R`; `75` skipped H3b entries, `37` newly eligible later entries unlocked, `0` score-floor violations, `min_score = 0.6817`). Frozen `111`-trade and `70`-trade SHA-256s intact. |

---

## 2. Phase A — Why Frozen H3b Has Weak Expectancy (`PF ~1.07`)

Full-period continuous `RunBacktestUseCase` (`2026-05-17 .. 2026-11-19`, `215` trades, `87` active days, `0.5%` risk, Fusion Markets `\$12/\$1M/side` + `0.0533p/side` slippage):
- **Reconciling `+5.86 R` vs `+\$246.56` (2-chunk) / `+\$263.64` (continuous):** Floor `0.01`-lot rounding lowers average planned risk from `\$50.00` to `\$47.40` (range `\$38–\$52`); continuous compounding adds `+\$17.08` over the 2-chunk reset (`+\$263.64`, exact `+5.8581 R`).
- **Three Primary Structural Failure Mechanisms Identified:**
  1. **Exhausted-Impulse Chasing (`kaufman_er_20` top tercile):** `T3_HIGH` (`n = 72`) lost **`-10.79 R` (`-\$546.27`, `PF 0.66`, `WR 41.7%`)** vs `T1+T2` (`+16.65 R`, `PF 1.40`) — confirmed in `DEV_MAY_AUG` (`-3.08 R` vs `+9.46 R`). Entering after an already straight-line move catches local exhaustion peaks.
  2. **Marginal Low-Score Entries (`final_score` `0.60–0.68`):** `T1_LOW` (`n = 72`) lost **`-7.66 R` (`-\$377.61`, `PF 0.74`, `WR 41.7%`)** vs `T2+T3` (`+13.51 R`) — confirmed in `DEV_MAY_AUG` (`-2.27 R` vs `+8.64 R`).
  3. **Premature `1.0 ATR` Breakeven Shake-Outs:** `40/215` trades (`18.6%`) hit `mean MFE = 1.24 ATR`, got ratcheted to `BREAKEVEN` (`entry ± \$0.20`), and exited flat (`-0.01 R`) before reaching `TRAILING_SL` (`+0.77 R`) or `TARGET` (`+1.95 R`).
  - *Secondary observations:* `BUY` (`n = 77`, `-\$40.91`, `PF 0.97`) trailed `SELL` (`n = 138`, `+\$304.55`, `PF 1.13`); `SIDEWAYS` (`n = 58`, `-\$110.67`, `PF 0.91`) and `09:00 UTC` (`n = 36`, `-\$344.50`, `PF 0.51`) were concentrated loss pockets.

---

## 3. Phases C–D — Controlled Actual-Application Backtest Results (`2026-05-17 .. 2026-11-19`)

All 5 configurations ran end-to-end through actual `RunBacktestUseCase` on identical `v_market_ticks_expanded_6m_v1` data, warmup, costs, `0.5%` risk, and `8.0%` HWM kill-switch (`0` halts across all 5 runs):

| Configuration (`RunBacktestUseCase`) | Trades (`Cov %`) | Net P&L (`Delta`) | Total Net `R` (`Delta R`) | Mean `R` | PF | Tick-MTM MaxDD (`Delta`) | Paired Daily `Delta R` (`95% CI`) | Day Perm `p` (`BH-FDR q`) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `H0_ORIGINAL` (`17:00`, no E1) | `265` (`123.3%`) | `-\$301.47` | `-6.34 R` | `-0.0239` | `0.935` | `6.80%` | — | — |
| **`H3B_FROZEN`** *(Baseline)* | `215` (`100.0%`) | `+\$263.64` | `+5.86 R` | `+0.0273` | `1.072` | `5.64%` | Baseline | — |
| **`H3B_CHANGE_A_ER50`** (`max_kaufman_er=0.50`) | **`172` (`80.0%`)** | **`+\$596.17` (`+\$332.53`)** | **`+11.96 R` (`+6.11 R`)** | **`+0.0696`** | **`1.224`** | **`3.20%` (`-2.44%`)** | `+0.0599` (`[-0.070, +0.190]`) | `0.361` (`0.447`) |
| `H3B_CHANGE_B_BE14` (`be_trigger_atr=1.4`) | `201` (`93.5%`) | `+\$421.09` (`+\$157.45`) | `+9.30 R` (`+3.45 R`) | `+0.0463` | `1.107` | `5.57%` (`-0.07%`) | `+0.0338` (`[-0.051, +0.118]`) | `0.447` (`0.447`) |
| **`H3B_CHANGE_C_SCORE68`** (`min_score=0.68`) | **`177` (`82.3%`)** | **`+\$512.53` (`+\$248.89`)** | **`+10.69 R` (`+4.83 R`)** | **`+0.0604`** | **`1.172`** | **`4.50%` (`-1.14%`)** | `+0.0474` (`[-0.074, +0.168]`) | `0.447` (`0.447`) |

---

## 4. Phase F — Subperiod, Directional & Cost-Stress Robustness

| Configuration | `DEV_MAY_AUG` (`P&L / PF`) | `LATER_SEP_NOV` (`P&L / PF`) | `BUY` (`P&L / PF`) | `SELL` (`P&L / PF`) | `1.5x` Cost Stress (`P&L / PF`) | `2.0x` Cost Stress (`P&L / PF`) | Single-Day Fragile? |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `H0_ORIGINAL` | `-\$262.62 / 0.894` | `-\$38.86 / 0.982` | `-\$360.21 / 0.780` | `+\$58.74 / 1.020` | `-\$398.71 / 0.915` | `-\$495.95 / 0.895` | — |
| `H3B_FROZEN` | `+\$305.43 / 1.181` | `-\$41.78 / 0.979` | `-\$40.91 / 0.969` | `+\$304.55 / 1.128` | `+\$176.83 / 1.047` | `+\$90.02 / 1.024` | — |
| **`H3B_CHANGE_A_ER50`** | `+\$232.40 / 1.173` | **`+\$363.77 / 1.276`** | **`+\$24.12 / 1.024`** | **`+\$572.04 / 1.345`** | **`+\$527.44 / 1.195`** | **`+\$458.70 / 1.168`** | **No** (`+\$2.38/d` ex-best) |
| `H3B_CHANGE_B_BE14` | `+\$284.05 / 1.151` | `+\$137.03 / 1.066` | `-\$178.46 / 0.883` | `+\$599.55 / 1.248` | `+\$340.13 / 1.085` | `+\$259.17 / 1.064` | No |
| **`H3B_CHANGE_C_SCORE68`** | **`+\$439.37 / 1.318`** | **`+\$73.16 / 1.046`** | **`+\$410.99 / 1.475`** | **`+\$101.54 / 1.048`** | **`+\$439.38 / 1.145`** | **`+\$366.23 / 1.119`** | **No** (`+\$1.42/d` ex-best) |

- **Key nuance:** `Change A (max_kaufman_er=0.50)` delivered the largest overall profit (`+\$596.17`, `PF 1.224`) and biggest drawdown cut (`3.20%` MTM DD) by turning `LATER_SEP_NOV` from `-\$41.78` into `+\$363.77`, while `Change C (min_score=0.68)` was the only candidate to outperform frozen `H3b` in **both** `DEV` (`+\$439.37`) and `LATER` (`+\$73.16`) and lifted `BUY` strongly (`PF 1.475`). `Change B (be_trigger_atr=1.4)` hurt `BUY` (`PF 0.883`) and is not recommended.

---

## 5. Phase E — Independent Clean-Room Verification & Regression

- **Clean-Room Auditor** ([task_g3_6r_phase_e_independent.py](file:///d:/gigs/goldedge/scripts/audit/task_g3_6r_phase_e_independent.py)): AST-verified **ZERO `src/goldedge/` imports**; directly replayed all `215` frozen `H3B_FROZEN` trades, all `172` modified `H3B_CHANGE_A_ER50` trades, and all `177` modified `H3B_CHANGE_C_SCORE68` trades (**`564` trades total**) from raw PostgreSQL ticks → **`PASS` (`0` mismatches)** on entry price, exit price, exit reason, commission, P&L (`\$263.64`, `\$596.17`, and `\$512.53`), net R, and cumulative drawdown (`-11.1722`, `-6.4839`, and `-9.1453`). For `Change C`, also verified `140` retained common entries, `75` skipped low-score H3b entries, `37` newly eligible later entries unlocked by freeing the 1-position slot, and `0` score-floor violations (`min_score = 0.6817`).
- **Candidate Separation Discipline:** `Change A` and `Change C` are kept strictly **separate** (not combined) to avoid adding an extra historical optimization choice on research-exposed data.
- **Adversarial & Opt-In Parity Tests (`6/6 PASS` in [g3_6r_adversarial_test_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_adversarial_test_results.json)):** Confirmed default-OFF opt-in parity (`max_kaufman_er=None`, `be_trigger_atr=1.0`), exact `1.0` vs `1.4` ATR breakeven behavior, and safety locks.
- **Full Regression Suite (`13/13 PASS` in [g3_6r_regression_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_regression_results.json)):** `test_task_g3_6r_validation.py` through `test_task8_hwm_kill_switch.py` (including all `14/14` G3.5R canonical execution regression tests).

---

## 6. Mandatory §9 Final Answers

1. **Why does current H3b have only `~PF 1.07`?** Exhausted high-ER entries (`-10.79 R`), weak `0.60–0.68` score entries (`-7.66 R`), and premature `1.0 ATR` breakeven shake-outs (`40` trades, `-0.01 R` despite `1.24 ATR` MFE) offset its `TARGET`/`TRAILING_SL` winners.
2. **Which trades/conditions drive weak expectancy?** Top-tercile `kaufman_er >= 0.47` (`PF 0.66`), low score `0.60–0.68` (`PF 0.74`), `SIDEWAYS` (`PF 0.91`), and `09:00 UTC` (`PF 0.51`).
3. **Are losses concentrated in BUY, SELL, entry quality, exits, or regimes?** Primarily **entry quality** (chasing exhausted ER moves and marginal `0.60–0.68` scores), with `BUY` (`PF 0.97`) weaker than `SELL` (`PF 1.13`).
4. **Which three hypotheses were tested?** `Change A: max_kaufman_er=0.50`, `Change B: be_trigger_atr=1.4`, `Change C: min_score_threshold=0.68` (strictly isolated, opt-in default-OFF in `RunBacktestUseCase`).
5. **Unchanged H3b vs. modified results?** Frozen H3b: `215` trds, `+\$263.64`, `PF 1.072`, `5.64%` MTM DD. `Change A`: `172` trds, `+\$596.17`, `PF 1.224`, `3.20%` MTM DD. `Change C`: `177` trds, `+\$512.53`, `PF 1.172`, `4.50%` MTM DD. `Change B`: `201` trds, `+\$421.09`, `PF 1.107`, `5.57%` MTM DD.
6. **Did any modification improve net expectancy across historical periods?** `Change C` improved over H3b in **both** `DEV` (`+\$439` vs `+\$305`) and `LATER` (`+\$73` vs `-\$42`); `Change A` was strongly positive in both (`+\$232` and `+\$364`) with massive improvement in `LATER`, though paired daily `p = 0.36–0.45` (`q = 0.45`) due to `6.2`-month sample power.
7. **Did improvements survive cost and drawdown stress?** **Yes** — both `Change A` (`+\$458.70`, `PF 1.168`) and `Change C` (`+\$366.23`, `PF 1.119`) stayed comfortably positive even at `2.0x` severe cost stress, with lower Tick-MTM DD (`3.20%` and `4.50%` vs `5.64%`).
8. **Did the independent validator reproduce every modified trade?** **Yes** — `564/564` trades (`215` `H3B_FROZEN` + `172` `H3B_CHANGE_A_ER50` + `177` `H3B_CHANGE_C_SCORE68`) matched with `0` mismatches, including Change C's `140` retained entries, `75` skipped low-score H3b entries, `37` newly eligible later entries, and `0` score-floor violations (`min_score = 0.6817`). Evidence: [g3_6r_independent_reconciliation.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_independent_reconciliation.json), [g3_6r_trade_by_trade_reconciliation.parquet](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_trade_by_trade_reconciliation.parquet) (`SHA-256: 02EF5098D5E3C9A47D53588F5B9E925AC73AD954A283DA0F345DCCCFD05C98F5`), [g3_6r_adversarial_test_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_adversarial_test_results.json) (`6/6 PASS`), and [g3_6r_regression_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_regression_results.json) (`13/13 PASS`).
9. **Were any implementation defects discovered and fixed?** Zero application defects; all opt-in flags passed unit and regression verification cleanly.
10. **What strategy change is supported for further testing?** Keep frozen H3b defaults unchanged for now; **`Change A (max_kaufman_er=0.50)`** and **`Change C (min_score_threshold=0.68)`** are the two strongest **`EXPLORATORY_ONLY`** candidates to evaluate next on a `12–24m` cTrader backfill or prospective unseen shadow tracking (`G3.7`, once separately approved).

---

## 7. Deliverable Index (`14/14` Present)

Stored in `backtest_results/audit/task_g3_6r_h3b_profitability_improvement/`:
1. [g3_6r_baseline_manifest.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_baseline_manifest.json)
2. [g3_6r_baseline_diagnosis.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_baseline_diagnosis.json)
3. [g3_6r_trade_attribution.parquet](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_trade_attribution.parquet)
4. [g3_6r_drawdown_attribution.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_drawdown_attribution.json)
5. [g3_6r_structural_hypotheses.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_structural_hypotheses.json)
6. [g3_6r_controlled_experiments.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_controlled_experiments.json)
7. [g3_6r_subperiod_robustness.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_subperiod_robustness.json)
8. [g3_6r_cost_stress.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_cost_stress.json)
9. [g3_6r_application_implementation_manifest.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_application_implementation_manifest.json)
10. [g3_6r_trade_by_trade_reconciliation.parquet](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_trade_by_trade_reconciliation.parquet)
11. [g3_6r_independent_reconciliation.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_independent_reconciliation.json)
12. [g3_6r_adversarial_test_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_adversarial_test_results.json)
13. [g3_6r_regression_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_regression_results.json)
14. [g3_6r_final_report.md](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_6r_h3b_profitability_improvement/g3_6r_final_report.md)
