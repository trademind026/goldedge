# TASK G3.1 — XAUUSD Historical Data Integrity, ML Training Feasibility & Independent Verification (Corrective Verification Pass)

**Project:** GoldEdge (`XAUUSD` Spot Gold CFD)  
**Phase:** G3 — Macro + Micro ML Development (Current: `G3.1 — Verified with Corrective Pass`, Previous: `G2`, Next: `G3.2 — Macro/Micro Label Design`)  
**Execution Mode:** STRICT SHADOW MODE (`SHADOW_MODE = True`, `TRADING_MODE = "demo"`, `HWM_MAX_DRAWDOWN_PCT = 8.0`, `market_orders = 0`, `market_trades = 0`)  
**Git Base Revision:** `1bf6c45b3f5b790e3212c9b5b7a683d6fd5e3622`  

---

## 1. Executive Verdicts

> [!IMPORTANT]
> - **Overall Verification Verdict:** **`VERIFIED`** (all 4 G3.1 corrective issues resolved and re-verified; `0` discrepancies across all 6 Phase H independent checks; `7/7` regression suites `PASS`; zero production models trained and zero trading thresholds tuned).
> - **Macro (15-Minute) ML Readiness:** **`CONDITIONALLY_READY`** (for label design and research pipeline development; **not yet ready for production model training** without expanding historical coverage to 6–12 months). Intraday quote integrity is high (`68` clean weekday trading days, `96,862` ML-eligible 1m bars, `6,321` non-overlapping 15m windows), **however** day-level cluster dependence reduces conservative effective sample size to **`N_eff_day = 1,554.2`** (`EPV_day_clustered = 17.4` for 15 features, per-fold 95% CI `±4.97%`), and Aug–Nov 2026 is **research-exposed**.
> - **Micro (30-Tick) ML Readiness:** **`CONDITIONALLY_READY`** (for top-of-book quote-direction / execution-timing research, **not** genuine order-flow prediction). Massive raw tick depth (`19,942,829` overlapping / `664,806` non-overlapping 30-tick windows), **however** day-level volatility clustering reduces conservative `N_eff_day = 4,360.1` (`EPV_day_clustered = 234.4`, per-fold 95% CI `±2.97%`), zero Level II / traded-volume data exists, and Lee-Ready order-flow features have been explicitly disabled and blocked from ML schemas.

---

## 2. Corrective Verification Pass — Resolution of All 4 Identified Issues

