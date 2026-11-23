# Task 7B (Phase 1) — Read-Only Investigation Report: Historical Real Volume vs Synthetic Quote-Arrival Frequency

**Timestamp:** `2026-11-18T17:15Z`  
**Mode:** Strictly Read-Only Audit (zero files or database records modified before completion of this report)

---

## 1. Database Schema & Direct SQL Quantification

### A. Tables Audited (`public` schema, PostgreSQL + TimescaleDB)
1. **`market_ticks`** (`PRIMARY KEY (symbol, time)` across 13 chunks):
   - Columns: `time TIMESTAMPTZ NOT NULL`, `symbol VARCHAR(20) NOT NULL`, `bid DOUBLE PRECISION NOT NULL`, `ask DOUBLE PRECISION NOT NULL`, `last DOUBLE PRECISION`, `volume BIGINT`, `flags INTEGER`.
2. **`market_bars_1m`** (`PRIMARY KEY (symbol, time)`):
   - Columns: `time TIMESTAMPTZ NOT NULL`, `symbol VARCHAR(20) NOT NULL`, `open..close DOUBLE PRECISION`, `tick_volume BIGINT NOT NULL`, `real_volume BIGINT`, `spread INTEGER NOT NULL`.

### B. Full-Table & Window Quantification (`XAUUSD`)
- **Full `market_ticks` range:** `2026-08-20 00:00:00.148+00` to `2026-11-18 17:10:52.939173+00` (`18,059,851` total rows as of final sample; `18,058,818` rows in volume-breakdown snapshot):
  - `NULL volume` records: **`18,058,818` (`100.00%`)**
  - `volume = 1` physically stored: **`0`**
  - Other `non-NULL volume`: **`0`**
  - Non-positive quotes (`bid <= 0 OR ask <= 0`): **`0`**
  - Inverted quotes (`bid > ask`): **`11,612`**
  - Zero-spread quotes (`bid = ask`): **`433`**
  - Duplicate `(symbol, time)` timestamps: **`0`** (uniqueness enforced by `market_ticks_pkey`)
- **Task 4 Frozen Window (`2026-08-24T00:00:00Z` to `2026-11-16T20:01:23Z`):**
  - Total tick rows: **`17,303,717`**
  - Strictly valid quotes (`bid > 0 AND ask > 0 AND bid < ask`): **`17,291,884`**
  - Inverted quotes (`bid > ask`): **`11,409`**
  - Zero-spread quotes (`bid = ask`): **`424`**
  - Non-empty 1m buckets (`< 2026-11-16T20:02:00Z`): **`83,719`** (average **`206.55` valid quotes/min**, min `5`, max `559`), including **`3` minute buckets where all quotes were invalid/inverted (`valid_cnt = 0`)**.
- **Existing `market_bars_1m` (`17,492` rows, `2026-08-06`..`2026-11-18`):**
  - **`9,031` rows have `real_volume = 0`** (written prior to Task 7 `NULL::bigint` fix); `8,461` rows have `real_volume IS NULL`.

**Conclusion on Storage vs Retrieval:** Synthetic `volume = 1` does **not** exist physically inside `market_ticks`; `100%` of stored tick rows have `volume IS NULL`. However, synthetic fabrication/mislabeling exists in ingestion fallback code (`ctrader_provider.py`), derived `market_bars_1m` (`9,031` rows with `real_volume = 0`), aggregation validity filters, indicators (`vwap`, `volume_ratio`), chart filters (`apply_gold_adaptive_kalman`), and REST responses (`order_flow_momentum`).

---

## 2. Codebase Inventory & Defect Findings

