# GoldEdge AI Project Context (Operational State, Strategy & Benchmarks)

See `GEMINI.md` for mandatory **Strict Hexagonal Architecture** rules. This file captures verified project state, safety locks, engineering invariants, historical benchmarks, and dev workflows for future AI sessions.

---

## 1. Current Operational Status & Safety Locks
- **Asset & Spec:** `XAUUSD` Spot Gold CFD (`contract_size = 100.0`, `tick_size = 0.01`, `tick_value = 1.0`, `min_lot = 0.01`, `volume_step = 0.01`, `margin_required_per_lot = 1000.0`).
- **Execution Status (`src/goldedge/config/settings.py`):**
  - `SHADOW_MODE = True` (**strictly shadow-only — NEVER enable live/demo broker orders without explicit user approval**).
  - `TRADING_MODE = "demo"`, `BROKER_TYPE = "ctrader"`, `KALMAN_WARMUP_HOURS = 24`.
  - Shadow telemetry journal: `backtest_results/shadow_live_rollovers.jsonl` (restart-idempotent via `JsonlShadowJournal`).
- **Frozen Strategy Configurations (Do Not Overfit New Filters Onto Holdout):**
  - **Active Production/Shadow Signal Pipeline (`SignalGeneratorService`, `MEDIUM_LIVE` / `MEDIUM_BACKTEST`):**
    - Entry score `>= 0.60`, `SL = 1.5x ATR`, `TP = 3.0x ATR`, `block_high_volatility = True`, `block_buy_sideways = True`, H1 EMA20/EMA50 trend gate, cooldown `15` 1m bars per `(strategy, direction)`.
    - Exit mechanics (`run_backtest.py` / `ShadowValidationTracker`): Breakeven at `1.0 ATR` (`+2 pips = $0.20`), trailing tier 1 at `1.6 ATR` (`1.10 ATR` trail), tier 2 at `2.5 ATR` (`0.75 ATR` trail), `TIME_SQUEEZE` from `75%..100%` of `effective_max_hold` (`60m` in `07..19 UTC`, `90m` off-hours), `use_kalman_exit = False`.
    - Ridge model (`models/gold_ridge_pnl.pkl`, 11 features) is loaded in shadow telemetry (`ridge_pred_pnl`) and kept **observation-only**.
  - **Selected Candidate (`15:00 UTC + E1`):**
    - `session_close_utc = 15`: blocks new entries at `>= 15:00 UTC` (never force-closes open positions).
    - `E1`: early-failure exit at `15` minutes if `MFE < 0.4 ATR`.
  - **Why Still Shadow-Only:** Holdout net expectancy remains negative (`-$74.61`, `PF 0.95`) due to October–November `SELL` trades (`-$517.29`), and `1.0%`-risk tick-level MTM drawdown (`-$832.05` / `8.32%` of `$10k`) exceeds the `$800` hard drawdown stop.

---

## 2. Implemented Engineering Invariants
1. **Execution-Price Risk Sizing (Priority 1):**
   - `PositionSizingService.resolve_executable_entry(direction, bid, ask)` returns `ask` (`BUY`) or `bid` (`SELL`).
   - Both `RunBacktestUseCase` (`src/goldedge/application/use_cases/run_backtest.py`) and `ScanMarketUseCase` (`src/goldedge/application/use_cases/scan_market.py`) resolve executable entry **before** calling `sizing_service.calculate_size(...)` (never size against 5m bar close).
   - `calculate_size(...)` in `src/goldedge/domain/services/sizing_service.py` includes optional `commission_per_lot`, rejects when `min_lot * loss_per_lot > max_monetary_risk`, and steps down cleanly so `planned_risk <= equity * risk_per_trade_pct / 100.0` (verified `0/92` trades over `1.0%`; max planned `0.9996%`).
