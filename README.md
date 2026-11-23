# GoldEdge

An institutional-grade, high-frequency algorithmic trading system for **XAU/USD (Spot Gold) CFD**.
Engineered using Strict Hexagonal Architecture (`GEMINI.md`), cTrader Open API Protobufs over TCP, Redis Streams, TimescaleDB, and a scalar-optimized 2D Volume-Adaptive Kalman Filter.

See [`GEMINI.md`](GEMINI.md) for mandatory Hexagonal Architecture rules and [`AGENTS.md`](AGENTS.md) for AI session context and safety invariants.

---

## 1. Current Operational Status & Safety Locks

- **Primary Symbol & Contract Spec (`XAUUSD`):** `contract_size = 100.0`, `tick_size = 0.01`, `tick_value = 1.0`, `min_lot = 0.01`, `volume_step = 0.01`, `margin_required_per_lot = 1000.0`.
- **Execution Mode (`src/goldedge/config/settings.py`):**
  - `SHADOW_MODE = True` (**strictly shadow-only — live/demo broker orders remain disabled** until out-of-sample `SELL` expectancy and drawdown targets are validated on forward shadow data).
  - `TRADING_MODE = "demo"`, `BROKER_TYPE = "ctrader"`, `KALMAN_WARMUP_HOURS = 24`.
  - Telemetry journal: `backtest_results/shadow_live_rollovers.jsonl` (restart-idempotent via `JsonlShadowJournal`).
- **Frozen Signal & Exit Pipeline (`SignalGeneratorService`, `MEDIUM_LIVE` / `MEDIUM_BACKTEST`):**
  - Entry score `>= 0.60`, `SL = 1.5x ATR`, `TP = 3.0x ATR`, `block_high_volatility = True`, `block_buy_sideways = True`, H1 `EMA20`/`EMA50` directional trend gate, cooldown `15` 1m bars per `(strategy, direction)`.
  - Exit mechanics (`run_backtest.py` / `ShadowValidationTracker`): Breakeven at `1.0 ATR` (`+2 pips = $0.20`), trailing tier 1 at `1.6 ATR` (`1.10 ATR` trail), tier 2 at `2.5 ATR` (`0.75 ATR` trail), `TIME_SQUEEZE` from `75%..100%` of `effective_max_hold` (`60m` in `07..19 UTC`, `90m` off-hours), `use_kalman_exit = False`.
  - Ridge model (`models/gold_ridge_pnl.pkl`, 11 features) runs **observation-only** in shadow telemetry (`ridge_pred_pnl`).
- **Selected Candidate (`15:00 UTC + E1`):**
  - `session_close_utc = 15`: blocks new entries at `>= 15:00 UTC` (never force-closes open positions).
  - `E1`: early-failure exit at `15` minutes if `MFE < 0.4 ATR`.

---

## 2. Architecture Overview

GoldEdge strictly follows **Ports & Adapters (Hexagonal Architecture)** (`GEMINI.md`):

- `src/goldedge/domain/`: Pure Python business logic, models, and services (`denoise_service.py`, `signal_generator.py`, `sizing_service.py`, `shadow_tracker.py`, `regime_detector.py`, `trade_scorer.py`, `mtf_trend.py`). No `pandas`, `sqlalchemy`, or HTTP imports.
- `src/goldedge/domain/ports/`: `typing.Protocol` interfaces (`IShadowJournal`, `ITickRepository`, `ICandleRepository`, `IBrokerAdapter`, `IEventBus`, `IExpectedPnlModel`).
- `src/goldedge/application/`: Use-case orchestrators (`RunBacktestUseCase`, `ScanMarketUseCase`, `ExecuteTradeUseCase`, `ManagePositionUseCase`).
- `src/goldedge/adapters/`: Infrastructure implementations (TimescaleDB/SQLAlchemy repositories, cTrader & MT5 adapters, `JsonlShadowJournal`, `ScanScheduler`, Flask REST routes).
- `src/goldedge/config/`: Dependency injection (`container.py`), `settings.py`, and immutable `risk_profiles.py`.

---

## 3. Core Engineering Features

