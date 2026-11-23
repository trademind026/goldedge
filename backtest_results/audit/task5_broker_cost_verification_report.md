# Task 5 — Fusion Markets Actual Trading Cost Verification & Backtest Cost Calibration

**Outcome Tag:** `BROKER_COST_BASELINE_V1`  
**Final Verdict:** **`PARTIALLY VERIFIED`**

Live read-only queries against Fusion Markets' cTrader Open API (`demo.ctraderapi.com`, `ctidTraderAccountId = 48439552`) and reconciliation against all `15` actual executed historical deals (`7` closed round-trip positions) independently **VERIFIED** the contract specifications, swap parameters (`-5.653` / `+2.677` pips, Wed `3x`, `22:00 UTC`), spread distribution (`17,291,884` valid ticks), and **exact penny-accurate commission formula** (`$12.00` per `$1,000,000` notional per side = **`~$10.01/lot` RT** across the 118-trade backtest, `~$3.01/lot` higher than Task 4's `$7.00/lot` assumption).

Verdict is **`PARTIALLY VERIFIED`** (not `VERIFIED`) solely because three real-money items remain unavailable on the demo environment: (1) cTrader API exposes margining mode (`HEDGED`) rather than retail marketing tier (`Classic`/`Zero`) and live real-money accounts publicly quote `$2.25/$100k` (`$22.50/$1M` per side) vs demo `48439552`'s `$12/$1M` per side; (2) real-money execution & stop-loss slippage is **`UNVERIFIED`** (only `15` demo fills exist, showing `0.0p` median / `0.0533p` mean); and (3) `0` historical deals were held overnight across `21:00/22:00 UTC`, so swap charges are verified from live symbol metadata (`ProtoOASymbol`) rather than statement debits, and historical daily swap rate changes are unavailable.

---