2. **Dual Drawdown Reporting — Closed + Tick-MTM (Priority 2):**
   - `RunBacktestUseCase` (`_MtmDrawdownTracker`) preserves closed-trade `max_dd` (`closed_max_dd`, `closed_max_dd_pct`) **and** computes tick-by-tick mark-to-market drawdown (`Equity_t = ClosedEquity_t + UnrealizedPnL_t` using executable `bid`/`ask` and consistent commissions): `mtm_max_dd`, `mtm_max_dd_pct` (HWM-based), `mtm_max_dd_initial_pct`, `raw_tick_mtm_max_dd`, `max_planned_risk_pct`, `max_realized_loss_pct`, `sl_slippage_events`, and `worst_sl_gap_usd`.
   - Exposed via `src/goldedge/adapters/primary/rest_api/routes/backtest.py`.
3. **Scalar Kalman Optimization:**
   - `VolumeAdaptiveKalmanFilter.update_tick` (`src/goldedge/domain/services/denoise_service.py`) uses pure scalar math (`< 1e-12` diff vs NumPy reference, verified in `tests/test_shadow_validation.py`).
4. **Three-Portfolio Independent Shadow Tracking (Priority 4):**
   - `ShadowValidationTracker` (`src/goldedge/domain/services/shadow_tracker.py`) preserves backward-compatible `BUY`/`SELL` telemetry (`SHADOW_OPEN`, `SHADOW_EXIT`) and tracks **3 independent virtual `$10,000` Candidate portfolios** (`< 15:00 UTC` + `E1 15m < 0.4 ATR`) under `summary(symbol)["portfolios"]`:
     - `CANDIDATE`: 1 concurrent position (`BUY` or `SELL`), baseline Candidate comparison.
     - `BUY_ONLY`: `BUY` signals only.
     - `SELL_ONLY`: `SELL` signals only.
   - Closed portfolio exits persist via idempotent `SHADOW_PORTFOLIO_EXIT` events in `JsonlShadowJournal` (`src/goldedge/adapters/secondary/persistence/jsonl_shadow_journal.py`) and replay cleanly on restart in `ScanScheduler` (`src/goldedge/adapters/primary/scheduler/scan_scheduler.py`).
   - High-Water-Mark risk monitoring flags (`_evaluate_hwm_risk_alerts` in `shadow_tracker.py`, alert-only in shadow): `WARNING_DD_5PCT` (`5%`), `HARD_STOP_8PCT` (`8%`), `EMERGENCY_10PCT` (`10%`), `DAILY_LOSS_1_5PCT` (`1.5%`), `WEEKLY_LOSS_3PCT` (`3%`).

---

## 3. Verified Historical Benchmarks (Frozen 95-Day Snapshot)
- **Dataset:** `2026-08-13 00:00:00Z` – `2026-11-16 20:01:23Z` (with 4d warmup from `2026-08-09`).
  - **Dev split:** `2026-08-13` – `2026-10-16` (`61,725` replay mins).
  - **Holdout split:** `2026-10-17` – `2026-11-16 20:01:23Z` (`28,835` replay mins).
  - **Frozen baseline artifacts (do not overwrite):** `backtest_results/frozen_111_baseline_trades.csv`, `backtest_results/frozen_111_baseline_journeys.json`, `backtest_results/frozen_manifest.json`.

| Run (95d Full) | Trades (WR) | Net P&L | PF | Closed MaxDD | Tick-MTM MaxDD | Notes |
| :--- | :---: | ---: | ---: | ---: | ---: | :--- |
| **Frozen Baseline** (`17:00 UTC`, old close-sizing) | `111` (`59.5%`) | `+$1,303.25` | `1.31` | `-$1,094.73` (`10.95%` init / `9.08%` HWM) | `-$1,206.56` (`12.07%` init / `9.94%` HWM) | Unrecovered by Nov 16 |
| **Candidate (`15:00 + E1`, `1.0%` exec-sized)** | `92` (`58.7%`) | `+$2,185.62` | `1.73` | `-$684.63` (`5.46%` HWM) | `-$832.05` (`6.59%` HWM / `8.32%` init) | Max planned risk `0.9996%`; Dev `+$2,260.23` (`PF 2.68`), Holdout `-$74.61` (`PF 0.95`) |
| **Candidate (`0.5%` risk / `1.5%` daily limit)** | `92` (`58.7%`) | `+$1,002.86` | `1.76` | `-$300.84` (`2.69%` HWM) | `-$366.87` (`3.27%` HWM) | Stays `< 5%` Warning & `< 8%` Hard Stop |

