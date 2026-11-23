# Task 11A — Independent Raw-Tick Reconciliation of Market Regime Candidates

**Date:** 2026-11-19  
**Scope:** Independent trade-by-trade, trailing-stop, cost, portfolio-constraint, equity-path, and tick-MTM drawdown reconciliation for `H0_BASELINE_V2`, `H4a_SCORE_CAP_080` (`H8_H4a_ALL_REGIMES`), and `R1..R11` across `W1`, `W2`, and `W3` (**39 candidate-window pairs**).  
**Safety Status:** `SHADOW_MODE = True`, `TRADING_MODE = "demo"`, `market_orders = 0`, `market_trades = 0`, zero modifications to `src/goldedge/`, zero modifications to frozen Task 4 / V2 / Task 8 / Task 9 / Task 10 artifacts.  
**Verdict:** **VERIFIED (39/39 Candidate-Window Pairs Fully Reconciled — Task 11 Formally Upgraded to VERIFIED)**

---

## Phase 1 — Audit of Prior Task 11 Evidence

Inspecting [task11_independent_reconciliation.json](file:///d:/gigs/goldedge/backtest_results/audit/task11_independent_reconciliation.json), [task11_regime_hypothesis_results.csv](file:///d:/gigs/goldedge/backtest_results/audit/task11_regime_hypothesis_results.csv), and [task11_h4a_regime_comparison.json](file:///d:/gigs/goldedge/backtest_results/audit/task11_h4a_regime_comparison.json) confirmed a genuine evidence gap prior to Task 11A:

- Task 11's `task11_independent_reconciliation.json` only reconciled **`H0_BASELINE_V2`** (`W1=118`, `W2=131`, `W3=143`) against frozen V2 baseline ledgers.
- None of the 12 candidate strategies (`H4a_SCORE_CAP_080` and `R1..R11`) had saved trade-level execution ledgers, trailing-stop update logs, or independent second-engine raw-tick reconciliation across `W1`, `W2`, and `W3`.
- Additionally, `R3`, `R8`, and `R9` had only been evaluated on `W3` in Task 11; Task 11A expanded independent verification to **all 13 configurations (`H0`, `H4a`, `R1..R11`) across all 3 windows (`W1`, `W2`, `W3`)**.

---

## Phases 2 & 3 — Clean-Room Independent Raw-Tick Reconciliation

Executed [task11a_independent_candidate_reconciler.py](file:///d:/gigs/goldedge/scripts/audit/task11a_independent_candidate_reconciler.py) (**zero `src/goldedge/` imports**), which runs a standalone clean-room execution engine (`CleanKalman2D`, `CleanBarAgg`, `CleanRegimeDetector`, `CleanPos`, `CleanMtm`, `CleanRoomPortfolio`) directly against PostgreSQL `market_ticks` (`bid > 0 AND ask > 0 AND bid < ask`) and cross-checks every trade against:
1. Task 11's exact `RPortfolio` research engine (instrumented without altering any execution parameter or formula),
2. Task 11's published CSV/JSON summaries (`task11_regime_hypothesis_results.csv` & `task11_statistical_validation.json`), and
3. Frozen V2 baseline trade ledgers (`w1_trades.csv`, `w2_trades.csv`, `w3_trades.csv`) for `H0_BASELINE_V2`.

### Aggregate Reconciliation Summary (`task11a_validation_tests.json`)
- **Total Candidate-Window Pairs Audited:** `39` (`13` configs × `3` windows)
- **Fully Reconciled Pairs:** **`39 / 39`** (`0` unverified or mismatched)
- **Total Individual Trades Reconciled:** **`3,791`** (`0` missing, `0` extra, `0` entry/exit/quote/qty/cost mismatches)
- **Total Trailing-Stop / Breakeven / Time-Squeeze Updates Verified:** **`91,401`** (`0` stop-update mismatches)
- **Portfolio Constraint Violations (Overlapping/Inverted Positions):** **`0`** across all 39 pairs
- **Frozen SHA-256 Fingerprints:** `task4_match = true`, `v2_w1_match = true`, `v2_w2_match = true`, `v2_w3_match = true`

### W3 Per-Candidate Reconciliation Table (`task11a_candidate_reconciliation.json` & `task11a_equity_path_comparison.json`)
| Candidate | Status | Matched / Total (`W3`) | Stop Updates Verified (`mm`) | Max Net P&L Diff | Max Equity Path Diff | Tick-MTM DD% (`Diff`) | Task 11 Published Match |
| :--- | :---: | :---: | :---: | ---: | ---: | :---: | :---: |
| `H0_BASELINE_V2` | **FULLY_RECONCILED** | `143 / 143` | `3,750` (`0`) | `$0.000000` | `$0.000000` | `12.78%` (`0.000000`) | `True` (+ Frozen V2 `0` mm) |
| `H8_H4a_ALL_REGIMES` | **FULLY_RECONCILED** | `99 / 99` | `2,542` (`0`) | `$0.000000` | `$0.000000` | `6.18%` (`0.000000`) | `True` |
| `R1_ADX20_ON_H0` | **FULLY_RECONCILED** | `102 / 102` | `2,840` (`0`) | `$0.000000` | `$0.000000` | `8.60%` (`0.000000`) | `True` |
| `R2_NO_RAW_HIVOL_ON_H0` | **FULLY_RECONCILED** | `133 / 133` | `3,404` (`0`) | `$0.000000` | `$0.000000` | `10.52%` (`0.000000`) | `True` |
| `R3_SIDEWAYS_ER25_ON_H0` | **FULLY_RECONCILED** | `139 / 139` | `3,376` (`0`) | `$0.000000` | `$0.000000` | `13.11%` (`0.000000`) | `True` |
| `R4_NO_POST_TRANS3_ON_H0` | **FULLY_RECONCILED** | `104 / 104` | `2,010` (`0`) | `$0.000000` | `$0.000000` | `14.27%` (`0.000000`) | `True` |
| `R5_NO_SW_TO_BULL_BUY_H0` | **FULLY_RECONCILED** | `131 / 131` | `3,622` (`0`) | `$0.000000` | `$0.000000` | `10.83%` (`0.000000`) | `True` |
| `R6_TREND_CAP080_SW_OPEN` | **FULLY_RECONCILED** | `117 / 117` | `2,755` (`0`) | `$0.000000` | `$0.000000` | `10.39%` (`0.000000`) | `True` |
| `R7_H4a_PLUS_ADX20` | **FULLY_RECONCILED** | `70 / 70` | `1,765` (`0`) | `$0.000000` | `$0.000000` | `5.22%` (`0.000000`) | `True` |
| `R8_H4a_PLUS_NO_RAW_HIVOL` | **FULLY_RECONCILED** | `90 / 90` | `2,248` (`0`) | `$0.000000` | `$0.000000` | `7.83%` (`0.000000`) | `True` |
| `R9_H4a_PLUS_NO_POST_TRANS3` | **FULLY_RECONCILED** | `66 / 66` | `1,221` (`0`) | `$0.000000` | `$0.000000` | `8.59%` (`0.000000`) | `True` |
| `R10_H4a_NO_SW_TO_BULL_BUY` | **FULLY_RECONCILED** | `88 / 88` | `2,429` (`0`) | `$0.000000` | `$0.000000` | `5.31%` (`0.000000`) | `True` |
| `R11_H4a_PLUS_H7_REF` | **FULLY_RECONCILED** | `99 / 99` | `2,486` (`0`) | `$0.000000` | `$0.000000` | `5.23%` (`0.000000`) | `True` |

*(All 13 configurations also achieved `FULLY_RECONCILED`, `0` mismatches, and `$0.000000` max P&L/equity/MTM-DD diff on both `W1` and `W2`; full metrics in [task11a_candidate_reconciliation.json](file:///d:/gigs/goldedge/backtest_results/audit/task11a_candidate_reconciliation.json) and [task11a_equity_path_comparison.json](file:///d:/gigs/goldedge/backtest_results/audit/task11a_equity_path_comparison.json).)*

---

## Phase 4 — Root-Cause Investigation & Resolution Log

During clean-room validator development, the reconciler caught two defects in the new **audit validator script itself** (zero defects in Task 11's research engine or frozen baselines):
1. **Clean-Room Kalman & Bar Aggregator Parity:** Initial `CleanKalman2D` used fixed noise constants instead of the adaptive `VolumeAdaptiveKalmanFilter` specification (`q_p=1e-4, q_v=3e-5, r_base=1e-2`), and `CleanBarAgg` stored raw `tick_count` as `tick_rate` rather than ticks per minute (`tick_count / tf_mins`). Correcting both in `task11a_independent_candidate_reconciler.py` eliminated all trade discrepancies across 36/39 pairs immediately.
2. **R11 Config Parity:** Initial `_build_configs()` omitted `buy_t1_trigger_atr=1.2` and `buy_t1_dist_atr=0.8` from Task 11's `R11_H4a_PLUS_H7_REF` config, which the published-table cross-check immediately flagged (`pub_ok=False`, `5.31%` vs `5.23%` MTM DD). Restoring Task 11's exact `R11` parameters and re-running atomically brought reconciliation to **`39/39 FULLY_RECONCILED` (`pub_ok=True`)**.

---

## Phase 5 — Deliverables & Acceptance Criteria Closure

All 5 required deliverables are present under `backtest_results/audit/`:
1. [task11a_candidate_reconciliation.json](file:///d:/gigs/goldedge/backtest_results/audit/task11a_candidate_reconciliation.json)
2. [task11a_trade_level_differences.csv](file:///d:/gigs/goldedge/backtest_results/audit/task11a_trade_level_differences.csv) (`3,791` reconciled trade rows)
3. [task11a_equity_path_comparison.json](file:///d:/gigs/goldedge/backtest_results/audit/task11a_equity_path_comparison.json)
4. [task11a_validation_tests.json](file:///d:/gigs/goldedge/backtest_results/audit/task11a_validation_tests.json)
5. [task11a_final_report.md](file:///d:/gigs/goldedge/backtest_results/audit/task11a_final_report.md)

**Verification & Safety:**
- Unit & regression suite: [test_task11a_reconciliation.py](file:///d:/gigs/goldedge/tests/test_task11a_reconciliation.py) + 10 prior modules (all 11 modules passing).
- Safety locks intact: `SHADOW_MODE = True`, `TRADING_MODE = "demo"`, `market_orders = 0`, `market_trades = 0`, zero changes to `src/goldedge/`.
- **Acceptance Status:** Every reported Task 11 candidate is now independently reconciled at the trade, trailing-stop, cost, equity-path, and tick-MTM drawdown level with **zero unexplained differences**. Task 11 is formally **VERIFIED**; ready for Task 12 upon user instruction.
