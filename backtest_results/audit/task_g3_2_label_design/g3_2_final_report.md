# TASK G3.2 — GoldEdge Macro/Micro Label Design, Cost Accuracy & Independent Verification (Final Corrective Pass)

**Project:** GoldEdge (`XAUUSD` Spot Gold CFD)  
**Phase:** G3 — Macro + Micro ML Development (Current: `G3.2 — Verified`, Previous: `G3.1`, Next: `G3.3 — Leakage-Proof ML Training Pipeline`)  
**Execution Mode:** STRICT SHADOW MODE (`SHADOW_MODE = True`, `TRADING_MODE = "demo"`, `HWM_MAX_DRAWDOWN_PCT = 8.0`, `market_orders = 0`, `market_trades = 0`)  
**Label Spec Version:** `G3_2_LABEL_SPEC_V1` | **Dataset Fingerprint SHA-256:** `0180A22F36A4B66D`  

---

## 1. Final Verdict

> [!IMPORTANT]
> **Verdict:** **`VERIFIED`**
> - **Phase A1 Candle Source (`PASS`):** All **`96,862`** ML-eligible 1-minute bars (`2026-08-13T00:00Z` – `2026-11-19T11:45Z`) materialized and continuously refreshable via `refresh_market_bars_1m_quote_semantics_v1(...)`; `0` OHLC/tick_count/spread mismatches vs independent reconstruction; `0` forward-filled bars; `1` quarantined Sunday tick (`2026-11-22T16:00:02Z`) excluded.
> - **Phase A2 Execution Cost Parity (`PASS`):** Actual configured Fusion Markets cTrader demo account `48439552` (`FUSION_CTRADER_DEMO_48439552`) charges **`\$12.00` per `\$1,000,000` notional per side** (`\$24/\$1M` RT = `0.24 bps` = **`\$0.1003/oz`** at `\$4,179.94`, `2.23x` higher than G3.1's stale `\$0.045/oz` shorthand) plus **`0.0533` pips/side slippage (`\$0.01066/oz` RT)** plus executable bid/ask spread (`\$0.0700/oz` median) = **`\$0.1810/oz` (`0.433 bps`)** total RT friction.
> - **Phase B & C Labels (`PASS`):** Generated **`19,004`** 5m-grid Macro 15m labels (`6,292` non-overlapping) and **`664,806`** non-overlapping 30-tick Micro labels in Snappy Parquet using pure-Python Hexagonal domain service [label_spec_service.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/label_spec_service.py), explicitly separating **H3b entry-timing / short-term quote-movement** targets from **standalone 8s round-trip** targets.
> - **Phase D Causality & Phase E Full Clean-Room Reconciliation (`PASS`):** **`0`** causality violations across 100% of both Parquet files; **`0 / 19,004` (`100.0%` coverage)** Macro mismatches and **`0 / 20,429`** stratified + boundary Micro mismatches across all 85 active days (ZERO `src/goldedge/` imports); `0` unaccounted 5m buckets; `9/9` boundary edge cases verified.
> - **Phase G Baselines & Safety (`PASS`):** `8/8` regression suites `PASS`; frozen H0/H3b unchanged; zero ML models trained and zero thresholds optimized. **Well-constructed labels do not imply predictive accuracy or profitability.**

---

## 2. Final Corrective Pass — Resolution of All 3 Review Items

| Issue | Resolution Implemented & Independently Verified |
| :--- | :--- |
| **Issue 1 (High) — Micro Target Definition (Entry Timing vs Standalone 8s Scalp)** | Added `classify_micro_entry_timing(...)` to [label_spec_service.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/label_spec_service.py) and populated `g3_2_micro_labels.parquet` with **two explicitly separated ex-post training target families** (predicted ex-ante from causal features `<= t_0`, never using future ticks as confirmation): (1) **Primary H3b Entry-Timing & Short-Term Quote Movement** (`quote_move_class_1x_spread`, `quote_move_class_010`, `buy_timing_adv_usd` = `(ask_{t+30} - ask_t)*(1 + c_side)`, `sell_timing_adv_usd` = `(bid_t - bid_{t+30})*(1 - c_side)`, `buy_timing_class` & `sell_timing_class` in `{ENTER_NOW, WAIT_ADVERSE, NEUTRAL}` with zero duplicate round-trip commission); and (2) **Standalone 8s Round-Trip** (`net_class`, net of full `\$0.181/oz` RT friction). Unit-tested in [test_task_g3_2_validation.py](file:///d:/gigs/goldedge/tests/test_task_g3_2_validation.py). |
| **Issue 2 (Medium) — Expanded Independent Clean-Room Reconciliation** | Upgraded [task_g3_2_phase_de_independent_validator.py](file:///d:/gigs/goldedge/scripts/audit/task_g3_2_phase_de_independent_validator.py) (ZERO `src/goldedge/` imports) to verify **100.0% of all Macro rows (`19,004 / 19,004`)** across all 85 active days plus **`20,429` stratified + boundary Micro rows** (first/last 25 per day, 150 evenly spaced, top-25 widest spreads, top-25 slowest elapsed times per day, checking both standalone RT and entry-timing/quote-move labels): **`0 / 19,004` Macro mismatches, `0 / 20,429` Micro mismatches**. |
| **Issue 3 (Low) — Exact Macro 5m Bucket Accounting (`19,394` vs `19,393`)** | Verified exact identity (`0` unaccounted rows): **`19,394`** raw 5m buckets (G3.1 pre-quarantine) **`- 1`** quarantined Sunday bucket (`2026-11-22 16:00Z`) **`= 19,393`** ML-eligible 5m buckets **`== 19,004`** generated Macro labels **`+ 100`** warmup **`+ 212`** incomplete-15m/gap **`+ 77`** no-entry-tick (`19,004 + 389 = 19,393`). Asserted in `g3_2_independent_reconciliation.json` and `test_task_g3_2_validation.py`. |

---

## 3. Phase A — Candle Source & Execution Cost Verification

Full outputs: [g3_2_candle_source_reconciliation.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_2_label_design/g3_2_candle_source_reconciliation.json) & [g3_2_cost_parity_verification.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_2_label_design/g3_2_cost_parity_verification.json).

- **A1 Candle Source:** [011_refresh_bars_quote_semantics_v1.sql](file:///d:/gigs/goldedge/migrations/011_refresh_bars_quote_semantics_v1.sql) materialized all `96,862` ML-eligible 1m bars via `refresh_market_bars_1m_quote_semantics_v1(...)`; `0` discrepancies, `0` forward-filled rows.
- **A2 Verified Fusion Markets cTrader Demo (`48439552`) Costs at `\$4,179.94`:**
  - Commission (RT): `\$12.00 / \$1M` notional per side = **`\$0.10032/oz`** (`\$10.03/lot`, `0.240 bps`)
  - Slippage (RT): `0.0533 pips/side * \$0.10 * 2` = **`\$0.01066/oz`** (`\$1.07/lot`, `0.026 bps`)
  - Median Tick Spread: **`\$0.07000/oz`** (`\$7.00/lot`, `0.167 bps`)
  - **Total RT Friction:** **`\$0.18098/oz`** (`\$18.10/lot`, **`0.433 bps`**) + `FUSION_XAUUSD_SWAP_SPEC` (`17:00 NY`, Wed `3x`) applied in B3.

---

## 4. Phase B & C — Macro & Micro Label Distributions

Full outputs: [g3_2_label_distribution.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_2_label_design/g3_2_label_distribution.json) & [g3_2_barrier_order_validation.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_2_label_design/g3_2_barrier_order_validation.json).

### Macro 15m (Non-Overlapping `n = 6,292`; Total 5m Grid `n = 19,004`)

| Threshold | Raw `UP` / `DOWN` / `FLAT` | Net-of-Cost `UP` / `DOWN` / `FLAT` |
| :---: | :---: | :---: |
| **`0.05%`** | `29.28%` / `32.37%` / `38.35%` | `28.53%` / `31.47%` / `40.00%` |
| **`0.10%`** | `16.86%` / `17.82%` / `65.32%` | **`16.37%` (`1,030`) / `17.36%` (`1,092`) / `66.27%` (`4,170`)** |
| **`0.15%`** | `9.58%` / `9.57%` / `80.85%` | `9.30%` / `9.20%` / `81.50%` |
| **`0.20%`** | `5.18%` / `5.13%` / `89.69%` | `5.12%` / `4.88%` / `90.00%` |

- **B2 Tick-Chronological Barriers (`0.10%`):** `1,072` BUY and `1,089` SELL windows touched BOTH barriers (`14` BUY / `17` SELL inside the same 1m candle), all resolved chronologically via executable ticks.
- **B3 Isolated Hypothetical H3b Outcomes (Non-Overlapping, NOT portfolio-executed trades):** BUY mean `-0.0841R`; SELL mean `+0.0033R`.
- **Day-Clustered Uncertainty (`85` dates):** Net `DIRECTIONAL 0.10%` `33.73% ± 2.34%` (`N_eff = 1,569.5`, `VIF = 4.009`).

### Micro 30-Tick (`n = 664,806` Non-Overlapping Windows)

| Target Family | Purpose | Distribution |
| :--- | :--- | :--- |
| **`quote_move_class_1x_spread`** | Short-term quote drift (`±1x` spread, no duplicate RT comm) | `UP 43.18%` / `DOWN 43.57%` / `FLAT 13.25%` |
| **`quote_move_class_010`** | Short-term quote drift (`±\$0.10`, no duplicate RT comm) | `UP 40.73%` / `DOWN 41.13%` / `FLAT 18.14%` |
| **`buy_timing_class`** | H3b BUY ex-ante entry timing target (`(ask_{t+30} - ask_t)*(1+c_side)` vs `±1x` spread) | `ENTER_NOW 43.58%` / `WAIT_ADVERSE 44.32%` / `NEUTRAL 12.10%` |
| **`sell_timing_class`** | H3b SELL ex-ante entry timing target (`(bid_t - bid_{t+30})*(1-c_side)` vs `±1x` spread) | `ENTER_NOW 43.09%` / `WAIT_ADVERSE 43.03%` / `NEUTRAL 13.89%` |
| **`net_class` (Standalone RT)** | Hypothetical 8s round-trip net of `\$0.181/oz` RT friction | `UP 33.96%` / `DOWN 34.35%` / `FLAT 31.69%` |

- **C3 Fixed-Time Comparison:** 30-tick net labels agree with `5s` `69.39%`, `10s` `72.10%`, `15s` `63.81%`.
- **C4 Adverse-Selection Research (Observational Only):** Immediate post-entry 30-tick `UP` vs `DOWN` corresponds to lower 15m BUY MAE (`\$3.67` vs `\$4.97`) and higher H3b BUY R (`+0.041R` vs `-0.191R`) because the first 30 ticks are embedded inside the 15m path; predicting adverse starts *before* entry may reduce MAE, whereas *waiting* 30 ticks for confirmation forfeits initial favorable movement.

---

## 5. Deliverables Index (`backtest_results/audit/task_g3_2_label_design/`)

1. [g3_2_candle_source_reconciliation.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_2_label_design/g3_2_candle_source_reconciliation.json)
2. [g3_2_cost_parity_verification.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_2_label_design/g3_2_cost_parity_verification.json)
3. [g3_2_macro_labels.parquet](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_2_label_design/g3_2_macro_labels.parquet) (`19,004` rows, `3.17 MB`)
4. [g3_2_micro_labels.parquet](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_2_label_design/g3_2_micro_labels.parquet) (`664,806` rows, `49.82 MB`)
5. [g3_2_label_schema.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_2_label_design/g3_2_label_schema.json)
6. [g3_2_barrier_order_validation.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_2_label_design/g3_2_barrier_order_validation.json)
7. [g3_2_label_distribution.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_2_label_design/g3_2_label_distribution.json)
8. [g3_2_causality_audit.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_2_label_design/g3_2_causality_audit.json)
9. [g3_2_independent_reconciliation.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_2_label_design/g3_2_independent_reconciliation.json) (`0/19,004` Macro, `0/20,429` Micro)
10. [g3_2_regression_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_2_label_design/g3_2_regression_results.json) (`8/8 PASS`)
11. [g3_2_final_report.md](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_2_label_design/g3_2_final_report.md)

**Ready for Next Step:** Proceed to **Task G3.3 — Leakage-Proof ML Training Pipeline** (chronological purged walk-forward validation).
