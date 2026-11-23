# Task 8 Final Report — 8% High-Water-Mark (HWM) Hard Risk Kill-Switch

**Final Verdict:** `VERIFIED`
**Safety Status:** `SHADOW_MODE = True` | `TRADING_MODE = demo` | `market_orders = 0` | `market_trades = 0` | Frozen Task 4 (`SHA-256: 73306E15...F168`) & Task 6C `RECOVERED_FULL_HISTORY_BASELINE_V2` baselines preserved unchanged.

---

## 1. Root-Cause Findings (Phase 1 Investigation)

Before Task 8, GoldEdge had no persistent or enforceable equity HWM drawdown stop:

1. **Unwired HWM Parameter in Sizing:** `PositionSizingService.calculate_size(...)` defaulted `current_drawdown_pct = 0.0`, while backtest and scan callers never passed live HWM drawdown.
2. **Monitoring-Only Drawdown Trackers:** `RunBacktestUseCase` (`_MtmDrawdownTracker`) and `ShadowValidationTracker` (`_evaluate_hwm_risk_alerts` firing `HARD_STOP_8PCT` alerts) logged/reported drawdown after the fact without blocking entries or liquidating open exposure.
3. **Volatile In-Memory Halt Flag:** `ForexLiveExecutionEngine.halted` lived solely in RAM and reset silently on container restart or reconnect.
4. **Incomplete Broker Equity:** `CTraderBrokerAdapter.get_account_info()` returned `"equity": balance` without open-position unrealized P&L, swap, or pending commission adjustments, preventing intrabar MTM kill-switch detection.
5. **No Database Schema or Authorized Reset Audit:** No persistent PostgreSQL table existed for risk halt state, trigger metadata, emergency-exit confirmation, or operator reset authorization.
6. **Silent Order Failure Attribution:** `TradeRepository` lacked `order_exists_for_signal` and `save_rejected_order` guards assumed by `ExecuteTradeUseCase`, and `Position` instantiation passed an invalid `signal_id` kwarg in the live execution use-case.

---

## 2. Implemented Architecture & Components Changed

### Authoritative Policy (`HwmKillSwitchService`)
- **Formula:** `drawdown_pct = ((hwm_equity - current_equity) / hwm_equity) * 100.0` evaluated on account equity including unrealized P&L at executable quotes (`bid` for `BUY`, `ask` for `SELL`).
- **Threshold & Stickiness:** Hard halt triggers when `round(drawdown_pct, 10) >= 8.0`. Once halted, `hwm_equity` freezes, all new entries return `(False, reason)`, and subsequent equity recovery does **not** clear the halt.
- **Restart & DB Fail-Closed Safety:** Persisted atomically in PostgreSQL via `SELECT ... FOR UPDATE`; load or commit failures set fail-closed/degraded mode that blocks new entries rather than assuming an unhalted account.
- **Explicit Reset:** `reset_halt(...)` requires non-empty `authorized_by`, `reason` (>= 3 chars), and matching `HWM_RESET_SECRET` (when configured), and writes an immutable record to `risk_hwm_audit_log`.
- **Hexagonal Purity Verified:** `src/goldedge/domain/models/hwm_risk.py`, `src/goldedge/domain/ports/outbound/hwm_risk_repo.py`, and `src/goldedge/domain/services/hwm_kill_switch_service.py` use pure Python only (verified zero `pandas`, `sqlalchemy`, `requests`, `flask`, or `psycopg2` imports in those files).