| Component | File | Current Defect / Finding |
|---|---|---|
| **Tick Repository (`get_ticks_as_df`)** | `src/goldedge/adapters/secondary/db/tick_repository.py` | Task 7 removed `COALESCE(volume, 1)`; now selects `volume AS real_volume` (`NULL`). No invalid-quote or duplicate semantic documentation yet. |
| **Historical API Ingestor** | `src/goldedge/adapters/secondary/market_data/ctrader_provider.py` | Line `112`: still hardcodes **`df["volume"] = 1`** when persisting chunks! Line `96–97`: arbitrarily calls `drop_duplicates(subset=["timestamp"], keep="last")`. |
| **Live Stream & Scheduler** | `src/goldedge/adapters/primary/market_data/ctrader_live.py`, `src/goldedge/adapters/primary/scheduler/scan_scheduler.py` | `ScanScheduler._ingest_warmup_tick` (line 80) and `_handle_tick` (line 312) only check `if bid <= 0: return`, silently substituting `ask = bid` if absent and accepting inverted/zero-spread quotes (`bid >= ask`) into `tick_volume`, creating a parity gap vs DB validity. `dt <= prev_ts` drops distinct same-timestamp updates. |
| **Candle & Aggregator Repos** | `candle_repository.py`, `pandas_aggregator_repo.py`, `mtf_trend.py` | `CandleRepository.upsert_1m_bars_from_ticks` (line 111) filters `AND bid > 0` (missing `ask > 0 AND bid < ask`). `PandasAggregatorRepository` counts raw `bid` without validity filter. No `tick_rate` or `is_feed_gap` representation exists to separate missing feed intervals from zero activity. |
| **`VolumeAdaptiveKalmanFilter`** | `src/goldedge/domain/services/denoise_service.py` | Accepts `volume: Optional[float] = None`; because `real_volume` is 100% `NULL`, `vol_factor` evaluates to `1.0` on every tick — **volume adaptation is dead/inactive**. Docstring still claims `"High volume (informed flow) -> small R"`. |
| **Chart Kalman & Order Flow Route** | `src/goldedge/adapters/primary/rest_api/routes/charts.py`, `src/goldedge/gold/adapters/features/gold_kalman_filter.py`, `dashboard/lib/api.ts` | `charts.py` lines 124–133 actively pass `df["tick_volume"]` into `apply_gold_adaptive_kalman` / `apply_forex_adaptive_kalman`, whose docstring claims *"institutional participation"*. `charts.py` lines 166–210 & `api.ts` line 421 compute **`"order_flow_momentum"`** from `sign(returns) * log1p(tick_volume)`. |
| **`IndicatorService`** | `src/goldedge/domain/services/indicator_service.py` | Computes `ta.vwap(..., df["tick_count"])` and exposes it as **`"vwap"`** (`vwap_dist`) even though weighting uses synthetic quote frequency (`real_volume` is `NULL`). Exposes `volume_ratio`, `vol_sma20`, `volume_spike` aliases alongside `tick_intensity_ratio`. |
| **Signals, Scorer & Live Telemetry** | `signal_generator.py`, `trade_scorer.py`, `scan_market.py`, `run_backtest.py`, `run_tick_replay.py` | `SignalGeneratorService` & `TradeScorer` look up `"vwap"`, `"vwap_dist"`, `"volume_ratio"`, `"tick_volume"` (`_session_orb` uses `vol_surge`). `ScanMarketUseCase` logs `"vwap=..."` in `[SHADOW_ROLLOVER]` and passes `volume_ratio` / `vwap_dist`. Frozen pickled `models/gold_ridge_pnl.pkl` (observation-only) expects legacy feature keys `['rsi', 'atr_ratio', 'volume_ratio', 'ema20_slope', 'ema_spread', 'vwap_dist', 'kalman_velocity', 'spread_mean', 'hour_sin', 'hour_cos', 'quant_score']`. |
| **Backtest Engine Tick Counter** | `src/goldedge/application/use_cases/run_backtest.py` | Lines `616` & `797` compute `tick_cnt = int((bids > 0.0).sum())`, counting `11,409` inverted and `424` zero-spread quotes into 1m bar `tick_count` instead of `valid_mask.sum()` (`bid > 0 AND ask > 0 AND bid < ask`). |

---

## 3. Remediation Plan (`QUOTE_ARRIVAL_SEMANTICS_V1`)
1. **Preserve all `market_ticks` & Task 4 frozen baselines untouched**; deploy `migrations/008_quote_arrival_semantics_v1.sql` creating view `v_market_ticks_quote_semantics_v1` + versioned `market_bars_1m_quote_semantics_v1` reconstructed cleanly from valid quotes (`bid > 0 AND ask > 0 AND bid < ask`) with `real_volume = NULL`, `tick_count`, `tick_rate`, and `is_feed_gap`.
2. **Unify strict quote validity (`bid > 0 AND ask > 0 AND bid < ask`) and explicit duplicate/gap handling** across `ctrader_provider.py` (`volume=None`), `ScanScheduler`, `CandleRepository`, `PandasAggregatorRepository`, `mtf_trend.py`, and `RunBacktestUseCase`.
3. **Eliminate synthetic volume/order-flow/VWAP misrepresentations:**
   - Decouple `VolumeAdaptiveKalmanFilter` explicitly (document inactive traded-volume term; stop `charts.py` from feeding `tick_volume` as traded volume to `apply_*_adaptive_kalman`).
   - Rename `volume_ratio` -> `tick_rate_ratio`, `vol_sma20` -> `tick_rate_sma20`, `volume_spike` -> `tick_rate_spike`.
   - Rename quote-weighted VWAP -> **`tick_weighted_price`** (`tick_weighted_price_dist`), and compute genuine `vwap` strictly only when non-null `real_volume > 0` exists (`NaN`/`None` otherwise).
   - Replace `"order_flow_momentum"` in `routes/charts.py` and `dashboard/lib/api.ts` with `"quote_rate_momentum"` (explicit quote-arrival proxy, never institutional order flow).
4. **Validate via full Phase 6 automated tests + Phase 7 full-period GoldEdge application backtest + independent backtest reconciliation** on the frozen Task 4 window (`2026-08-24`..`2026-11-16`).
