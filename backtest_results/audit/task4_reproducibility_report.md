# Task 4 — XAUUSD Deterministic Backtest Reproducibility & Baseline Freeze

**Final Verdict:** **`VERIFIED`**

All five isolated subprocess backtest runs (`$7/lot` RT commission + `0.3 pip` market slippage, `2026-08-24` to `2026-11-16`, `MEDIUM_BACKTEST`) produced **byte-identical CSV ledgers** (single unique SHA-256: `e51c891499e485a84d2f3acbdb225af241bf844545b68d9ce6f6b7549a438c36`), zero variance across all `118` trades (`0.0` spread across every price, quantity, SL/TP, exit reason, cost, and P&L field), identical aggregate metrics (`118` trades, `64W / 53L / 1BE`, `54.24%` WR, `PF 1.0303`, **`+$130.70`**, Closed MaxDD `-$1,339.65` / `12.22%`, Tick-MTM MaxDD **`-$1,412.95` / `12.83%`**), and **`0.000000` difference** across all `118 / 118` trades against the independent Task 3 raw-SQL/Python validator. The Task 3 win-rate discrepancy (`55.08%` vs `54.24%`) was traced to an IEEE-754 float artifact (`+8.039902077427996e-12` USD on Trade #70) and resolved via a uniform cent-precision outcome classification rule without altering trade P&L.

---

## 1. Deliverables & Preserved Frozen Artifacts

- **Manifest:** [`task4_baseline_manifest.json`](file:///d:/gigs/goldedge/backtest_results/audit/task4_baseline_manifest.json)
- **Data Fingerprint:** [`task4_data_fingerprint.json`](file:///d:/gigs/goldedge/backtest_results/audit/task4_data_fingerprint.json)
- **Five-Run Comparison CSV:** [`task4_run_comparison.csv`](file:///d:/gigs/goldedge/backtest_results/audit/task4_run_comparison.csv)
- **118-Trade Field-by-Field Comparison CSV:** [`task4_trade_ledger_comparison.csv`](file:///d:/gigs/goldedge/backtest_results/audit/task4_trade_ledger_comparison.csv)
- **Frozen Reference CSVs:** [`task4_run_1.csv`](file:///d:/gigs/goldedge/backtest_results/audit/task4_run_1.csv) (`$7/lot + 0.3p` stressed) and [`task4_zero_cost_ref.csv`](file:///d:/gigs/goldedge/backtest_results/audit/task4_zero_cost_ref.csv) (`$0/lot + 0.0p` gross)
- **Orchestrator & Isolated Runner Scripts:** [`task4_reproducibility.py`](file:///d:/gigs/goldedge/scripts/audit/task4_reproducibility.py), [`task4_single_run.py`](file:///d:/gigs/goldedge/scripts/audit/task4_single_run.py)
- **Preserved Prior Artifacts (Hashes Verified Unchanged):**
  - `frozen_111_baseline_trades.csv`: `73306e1529adc48284bce9ab6ac35122f33b9999d2464af441867eb3bb6cf168`
  - `post_remediation_baseline_trades.csv`: `738def12b26675df064958137f1f5b5ec33e9e0942646e9c0ebb7c8f39277541`
  - `task3_cost_reconciliation_118.csv`: `c41013161e674075c103437a0a8d0a6beba41dbbf1c2c89f4af676870c0cd293`

---

## 2. Phase 1 — Git State, Source Hashes, Config & Data Fingerprint

### Git State
- **HEAD Commit:** `1bf6c45b3f5b790e3212c9b5b7a683d6fd5e3622`
- **Working Tree:** Dirty (expected active Task 1–4 audit/dashboard/remediation changes; complete `git status --short` list persisted in [`task4_baseline_manifest.json`](file:///d:/gigs/goldedge/backtest_results/audit/task4_baseline_manifest.json)).

### Canonical Configuration Snapshot (`config_sha256 = e4a8e5c4e89ee7bad20192e3bf7d3d05991e01dc785008712b6d94fae7ba6709`)
- **Symbol & Spec:** `XAUUSD` (`contract_size = 100.0`, `tick_size = 0.01`, `tick_value = 1.0`, `min_volume = 0.01`, `max_volume = 100.0`, `volume_step = 0.01`)
- **Period & Warmup:** `2026-08-24T00:00:00+00:00` to `2026-11-16T20:01:23+00:00` (`4`-day warmup from `2026-08-20T00:00:00+00:00`)
- **Profile (`MEDIUM_BACKTEST`):** `initial_capital = 10000.0`, `risk_per_trade_pct = 1.0%`, `max_daily_loss_pct = 3.0%`, `max_trades_per_day = 8`, `sl_atr_multiplier = 1.5`, `tp_atr_multiplier = 3.0`, `min_score_threshold = 0.60`, `block_high_volatility = True`, `block_buy_sideways = True`, `use_kalman_exit = False`, `session_close_utc = None`
- **Stressed Cost Model:** `commission_per_lot = $7.00` RT, `slippage_pips = 0.3` per side on market fills (`$0.00` on limit TP), `swap_long = 0.0`, `swap_short = 0.0`, `rollover_utc_hour = 21`
- **Safety Locks:** `SHADOW_MODE = True`, `TRADING_MODE = "demo"`, `BROKER_TYPE = "ctrader"`, `market_orders = 0`, `market_trades = 0`

### Backtest Source File Hashes (`SHA-256`)

| File | SHA-256 |
| :--- | :--- |
| [`run_backtest.py`](file:///d:/gigs/goldedge/src/goldedge/application/use_cases/run_backtest.py) | `b9695a0e4a2796796126452bb5a1eaabd3ec9d5839732c13a09711cc32853a51` |
| [`denoise_service.py`](file:///d:/gigs/goldedge/src/goldedge/domain/services/denoise_service.py) | `a0cf9402110736dac09633edfed378981a47c507bc0cde3af3170ba6dea35d70` |
| [`indicator_service.py`](file:///d:/gigs/goldedge/src/goldedge/domain/services/indicator_service.py) | `4d3e507e1038909364ccfad63e8ef6a3044258880b22d64f7acb7b03c842e7c6` |
| [`regime_detector.py`](file:///d:/gigs/goldedge/src/goldedge/domain/services/regime_detector.py) | `ffa3546eff26c46034bd75fa7638fb26cca524e6f010aa79129bc1aa83cc4c40` |
| [`signal_generator.py`](file:///d:/gigs/goldedge/src/goldedge/domain/services/signal_generator.py) | `d789dab76681bb965239c00db852a7cce7a7aa3519d6d6637517c53623571bd4` |
| [`trade_scorer.py`](file:///d:/gigs/goldedge/src/goldedge/domain/services/trade_scorer.py) | `36c3d538cf2ab2bee8938dd0d32c33a3d6da4c14db75dfd062dcc8d0f428499d` |
| [`sizing_service.py`](file:///d:/gigs/goldedge/src/goldedge/domain/services/sizing_service.py) | `6ba518b635cc82fbfcc7753e4caa1ef4e2477565640f54f806c8bc79f0f38784` |
| [`mtf_trend.py`](file:///d:/gigs/goldedge/src/goldedge/domain/services/mtf_trend.py) | `b8db8c89bd0acbe275d673ebabf70b2776b2cc000affa230bac751a38060b4e2` |
| [`tick_repository.py`](file:///d:/gigs/goldedge/src/goldedge/adapters/secondary/db/tick_repository.py) | `f96cef4bd384c572115849056316b77a850185b465aa9594a5364bff663a662a` |
| [`backtest_repository.py`](file:///d:/gigs/goldedge/src/goldedge/adapters/secondary/db/backtest_repository.py) | `42a02bfb2b4c1dbaa6e1a903e9a5a0fe7deb7bb0929115b69f5590471daa66df` |
| [`risk_profiles.py`](file:///d:/gigs/goldedge/src/goldedge/config/risk_profiles.py) | `e94b9576c3817841b0282de48048a9cc5c8731c1bdee12fcd028b556368ec95a` |
| [`settings.py`](file:///d:/gigs/goldedge/src/goldedge/config/settings.py) | `da551fc5cfc8784a36ef825f9084b526cfe00220772181e1728eb9b441c16dc3` |
| [`backtestHelpers.ts`](file:///d:/gigs/goldedge/dashboard/lib/backtestHelpers.ts) | `f05adb99aa5b257a382f596be7a4b753ccb7d552d37674f6c7482808051405b8` |
| [`task4_single_run.py`](file:///d:/gigs/goldedge/scripts/audit/task4_single_run.py) | `c0702c376cc26bfe1fa20c4ba9867ea77b29e6af21174f5ef006b9022a7123f6` |

### Market Tick Data Fingerprint ([`task4_data_fingerprint.json`](file:///d:/gigs/goldedge/backtest_results/audit/task4_data_fingerprint.json))

| Window | UTC Range | Rows | Inverted / Zero-Spread | Min / Max Bid | Cryptographic Stream SHA-256 (`time\|bid\|ask\|vol`) |
| :--- | :--- | ---: | :---: | :---: | :--- |
| **Warmup (4d)** | `2026-08-20T00:00:00Z` .. `< 2026-08-24T00:00:00Z` | `563,212` | `203` / `9` | `4450.69` / `4632.19` | `ffbe8329235641536538dc736ee19a6eb11bb07557842ae626de4ba498cafd56` |
| **Replay (85d)** | `2026-08-24T00:00:00Z` .. `< 2026-11-16T20:01:23Z` | `17,303,717` | `11,409` / `424` | `3832.36` / `4696.78` | `b5ebbaeadbb99e8d2b8a1fdb7d357b8899dfb7385b4b8dc5a3d27b5d06ea2870` |

---

## 3. Phase 2 — Five Independent Subprocess Runs & Trade-by-Trade Comparison

Each of the five runs was launched in a brand-new OS child process ([`task4_single_run.py`](file:///d:/gigs/goldedge/scripts/audit/task4_single_run.py)) with fresh Python interpreter, SQLAlchemy engine, Kalman filter, indicator buffers, regime detector, virtual broker, and HWM state (`commission = $7.00/lot`, `slippage = 0.3p`).

| Run | Trades | W / L / BE | Win Rate | PF | Net P&L | Closed MaxDD | Tick-MTM MaxDD | Elapsed | Output CSV SHA-256 |
| :---: | :---: | :---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| **Run 1** | `118` | `64 / 53 / 1` | `54.24%` | `1.0303` | `+$130.70` | `-$1,339.65` (`12.22%`) | `-$1,412.95` (`12.83%`) | `222.91s` | `e51c891499e485a84d2f3acbdb225af241bf844545b68d9ce6f6b7549a438c36` |
| **Run 2** | `118` | `64 / 53 / 1` | `54.24%` | `1.0303` | `+$130.70` | `-$1,339.65` (`12.22%`) | `-$1,412.95` (`12.83%`) | `220.25s` | `e51c891499e485a84d2f3acbdb225af241bf844545b68d9ce6f6b7549a438c36` |
| **Run 3** | `118` | `64 / 53 / 1` | `54.24%` | `1.0303` | `+$130.70` | `-$1,339.65` (`12.22%`) | `-$1,412.95` (`12.83%`) | `221.73s` | `e51c891499e485a84d2f3acbdb225af241bf844545b68d9ce6f6b7549a438c36` |
| **Run 4** | `118` | `64 / 53 / 1` | `54.24%` | `1.0303` | `+$130.70` | `-$1,339.65` (`12.22%`) | `-$1,412.95` (`12.83%`) | `221.55s` | `e51c891499e485a84d2f3acbdb225af241bf844545b68d9ce6f6b7549a438c36` |
| **Run 5** | `118` | `64 / 53 / 1` | `54.24%` | `1.0303` | `+$130.70` | `-$1,339.65` (`12.22%`) | `-$1,412.95` (`12.83%`) | `222.53s` | `e51c891499e485a84d2f3acbdb225af241bf844545b68d9ce6f6b7549a438c36` |
| **Zero-Cost Ref** | `118` | `66 / 51 / 1` | `55.93%` | `1.0926` | `+$400.64` | `-$1,267.20` (`11.48%`) | `-$1,354.97` (`12.20%`) | `220.68s` | `8478d4a5d49f6e48faae2a0861f634a5f06d1109464d7544f764d80405dc86dd` |

- **Trade-by-Trade Ledger Comparison (`118 / 118` rows in [`task4_trade_ledger_comparison.csv`](file:///d:/gigs/goldedge/backtest_results/audit/task4_trade_ledger_comparison.csv)):**
  - `exact_match_all_5_runs = 118 / 118`, `string_fields_identical = 118 / 118`
  - Max spread across all 5 runs: `entry_price = 0.0`, `exit_price = 0.0`, `qty = 0.0`, `sl = 0.0`, `target = 0.0`, `gross_pnl = 0.0`, `commission_usd = 0.0`, `slippage_usd = 0.0`, `swap_usd = 0.0`, `net_pnl = 0.0`.
  - No Phase 3 nondeterminism existed in execution, SQL ordering, timestamps, or state isolation.

---

## 4. Phase 4 — Reporting Consistency & Resolution of Win-Rate Discrepancy (`55.08%` vs `54.24%`)

### Root Cause Found
Inspection of Trade `#70` (`2026-10-20 12:30:00Z`, `SELL`, `BREAKEVEN`, `0.13` lots, `commission = $0.91`) showed:
- `entry_price = 4117.610000000001` (IEEE-754 float64 from `raw_bid 4117.64 - 0.03`), `exit_price = 4117.54`
- Exact mathematical P&L: `(0.07 * 0.13 * 100) - 0.91 = 0.91 - 0.91 = $0.00`
- Raw IEEE-754 float64 evaluation: `gross_pnl = 0.9100000000080399`, **`pnl = +8.039902077427996e-12` (`+$0.000000000008`)**
- Prior `df["pnl"] > 0` classified `+8.04e-12` as Win `#65` (`65/118 = 55.08%`), while Task 3's rounded ledger (`round(net, 6) = 0.0`) classified `net > 0` as `False` (`64/118 = 54.24%`), and folded exact `0.0` into `losses = n - wins`.

### Unified Reporting Rule Applied (Zero Alteration of Trade P&L)
Standardized cent-precision outcome classification (`round(float(pnl), 2)`) identically across [`run_backtest.py`](file:///d:/gigs/goldedge/src/goldedge/application/use_cases/run_backtest.py), [`backtest_repository.py`](file:///d:/gigs/goldedge/src/goldedge/adapters/secondary/db/backtest_repository.py), and [`backtestHelpers.ts`](file:///d:/gigs/goldedge/dashboard/lib/backtestHelpers.ts):
- **`WIN`:** `round(pnl, 2) > 0.0` (`>= +$0.01`)
- **`LOSS`:** `round(pnl, 2) < 0.0` (`<= -$0.01`)
- **`BREAKEVEN`:** `round(pnl, 2) == 0.0` (`|pnl| < $0.005`)
- **`win_rate`:** `wins / total_trades` (`64 / 118 = 54.24%` stressed; `66 / 118 = 55.93%` zero-cost; `wins + losses + breakevens = 118`)
- Added regression unit test `test_cent_precision_classification_treats_float_epsilon_as_breakeven` in [`test_backtest_history.py`](file:///d:/gigs/goldedge/tests/test_backtest_history.py); all `16 / 16` unit tests pass (`OK`).

---

## 5. Phase 5 — Independent Raw-SQL/Python Reconciliation

Re-running [`_independent_replay`](file:///d:/gigs/goldedge/scripts/audit/task3_independent_validation.py) (zero shared production execution logic) against both frozen ledgers confirms:

| Frozen Baseline | Trades Reconciled | Max Entry Diff | Max Exit Diff | Max P&L Diff | Qty / Reason Mismatches | Independent Net P&L | Independent PF | Independent Tick-MTM DD |
| :--- | :---: | :---: | :---: | :---: | :---: | ---: | ---: | ---: |
| **Stressed (`$7/lot + 0.3p`)** | `118 / 118` | `0.0` | `0.0` | `$0.000000` | `0 / 0` | `+$130.70` | `1.0303` | `-$1,412.95` (`12.83%`) |
| **Zero-Cost (`$0/lot + 0.0p`)** | `118 / 118` | `0.0` | `0.0` | `$0.000000` | `0 / 0` | `+$400.64` | `1.0926` | `-$1,354.97` (`12.20%`) |
