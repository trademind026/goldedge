# Task 6C — Independent Full-History Backtest Validation & Baseline Freeze Report

**Final Verdict:** **`VERIFIED`**
**Frozen Baseline Version:** `RECOVERED_FULL_HISTORY_BASELINE_V2`
**Safety Status:** `SHADOW_MODE = True` | `TRADING_MODE = "demo"` | `market_orders = 0` | `market_trades = 0`
**Semantics & Cost Schedule:** `QUOTE_ARRIVAL_SEMANTICS_V1` | Fusion Markets cTrader Demo `48439552` (`$12/M` per side notional, `0.0533` pips mean slippage, NY `17:00` DST rollover/swap)

---

## 1. Verdict Summary

| Audit Dimension | Verdict | Key Verification Evidence |
| :--- | :---: | :--- |
| **Dataset Integrity (Aug 13 – Nov 18, 2026)** | **PASS** | `19,774,744` total ticks (`19,762,699` valid quotes `bid > 0 & ask > 0 & bid < ask`), `0` duplicates, `0` non-null `real_volume`, `0` unexplained intraday gaps (`69` breaks >600s all weekend closes / daily NY rollover breaks & keepalives / Labor Day early close). Live backfill `--idempotency-check`: `28,398` fetched, `Inserted=0`. |
| **Task 4 Preservation** | **PASS** | Warmup (`563,212` rows, legacy SHA-256 `ffbe8329...`) and Replay (`17,303,717` rows, legacy SHA-256 `b5ebbaea...`) untouched; frozen Task 4 ledger hashes (`73306e15...`, `e51c8914...`) bit-identical. |
| **Actual Application Backtest (`RunBacktestUseCase`)** | **PASS** | W1=`118` (`+$116.54`), W2=`131` (`+$151.69`), W3=`143` (`+$211.48`); full-precision trade ledgers and per-bar trailing journeys archived per window. |
| **Independent Raw-Tick Reconciliation** | **PASS** | Zero `src/goldedge` imports (`psycopg2` direct SQL). **`0 / 392`** trade mismatches and **`0 / 392`** trailing-step journey mismatches across W1 (`118`), W2 (`131`), and W3 (`143`); max P&L diff = **`$0.000000`**. |
| **Unit, Lookahead & Execution Regression Suite** | **PASS** | **`36 / 36`** tests passed across `6` unittest modules (`0` failures, `0` errors, `0` skips), including non-vacuous future-tick perturbation lookahead invariance and Oct 30 → Nov 2 DST rollover tests. |
| **Baseline Freeze (`V2`)** | **PASS** | Frozen under `backtest_results/audit/frozen_v2_recovered/` with manifest `task6c_frozen_manifest.json` (`sha256 = 76e59a29...`). |

---

## 2. Dataset Integrity Audit (`2026-08-13` – `2026-11-18 18:09:38Z`)

Audited via `scripts/audit/task6c_dataset_audit.py` (`backtest_results/audit/task6c_dataset_integrity.json`):

- **Recovered Full Span (`2026-08-13T00:00:00.153Z` – `2026-11-18T18:09:38.022225Z`):**
  - Total rows: **`19,774,744`** across **`84`** UTC trading days
  - Strictly valid quotes (`bid > 0 AND ask > 0 AND bid < ask`): **`19,762,699`** (`99.939%`)
  - Filtered quotes: `11,612` inverted (`bid > ask`), `433` zero-spread (`bid == ask`), `0` non-positive/null
  - Non-null `real_volume` rows: **`0`** (`100% NULL`)
  - Duplicate timestamps `(symbol, time)`: **`0`**
- **Gap & Session-Break Classification (`backtest_results/audit/task6c_session_breaks_and_gaps.csv`):**
  - `69` gaps `> 600s` total: Friday-Sunday weekend closes, daily NY `17:00` rollover breaks (`20:59–22:00Z` EDT / `21:59–23:00Z` EST, including `7` stagnant `3856.19/3856.39` keepalive ticks inside Nov 16 rollover), and `2026-09-07` US Labor Day early close (`18:30–22:00Z`).
  - Unexplained intraday gaps (`> 600s`): **`0`**.
- **Backfill Idempotency Proof:**
  - `scripts/audit/backfill_missing_ticks.py --idempotency-check` fetched `28,398` valid ticks across 2 chunks (`nov17_gap1`) with **`Inserted=0`** via `ON CONFLICT (symbol, time) DO NOTHING`.

---

## 3. Window Performance, Risk & Directional Breakdown (All 3 Windows)