1. **Execution-Price Risk Sizing (`sizing_service.py`, `run_backtest.py`, `scan_market.py`):**
   - `PositionSizingService.resolve_executable_entry(direction, bid, ask)` resolves `ask` (`BUY`) or `bid` (`SELL`) **before** calling `calculate_size(...)` (never sized against signal-bar close).
   - Supports optional `commission_per_lot`, rejects trades if `min_lot * loss_per_lot > max_monetary_risk`, and steps down cleanly so planned risk never exceeds `equity * risk_per_trade_pct / 100.0` (verified max `0.9996%` across 92 trades at `1.0%` budget).
2. **Dual Drawdown Reporting (`RunBacktestUseCase` + `routes/backtest.py`):**
   - Preserves closed-trade drawdown (`max_dd`, `closed_max_dd`, `closed_max_dd_pct` HWM-based).
   - Computes true tick-by-tick mark-to-market drawdown (`Equity_t = ClosedEquity_t + UnrealizedPnL_t` using executable `bid`/`ask` and consistent commissions): `mtm_max_dd`, `mtm_max_dd_pct` (HWM-based), `mtm_max_dd_initial_pct`, `raw_tick_mtm_max_dd`, `max_planned_risk_pct`, `max_realized_loss_pct`, `sl_slippage_events`, and `worst_sl_gap_usd`.
3. **Scalar Kalman Optimization (`denoise_service.py`):**
   - `VolumeAdaptiveKalmanFilter.update_tick` runs pure-Python scalar math (~31x speedup vs per-tick NumPy allocations, `< 1e-12` numerical difference).
4. **Three-Portfolio Independent Shadow Tracking (`shadow_tracker.py`, `scan_scheduler.py`, `jsonl_shadow_journal.py`):**
   - Tracks **3 independent virtual `$10,000` Candidate portfolios** (`< 15:00 UTC` + `E1 15m < 0.4 ATR`) under `summary(symbol)["portfolios"]` alongside raw `BUY`/`SELL` telemetry:
     - `CANDIDATE`: max 1 concurrent position (`BUY` or `SELL`).
     - `BUY_ONLY`: `BUY` signals only.
     - `SELL_ONLY`: `SELL` signals only.
   - Persists deduplicated `SHADOW_ROLLOVER`, `SHADOW_OPEN`, `SHADOW_EXIT`, and `SHADOW_PORTFOLIO_EXIT` events with lossless restart recovery and HWM risk-alert monitoring (`WARNING_DD_5PCT`, `HARD_STOP_8PCT`, `EMERGENCY_10PCT`, `DAILY_LOSS_1_5PCT`, `WEEKLY_LOSS_3PCT`).

---

## 4. Verified Historical Benchmarks (Frozen 95-Day Replay)

- **Window:** `2026-08-13 00:00:00Z` – `2026-11-16 20:01:23Z` (4-day warmup from `2026-08-09`; `18M+` ticks).
  - **Dev split:** `2026-08-13` – `2026-10-16` (`61,725` replay mins).
  - **Holdout split:** `2026-10-17` – `2026-11-16 20:01:23Z` (`28,835` replay mins).
  - **Frozen Baseline Artifacts (do not overwrite):** `backtest_results/frozen_111_baseline_trades.csv`, `backtest_results/frozen_111_baseline_journeys.json`, `backtest_results/frozen_manifest.json`.

| Run (95d Full, `$10,000` Start) | Trades (WR) | Net P&L | PF | Closed MaxDD | Tick-MTM MaxDD | Notes |
| :--- | :---: | ---: | ---: | ---: | ---: | :--- |
| **Frozen Baseline** (`17:00 UTC`, old close-sizing) | `111` (`59.5%`) | `+$1,303.25` | `1.31` | `-$1,094.73` (`9.08%` HWM) | `-$1,206.56` (`9.94%` HWM) | Unrecovered by Nov 16 |
| **Candidate (`15:00 + E1`, `1.0%` exec-sized)** | `92` (`58.7%`) | `+$2,185.62` | `1.73` | `-$684.63` (`5.46%` HWM) | `-$832.05` (`6.59%` HWM / `8.32%` init) | Max planned risk `0.9996%`; Dev `+$2,260.23` (`PF 2.68`), Holdout `-$74.61` (`PF 0.95`) |
| **Candidate (`0.5%` risk / `1.5%` daily limit)** | `92` (`58.7%`) | `+$1,002.86` | `1.76` | `-$300.84` (`2.69%` HWM) | `-$366.87` (`3.27%` HWM) | Stays below `5%` warning & `8%` hard stop |

