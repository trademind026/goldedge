# Task 7 — XAUUSD Financial Cost, Rollover & Volume Semantics Remediation Report

**Date:** `2026-11-18 UTC`  
**Symbol / Spec:** `XAUUSD` (`contract_size = 100.0`, `pip_size = 0.10`, `min_lot = 0.01`, `volume_step = 0.01`)  
**Account / Mode Verified:** `Fusion Markets` / `cTrader` Demo Account `48439552` (`TRADING_MODE = "demo"`, `SHADOW_MODE = True`, `market_orders = 0`, `market_trades = 0`)  
**Verdict:** **`VERIFIED`** *(All Task 7 implementation, unit/parity, and independent reconciliation acceptance criteria passed with zero discrepancy; three external historical/live broker dependencies cannot be retrieved offline and are explicitly flagged `UNVERIFIED` below per specification.)*

---

## 1. Executive Summary

Task 7 remediated the confirmed financial-cost, timezone/DST rollover, and volume-semantics defects identified in Tasks 1–6 while **preserving all frozen strategy rules, indicators, entry/exit conditions, score threshold (`0.60`), risk settings, and frozen baseline artifacts**:

1. **Commission Correction:** Replaced fixed/approximate backtest commission handling with pure-Python [BrokerCostService](file:///d:/gigs/goldedge/src/goldedge/domain/services/broker_cost_service.py) implementing the verified demo account `48439552` notional rate `abs(lots) * 100 * execution_price * 12 / 1,000,000` per side, rounded independently per execution leg to `$0.01`, with partial-close allocation, duplicate-deduction protection, configurable schedules, and a strict live-mode safety guard that refuses to apply demo rates to `trading_environment == "live"`. Reconciled **`15 / 15` historical demo deals** (`7 / 7` positions) to **`$0.00` discrepancy**.
2. **Rollover & Swap Correction:** Removed hardcoded `21:00 UTC` rollover assumptions in favor of `America/New_York` (`17:00` NY local) timezone conversions using Python `zoneinfo` (`21:00 UTC` EDT summer / `22:00 UTC` EST winter), configured with Fusion Markets symbol metadata (`swapLong = -5.653` pips, `swapShort = +2.677` pips, `PIPS` mode, `WEDNESDAY` `3x` triple swap, weekend charging disabled, configurable `daily_rate_overrides` flagged unverified for history). Passed **`10 / 10` rollover/DST tests**.
3. **Volume Semantics Remediation:** Removed the `COALESCE(volume, 1) as volume` fabrication in [TickRepository](file:///d:/gigs/goldedge/src/goldedge/adapters/secondary/db/tick_repository.py#L60-L69), standardized `NULL` / `None` `real_volume` propagation and `tick_count` (quote updates per bar with `bid > 0`) identically across live [ScanScheduler](file:///d:/gigs/goldedge/src/goldedge/adapters/primary/scheduler/scan_scheduler.py), [candle_repository.py](file:///d:/gigs/goldedge/src/goldedge/adapters/secondary/db/candle_repository.py), [mtf_trend.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/mtf_trend.py), [indicator_service.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/indicator_service.py), and [RunBacktestUseCase](file:///d:/gigs/goldedge/src/goldedge/application/use_cases/run_backtest.py). Zero-cost baseline parity after remediation matched Task 4 across **`118 / 118` trades (`0.0` entry, exit, and P&L difference)**.

---

## 2. Implemented Changes Summary

| Domain | Change Implemented | Files |
|---|---|---|
| **Commission & Swap Engine** | Pure-Python `CommissionSchedule`, `SwapSchedule`, `FUSION_CTRADER_DEMO_48439552`, `FUSION_XAUUSD_SWAP_SPEC`, `resolve_commission_schedule`, and `BrokerCostService` (no Pandas/SQLAlchemy/NumPy). Live guard raises `ValueError` if `fusion_ctrader_demo` is requested with `trading_environment="live"`. | [broker_cost_service.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/broker_cost_service.py), [settings.py](file:///d:/gigs/goldedge/src/goldedge/config/settings.py) |
| **Backtest Engine Financial Legs** | Added `commission_profile`, `trading_environment`, `account_id`, `cost_service`, `apply_swap`, and `use_tick_intensity_features` to `RunBacktestUseCase.execute(...)`. `_OpenPosition` tracks `commission_entry_usd`, `commission_exit_usd`, partial closes (`close_partial`), idempotent guards (`_entry_comm_recorded`, `_exit_comm_lots_charged`), and DST rollover sweeps (`apply_rollovers_until`). Preserved legacy `fixed_per_lot` fallback for backward-compatible B5 unit tests. | [run_backtest.py](file:///d:/gigs/goldedge/src/goldedge/application/use_cases/run_backtest.py) |
| **Volume Semantics & Parity** | Removed `COALESCE(volume, 1)` from `TickRepository`; query exposes `volume AS real_volume` (`100% NULL` in spot CFD DB). Updated `candle_repository.py` (`NULL::bigint AS real_volume`), `scan_scheduler.py` (`if bid <= 0: return`, `real_volume=None`), `VolumeAdaptiveKalmanFilter.update_tick(price, volume: Optional[float] = None)`, `mtf_trend.py`, and `IndicatorService` (`tick_count`, `tick_vwap`, `tick_intensity_ratio`, `real_volume`, preserving backward aliases). Added `use_tick_intensity_features: bool = True` toggle to `SignalGeneratorService` & `TradeScorer`. | [tick_repository.py](file:///d:/gigs/goldedge/src/goldedge/adapters/secondary/db/tick_repository.py), [candle_repository.py](file:///d:/gigs/goldedge/src/goldedge/adapters/secondary/db/candle_repository.py), [scan_scheduler.py](file:///d:/gigs/goldedge/src/goldedge/adapters/primary/scheduler/scan_scheduler.py), [denoise_service.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/denoise_service.py), [mtf_trend.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/mtf_trend.py), [indicator_service.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/indicator_service.py), [signal_generator.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/signal_generator.py), [trade_scorer.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/trade_scorer.py) |
| **Tests & Audit Harness** | Added comprehensive Task 7 unit & parity suite (`test_task7_remediation.py`), updated obsolete B4 `COALESCE(volume, 1)` SQL string assertion in `test_remediation_b1_b6.py`, and built end-to-end verification script `scripts/audit/task7_validation.py`. | [test_task7_remediation.py](file:///d:/gigs/goldedge/tests/test_task7_remediation.py), [test_remediation_b1_b6.py](file:///d:/gigs/goldedge/tests/test_remediation_b1_b6.py), [task7_validation.py](file:///d:/gigs/goldedge/scripts/audit/task7_validation.py) |

---

## 3. Commission Reconciliation (15 Historical Demo Deals)

Executed via `BrokerCostService(FUSION_CTRADER_DEMO_48439552)` against `backtest_results/audit/task5_commission_reconciliation.csv` ([task7_commission_reconciliation.csv](file:///d:/gigs/goldedge/backtest_results/audit/task7_commission_reconciliation.csv)):

- **Formula verified:** `round(abs(lots) * 100 * execution_price * 12 / 1_000_000, 2)` independently on each open, partial, and close execution leg.
- **Historical deal match:** **`15 / 15` deals**, **`7 / 7` positions**, max side discrepancy = **`$0.00`**, max round-trip discrepancy = **`$0.00`**.
- **Partial closes, multi-execs, duplicate-charge guard, live blocker:** Verified in `TestTask7CommissionCorrection` (`test_partial_close_multi_exec_and_duplicate_commission_guard` and `test_live_safety_guard_blocks_unverified_demo_commission_profile`).

---

## 4. Rollover & DST Validation (`America/New_York` 17:00)

All **`10 / 10` test scenarios** in [task7_rollover_dst_tests.csv](file:///d:/gigs/goldedge/backtest_results/audit/task7_rollover_dst_tests.csv) passed:

| Scenario | Rollover UTC Hour | Swap P&L (USD / 1.00 lot) | Result |
|---|---:|---:|:---:|
| Summer EDT Weekday (`2026-09-01`, BUY / SELL) | `21:00 UTC` | `-$56.53` / `+$26.77` | PASS |
| Wednesday Triple Swap (`2026-09-02`, BUY / SELL, `3x`) | `21:00 UTC` | `-$169.59` / `+$80.31` | PASS |
| Winter EST Pre-Rollover Check (`2026-11-16 20:00..21:30 UTC`) | None (`-1`) | `$0.00` (no `21:00Z` false trigger) | PASS |
| Winter EST Rollover (`2026-11-16 20:00..22:05 UTC`, BUY) | `22:00 UTC` | `-$56.53` | PASS |
| DST Fall-Back (`2026-11-02`, SELL) / Spring-Forward (`2026-03-09`, BUY) | `22:00Z` / `21:00Z` | `+$26.77` / `-$56.53` | PASS |
| Weekend Exclusion (`2026-09-04 Fri 18Z -> Mon 10Z`, BUY) | `21:00 UTC` (Fri only) | `-$56.53` (`Sat/Sun = $0`) | PASS |
| Intraday Exit Before Rollover (`2026-09-02 10:00..20:59 UTC`) | None (`-1`) | `$0.00` | PASS |

---

## 5. Application Backtest Comparison (`2026-08-24` to `2026-11-16`)

Executed end-to-end inside `goldedge-backend-dev` via `RunBacktestUseCase` (`MEDIUM_BACKTEST`, score `>= 0.60`, `use_kalman_exit = False`, `$10,000` initial capital) and saved in [task7_application_backtest_comparison.csv](file:///d:/gigs/goldedge/backtest_results/audit/task7_application_backtest_comparison.csv) & [task7_independent_validation.json](file:///d:/gigs/goldedge/backtest_results/audit/task7_independent_validation.json):

| Run Scenario | Trades | Win% | Gross P&L | Comm ($) | Slip ($) | Swap ($) | Net P&L | PF | Closed DD | Tick-MTM DD (HWM) | BUY / SELL Net |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Task 7 Zero-Cost Parity** (`$0`, `0p`, no swap) | `118` | `55.93%` | `+$400.64` | `$0.00` | `$0.00` | `$0.00` | **`+$400.64`** | `1.0926` | `-$1,267.20` (`11.48%`) | `-$1,354.97` (`12.20%`) | `-$386.54` / `+$787.18` |
| **Task 4 Frozen Reference** (`$7/lot` + `0.30p`, no swap) | `118` | `54.24%` | `+$256.00` | `$125.30` | `$103.26` | `$0.00` | `+$130.70` | `1.0303` | `-$995.77` (`9.81%`) | `-$1,301.95` (`12.83%`) | `-$423.89` / `+$554.59` |
| **Task 7 Verified Demo (`0.00p` median slip)** (`$12/M` per side + NY17Z Swap) | `118` | `54.24%` | `+$321.13` | `$177.46` | `$0.00` | `$0.00` | **`+$143.67`** | `1.0334` | `-$1,312.65` (`12.00%`) | `-$1,385.44` (`12.60%`) | `-$438.60` / `+$582.28` |
| **Task 7 Verified Demo (`0.0533p` mean slip)** (`$12/M` per side + NY17Z Swap) | `118` | `55.08%` | `+$293.35` | `$177.26` | `$18.19` | `$0.00` | **`+$116.09`** | `1.0270` | `-$1,333.53` (`12.19%`) | `-$1,406.47` (`12.80%`) | `-$437.53` / `+$553.63` |
| **Task 7 Verified Demo (`0.30p` conservative slip)** (`$12/M` per side + NY17Z Swap) | `118` | `53.39%` | `+$218.37` | `$176.57` | `$102.00` | `$0.00` | **`+$41.80`** | `1.0097` | `-$1,378.21` (`12.63%`) | `-$1,448.29` (`13.21%`) | `-$451.33` / `+$493.13` |
| **Research: `use_tick_intensity_features=False`** (`$12/M` + NY17Z Swap + `0.0533p`) | `126` | `53.97%` | `+$180.84` | `$190.10` | `$19.62` | `$0.00` | **`-$9.26`** | `0.9980` | `-$1,190.67` (`10.91%`) | `-$1,289.09` (`11.73%`) | `-$84.54` / `+$75.29` |

### Key Analytical Findings
1. **Zero-Cost Parity Preserved (`$0.000000` drift):** Comparing Task 7 zero-cost vs frozen `task4_zero_cost_ref.csv` yielded `0.0` entry-price, `0.0` exit-price, and `0.0` P&L discrepancy across all `118` trades.
2. **Independent Recalculation (`0.0` diff):** Pure-Python independent recalculation over all `118` trades in both Verified Demo runs (`0.00p` and `0.0533p`) matched commission, swap P&L, and net P&L to **`$0.000000`**.
3. **True Cost Impact:** Correcting commission from the understated `$7.00/lot` fixed assumption (`$125.30` total) to the verified notional formula (`$12/M` per side, `~$10.29–$11.04/lot` RT = `$176.57–$177.46` total) adds `+$51.27` (`+40.9%`) in commission drag, dropping `0.30p`-stress Net P&L from `+$130.70` (`PF 1.0303`) to `+$41.80` (`PF 1.0097`).
4. **Why Overnight Swap = `$0.00` in Baseline:** All `118` trades opened within `07:00–17:00 UTC` and exited before NY `17:00` rollover (`21:00Z`/`22:00Z`) due to `60m`/`90m` max hold; overnight swap triggers cleanly in DST/rollover unit/integration tests whenever a position spans `17:00` NY time.

---

## 6. Volume Semantics Validation & How to Fix True Volume

Database verification (`market_ticks` across `2026-08-24`..`2026-11-16`, [task7_volume_semantics_validation.csv](file:///d:/gigs/goldedge/backtest_results/audit/task7_volume_semantics_validation.csv)):
- **`17,303,717` total XAUUSD ticks**, **`0` non-null `volume` values (`100% SQL NULL`)**.
- **`VolumeAdaptiveKalmanFilter(px, None)` vs `(px, 1.0)` diff:** `0.0` (`< 1e-12`).
- **Disabling `tick_intensity_ratio` / `tick_vwap` (`use_tick_intensity_features=False`):** Shifted trades `118 -> 126`, dropped net P&L from **`+$116.09` (`PF 1.0270`) to `-$9.26` (`PF 0.9980`)**, proving that quote-arrival intensity carries empirically distinct microstructure information (not true contract volume!) and should remain toggleable rather than silently stripped out without dedicated research.

### Answering: *"Strict synthetic quote-arrival frequency (1 per quote tick) vs actual traded volume — how can we fix?"*
There are two distinct layers to fixing this permanently:
1. **Software / Semantic Fix (Implemented in Task 7):**
   - Stop fabricating `COALESCE(volume, 1)` in SQL and live schedulers.
   - Separate `real_volume` (`NULL` unless a real exchange feed supplies executed lots/contracts) from `tick_count` / `tick_volume` (`quote arrivals per minute`).
   - Rename quote-based features (`tick_vwap`, `tick_intensity_ratio`, `tick_intensity_spike`) so no downstream component mistakes quote-arrival bursts for traded dollar volume.
   - Expose `use_tick_intensity_features` so future research can test strategies completely free of broker quote-rate artifacts.
2. **Market-Data / Feed Fix (External Data Source Reality):**
   - Spot CFD brokers like cTrader / Fusion Markets **do not broadcast centralized transacted order-flow lot volume** on Spot Gold CFDs (`real_volume` does not exist in their CFD price feed).
   - If a strategy truly requires executed traded volume, delta, or VWAP weighted by real transacted contracts, you must either:
     - **(a) Subscribe to a centralized exchange futures feed** such as CME COMEX Gold Futures (`GC` / `MGC` via Databento, Interactive Brokers, Rithmic, or dxFeed) to obtain real exchange trade prints & contract volume, OR
     - **(b) Replace volume-dependent rules altogether** in future strategy research (`M2`/Candidate optimization) with pure executable price/spread/volatility/Kalman-kinematic signals that have zero dependence on broker quote-rate variability.

---

## 7. Unit Test Suite & Integrity Verification

Executed inside `goldedge-backend-dev` against the exact edited workspace (`PYTHONPATH=/app:/app/src:/tmp`):
```
python3 -m unittest test_remediation_b1_b6 test_shadow_validation test_backtest_history test_task7_remediation -v
Ran 27 tests in 0.768s — OK (0 failures, 0 errors, 0 skipped)
```
Backend container restarted cleanly (`docker compose -f docker-compose.dev.yml restart backend`) and verified live HTTP health (`GET http://127.0.0.1:5000/api/backtest/history` -> `200`).

### Preserved Baseline Artifact Hashes & Live Safety State
- `SHADOW_MODE`: `True` | `TRADING_MODE`: `"demo"` | `BROKER_COMMISSION_PROFILE`: `"fusion_ctrader_demo"`
- DB Safety Counts: `market_orders = 0`, `market_trades = 0`
- `frozen_111_baseline_trades.csv` SHA-256: `73306e1529adc48284bce9ab6ac35122f33b9999d2464af441867eb3bb6cf168` (unchanged)
- `task4_run_1.csv` SHA-256: `e51c891499e485a84d2f3acbdb225af241bf844545b68d9ce6f6b7549a438c36` (unchanged)
- `task4_zero_cost_ref.csv` SHA-256: `8478d4a5d49f6e48faae2a0861f634a5f06d1109464d7544f764d80405dc86dd` (unchanged)

---

## 8. Explicitly Unverified Items (Do Not Extrapolate)

1. **Historical Daily Swap-Rate Revisions (`2026-08-24`–`2026-11-16`):** Only current Fusion Markets `XAUUSD` symbol metadata (`swapLong = -5.653` pips, `swapShort = +2.677` pips) could be verified via cTrader Open API. Past daily swap adjustments cannot be queried; `SwapSchedule.daily_rate_overrides` & `historical_rates_verified=False` make this limitation explicit.
2. **Real-Money Live Account Commission Schedule:** Verified rate (`$12 / $1M` notional per side) applies strictly to Demo account `48439552`. Applying `fusion_ctrader_demo` under `TRADING_MODE == "live"` is explicitly blocked by `resolve_commission_schedule`.
3. **True Executed Traded Volume:** Spot CFD tick feed delivers quote updates only (`0 / 17,303,717` rows have `volume != NULL`). Quote-arrival frequency should never be interpreted as transacted institutional contract volume.

---

## 9. Deliverables Index
- Report: [task7_remediation_report.md](file:///d:/gigs/goldedge/backtest_results/audit/task7_remediation_report.md)
- Commission Reconciliation: [task7_commission_reconciliation.csv](file:///d:/gigs/goldedge/backtest_results/audit/task7_commission_reconciliation.csv)
- Rollover & DST Matrix: [task7_rollover_dst_tests.csv](file:///d:/gigs/goldedge/backtest_results/audit/task7_rollover_dst_tests.csv)
- Volume Semantics Check: [task7_volume_semantics_validation.csv](file:///d:/gigs/goldedge/backtest_results/audit/task7_volume_semantics_validation.csv)
- Application Backtest Comparison: [task7_application_backtest_comparison.csv](file:///d:/gigs/goldedge/backtest_results/audit/task7_application_backtest_comparison.csv)
- Machine-Readable Verification Summary: [task7_independent_validation.json](file:///d:/gigs/goldedge/backtest_results/audit/task7_independent_validation.json)
