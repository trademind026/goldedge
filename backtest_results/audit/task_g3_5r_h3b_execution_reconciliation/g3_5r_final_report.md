# TASK G3.5R — GoldEdge H3b Baseline/E1 Execution Discrepancy: Root Cause, Correction & Independent Verification

**Project:** GoldEdge — `XAUUSD` Spot Gold CFD  
**Task:** `G3.5R` — H3b Baseline/E1 Execution Reconciliation & Delayed-Entry Reassessment  
**Previous:** `G3.5` — Probability Calibration, H3b Economic Utility & Statistical Robustness  
**Generated (UTC):** `2026-11-23T11:02:08Z`

---

## 1. Executive Verdicts (§13 Mandatory Separation)

| Dimension | Verdict | Evidence-Based Summary |
| :--- | :---: | :--- |
| **Engineering & Verification** | **`VERIFIED`** | Original G3.5 evidence frozen & SHA-256 hashed prior to edits; all `6` confirmed research-simulator defects fixed (`0` defects found in `RunBacktestUseCase`); clean-room independent execution auditor (AST-verified zero `src/goldedge/` imports) `PASS` with **`0` mismatches across `112/112` trades**; `14/14` synthetic & adversarial execution tests `PASS`; `12/12` regression suites `PASS`. |
| **Baseline Reconciliation** | **`EXACTLY_RECONCILED`** | On the exact same `112` H3b opportunities (`39` unique OOT days), the repaired canonical simulator matches `RunBacktestUseCase` to **`+0.000000 R` total difference** (`+0.772510 R` vs `+0.772510 R`), **`0` exit-reason mismatches**, **`0.000000` max entry/exit price diff**, **`\$0.0000` max commission/P&L diff**, **`0.00000` max R diff**, and exact **`-10.9091 R` cumulative drawdown**. |
| **Delayed-Entry & Fast-Spike Research** | **`INVALIDATED`** | The G3.5 `+13.18 R` to `+21.81 R` delayed-entry gains (`WAIT_5S/10S/15S/30T`, `q = 0.0077..0.0402`) were **100% an artifact of the corrupted-ATR bug** falsely triggering `> 0.5 ATR` invalidation on bogus `-1.5 R` to `-2.25 R` `BREAKEVEN` trades. On the reconciled canonical simulator, **all `15/15` policies in the 2×2 factorial design fail multiple-testing correction with `BH-FDR q = 1.0000`**. Neither delayed entry nor the fast-spike guard justifies `G3.6` implementation. |

> [!IMPORTANT]
> **Safety & Governance Locks Intact:** `SHADOW_MODE = True`, `TRADING_MODE = "demo"`, `market_orders = 0`, `market_trades = 0`, `HWM_MAX_DRAWDOWN_PCT = 8.0%`. `RunBacktestUseCase`, frozen H0 (`70`-trade), frozen `111`-trade baseline, and existing Kalman exit logic remain **100% unmodified**. **`G3.6` remains NOT AUTHORIZED.**

---

## 2. Root-Cause Decomposition of the `-13.791280 R` Discrepancy (Phases A–D)

Both evaluations contained the **exact same `112` underlying H3b opportunities** (`100%` identical UTC timestamps and BUY/SELL directions across `39` unique days). All `6` defects resided exclusively in the **G3.5 research simulator & ATR extraction** (`CASE_B`), with **zero defects in `RunBacktestUseCase`**:

| Mismatch Class | Trades (`n`) | Original `Sim - App` (`R`) | Exact Root Cause |
| :--- | :---: | :---: | :--- |
| **`SIZING_MISMATCH`** *(Corrupted Initial ATR)* | `54` (`48.2%`) | **`-14.100440 R`** | `task_g3_5_phase_cdefg_utility.py` derived `atr = abs(entry - sl) / 1.5` from `tr["theoretical_stop_price"]` — which in `RunBacktestUseCase` is the **final ratcheted exit SL** (e.g. `entry ± \$0.20` on `BREAKEVEN`!). That collapsed `atr` to `\$0.133` (~`25x` too small), oversized lots by `~25x` (`comm = \$17.12` vs `\$0.84`), and caused `13` `BREAKEVEN` trades to immediately stop out as `SL_HIT` (`-16.91 R`). Fixed via immutable `initial_atr = abs(tp - entry_price) / 3.0`. |
| **`E1_MISMATCH`** | `18` (`16.1%`) | `+0.134100 R` | Simulator checked E1 continuously on first tick `elapsed >= 900s` instead of canonical `RunBacktestUseCase` end of 15th completed post-entry 1m bar (`bars_held == 15`). |
| **`COST_MISMATCH`** *(TP Exit Slippage)* | `10` (`8.9%`) | `+0.174780 R` | Simulator subtracted exit slippage on limit `TARGET` fills; canonical `RunBacktestUseCase` fills limit `TARGET` at exact `float(tp)` with zero exit slippage. |
| **`EXACT_MATCH`** (`SL -> SL_HIT`) | `30` (`26.8%`) | `+0.000280 R` | Unratcheted `SL` trades already matched to `0.000009 R/trade`. |
| **TOTAL (Before Fix)** | **`112`** | **`-13.791280 R`** | **100.000% explained (`0` `UNKNOWN` trades).** |
| **TOTAL (After Fix)** | **`112`** | **`+0.000000 R`** | **Exact 112/112 canonical reconciliation.** |