| Issue | Root Cause Verified | Fix Implemented & Verified |
| :--- | :--- | :--- |
| **Issue 1 — Suspicious Sunday Tick (`2026-11-22 16:00:02.669772Z`)** | Only off-session weekend tick in the entire 19.96M-row DB (`bid=3537.89, ask=3538.13, volume=NULL, flags=NULL`). Caused by `ctrader_live.py` receiving a Friday-close `ProtoOASpotEvent` reconnect snapshot with `timestamp_ms=None`, which `LiveTickerService` stamped with local UTC wall clock hours before Fusion Markets' Sunday `~22:00–23:00 UTC` open. | Applied non-destructive [010_quote_quarantine_v1.sql](file:///d:/gigs/goldedge/migrations/010_quote_quarantine_v1.sql): quarantined the row in `market_ticks_quarantine` (leaving raw `market_ticks` untouched) and added `is_quarantined` + `is_ml_eligible_quote` to `v_market_ticks_quote_semantics_v1` (excluding Saturday UTC and Sunday `< 21:00 UTC`). ML-eligible ticks = **`19,947,246`**, ML-eligible 1m bars = **`96,862`**. |
| **Issue 2 — Exact `843`-Bar Reconciliation (`96,863` vs `96,020`)** | Full outer join between `market_ticks` valid 1m buckets (`96,863`) and `market_bars_1m_quote_semantics_v1` (`96,020`, spanning `2026-08-13T00:00Z` – `2026-11-18T18:08Z`) showed `0` bars only in `qs_v1` and exactly `843` post-cutoff bars only in `market_ticks`. | Exact breakdown verified (`0` unexplained): **`291`** bars (`2026-11-18 18:09–23:59Z` live ticks post-refresh) + **`551`** bars (`2026-11-19 00:00–11:45Z` prior to feed outage) + **`1`** quarantined bar (`2026-11-22 16:00Z`) = **`843`**. Reconciled `96,020 / 96,020` bars bit-identically. |
| **Issue 3 — Broken Lee-Ready Order-Flow Feature** | `microstructure_feature_repo.py` evaluated `price >= ask` / `price <= bid` on midpoint quotes, yielding `0` on 100% of rows (`real_volume` is 100% `NULL`; no L2 book). | Disabled & removed `aggressor`, `buy_vol`, `sell_vol`, `net_order_flow`, `tick_momentum` from [microstructure_feature_repo.py](file:///d:/gigs/goldedge/src/goldedge/adapters/secondary/features/microstructure_feature_repo.py) (raises `ValueError` if `include_disabled_order_flow=True`), and added pure-domain schema guard [ml_feature_contract.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/ml_feature_contract.py) (`validate_ml_feature_names` raises `ValueError` on any excluded order-flow/L2/volume feature). Unit-tested in [test_task_g3_1_validation.py](file:///d:/gigs/goldedge/tests/test_task_g3_1_validation.py). |
| **Issue 4 — Overly Optimistic `VIF = 1.0` Effective Sample Size** | Non-overlapping horizons remove direct label overlap (`return lag_1 ACF ~ 0`) but leave strong within-day regime/volatility clustering and feature autocorrelation intact. | Added day-level cluster dependence (one-way random-effects ICC, cluster DEFF, day-block SE) and non-overlapping feature ACF in Phase D & Phase F. Macro `N_eff` drops from `6,321` → **`1,554.2`** (`day_block_VIF = 4.07`, `EPV = 17.4`, val 95% CI `±4.97%`); Micro `N_eff` drops from `664,806` → **`4,360.1`** (`day_block_VIF = 152.47` on volatility, `EPV = 234.4`, val 95% CI `±2.97%`). |

---

## 3. Phase A — Complete TimescaleDB Historical Data Audit

Full outputs: [g3_1_historical_data_audit.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_historical_data_audit.json) & [g3_1_daily_coverage.csv](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_daily_coverage.csv).

| Metric | Verified Value | Notes |
| :--- | ---: | :--- |
| **Total `market_ticks` rows** | `19,959,292` | 100% `XAUUSD`; raw table preserved untouched |
| **Valid quotes (`bid > 0 & ask > 0 & bid < ask`)** | `19,947,247` (`99.9397%`) | `12,045` invalid (`11,612` inverted + `433` zero-spread; `0` NULL / non-positive) |
| **Quarantined off-session quotes** | `1` | `2026-11-22T16:00:02.669772Z` (Sunday reconnect snapshot) |
| **ML-eligible valid quotes** | **`19,947,246`** | `2026-08-13T00:00:00.153Z` – `2026-11-19T11:45:39.744Z` |
| **ML-eligible 1m bars** | **`96,862`** | `96,020` in `market_bars_1m_quote_semantics_v1` + `842` live Nov 18–19 (`0` unexplained) |
| **Out-of-order timestamps / Traded volume** | `0` / `0` non-null | PK `(symbol, time)`; `real_volume` is 100% `NULL`; no L2 book |
| **Spread Distribution (`USD`)** | `p50 = \$0.07`, `p95 = \$0.12`, `p99 = \$0.20`, `max = \$7.20` | Mean `\$0.0740`, std `\$0.0587` |
| **Trading-Day Coverage (Weekdays)** | `69 / 72` `OK` (`>=95%`) | Clean continuous core: `68` weekdays (`2026-08-13` – `2026-11-18`); `Sep 7` was US Labor Day early close |

---

## 4. Phase B & C — Macro (15m) & Micro (30-Tick) Feasibility

Full outputs: [g3_1_macro_feasibility.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_macro_feasibility.json) & [g3_1_micro_feasibility.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_micro_feasibility.json).

### Macro 15m Threshold Sensitivity (Non-Overlapping 15m Grid, `n = 6,321`)
Total RT friction: `~0.25 bps` (`\$0.06` median bar-close spread + `\$0.045/oz` Fusion Markets RT commission at `\$4,179.94` median price).

| Threshold | Endpoint `UP` | Endpoint `DOWN` | Endpoint `FLAT` | Barrier `UP_only` | Barrier `DOWN_only` | Barrier `BOTH` | Barrier `NEITHER` |
| :---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **`0.05%`** | `29.28%` (`1,851`) | `32.49%` (`2,054`) | `38.22%` (`2,416`) | `32.72%` | `35.20%` | `26.66%` | `5.43%` |
| **`0.10%`** | **`16.82%` (`1,063`)** | **`17.85%` (`1,128`)** | **`65.34%` (`4,130`)** | `28.68%` | `29.82%` | `5.44%` | `36.05%` |
| **`0.15%`** | `9.68%` (`612`) | `9.71%` (`614`) | `80.60%` (`5,095`) | `16.63%` | `18.18%` | `1.42%` | `63.77%` |
| **`0.20%`** | `5.25%` (`332`) | `5.17%` (`327`) | `89.57%` (`5,662`) | `10.14%` | `9.86%` | `0.41%` | `79.59%` |

### Micro 30-Tick Summary (`19,942,829` overlapping / `664,806` non-overlapping)
- **Elapsed Time (`30` Valid Quotes):** `p05 = 4.41s`, **median = `8.18s`** (`OVERLAP 5.78s` vs `OFF 12.87s`), `p95 = 20.34s`, `p99 = 32.36s`.
- **Move vs Friction:** Median `|mid_move| = \$0.31` vs `\$0.115` total RT spread+commission (`2.70x`). Executable post-spread `> \$0.10`: `UP 34.89%` / `DOWN 35.25%` / `FLAT 29.86%`.
- **Short-Lag Microstructure:** Positive tick-to-tick autocorrelation (`lag_1 = +0.0581`, `lag_2 = +0.0475`, `lag_3 = +0.0285`), decaying to `~0` by `lag_5`.

---

## 5. Phase D — Temporal Dependence, Day-Clustered `N_eff` & Leakage Controls

Full output: [g3_1_independence_leakage_audit.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_independence_leakage_audit.json).

| Series (Non-Overlapping Grid) | Raw `N` | Within-Day `ICC` | Day-Block `VIF` | Day-Clustered `N_eff` | Feature / Vol `lag_1 ACF` |
| :--- | ---: | ---: | ---: | ---: | ---: |
| **Macro 15m `directional_0.10pct`** | `6,321` | `0.0139` | `4.067` | **`1,554.2`** | `|ret|` `0.1855` (`N_eff=2,741`) |
| **Macro 15m `atr14_5m` / `regime_bear`** | `6,321` | — | — | `652.9` / `812.8` | **`0.9318` / `0.7288`** |
| **Micro 30-tick `exec_up_gt0`** | `664,806` | `0.0002` | `4.811` | `138,181.9` | — |
| **Micro 30-tick `abs_30t_move_usd`** | `664,806` | `0.0148` | `152.474` | **`4,360.1`** | strong intraday vol clustering |

- **Derived Chronological Validation Protocol:** Expanding Purged Walk-Forward CV with **Macro `purge = 15m, embargo = 60m`** and **Micro `purge = 60s, embargo = 300s`**, shared chronological cutoffs across Macro and Micro, day-block confidence intervals, and untouched post-freeze prospective shadow evaluation.

---

## 6. Phases E, F, G & H — Feature Readiness, Model Feasibility, Expansion & Reconciliation

Full outputs: [g3_1_feature_readiness.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_feature_readiness.json), [g3_1_model_feasibility.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_model_feasibility.json), [g3_1_data_expansion_assessment.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_data_expansion_assessment.json), [g3_1_independent_reconciliation.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_independent_reconciliation.json), [g3_1_regression_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_regression_results.json).

- **Model Feasibility (Day-Clustered):**
  - **Macro:** `EPV_day_clustered = 17.4` (`15` features, `N_eff_day = 1,554.2`, val 95% CI `±4.97%`). Regularized Logistic Regression with Platt/temperature scaling is the right G3.3 baseline; Constrained LightGBM requires expanding historical data to `6–12` months first.
  - **Micro:** `EPV_day_clustered = 234.4` (`8` features, `N_eff_day = 4,360.1`, val 95% CI `±2.97%`). Suited for quote-direction / execution-timing filtering, not standalone 8s scalping.
- **Historical Data Expansion (before G3.3):** Probe Fusion Markets cTrader `ProtoOAGetTickDataReq` backward (paginating `<= 1 week` chunks via `hasMore`, independently reconciling bid/ask coverage) to add `6–12` months of broker-consistent history without spread fabrication.
- **Phase H Independent Reconciliation (`6/6 PASS`) & Regression (`7/7 PASS`):** Zero discrepancies across tick validity, `96,020` 1m bars, `843`-bar diff breakdown, `804` 5m causal bars, order-flow exclusion enforcement, Sunday quarantine, and safety locks (`SHADOW_MODE=True`, `TRADING_MODE="demo"`, `HWM=8.0%`, `market_orders=0`, `market_trades=0`, frozen SHA-256 intact).

---

## 7. Deliverables Index

1. [g3_1_historical_data_audit.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_historical_data_audit.json)
2. [g3_1_daily_coverage.csv](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_daily_coverage.csv)
3. [g3_1_macro_feasibility.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_macro_feasibility.json)
4. [g3_1_micro_feasibility.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_micro_feasibility.json)
5. [g3_1_independence_leakage_audit.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_independence_leakage_audit.json)
6. [g3_1_feature_readiness.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_feature_readiness.json)
7. [g3_1_model_feasibility.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_model_feasibility.json)
8. [g3_1_data_expansion_assessment.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_data_expansion_assessment.json)
9. [g3_1_independent_reconciliation.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_independent_reconciliation.json)
10. [g3_1_regression_results.json](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_regression_results.json)
11. [g3_1_final_report.md](file:///d:/gigs/goldedge/backtest_results/audit/task_g3_1_ml_feasibility/g3_1_final_report.md)

**Ready for Next Step:** Proceed to **Task G3.2 — Macro/Micro Label Design**.