### Files Created / Modified
| Layer | File | Summary of Changes |
|---|---|---|
| **DB Migration** | `migrations/009_hwm_kill_switch.sql` | Created `risk_hwm_state`, `risk_hwm_audit_log`, and scope/time index |
| **Domain** | `src/goldedge/domain/models/hwm_risk.py` | `HwmRiskState`, `HwmEvaluationResult`, `EmergencyExitRecord` dataclasses |
| **Domain** | `src/goldedge/domain/models/trade.py` | Added `ExitReason.HWM_KILL_SWITCH_8PCT` |
| **Domain Port** | `src/goldedge/domain/ports/outbound/hwm_risk_repo.py` | `IHwmRiskRepository` protocol (`load_state`, `save_state_atomic`, `append_audit`, `get_audit_history`) |
| **Domain Service** | `src/goldedge/domain/services/hwm_kill_switch_service.py` | Thread-safe (`RLock`) authoritative `HwmKillSwitchService`, `compute_unrealized_pnl`, `compute_account_equity` |
| **Application** | `src/goldedge/application/services/hwm_emergency_exit.py` | `HwmEmergencyLiquidator`: closes open positions at executable `bid`/`ask`, bounded retries, post-close verification for partial fills/rejections/disconnects (never marks closed prematurely) |
| **Application** | `src/goldedge/application/services/execution_context.py` | Added `account_equity`, `account_currency`, `unrealized_pnl` on `LiveExecutionContext` |
| **Application** | `src/goldedge/application/use_cases/execute_trade.py` | Enforced `hwm_kill_switch.can_open_new_position(...)` pre-validation & pre-order; fixed `Position` kwargs & `trade_repo` guards |
| **Application** | `src/goldedge/application/use_cases/manage_position.py` | Per-tick intrabar unrealized MTM check -> triggers halt & `HwmEmergencyLiquidator` |
| **Application** | `src/goldedge/application/use_cases/scan_market.py` | Wired authoritative `hwm_kill_switch` & `emergency_liquidator` on live path |
| **Application** | `src/goldedge/application/event_handlers/on_signal_generated.py` | Pre-dispatch HWM halt enforcement |
| **Application** | `src/goldedge/application/use_cases/run_backtest.py` | Added `max_hwm_drawdown_pct: float \| None = 8.0`, intrabar MTM breach & emergency close (`HWM_KILL_SWITCH_8PCT` with executable quotes + exit slippage), pre-sizing halt lock, and summary metrics |
| **Gold Engine** | `src/goldedge/gold/application/live_execution_engine.py` | Startup `restore_all()`, `on_tick` intrabar MTM halt + emergency liquidation, entry blocking, `kill_switch` persistence, `reset_hwm_kill_switch` |
| **Adapters (DB)** | `src/goldedge/adapters/secondary/db/hwm_risk_repository.py` & `__init__.py` | Transactional PostgreSQL persistence & audit repository |
| **Adapters (Broker)** | `src/goldedge/adapters/secondary/broker/ctrader_broker_adapter.py` | Live open-position unrealized P&L/swap/commission equity calculation & close execution result inspection |
| **Adapters (API/Sched)** | `src/goldedge/adapters/primary/rest_api/routes/live.py`, `routes/backtest.py`, `scheduler/scan_scheduler.py` | `GET /api/live/risk/hwm`, `POST /api/live/risk/reset`, startup restore, backtest HWM summary exposure |
| **Config / DI** | `src/goldedge/config/settings.py`, `src/goldedge/config/container.py` | `HWM_MAX_DRAWDOWN_PCT = 8.0`, `HWM_INITIAL_EQUITY = 10000.0`, `HWM_RESET_SECRET`; singleton `hwm_kill_switch` & liquidator wiring |
| **Tests & Audit** | `tests/test_task8_hwm_kill_switch.py`, `scripts/audit/task8_app_backtest_hwm.py`, `scripts/audit/task8_independent_validator.py` | 15 Task 8 test scenarios, Phase 6 app comparison, Phase 7 independent validator |

---

## 3. Automated Test Suite Results (Post-Final-Edit Verification)

Executed inside `goldedge-backend-dev` after backend container restart:

`Ran 51 tests in 5.249s — OK` (`0` failures, `0` errors, `0` skipped)