## 1. Deliverables & Verification Artifacts
- Account & Symbol Metadata Snapshot (Redacted): [`task5_account_specifications.json`](file:///d:/gigs/goldedge/backtest_results/audit/task5_account_specifications.json)
- 15-Deal Broker Commission & Slippage Reconciliation: [`task5_commission_reconciliation.csv`](file:///d:/gigs/goldedge/backtest_results/audit/task5_commission_reconciliation.csv)
- 17.29M-Tick Spread Percentile Breakdown: [`task5_spread_analysis.csv`](file:///d:/gigs/goldedge/backtest_results/audit/task5_spread_analysis.csv)
- 118-Trade Rollover & Swap Audit: [`task5_swap_reconciliation.csv`](file:///d:/gigs/goldedge/backtest_results/audit/task5_swap_reconciliation.csv)
- Independent Scenario Comparison (`A`, `B`, `C0`, `C`, `D`): [`task5_cost_scenario_comparison.csv`](file:///d:/gigs/goldedge/backtest_results/audit/task5_cost_scenario_comparison.csv)
- 118-Trade Cost Ledger Across Scenarios: [`task5_trade_level_cost_ledger.csv`](file:///d:/gigs/goldedge/backtest_results/audit/task5_trade_level_cost_ledger.csv)
- Evidence Manifest: [`task5_evidence_manifest.json`](file:///d:/gigs/goldedge/backtest_results/audit/task5_evidence_manifest.json)
- Read-Only Probe & Independent Calibration Scripts: [`task5_ctrader_metadata_probe.py`](file:///d:/gigs/goldedge/scripts/audit/task5_ctrader_metadata_probe.py), [`task5_cost_calibration.py`](file:///d:/gigs/goldedge/scripts/audit/task5_cost_calibration.py)

---

## 2. Phase 1 & Phase 2 — Broker Account & XAUUSD Specification Comparison

Queried directly from `demo.ctraderapi.com` via `ProtoOATraderReq`, `ProtoOAAssetListReq`, `ProtoOASymbolsListReq`, `ProtoOASymbolByIdReq`, and `ProtoOAReconcileReq` with zero credentials printed/saved:

| Field | Fusion Markets cTrader API Evidence | Task 4 Frozen Baseline | Status / Discrepancy |
| :--- | :--- | :--- | :--- |
| **Broker / Platform** | `fusionmarkets` / `cTrader` (`symbolId = 41`) | `ctrader` | **VERIFIED** |
| **Account ID / Env** | `48439552` / `demo` (`balance = $9,759.42`, `0` open/pending) | `demo` / `SHADOW_MODE = True` | **VERIFIED** |
| **Account Type / Tier** | `ProtoOATrader.accountType = HEDGED`; retail tier name **`UNVERIFIED`** (`$12/$1M` raw-spread comm structure) | Assumed Zero-like fixed `$7/lot` | **`UNVERIFIED`** retail tier vs live public `$22.50/$1M` |
| **Currency / Leverage** | `USD` (`depositAssetId = 15`) / `1:500` (`leverageInCents = 50000`) | `USD` / est. `1:100` for display margin only | **VERIFIED** (backtest sizing uses `1%` equity risk, not leverage) |
| **Swap-Free Flag** | `swapFree = False` | Non-swap-free assumed | **VERIFIED** |
| **Contract Size / Min / Step / Max** | `100.0` oz (`lotSize = 10000`) / `0.01` / `0.01` / `100.0` lots | `100.0` / `0.01` / `0.01` / `100.0` (`max_lot=50.0` risk cap) | **MATCH** (`max_lot=50.0` tighter safely) |
| **Digits / Pip Size / Tick Value** | `digits = 2` (`$0.01`), `pipPosition = 1` (`$0.10`), `$1.00 / tick / lot` | `0.01`, `$0.10` pip, `$1.00 / tick / lot` | **MATCH** |
| **Min Stop Distance** | `slDistance = 0`, `tpDistance = 0` (`SYMBOL_DISTANCE_IN_POINTS`) | `0` enforced (`1.5 ATR` SL, `3.0 ATR` TP) | **MATCH** |
| **Trading Schedule & Daily Break** | `America/New_York` Sun `18:01` – Fri `16:59`, ~61m daily break (`16:59–18:00 NY`) | Empirical market ticks follow NY EDT/EST breaks (`20:59Z` -> `21:59Z` Nov 1) | **MATCH** |
| **Rollover Hour (DST Flag)** | `swapTime = 1320` min UTC (**`22:00 UTC`** winter EST; `17:00 NY` = `21:00 UTC` summer EDT) | Hardcoded `rollover_utc_hour = 21` | **DISCREPANCY** (1h off post-Nov 1 DST, **$0 impact** on 118 intraday trades) |
| **Commission Schedule** | **`USD_PER_MILLION_USD`, `$12.00 / $1M` per side (`$24 / $1M` RT)**, `$0` min comm | Fixed `$7.00 / lot` RT | **DISCREPANCY** (actual notional comm = **`$9.25–$11.11/lot` RT**, mean `$10.01`) |
| **Swap Rates & 3x Day** | `PIPS`: `Long = -5.653p` (`-$56.53/lot`), `Short = +2.677p` (`+$26.77/lot`), `WEDNESDAY` `3x` | `0.0` (`$0` swap) | **DISCREPANCY** in config if holding overnight; **`$0.00`** impact on 118 intraday trades |

---

## 3. Phase 3 — Authoritative Commission Reconciliation (`15 / 15` Deals = `$0.00` Diff)

Inspection of all `15` historical deals (`7` closed round-trip positions + `1` scale-in) on account `48439552` proves an exact, zero-minimum-commission notional fee:
$$\text{Commission}_{\text{side}} = -\operatorname{round}\left(\frac{\text{Lots} \times 100 \times \text{ExecutionPrice} \times 12.0}{1{,}000{,}000},\ 2\right)$$

| Sample Position / Deal | Lots | Exec Price | Notional Exposure | Expected Side Comm | Actual Charged Comm | RT Comm / Lot | Diff |
| :--- | :---: | ---: | ---: | ---: | ---: | ---: | :---: |
| `41150718` (`25251138`+`71` / `211`) | `2.00` (`1+1`) | `4599.14` / `4597.93` | `$919,586` (close) | `-$11.04`/side | `-$11.04` (`-$22.08` RT) | **`$11.04/lot`** | **`$0.00`** |
| `41355380` (`25481814` / `23`) | `1.00` | `4307.59` / `4307.79` | `$430,759` | `-$5.17`/side | `-$5.17` (`-$10.34` RT) | **`$10.34/lot`** | **`$0.00`** |
| `41355543` (`25482100` / `886`) | `0.07` | `4311.23` / `4326.23` | `$30,284` | `-$0.36`/side | `-$0.36` (`-$0.72` RT) | **`$10.29/lot`** | **`$0.00`** |
| `41387057` (`25542768` / `871`) | `1.00` | `4478.43` / `4478.91` | `$447,891` | `-$5.37`/side | `-$5.37` (`-$10.74` RT) | **`$10.74/lot`** | **`$0.00`** |
| `43053101` (`26064856` / `6241`) | `0.01` | `4278.70` / `4284.92` | `$4,285` | `-$0.05`/side | `-$0.05` (`-$0.10` RT) | **`$10.00/lot`** | **`$0.00`** |

- **All `15 / 15` deals (`max_side_comm_diff = $0.00`) and `7 / 7` closes (`max_rt_comm_diff = $0.00`) reconcile identically.**
- Charged separately on BOTH entry (`50%`) and exit (`50%`); no minimum commission (`0.01` lot charged exact `$0.05/side`).
- Across the `118` frozen backtest trades (`$3,832–$4,697/oz`), Task 4's `$7.00/lot` RT (`$125.30` total) underestimates actual account `48439552` notional commission (**`$9.25–$11.11/lot` RT**, mean **`$10.01/lot`**, **`$177.26` total**) by **`+$51.96`** (`+41.5%`). If a live Zero account applies Fusion's published `$2.25/$100k` (`$22.50/$1M` per side), actual commission would be `~1.875x` demo (`~$18.77/lot` RT).

---

## 4. Phase 4 — Spread Percentiles (`17,291,884` Ticks) & Execution Slippage Evidence

### Historical Valid Bid/Ask Spread (`2026-08-24` – `2026-11-16 20:01:23Z`, [`task5_spread_analysis.csv`](file:///d:/gigs/goldedge/backtest_results/audit/task5_spread_analysis.csv))

| Segment | Valid Ticks | Mean | Median (p50) | p75 | p90 | p95 | p99 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **OVERALL** | `17,291,884` | `$0.0735` (`0.73p`) | `$0.07` (`0.70p`) | `$0.09` | `$0.11` | **`$0.12` (`1.20p`)** | `$0.20` |
| Asia (`00-06 UTC`) | `5,173,117` | `$0.0709` | `$0.07` | `$0.09` | `$0.10` | `$0.11` | `$0.17` |
| London (`07-11 UTC`) | `3,537,949` | `$0.0724` | `$0.07` | `$0.09` | `$0.10` | `$0.11` | `$0.18` |
| Overlap (`12-16 UTC`) | `5,226,687` | `$0.0745` | `$0.07` | `$0.09` | `$0.11` | `$0.12` | `$0.20` |
| NY Late (`17-20 UTC`) | `2,638,693` | `$0.0764` | `$0.07` | `$0.10` | `$0.11` | `$0.13` | `$0.21` |
| Rollover Window (`21-23 UTC`) | `715,438` | `$0.0802` | `$0.07` | `$0.09` | `$0.10` | `$0.14` | `$0.30` |
| Rollover `+/-5m` (`20:55-23:05Z`) | `390,309` | **`$0.0931`** | `$0.07` | `$0.10` | `$0.20` | **`$0.20`** | **`$0.47`** |
| Top 5% High-Vol 1m (`>=p95` range) | `1,497,198` | **`$0.0987`** | `$0.09` | `$0.10` | `$0.12` | **`$0.15`** | **`$0.35`** |

### Execution Slippage Separation
- **Recorded Account Demo Market Fills (`15` deals vs preceding millisecond `market_ticks` quote):**
  - `14 / 15` fills (`0.01`, `0.07`, `1.00` lots) filled at exact BBO (`0.0` pips adverse slippage).
  - `1 / 15` fill (`25251211`, closing `2.00` lots SELL at `4597.93` vs `pre_bid = 4598.01`, 2ms earlier) had **`+0.8` pip (`$0.08/oz`)** adverse slippage -> **Demo median = `0.0p`, Demo mean = `0.0533p` (`$0.0053/oz`)**.
- **Separated Components Status:**
  - Market entry / exit slippage: `0.0533p` mean observed on **demo**; **Live real-money slippage = `UNVERIFIED`** (`0` live orders).
  - Stop-loss gap slippage: Measured from tick gaps beyond `pos.sl` (`104`–`109` events, `-$164` to `-$209` extra beyond trigger price); live real-account SL liquidity slippage = **`UNVERIFIED`**.
  - Limit take-profit execution: Modeled at `0.0p` slippage (`TARGET` limit fills at `tp`); live positive/negative limit fill improvement = **`UNVERIFIED`**.

---

## 5. Phase 5 — Swap & Rollover Verification ([`task5_swap_reconciliation.csv`](file:///d:/gigs/goldedge/backtest_results/audit/task5_swap_reconciliation.csv))

- **Authoritative Server Specs (`ProtoOASymbol`):** `swapCalculationType = PIPS`, `swapLong = -5.653 pips` (`-$56.53/lot/night`), `swapShort = +2.677 pips` (`+$26.77/lot/night`), `swapRollover3Days = WEDNESDAY` (`3x`: `-$169.59/lot` Long, `+$80.31/lot` Short), `swapTime = 1320` min UTC (`22:00 UTC` winter EST), `chargeSwapAtWeekends = False`.
- **Reconciliation Against Backtest & Account History:** **`0 / 118` backtest trades** and **`0 / 7` historical closed deals** remained open across either `21:00 UTC` (EDT) or `22:00 UTC` (EST). Therefore, exact realized swap = **`$0.00`**, matching Task 4 (`$0.00`). Daily dynamic swap changes across Aug–Nov 2026 are **`UNVERIFIED`** from static snapshot alone (`hypothetical_1night_swap` is tabulated per trade in the CSV for reference).

---

## 6. Phase 6 & Phase 7 — Independent Cost Recalculation (`118 / 118` Trades, `$0.000000` Error)

Completely independent Python/raw-SQL simulator (zero production execution/cost imports). No spread is double-counted: `gross_mid_pnl - total_spread_cost - commission - slippage - swap == net_pnl` holds to **`$0.000000`** on every trade across every scenario ([`task5_cost_scenario_comparison.csv`](file:///d:/gigs/goldedge/backtest_results/audit/task5_cost_scenario_comparison.csv)).

| Scenario | Cost Assumptions | Trades | W / L / BE | Win% | Gross Mid P&L | Spread Cost | Comm | Slip | Swap | **Net P&L** | PF | Closed MaxDD | Tick-MTM MaxDD |
| :--- | :--- | :---: | :---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **A: Zero Cost** | `$0` comm, `0p` slip, `$0` swap, actual bid/ask | `118` | `66 / 51 / 1` | `55.93%` | `+$589.46` | `$188.82` | `$0.00` | `$0.00` | `$0.00` | **`+$400.64`** | `1.0926` | `-$1,267.20` (`11.48%`) | `-$1,354.97` (`12.20%`) |
| **B: Task 4 Stress** | `$7/lot` RT comm, `0.3p/side` mkt slip, `$0` swap | `118` | `64 / 53 / 1` | `54.24%` | `+$540.66` | `$181.40` | `$125.30` | `$103.26` | `$0.00` | **`+$130.70`** | `1.0303` | `-$1,339.65` (`12.22%`) | `-$1,412.95` (`12.83%`) |
| **C0: Verified (0p Med)** | `$12/$1M/side` notional, `0.0p` demo med slip, `$0` swap | `118` | `64 / 54 / 0` | `54.24%` | `+$505.01` | `$183.88` | `$177.46` | `$0.00` | `$0.00` | **`+$143.67`** | `1.0334` | `-$1,312.65` (`12.00%`) | `-$1,385.44` (`12.60%`) |
| **C: Verified (0.053p Mean)** | `$12/$1M/side` notional, `0.0533p` demo mean slip, `$0` swap | `118` | `65 / 53 / 0` | `55.08%` | `+$495.01` | `$183.46` | `$177.26` | `$18.19` | `$0.00` | **`+$116.09`** | `1.0270` | `-$1,333.53` (`12.19%`) | `-$1,406.47` (`12.80%`) |
| **D: Conservative Stress** | `$12/$1M/side` + **p95 spread floor (`$0.12`)** + `0.3p/side` slip | `118` | `55 / 63 / 0` | `46.61%` | `+$511.51` | `$256.10` | `$176.27` | `$101.82` | `$0.00` | **`-$22.68`** | `0.9948` | `-$1,404.65` (`12.88%`) | `-$1,475.22` (`13.47%`) |

- **Independent Reconciliation:** Scenario `A` matches [`task4_zero_cost_ref.csv`](file:///d:/gigs/goldedge/backtest_results/audit/task4_zero_cost_ref.csv) (`0.000000` diff) and Scenario `B` matches [`task4_run_1.csv`](file:///d:/gigs/goldedge/backtest_results/audit/task4_run_1.csv) (`0.000000` diff) across all `118` trades.
- **Takeaway on Calibration:** Account `48439552` demo costs under Scenario `C` (`+$116.09`, `PF 1.0270`) sit slightly below Task 4's `B` (`+$130.70`, `PF 1.0303`) because higher verified commission (`$177.26` vs `$125.30`) outweighs lower demo mean slippage (`$18.19` vs `$103.26`), while realistic stress `D` (`p95` spread + `0.3p` live slippage) pushes net P&L slightly negative (`-$22.68`, `PF 0.9948`).
- **Safety Locks & Integrity:** `SHADOW_MODE = True`, `market_orders = 0`, `market_trades = 0`, and all Task 1–4 frozen artifact hashes (`frozen_111_baseline_trades.csv`, `task4_run_1.csv`, `task4_zero_cost_ref.csv`) remain unchanged.