Secondary simulator fixes also aligned canonical `TIME_SQUEEZE` (`sl + progress*(price - sl)` for `hold_pct >= 0.75`, `7 <= hour < 20`), the `b >= a` zero-spread/inverted quote guard, 2-decimal per-leg commission rounding, and exact sub-second entry-tick matching. Full trace is in [g3_5r_root_cause_analysis.md](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/g3_5r_root_cause_analysis.md).

---

## 3. Phase G — Controlled 2×2 Factorial Delayed-Entry Reassessment (Canonical Simulator, `N = 112`)

| Factorial Cell / Policy (`ALL`, `N = 112`) | Missed / Blocked (`W / L`) | Mean Px Imp (`\$/oz`) | Total Net `R` (`Baseline = +0.772`) | Delta Total `R` | Day Perm `p` | `BH-FDR q` | Survives `q < 0.05`? |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`CELL1_IMMEDIATE_NO_GUARD`** *(Reconciled H3b)* | `0` (`0 / 0`) | `\$0.0000` | `+0.772` | `+0.000` | `1.0000` | — | Baseline |
| **`CELL2_IMMEDIATE_WITH_PRIOR10S_GUARD`** | `1` (`0 / 1`) | `\$0.0000` | `+1.796` | `+1.023` | `1.0000` | `1.0000` | No |
| `CELL3_WAIT_5S_NO_GUARD` | `0` (`0 / 0`) | `+\$0.1837` | `+3.380` | `+2.607` | `0.7736` | `1.0000` | No |
| `CELL4_WAIT_5S_WITH_GUARD` | `2` (`0 / 2`) | `+\$0.0883` | `+5.519` | `+4.746` | `0.4978` | `1.0000` | No |
| `CELL3_WAIT_10S_NO_GUARD` | `0` (`0 / 0`) | `+\$0.2449` | `+4.351` | `+3.578` | `0.3303` | `1.0000` | No |
| `CELL4_WAIT_10S_WITH_GUARD` | `4` (`2 / 2`) | `+\$0.1132` | `+4.905` | `+4.133` | `0.2079` | `1.0000` | No |
| `CELL3_WAIT_15S_NO_GUARD` | `0` (`0 / 0`) | `+\$0.2796` | `+1.138` | `+0.366` | `0.9660` | `1.0000` | No |
| `CELL4_WAIT_15S_WITH_GUARD` | `5` (`2 / 3`) | `+\$0.1063` | `+2.983` | `+2.211` | `0.6777` | `1.0000` | No |
| `CELL3_WAIT_30T_NO_GUARD` | `0` (`0 / 0`) | `+\$0.1557` | `+2.218` | `+1.445` | `0.5177` | `1.0000` | No |
| `CELL4_WAIT_30T_WITH_GUARD` | `2` (`0 / 2`) | `+\$0.0912` | `+4.260` | `+3.488` | `0.3408` | `1.0000` | No |
| `ML_C1_WAIT10S_NO_GUARD` | `0` (`0 / 0`) | `+\$0.3596` | `+1.530` | `+0.758` | `0.7611` | `1.0000` | No |
| `ML_C1_WAIT10S_WITH_GUARD` | `1` (`0 / 1`) | `+\$0.1967` | `+2.564` | `+1.792` | `0.3993` | `1.0000` | No |
| `SPREAD_WAIT10S_WITH_GUARD` | `1` (`1 / 0`) | `+\$0.0963` | `-0.510` | `-1.282` | `0.9615` | `1.0000` | No |
| `RANDOM_WAIT10S_NO_GUARD` (`seed=42`) | `0` (`0 / 0`) | `+\$0.0168` | `+0.985` | `+0.212` | `0.9675` | `1.0000` | No |

*Before vs. After:* `WAIT_15S_WITH_GUARD` collapsed from `+21.81 R (p=0.001, q=0.0077, 27 missed)` to `+2.21 R (p=0.6777, q=1.0000, 5 missed)`. `WAIT_10S_WITH_GUARD` collapsed from `+13.18 R (q=0.0402, 23 missed)` to `+4.13 R (p=0.2079, q=1.0000, 4 missed)`. The entire apparent G3.5 delayed-entry edge was a software artifact.

---

## 4. Phase F — Independent Clean-Room Verification, Synthetic Tests & Regression

