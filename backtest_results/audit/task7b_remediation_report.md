# Task 7B — Historical Real Volume vs Synthetic Quote-Arrival Frequency Remediation Report

**Outcome Tag:** `QUOTE_ARRIVAL_SEMANTICS_V1`  
**Final Verdict:** **`VERIFIED`**  
**Date:** `2026-11-18 UTC`  
**Base Git Commit:** `1bf6c45b3f5b790e3212c9b5b7a683d6fd5e3622` (`TRADING_MODE = "demo"`, `SHADOW_MODE = True`, `market_orders = 0`, `market_trades = 0`)

---

## 1. Root-Cause Summary (Phase 1 Investigation)

Full read-only investigation ([task7b_phase1_investigation_report.md](file:///d:/gigs/goldedge/backtest_results/audit/task7b_phase1_investigation_report.md)) established:
1. **`market_ticks` (`18,058,818` ticks at audit start -> `18,068,020` ticks at completion):** **`100.00%` have `volume IS NULL`**, `0` stored rows have `volume = 1`, `0` have non-null volume, **`11,612` rows had `bid > ask`**, **`433` had `bid = ask`**, and **`0` duplicate `(symbol, time)` rows** exist (`PRIMARY KEY (symbol, time)`).
2. **Where synthetic volume & semantic misrepresentation entered:**
   - [ctrader_provider.py](file:///d:/gigs/goldedge/src/goldedge/adapters/secondary/market_data/ctrader_provider.py#L112) hardcoded `df["volume"] = 1` on historical ingestion.
   - `market_bars_1m` contained **`9,031` legacy rows with `real_volume = 0`** out of `17,492` total bars.
   - Historical & live pipelines used inconsistent quote validity rules (`bid > 0` in `CandleRepository` and `ScanScheduler`, `(bids > 0.0).sum()` in `RunBacktestUseCase` vs strict `bid > 0 AND ask > 0 AND bid < ask` in execution checks), leaking inverted/corrupted quotes into `tick_count` and `ATR`.
   - `VolumeAdaptiveKalmanFilter` left fake volume scaling implicitly wired (`vol_factor = 1.0` only accidentally because `volume = None`), while [charts.py](file:///d:/gigs/goldedge/src/goldedge/adapters/primary/rest_api/routes/charts.py#L124-L133) actively fed `df["tick_volume"]` into `apply_gold_adaptive_kalman` / `apply_forex_adaptive_kalman` (whose docstring claimed *"institutional participation"*) and exposed `"order_flow_momentum"` from quote arrival counts.
   - [IndicatorService](file:///d:/gigs/goldedge/src/goldedge/domain/services/indicator_service.py), [SignalGeneratorService](file:///d:/gigs/goldedge/src/goldedge/domain/services/signal_generator.py), [TradeScorer](file:///d:/gigs/goldedge/src/goldedge/domain/services/trade_scorer.py), and [ScanMarketUseCase](file:///d:/gigs/goldedge/src/goldedge/application/use_cases/scan_market.py) labeled quote-count features as `vwap`, `vwap_dist`, `volume`, and `volume_ratio`.

---

## 2. Non-Destructive Database Migration & Historical Reconstruction (Phases 2 & 5)

Deployed [008_quote_arrival_semantics_v1.sql](file:///d:/gigs/goldedge/migrations/008_quote_arrival_semantics_v1.sql) without deleting, truncating, or altering `market_ticks` or legacy `market_bars_1m`:
- **`v_market_ticks_quote_semantics_v1`** (View): Exposes nullable `volume AS real_volume`, `is_valid_quote` (`bid > 0 AND ask > 0 AND bid < ask`), and `tick_count_unit` (`1` only when `is_valid_quote`, `0` otherwise).
- **`market_bars_1m_quote_semantics_v1`** (Versioned Table): Stores **`86,473` reconstructed 1m bars** across the Task 6 approved continuous window (`2026-08-20T00:00:00Z` through `2026-11-16T20:01:23Z`, excluding known post-Nov-16 live outages), with explicit `tick_count`, `tick_rate`, `real_volume = NULL`, `spread_points`, `raw_quote_count`, `invalid_quote_count`, `is_feed_gap`, and `semantic_version = 'QUOTE_ARRIVAL_SEMANTICS_V1'`.
  - Task 4 Window (`2026-08-24T00:00:00Z`–`2026-11-16T20:01:23Z`): **`83,716` valid 1m bars** (`3` corrupted all-inverted minutes excluded), **`17,291,884` valid ticks** (`+ 145` post-`20:01:23Z` ticks in the final `20:01` minute = exact **`17,292,029`** in `market_bars_1m_quote_semantics_v1`), **`0` non-null `real_volume` rows**.
- **`v_market_bars_1m_timeline_v1`** (View): Separates **`83,716` observed minutes** from **`38,446` missing feed/rollover/weekend minutes** flagged `is_feed_gap = TRUE` (`tick_rate = NULL`), preventing gaps from masquerading as zero market activity.

---

## 3. Code, Indicator, Signal & UI Corrections (Phases 3 & 4)

| Area | Files Modified / Created | Remediation Implemented |
|---|---|---|
| **DB Migration & Views** | [008_quote_arrival_semantics_v1.sql](file:///d:/gigs/goldedge/migrations/008_quote_arrival_semantics_v1.sql) | Created non-destructive `v_market_ticks_quote_semantics_v1`, `market_bars_1m_quote_semantics_v1`, `v_market_bars_1m_timeline_v1`. |
| **Domain Models & Ingestion** | [bar.py](file:///d:/gigs/goldedge/src/goldedge/domain/models/bar.py), [ctrader_provider.py](file:///d:/gigs/goldedge/src/goldedge/adapters/secondary/market_data/ctrader_provider.py), [tick_repository.py](file:///d:/gigs/goldedge/src/goldedge/adapters/secondary/db/tick_repository.py), [candle_repository.py](file:///d:/gigs/goldedge/src/goldedge/adapters/secondary/db/candle_repository.py), [pandas_aggregator_repo.py](file:///d:/gigs/goldedge/src/goldedge/adapters/secondary/database/pandas_aggregator_repo.py) | Separated `tick_count`, `tick_rate`, `real_volume`, `is_feed_gap`; replaced `df["volume"] = 1` with `None`; removed `COALESCE(volume, 1)`; enforced `bid > 0 AND ask > 0 AND bid < ask` across all SQL & Pandas aggregators. |
| **Live Scheduler & MTF** | [scan_scheduler.py](file:///d:/gigs/goldedge/src/goldedge/adapters/primary/scheduler/scan_scheduler.py), [mtf_trend.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/mtf_trend.py) | Enforced `bid > 0 AND ask > 0 AND bid < ask` in warmup & live ticks (never faking `ask = bid`), preserved distinct same-timestamp quotes while skipping exact dupes, added `tick_rate` & `is_feed_gap`. |
| **Kalman Filters** | [denoise_service.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/denoise_service.py), [gold_kalman_filter.py](file:///d:/gigs/goldedge/src/goldedge/gold/adapters/features/gold_kalman_filter.py), [charts.py](file:///d:/gigs/goldedge/src/goldedge/adapters/primary/rest_api/routes/charts.py) | Added `adapt_to_volume: bool = False` default on `VolumeAdaptiveKalmanFilter` (pure price-diff adaptation unless genuine `real_volume` explicitly supplied); stopped `charts.py` from passing `tick_volume` to chart Kalman; updated docstrings. |
| **Indicators, Signals & Scorer** | [indicator_service.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/indicator_service.py), [signal_generator.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/signal_generator.py), [trade_scorer.py](file:///d:/gigs/goldedge/src/goldedge/domain/services/trade_scorer.py) | Renamed `volume_ratio` -> `tick_rate_ratio`, `vol_sma20` -> `tick_rate_sma20`, `volume_spike` -> `tick_rate_spike`, quote-weighted `vwap` -> `tick_weighted_price` (`tick_weighted_price_dist`). Kept genuine `vwap = NaN` when `real_volume` is NULL. Updated signal & scorer condition names cleanly without optimizing thresholds. |
| **Use Cases, API & UI Types** | [run_backtest.py](file:///d:/gigs/goldedge/src/goldedge/application/use_cases/run_backtest.py), [scan_market.py](file:///d:/gigs/goldedge/src/goldedge/application/use_cases/scan_market.py), [run_tick_replay.py](file:///d:/gigs/goldedge/src/goldedge/application/use_cases/run_tick_replay.py), [charts.py](file:///d:/gigs/goldedge/src/goldedge/adapters/primary/rest_api/routes/charts.py), [api.ts](file:///d:/gigs/goldedge/dashboard/lib/api.ts) | `RunBacktestUseCase` aggregates strictly from valid quotes (`bid > 0 & ask > 0 & bid < ask`), skipping all-invalid 1m bars & feeding valid quotes only into Kalman; `ScanMarketUseCase` logs `twp`, `tick_rate_ratio`, and `vwap=None` (isolating legacy feature names strictly inside frozen observation-only `gold_ridge_pnl.pkl` call); replaced `order_flow_momentum` -> `quote_rate_momentum`. |
| **Tests & Validation** | [test_task7b_quote_semantics.py](file:///d:/gigs/goldedge/tests/test_task7b_quote_semantics.py), [test_task7_remediation.py](file:///d:/gigs/goldedge/tests/test_task7_remediation.py), [test_shadow_validation.py](file:///d:/gigs/goldedge/tests/test_shadow_validation.py), [task7b_validation.py](file:///d:/gigs/goldedge/scripts/audit/task7b_validation.py) | Added 10-check Phase 6 automated suite & full Phase 7 backtest + SQL verification script. |

---

## 4. Automated Test Results (Phase 6)

Executed inside `goldedge-backend-dev`:
```
python3 -m unittest test_remediation_b1_b6 test_shadow_validation test_backtest_history test_task7_remediation test_task7b_quote_semantics -v
Ran 33 tests in 0.841s — OK (0 failures, 0 errors, 0 skipped)
```
Both backend API endpoints verified live post-restart:
- `GET http://127.0.0.1:5000/api/backtest/history` -> `200`
- `GET http://127.0.0.1:5000/api/charts/data?symbol=XAUUSD&timeframe=1m&limit=50` -> `200`

---

## 5. Actual Application Backtest vs Frozen Baseline & Independent Reconciliation (Phase 7)

Window: **`2026-08-24T00:00:00Z` – `2026-11-16T20:01:23Z`**, `MEDIUM_BACKTEST`, Score `>= 0.60`, `$10,000` initial capital ([task7b_backtest_comparison.csv](file:///d:/gigs/goldedge/backtest_results/audit/task7b_backtest_comparison.csv) & [task7b_independent_validation.json](file:///d:/gigs/goldedge/backtest_results/audit/task7b_independent_validation.json)):

| Run | Trades | Win% | Gross P&L | Comm ($) | Slip ($) | Swap ($) | Net P&L | PF | Closed DD | Tick-MTM DD (HWM) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Frozen Task 4 Baseline** (`$7/lot + 0.30p`) | `118` | `54.24%` | `+$256.00` | `$125.30` | `$103.26` | `$0.00` | `+$130.70` | `1.0303` | `-$995.77` (`9.81%`) | `-$1,301.95` (`12.83%`) |
| **Task 7B App Corrected** (`$7/lot + 0.30p`) | `118` | `54.24%` | `+$256.45` | `$125.30` | `$103.26` | `$0.00` | **`+$131.15`** | `1.0305` | `-$1,339.20` (`12.22%`) | `-$1,412.50` (`12.82%`) |
| **Task 7B App Corrected Zero-Cost** (`$0, 0p`) | `118` | `55.93%` | `+$401.12` | `$0.00` | `$0.00` | `$0.00` | **`+$401.12`** | `1.0927` | `-$1,266.72` (`11.48%`) | `-$1,354.49` (`12.20%`) |
| **Task 7B Verified Demo** (`$12/M side + NY17Z Swap + 0.0533p`) | `118` | `55.08%` | `+$293.80` | `$177.26` | `$18.19` | `$0.00` | **`+$116.54`** | `1.0271` | `-$1,333.08` (`12.19%`) | `-$1,406.02` (`12.79%`) |

### Trade-by-Trade Reconciliation & Root-Cause of the Single Changed Trade
From [task7b_trade_by_trade_diff_vs_task4.csv](file:///d:/gigs/goldedge/backtest_results/audit/task7b_trade_by_trade_diff_vs_task4.csv) and [task7b_trade_by_trade_diff_vs_task4_zero.csv](file:///d:/gigs/goldedge/backtest_results/audit/task7b_trade_by_trade_diff_vs_task4_zero.csv):
- **`117 / 118` trades:** **`EXACT_MATCH`** (`$0.000000` entry price, exit price, exit timestamp, exit reason, quantity, and P&L difference).
- **`1 / 118` changed trade — `2026-10-08 15:50:00` (`SELL`, `TRAILING_SL`):**
  - Task 4 Frozen: `exit = $4,110.40`, `pnl = +$47.70`
  - Task 7B Corrected: `exit = $4,110.37` (`-$0.03`), `pnl = +$48.15` (`+$0.45`)
  - **Forensic Explanation:** Direct PostgreSQL inspection on `2026-10-08` confirmed that inverted quotes at `2026-10-08 14:59:00Z` had previously dragged the minute's low down (`raw_low = 4124.30` vs valid-quote `valid_low = 4124.50`, a `$0.20` artificial wick), plus `28` inverted quotes at `15:35:18–15:35:26Z`. Filtering corrupted quotes out of 1m candle OHLC under `QUOTE_ARRIVAL_SEMANTICS_V1` raised that 5m bar's `low` by `$0.20`, trimming EMA-smoothed `ATR14` at `15:50Z` by `-$0.03` and shifting the `1.10x ATR` trailing stop from `$4,110.40` to `$4,110.37`.

### Independent SQL & Execution Verification
- `market_bars_1m_quote_semantics_v1` vs `v_market_ticks_quote_semantics_v1` (`2026-08-20`..`2026-11-16`): `max_bar_open_diff_vs_view = 0.0`, exact `17,291,884` + `145` post-`20:01:23Z` cutoff ticks = `17,292,029`.
- **Independent Execution Verification (`118 / 118` trades against raw `v_market_ticks_quote_semantics_v1`):**
  - `max_entry_price_diff_usd = 0.0`
  - `max_gross_pnl_diff_usd = 0.0`
  - `max_commission_diff_usd = 0.0`
  - `max_slippage_diff_usd = 0.0`
  - `max_net_pnl_diff_usd = 0.0`

### Frozen Baseline Hash Verification
- `frozen_111_baseline_trades.csv`: `73306e1529adc48284bce9ab6ac35122f33b9999d2464af441867eb3bb6cf168` (intact)
- `task4_run_1.csv`: `e51c891499e485a84d2f3acbdb225af241bf844545b68d9ce6f6b7549a438c36` (intact)
- `task4_zero_cost_ref.csv`: `8478d4a5d49f6e48faae2a0861f634a5f06d1109464d7544f764d80405dc86dd` (intact)

---

## 6. Deliverables Index
- Main Report: [task7b_remediation_report.md](file:///d:/gigs/goldedge/backtest_results/audit/task7b_remediation_report.md)
- Phase 1 Read-Only Report: [task7b_phase1_investigation_report.md](file:///d:/gigs/goldedge/backtest_results/audit/task7b_phase1_investigation_report.md)
- DB Migration: [008_quote_arrival_semantics_v1.sql](file:///d:/gigs/goldedge/migrations/008_quote_arrival_semantics_v1.sql)
- Backtest Comparison & Diffs: [task7b_backtest_comparison.csv](file:///d:/gigs/goldedge/backtest_results/audit/task7b_backtest_comparison.csv), [task7b_trade_by_trade_diff_vs_task4.csv](file:///d:/gigs/goldedge/backtest_results/audit/task7b_trade_by_trade_diff_vs_task4.csv), [task7b_trade_by_trade_diff_vs_task4_zero.csv](file:///d:/gigs/goldedge/backtest_results/audit/task7b_trade_by_trade_diff_vs_task4_zero.csv), [task7b_independent_trades.csv](file:///d:/gigs/goldedge/backtest_results/audit/task7b_independent_trades.csv), [task7b_independent_validation.json](file:///d:/gigs/goldedge/backtest_results/audit/task7b_independent_validation.json)
- Tests & Validation Script: [test_task7b_quote_semantics.py](file:///d:/gigs/goldedge/tests/test_task7b_quote_semantics.py), [task7b_validation.py](file:///d:/gigs/goldedge/scripts/audit/task7b_validation.py)