- **`test_task8_hwm_kill_switch` (`15` tests):**
  1. `test_01_exactly_7_99_pct_does_not_halt` — OK (`7.99%` stays open/unhalted)
  2. `test_02_exactly_8_00_pct_triggers_halt` — OK (`8.00%` exact and non-round HWM halts)
  3. `test_03_greater_than_8_pct_triggers_halt` — OK
  4. `test_04_intrabar_unrealized_loss_breach_live_and_backtest` — OK
  5. `test_05_equity_recovery_after_activation_keeps_halt_sticky` — OK
  6. `test_06_application_restart_while_halted_preserves_halt_in_postgres` — OK
  7. `test_07_database_persistence_failure_fails_closed` — OK (load failure fail-closed + degraded save recovery)
  8. `test_08_concurrent_entry_requests_serialize_and_block_after_halt` — OK (threaded barrier)
  9. `test_09_open_position_emergency_executable_quotes_and_audit` — OK
  10. `test_10_partial_and_rejected_exits_never_mark_closed_prematurely` — OK
  11. `test_11_duplicate_kill_switch_activation_preserves_original_trigger` — OK
  12. `test_12_explicit_authorized_reset_with_validation_and_audit` — OK
  13. `test_13_account_isolation_and_currency_conversion_guard` — OK
  14. `test_14_live_vs_backtest_risk_control_parity` — OK
  15. `test_15_container_singleton_and_flask_hwm_routes` — OK (`/api/live/risk/hwm` & `/api/live/risk/reset` + DI singletons)
- **Existing Regression Suites (`36` tests):** `test_remediation_b1_b6`, `test_shadow_validation`, `test_backtest_history`, `test_task7_remediation`, `test_task7b_quote_semantics`, `test_task6c_validation` — **all `36` OK**.

---

## 4. Phase 6 — Actual Application Backtest Results (`RunBacktestUseCase` vs `RECOVERED_FULL_HISTORY_BASELINE_V2`)

### Baseline Non-Regression Check (`max_hwm_drawdown_pct=None` on W3)
- **Expected SHA-256 (`task6c_app_w3_trades.csv`):** `2021b78ab7beb55542562acee82912de12370fd298eb9694f70fbcbba98fce14`
- **Actual SHA-256 (`max_hwm_drawdown_pct=None`):** `2021b78ab7beb55542562acee82912de12370fd298eb9694f70fbcbba98fce14` (**bit-exact**)
- **Journeys SHA-256:** `2d89a82dd08c2f40eea21731666db53f1a41d4d763ff9ef6fa296d1e215b7dd6` (**bit-exact**, `143` trades, `+$211.479` net P&L)

### Window-by-Window Comparison (`V2 No Kill-Switch` vs `Task 8 With 8% HWM Kill-Switch`)

| Window | Variant | Trades | Win Rate | Net P&L | PF | Closed MaxDD | Tick-MTM MaxDD | Prevented |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| **W1** (`Aug 24–Nov 16`) | Baseline V2 | 118 | 55.08% | +$116.54 | 1.0271 | -$1,333.08 (`12.19%`) | -$1,406.02 (`12.79%`) | 0 |
| **W1** (`Aug 24–Nov 16`) | **8% Kill-Switch** | **65** | **58.46%** | **+$109.26** | **1.0436** | **-$829.35 (`7.58%`)** | **-$882.68 (`8.03%`)** | **124** |
| **W2** (`Aug 13–Nov 16`) | Baseline V2 | 131 | 54.96% | +$151.69 | 1.0324 | -$1,336.28 (`12.17%`) | -$1,409.22 (`12.78%`) | 0 |
| **W2** (`Aug 13–Nov 16`) | **8% Kill-Switch** | **78** | **57.69%** | **+$146.64** | **1.0506** | **-$830.32 (`7.56%`)** | **-$883.65 (`8.01%`)** | **124** |
| **W3** (`Aug 17–Nov 18`) | Baseline V2 | 143 | 55.24% | +$211.48 | 1.0414 | -$1,336.28 (`12.17%`) | -$1,409.22 (`12.78%`) | 0 |
| **W3** (`Aug 17–Nov 18`) | **8% Kill-Switch** | **78** | **57.69%** | **+$146.64** | **1.0506** | **-$830.32 (`7.56%`)** | **-$883.65 (`8.01%`)** | **149** |

### Exact Activation & Emergency-Exit Record