| Metric | W1: Original Task 4 (`Aug 24 – Nov 16`) | W2: Recovered Original (`Aug 13 – Nov 16`) | W3: Recovered Warmed (`Aug 17 – Nov 18`) |
| :--- | ---: | ---: | ---: |
| **Warmup Boundary** | `Aug 20 00:00Z – Aug 24 00:00Z` | Starts fresh `Aug 13 00:00Z` (`Aug 9–13` empty) | `Aug 13 00:00Z – Aug 17 00:00Z` |
| **Trades (Wins / Losses / BE)** | `118` (`65W / 53L / 0BE`) | `131` (`72W / 59L / 0BE`) | `143` (`79W / 64L / 0BE`) |
| **Win Rate** | `55.08%` | `54.96%` | `55.24%` |
| **Gross P&L** | `+$293.80` | `+$345.88` | `+$423.23` |
| **Commission (`$12/M` side)** | `$177.26` | `$194.19` | `$211.75` |
| **Slippage (`0.0533`p, reported)** | `$18.19` | `$19.79` | `$21.76` |
| **Overnight Swap** | `$0.00` | `$0.00` | `$0.00` |
| **Net P&L** | **`+$116.54`** | **`+$151.69`** | **`+$211.48`** |
| **Final Equity** | `$10,116.54` | `$10,151.69` | `$10,211.48` |
| **Profit Factor** | `1.0271` | `1.0324` | `1.0414` |
| **Closed-Equity Max DD (HWM)** | `-$1,333.08` (`12.19%`) | `-$1,336.28` (`12.17%`) | `-$1,336.28` (`12.17%`) |
| **Tick-MTM Max DD (HWM / Init)** | `-$1,406.02` (`12.79%` / `14.06%`) | `-$1,409.22` (`12.78%` / `14.09%`) | `-$1,409.22` (`12.78%` / `14.09%`) |
| **Max Consecutive Losses** | `7` | `7` | `7` |
| **Worst / Best Daily P&L** | `-$234.61` / `+$403.01` | `-$234.61` / `+$403.01` | `-$323.91` / `+$403.01` |
| **Avg / Max MAE (`USD` & `ATR`)** | `$59.02` (`0.903x`) / `$112.28` | `$58.61` (`0.899x`) / `$112.28` | `$58.36` (`0.897x`) / `$112.28` |
| **Avg / Max MFE (`USD` & `ATR`)** | `$80.44` (`1.238x`) / `$206.53` | `$79.43` (`1.221x`) / `$206.53` | `$79.56` (`1.225x`) / `$206.53` |
| **BUY Trades / Net P&L** | `29` (`14W`, `48.3%`) / `-$437.53` | `41` (`20W`, `48.8%`) / `-$450.50` | `41` (`20W`, `48.8%`) / `-$450.50` |
| **SELL Trades / Net P&L** | `89` (`51W`, `57.3%`) / `+$554.08` | `90` (`52W`, `57.8%`) / `+$602.19` | `102` (`59W`, `57.8%`) / `+$661.98` |

---

## 4. Independent Trade-by-Trade Reconciliation & Defects Identified

| Window | App Trades | Ind Trades | Trade Mismatches | Trailing Journey Mismatches | Max `|P&L diff|` | App vs Ind Final Equity | App vs Ind MTM Max DD |
| :--- | :---: | :---: | :---: | :---: | ---: | ---: | ---: |
| **W1** | `118` | `118` | **`0`** | **`0`** | `$0.000000` | `$10,116.5438` vs `$10,116.5438` | `-$1,406.0206` vs `-$1,406.0206` |
| **W2** | `131` | `131` | **`0`** | **`0`** | `$0.000000` | `$10,151.6914` vs `$10,151.6914` | `-$1,409.2233` vs `-$1,409.2233` |
| **W3** | `143` | `143` | **`0`** | **`0`** | `$0.000000` | `$10,211.4790` vs `$10,211.4790` | `-$1,409.2233` vs `-$1,409.2233` |

- Zero defects discovered in `RunBacktestUseCase` or domain services.
- Validation harness bugs fixed during reconciliation: scorer weighting (`w_ml=0.0, w_quant=1.0`), `TIME_SQUEEZE` ratchet base (`self.sl`), `exit_reason_flag`, pending exit commission in open-trade MTM, naive-UTC normalization in CSV comparison, dictionary key names (`slippage_usd`/`swap_usd`), legacy Task 4 hash verification alongside clean hash, and Nov 16 rollover keepalive gap classification.

---

## 5. BUY-Side Negative vs SELL-Side Positive Observations

1. **Asymmetric Distribution:** `SELL` represents `71.3%` of W3 trades (`102/143`, `+$661.98`, `57.8%` WR) vs `BUY` `28.7%` (`41/143`, `-$450.50`, `48.8%` WR) during XAUUSD's broader decline from ~`$4,675` (`Aug 24`) to ~`$3,890–$4,120` (`Oct–Nov`).
2. **Lower BUY MFE Follow-Through:** `BUY` avg MFE = `$70.91` vs `SELL` `$83.03` (avg MAE `$58.28` vs `$58.39`); only `2/41` (`4.9%`) `BUY` trades reached `TARGET` and `6/41` (`14.6%`) captured `TRAILING_SL`, while `24/41` (`58.5%`) closed via `SL` or `TIME_SQUEEZE`.
3. **Regime & Monthly Split:** `41/41` `BUY` trades fired exclusively in `TRENDING_BULL` (`2026-08` `14`/`-$119.26`, `2026-09` `13`/`-$170.11`, `2026-10` `4`/`-$305.59`, `2026-11` `10`/`+$144.45`), while `SELL` earned `+$190.30` (`2026-08`) and `+$823.11` (`2026-09`), dipped `-$446.55` (`2026-10`), and recovered `+$95.11` (`2026-11`).

---

## 6. Test Results, Hash Verification & Limitations

- **Tests (`6` modules):** `Ran 36 tests in 4.411s — OK` (`0` failures, `0` errors, `0` skips).
- **Task 4 Preserved:** `frozen_111_baseline_trades.csv` (`73306e15...`), `task4_run_1.csv` (`e51c8914...`), `task4_baseline_manifest.json` (`8f1a0e1a...`), warmup SHA-256 `ffbe8329...`, replay SHA-256 `b5ebbaea...`.
- **V2 Frozen Manifest:** `backtest_results/audit/frozen_v2_recovered/task6c_frozen_manifest.json` (`76e59a29...`).
- **Remaining Limitations:** Zero overnight swap events triggered (`swap_usd = $0.00`) due to intraday `60–90m` max hold; `real_volume = NULL` across cTrader spot CFD history; static Fusion demo fee snapshot with `SHADOW_MODE = True` lock.
