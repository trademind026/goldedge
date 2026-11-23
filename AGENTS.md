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

---

## 6. Master Research & ML Roadmap
- **Completed:**
  - `G1 — Backtest Root-Cause Investigation & Accuracy Verification` (`VERIFIED`: H0 `68` trds / `+$112.74`, H3b `90` trds / `+$1,584.43` / `4.16%` tick-MTM DD).
  - `G2 — TradeScorer Root-Cause Analysis & Calibration` (`VERIFIED`: opt-in `scorer_mode="g2_calibrated"`, `0` discrepancies).
  - `G3.1 — Historical Data Integrity & ML Training Feasibility` (`VERIFIED`: Macro `CONDITIONALLY_READY`, Micro `CONDITIONALLY_READY`; Sunday reconnect tick quarantined; `843`-bar diff reconciled; Lee-Ready order-flow disabled & blocked; day-clustered `N_eff` verified).
  - `G3.2 — Macro/Micro Label Design, Cost Accuracy & Independent Verification` (`VERIFIED`: `96,862` ML-eligible 1m bars refreshed & reconciled `0` mismatches; verified Fusion Markets `$12/$1M/side` + `0.0533p/side` slippage cost model; `19,004` Macro & `664,806` Micro labels in Parquet; `0` causality violations; `0` clean-room discrepancies).
  - `G3.3 — Leakage-Proof ML Training Pipeline, Historical Expansion & Independent Verification` (`VERIFIED`; `HISTORY_EXPANSION_VERIFIED` — `+18,403,826` staged ticks `2026-05-13..2026-08-12` → `6.2` continuous months / `187,235` 1m bars / `36,833` Macro & `1,278,171` Micro rows; `MACRO_TRAINING_READY` for walk-forward research, `MICRO_TRAINING_READY`; all 3 adjacent split boundaries `Train->Val->Cal->Test` independently verified `0` mismatches; `13/13` adversarial tests `PASS`; `9/9` regression suites `PASS`).
  - `G3.4 — Regularized Logistic Regression & LightGBM Training, Feature Ablation & Walk-Forward Comparison` (`Engineering: VERIFIED`; `Macro Directional: NO_DEMONSTRATED_EDGE` — volatility/movement-magnitude `1 - P(FLAT)` signal confirmed `-0.038` LL vs `B1`, but conditional `UP vs DOWN` AUC = `0.500`; `Micro Timing: INCONCLUSIVE` — spread-driven `-0.0147` LL vs `B1`, conditional `ENTER_NOW vs WAIT_ADVERSE` AUC = `0.5065` / `+$0.0128/oz` Q5-vs-Q1; causal Kalman added no improvement; clean-room `0` mismatches; `13/13` adversarial tests `PASS`; `10/10` regression suites `PASS`; `CALIBRATION` folds preserved for `G3.5`).
  - `G3.5 — Probability Calibration, H3b Economic Utility & Statistical Robustness` (`Engineering: VERIFIED`; `Macro Economic Value: NO_INCREMENTAL_VALUE` — rejecting low `P_MOVE` significantly harmed H3b `-5.17R` [`13W/1L`, `q=0.0058`]; `Micro Execution Value: NO_INCREMENTAL_VALUE` — `ML_C1_CAL_WAIT10S` failed BH-FDR `q=0.187` and trailed unconditional waiting; `Meta-Labeling: META_LABEL_RESEARCH_NOT_FEASIBLE` — `215` H3b trades / `87` days / `N_eff=63.06` over `6.2m`; **`G3.6` NOT AUTHORIZED**; clean-room `0` mismatches; `14/14` adversarial `PASS`; `11/11` regression `PASS`).
  - `G3.5R — H3b Baseline/E1 Execution Discrepancy Reconciliation` (`Engineering: VERIFIED`; `Baseline Reconciliation: EXACTLY_RECONCILED` — `112/112` trades matched `RunBacktestUseCase` to `+0.000000 R` / `$0.0000` P&L / `0` exit-reason mismatches; root cause was `CASE_B` research-simulator ATR reconstruction from final ratcheted exit SL [`-14.10R` across `54` trades] + tick-vs-bar E1 + TP exit slippage, `0` defects in `RunBacktestUseCase`; `Delayed-Entry Research: INVALIDATED` — all `15/15` 2x2 factorial delayed-entry & fast-spike guard policies collapsed to `BH-FDR q = 1.0000`; clean-room `0` mismatches; `14/14` synthetic/adversarial `PASS`; `12/12` regression `PASS`).
  - `G3.6R — Actual-Application H3b Backtest Profitability Improvement` (`Engineering: VERIFIED`; `Application Reconciliation: EXACTLY_RECONCILED` — `564/564` trades [`215` H3b + `172` Change A + `177` Change C] matched clean-room auditor with `0` mismatches, including `75` skipped & `37` newly eligible Change C trades; `Profitability Improvement: EXPLORATORY_ONLY` — isolated default-OFF opt-in `Change A (max_kaufman_er=0.50)` raised 6.2m profit `+$263.64 -> +$596.17`, `PF 1.072 -> 1.224`, cut Tick-MTM DD `5.64% -> 3.20%` (`80%` coverage), and separate `Change C (min_score=0.68)` reached `+$512.53` / `PF 1.172` / `4.50%` DD (improving both DEV & LATER), both surviving `2.0x` cost stress, but paired daily `p = 0.36..0.45` / `BH-FDR q = 0.447` on `6.2m` research-exposed history; `13/13` regression `PASS`; frozen H0/H3b defaults unchanged).