| Metric | W1 (`Aug 24–Nov 16`) | W2 (`Aug 13–Nov 16`) | W3 (`Aug 17–Nov 18`) |
|---|---|---|---|
| **Trigger Timestamp (UTC)** | `2026-10-16T07:39:25.668000+00:00` | `2026-10-16T07:39:25.668000+00:00` | `2026-10-16T07:39:25.668000+00:00` |
| **HWM Before Activation** | `$10,991.9412` | `$11,030.2915` | `$11,030.2915` |
| **Trigger Equity (at bid)** | `$10,109.3525` | `$10,146.7455` | `$10,146.7455` |
| **Exact Trigger Drawdown %** | `8.0294%` | `8.0102%` | `8.0102%` |
| **Open Positions at Trigger** | `1` (`BUY` `0.18` lots @ `4187.57533`) | `1` (`BUY` `0.19` lots @ `4187.57533`) | `1` (`BUY` `0.19` lots @ `4187.57533`) |
| **Requested / Exec Price** | `$4184.54` / `$4184.53467` (`bid - 0.0533p`) | `$4184.54` / `$4184.53467` (`bid - 0.0533p`) | `$4184.54` / `$4184.53467` (`bid - 0.0533p`) |
| **Emergency Exit Status** | `ALL_CLOSED_CONFIRMED` (`HWM_KILL_SWITCH_8PCT`) | `ALL_CLOSED_CONFIRMED` (`HWM_KILL_SWITCH_8PCT`) | `ALL_CLOSED_CONFIRMED` (`HWM_KILL_SWITCH_8PCT`) |
| **Final Equity / Post-Slip DD %** | `$10,109.2566` (`8.0303%`) | `$10,146.6442` (`8.0111%`) | `$10,146.6442` (`8.0111%`) |

*Note:* The slight `0.01%–0.03%` overshoot above `8.00%` (`8.0102%` / `8.0294%` at trigger, `8.0111%` / `8.0303%` after exit slippage) reflects discrete tick-by-tick price movement across consecutive ticks plus realistic `0.0533` pip exit slippage rather than idealized zero-slippage fills.

---

## 5. Phase 7 — Independent Raw-Tick Verification

Ran `scripts/audit/task8_independent_validator.py` (`PYTHONPATH=/app/scripts/audit`, **zero `src/goldedge` imports**, direct `psycopg2` raw `market_ticks`):

| Window | App Trades | Ind Trades | Trade Mismatches | Trail-Step Mismatches | Max P&L Diff | Trigger Time / HWM / Eq / Exit Diffs | Prevented (App / Ind) |
|---|---:|---:|---:|---:|---:|---|---:|
| **W1** | 65 | 65 | **0** | **0** | **$0.000000** | Exact match (`0.0` diff) | `124 / 124` |
| **W2** | 78 | 78 | **0** | **0** | **$0.000000** | Exact match (`0.0` diff) | `124 / 124` |
| **W3** | 78 | 78 | **0** | **0** | **$0.000000** | Exact match (`0.0` diff) | `149 / 149` |

- **Raw SQL Restart Persistence Check:** Persisted triggered W3 state into `risk_hwm_state`, closed DB session, reconnected independently, verified `halted = True`, `trigger_hwm = $11,030.2915`, `trigger_equity = $10,146.7455`, `trigger_drawdown_pct = 8.0102%`, and `halt_timestamp = 2026-10-16T07:39:25.668000+00:00`, then cleaned up test row.

---

## 6. Live Container Verification & Remaining Limitations

- **Live Container Health Check:** Restarted `goldedge-backend-dev` and queried `GET /api/live/risk/hwm` (HTTP `200`, `account_scope = demo:mock_account_id:USD`, `hwm_equity = 10000.0`, `halted = false`, `INITIALIZED` audit entry present).
- **Remaining Operational Boundaries:**
  - Triggering depends on live quote updates (`CTraderBrokerAdapter` / `ManagePositionUseCase`); while network/broker connectivity is down, server-side broker SLs remain the last line of defense.
  - Pre-trigger `PositionSizingService` lot sizing in backtest continues using `current_drawdown_pct = 0.0` so Task 6C strategy sizing stays unaltered before an 8% breach.
  - Execution remains strictly locked in shadow mode (`SHADOW_MODE = True`).