- **Directional & `SELL` Research Findings (Candidate `1.0%` exec-sized — Research Only):**
  - `BUY` (`n=29`, `62%` WR): `+$1,307.56` full 95d (exec-sized); isolated `BUY`-only snapshot (`$10k` start) achieved `PF 2.85` (`4.01%` MTM DD) and Holdout `+$392.28` (`PF 2.68`).
  - `SELL` (`n=63`, `57.1%` WR): `+$878.05` (`PF 1.39`), split sharply between **Dev (`34` trds, `+$1,395.34`, `PF 2.60`)** and **Holdout (`29` trds, `-$517.29`, `PF 0.63`)**.
  - Holdout `SELL` loss concentrations: `09:00 UTC` (`0W/5L`, `-$517.60` vs `+$0.31` across all other hours), `TRENDING_BEAR` (`6W/9L`, `-$489.19`, `PF 0.43` vs `SIDEWAYS` `8W/6L`, `-$28.10`, `PF 0.95`), and `<= 0.15 ATR` above 20-bar swing low (`3W/8L`, `-$352.56`, `PF 0.53`).
  - **Overfitting warning:** `kalman_vel` at `SELL` entry **inverted** between Dev (`k_vel < -0.01`: `18W/7L`, `PF 4.50`) and Holdout (`k_vel > 0`: `8W/0L`, `+$468.76` vs `k_vel < -0.01`: `3W/10L`, `-$603.06`, `PF 0.32`). Wait for fresh shadow forward trades before changing `SELL` rules.

---

## 4. Target Research Account Limits (`$10,000`, HWM-Based)
- Risk per trade: `0.5%` (`$50`) [or strictly `<= 1%` during `1.0%` benchmark runs]
- Daily loss: `1.5%` (`$150`) | Weekly loss: `3.0%` (`$300`)
- Warning drawdown: `5.0%` (`$500` HWM) | Hard stop: `8.0%` (`$800` HWM) | Emergency stop: `10.0%` (`$1,000` HWM)

---

## 5. Dev Environment & Verification Commands
- **Docker Compose (`docker-compose.dev.yml`):**
  - `goldedge-db-dev` (TimescaleDB, host `5433 -> 5432`)
  - `goldedge-redis-dev` (Redis `7`, `6379`)
  - `goldedge-data-feed-dev` (`src.goldedge.adapters.primary.market_data.ctrader_live`)
  - `goldedge-backend-dev` (Gunicorn port `5000`; bind-mounts `./src`, `./backtest_results`, `./scripts`, `./models`)
  - `frontend` (port `3000`, proxies `/api/flask/*` to backend)
- **Live Shadow Status (`2026-11-17 08:50 UTC`):** `103` rollovers logged in `backtest_results/shadow_live_rollovers.jsonl` (`market_orders = 0`, `market_trades = 0`), with `CANDIDATE`, `BUY_ONLY`, `SELL_ONLY` tracking at `$10,000.00` (`OK`).
- **Quick Checks (from Windows/PowerShell, run `docker` commands with `BypassSandbox: true`; avoid nested double-quotes in `python -c`):**
  - Copy & run unit suite inside backend container:
    - `docker cp tests/test_shadow_validation.py goldedge-backend-dev:/tmp/test_shadow_validation.py`
    - `docker exec -w /app -e PYTHONPATH=/app:/app/src:/tmp goldedge-backend-dev python3 -u /tmp/test_shadow_validation.py -v`
  - Restart backend cleanly (preserves Kalman warmup + shadow restore):
    - `docker compose -f docker-compose.dev.yml restart backend`