- **In Progress (`G3.6R.1 — Extended 12-Month Historical Backfill & Frozen Strategy Validation`, Historical Only):**
  - **Scope & Frozen Configs:** Testing frozen `H0_ORIGINAL`, `H3B_FROZEN`, `H3B_CHANGE_A_ER50` (`max_kaufman_er=0.50`), and `H3B_CHANGE_C_SCORE68` (`min_score_threshold=0.68`) separately (zero retuning, zero combining) on previously unexamined `2025-05-01 .. 2026-04-30` (with 10d warmup `2025-04-21 .. 2025-04-30`).
  - **Completed So Far:**
    - Phase 0 pre-backfill manifest saved: `backtest_results/audit/task_g3_6r_1_extended_history/g3_6r_1_frozen_config_manifest.json` (`SHADOW_MODE = True`, `market_orders = 0`, `market_trades = 0`, existing May–Nov DB counts snapshotted).
    - Phase 1 availability probe saved: `backtest_results/audit/task_g3_6r_1_extended_history/g3_6r_1_source_availability_report.json` (`28/28` months `2024-01 .. 2026-04` confirmed available on Fusion Markets cTrader Demo `symbolId=41`, `0` errors).
    - Isolated table created: `market_ticks_extended_pre202605` (non-destructive; `market_ticks` and `market_ticks_staged_expansion` untouched).
    - Resumable 12-month backfill (`591` 12h chunks) checkpointed at `backtest_results/audit/task_g3_6r_1_extended_history/extended_backfill_checkpoint.json` (in progress, ~`88/591` chunks / `8M+` ticks staged).
  - **Exact Resume / Continuation Commands (from repo root in PowerShell):**
    1. **Resume backfill to `591/591`** (idempotent, skips completed chunks):
       - `docker exec --env-file .env -e DATABASE_URL=postgresql://postgres:password@timescaledb:5432/goldedge -w /app -e PYTHONPATH=/app:/app/src goldedge-backend-dev python3 -u scripts/audit/task_g3_6r_1_run_backfill_resumable.py`
    2. **Run Phase 2–4 (coverage/quality audit + all 4 frozen `RunBacktestUseCase` runs + monthly/quarterly/BUY-SELL/cost-stress/paired BH-FDR verdicts):**
       - `docker exec -e DATABASE_URL=postgresql://postgres:password@timescaledb:5432/goldedge -w /app -e PYTHONPATH=/app:/app/src goldedge-backend-dev python3 -u scripts/audit/task_g3_6r_1_audit_and_backtests.py`
    3. **Run Phase 6 (AST-verified zero-app-import clean-room trade-by-trade reconciliation across all 4 runs):**
       - `docker exec -e DATABASE_URL=postgresql://postgres:password@timescaledb:5432/goldedge -w /app -e PYTHONPATH=/app:/app/src goldedge-backend-dev python3 -u scripts/audit/task_g3_6r_1_phase_e_independent.py`
    4. **Run full regression suite (including G3.5R ATR/E1 & G3.6R tests) and publish `g3_6r_1_final_report.md`** with decision gate classifications (`SUPPORTED_ADDITIONAL_HISTORY` / `MIXED_OR_INCONCLUSIVE` / `FAILED_ADDITIONAL_HISTORY` / `INSUFFICIENT_DATA`).
- **Next:** Complete `G3.6R.1` 12-month out-of-sample validation (`G3.6` ML integration remains **NOT AUTHORIZED**; frozen `H3b` defaults remain unchanged).
- **Upcoming:** `G3.7 — Frozen Unseen Forward Shadow Validation` → `G4 — Kalman Time-Aware Research` → `G5 — Final Regression & Verification` → `G6 — Trading-Readiness Decision`