- **Clean-Room Independent Execution Auditor** ([task_g3_5r_phase_f_independent.py](file:///d:/gigs/goldedge/scripts/audit/task_g3_5r_phase_f_independent.py)): AST-verified **ZERO `src/goldedge/` imports**; directly replayed all `112` trades from raw PostgreSQL ticks (`v_market_ticks_expanded_6m_v1`) → **`PASS`**: `0` entry mismatches, `0` exit mismatches, `0` E1 mismatches, `0` exit-reason mismatches, `0` commission/P&L/R mismatches, total R diff `-0.000030 R`, exact `-10.9091 R` drawdown, frozen `111`-trade & `70`-trade SHA-256s intact.
- **Synthetic & Adversarial Tests (`14/14 PASS` in [g3_5r_adversarial_test_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/g3_5r_adversarial_test_results.json)):** E1 bar-15 trigger, E1 suppression when MFE reached in bar 15, SL precedes E1, TP precedes E1 with exact limit fill, same-timestamp SL vs TP precedence, fast-quote BREAKEVEN, corrupted/zero-spread/inverted quotes skipped, 15:00 UTC session cutoff, short bid/ask execution, spread-anchored SL/TP, 2-decimal rounded commission, canonical TIME_SQUEEZE, sizing/net-R conversion, and bit-identical deterministic replay all `PASS`.
- **Full Regression Suite (`12/12 PASS` in [g3_5r_regression_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/g3_5r_regression_results.json)):** `test_task_g3_5r_validation.py` through `test_task8_hwm_kill_switch.py`.

---

## 5. Mandatory §13 Final Questions — Direct Answers

1. **Were the original 112 opportunities actually identical?** **Yes** (`100%` identical timestamps and BUY/SELL directions across `39` days).
2. **What caused the `~13.79 R` difference?** Primarily reconstructing initial ATR from the final ratcheted exit SL (`54` trades, `-14.10044 R`), plus continuous-tick vs minute-bar E1 (`18` trades, `+0.13410 R`) and exit slippage on limit TP (`10` trades, `+0.17478 R`).
3. **Was the defect in the application, research simulator, or both?** **Research simulator & ATR extraction ONLY (`CASE_B`)**; `0` defects in `RunBacktestUseCase`.
4. **Were execution semantics intentionally different?** No — they were unintended simulator divergences that have now been aligned to canonical `RunBacktestUseCase` semantics.
5. **Which trades and exits accounted for the discrepancy?** All `54` ratcheted-SL trades (`13` `BREAKEVEN -> SL_HIT` alone caused `-16.91 R`), `18` `EARLY_FAIL_15M` trades, and `10` `TARGET` trades.
6. **Were all defects fixed and independently retested?** **Yes** — all `6` fixed, `14/14` synthetic/adversarial tests `PASS`, `12/12` regression suites `PASS`, clean-room auditor `0` mismatches.
7. **Does the corrected immediate-entry reference agree with the actual application?** **Yes — `EXACTLY_RECONCILED`** (`+0.000000 R` total diff, `0` mismatches across all `112` trades).
8. **Do the original delayed-entry gains survive the corrected comparison?** **No — `INVALIDATED`** (`BH-FDR q = 1.0000` across all `15/15` policies).
9. **What role did the fast-spike invalidation guard play?** With corrupted ATR (`\$0.133`), the `0.5 ATR` guard (`\$0.066`) falsely blocked `21–27` trades (mostly bogus `-2 R` `BREAKEVEN` losses). With true ATR (`~\$3.00`), it triggers on only `1–5` trades with `p = 0.21..1.00` (`q = 1.0000`).
10. **Are the resulting improvements credible after costs and statistical uncertainty?** **No.**
11. **Are frozen H0/H3b and shadow safety preserved?** **Yes** (`SHADOW_MODE = True`, `market_orders = 0`, frozen hashes verified).
12. **Is any further non-ML research justified?** **Not for delayed-entry/fast-spike guards.** Proceed to **`G3.7` prospective frozen shadow observation** (and/or `12–24m` historical backfill if revisiting low-DOF strategy/meta-label research later).

---

## 6. Deliverable Index (`13/13` Present)

Stored in `backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/`:
1. [g3_5r_original_evidence_manifest.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/g3_5r_original_evidence_manifest.json)
2. [g3_5r_candidate_universe_audit.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/g3_5r_candidate_universe_audit.json)
3. [g3_5r_trade_by_trade_reconciliation.parquet](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/g3_5r_trade_by_trade_reconciliation.parquet)
4. [g3_5r_e1_event_timeline.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/g3_5r_e1_event_timeline.json)
5. [g3_5r_root_cause_analysis.md](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/g3_5r_root_cause_analysis.md)
6. [g3_5r_semantics_comparison.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/g3_5r_semantics_comparison.json)
7. [g3_5r_correction_manifest.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/g3_5r_correction_manifest.json)
8. [g3_5r_independent_execution_audit.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/g3_5r_independent_execution_audit.json)
9. [g3_5r_reconciled_baseline_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/g3_5r_reconciled_baseline_results.json)
10. [g3_5r_delayed_entry_research_reassessment.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/g3_5r_delayed_entry_research_reassessment.json)
11. [g3_5r_adversarial_test_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/g3_5r_adversarial_test_results.json)
12. [g3_5r_regression_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/g3_5r_regression_results.json)
13. [g3_5r_final_report.md](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_5r_h3b_execution_reconciliation/g3_5r_final_report.md)