### Directional & `SELL` Diagnostic Summary (Research Only)
- **`BUY` (`n=29`, `62%` WR):** `+$1,307.56` full 95d (exec-sized); remained profitable in Holdout (`PF 2.69` portfolio subset; `+$392.28`, `PF 2.68`, `4.01%` MTM DD in isolated `BUY`-only snapshot).
- **`SELL` (`n=63`, `57.1%` WR):** `+$878.05` (`PF 1.39`) overall, splitting between **Dev (`34` trades, `+$1,395.34`, `PF 2.60`)** and **Holdout (`29` trades, `-$517.29`, `PF 0.63`)**.
- **Holdout `SELL` weakness concentrations:** `09:00 UTC` (`0W/5L`, `-$517.60` vs `+$0.31` across all other hours), `TRENDING_BEAR` (`6W/9L`, `-$489.19`, `PF 0.43` vs `SIDEWAYS` `8W/6L`, `-$28.10`), and entries `<= 0.15 ATR` above 20-bar swing low (`3W/8L`, `-$352.56`, `PF 0.53`).
- **Overfitting caution:** `kalman_vel` at `SELL` entry inverted between Dev (`k_vel < -0.01`: `18W/7L`, `PF 4.50`) and Holdout (`k_vel > 0`: `8W/0L`, `+$468.76` vs `k_vel < -0.01`: `3W/10L`, `-$603.06`), so forward shadow validation is required before adding `SELL` filters.

---

## 5. Target Research Account Limits (`$10,000`, High-Water-Mark Based)

| Control | Limit |
| :--- | :--- |
| **Risk per trade** | `0.5%` (`$50`) `[strictly <= 1% during 1.0% benchmark runs]` |
| **Daily loss limit** | `1.5%` (`$150`) |
| **Weekly loss limit** | `3.0%` (`$300`) |
| **Warning drawdown** | `5.0%` (`$500` HWM) |
| **Hard drawdown stop** | `8.0%` (`$800` HWM) |
| **Emergency stop** | `10.0%` (`$1,000` HWM) |

---

## 6. Quickstart & Local Dev Environment

### Docker Dev Stack (`docker-compose.dev.yml`)
Starts `goldedge-db-dev` (TimescaleDB `5433 -> 5432`), `goldedge-redis-dev` (Redis 7 `6379`), `goldedge-data-feed-dev` (`ctrader_live`), `goldedge-backend-dev` (Gunicorn `5000`, bind-mounting `./src`, `./backtest_results`, `./scripts`, `./models`), and `frontend` (`3000`):

```powershell
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml restart backend
```

*(Base database services can also be started standalone via `docker-compose up -d redis timescaledb`, local package installed via `pip install -e .`, and tick daemon run via `python scripts/tick_collector_daemon.py`.)*

### Verification Tests
Run the shadow/sizing/MTM unit suite inside the backend container:

```powershell
docker cp tests/test_shadow_validation.py goldedge-backend-dev:/tmp/test_shadow_validation.py
docker exec -w /app -e PYTHONPATH=/app:/app/src:/tmp goldedge-backend-dev python3 -u /tmp/test_shadow_validation.py -v
```

### Database Backup & Restore (`scripts/`)
- **Binary TimescaleDB/PostgreSQL dump (`.dump`):**
  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts\backup_db.ps1 -Env dev
  powershell -ExecutionPolicy Bypass -File scripts\restore_db.ps1 -Env dev -BackupFile backups\goldedge_goldedge_<timestamp>.dump
  ```
- **All public tables to UTF-8 CSV archive (`.zip`):**
  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts\export_csv.ps1 -Env dev
  powershell -ExecutionPolicy Bypass -File scripts\import_csv.ps1 -Env dev -ZipFile backups\csv_goldedge_<timestamp>.zip
  ```
- Latest verified snapshots in `backups/`:
  - `backups/goldedge_goldedge_20261117_142954.dump` (`234.62 MB`)
  - `backups/csv_goldedge_20261117_143146.zip` (`169.46 MB`; `18,703,145` rows in `market_ticks`, `17,184` rows in `market_bars_1m`)
  - `backups/shadow_artifacts_20261117.zip` (`shadow_live_rollovers.jsonl` + frozen baseline CSV/JSON/manifest)
