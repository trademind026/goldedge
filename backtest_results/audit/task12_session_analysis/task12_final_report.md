# Task 12 — XAUUSD Session & Time-of-Day Performance Analysis Report

**Date:** 2026-11-19  
**Research Directory:** `backtest_results/audit/task12_session_analysis/`  
**Evaluated Configurations:** `H0_BASELINE_V2` (`score >= 0.60`, uncapped) & `H4a_SCORE_CAP_080` (`0.60 <= score < 0.80`) across `W1`, `W2`, and `W3`  
**Safety Status:** `SHADOW_MODE = True`, `TRADING_MODE = "demo"`, `market_orders = 0`, `market_trades = 0`, zero edits to `src/goldedge/`, zero edits to frozen Task 4 / V2 / Task 8 / Task 9 / Task 10 / Task 11 / Task 11A artifacts.  
**Verdict:** **VERIFIED (All 14 Deliverables Complete, 27/27 Candidate-Window Pairs Reconciled on Raw Ticks; Research Only — No Application Changes)**

---

## 1. Executive Summary

1. **Application Session Audit Finding:** GoldEdge's `SignalGeneratorService` (`07:00–17:00 UTC`), `TradeScorer._session_score` (`5%` weight), and `effective_max_hold` (`07:00–20:00 UTC`) all use **fixed UTC hours** rather than DST-aware session boundaries. Only `BrokerCostService` (NY 17:00 rollover) is DST-aware. Because both UK (`2026-10-25`) and US (`2026-11-01`) DST transitions occur inside `W1–W3`, fixed UTC hours drift across late-Asia, London open, and London/NY overlap boundaries.
2. **Where `H0` Fails vs. Where `H4a` Wins:**
   - Under `H0`, **`NY_POST_LONDON`** (`> 16:30` London local; primarily `UTC 16`) lost **`-$590.60` (`n=26, PF 0.491`)** and contributed `-$1,050.43` in gross losing dollars before `H0` breached the Task 8 `8%` HWM kill-switch on `2026-10-16 07:39:25 UTC`.
   - **`H4a_SCORE_CAP_080`** is **broadly profitable across time-of-day, not reliant on a single hour or session**: it earns **`+$663.64` even after removing its single best hour (`UTC 13`)** (vs `H0 -$59.61`), is profitable across **all 3 DST regimes** (`BOTH_DST +$708.22 PF 1.424`, `MISMATCH_GMT_EDT +$217.48 PF 1.663`, `BOTH_STD +$332.00 PF 1.291`), and never triggers the `8%` HWM kill-switch (`6.18%` max MTM DD).
3. **Session Filters vs. Score Cap:** No standalone session filter on `H0` matches `H4a` or prevents `H0` from hitting the `8%` HWM halt (except `Overlap Only`, which collapses trade count to `n=32`). On top of `H4a`, two smooth, cost-resilient filters (`S05_H4a_NO_UTC16` / `S06_H4a_CLOSE_15UTC` and `S17_H4a_NO_FIRST30M`) emerge as **`PROMISING_SHADOW_CANDIDATE`** for observation-only tracking, while **zero session hypotheses reach BH-FDR statistical significance (`min q_FDR = 1.0000`)**.

---

## 2. Baseline Verification

Verified in [task12_baseline_verification.json](file:///d:/gigs/goldedge/backtest_results/audit/task12_session_analysis/task12_baseline_verification.json):
- **Task 4 Frozen SHA-256 (`73306E15...`):** `match = true`
- **Task 6C V2 `W1` / `W2` / `W3` Trade Ledgers:** `v2_w1_match = true`, `v2_w2_match = true`, `v2_w3_match = true`
- **Task 8 HWM Config:** `HWM_MAX_DRAWDOWN_PCT = 8.0` verified intact.
- **Task 11A Reference Parity:** `39/39` pairs verified; `H0` (`W1=118 +$116.54`, `W2=131 +$151.69`, `W3=143 +$211.48`) and `H4a` (`W1=82 +$892.29`, `W2=91 +$929.38`, `W3=99 +$1,257.70`) reproduced identically.
- **Safety Locks:** `SHADOW_MODE = True`, `TRADING_MODE = "demo"`.

---

## 3. Session Calendar & Timezone Methodology

Full audit in [task12_session_logic_audit.json](file:///d:/gigs/goldedge/backtest_results/audit/task12_session_analysis/task12_session_logic_audit.json). All timestamps remain canonical **UTC** and are mapped strictly causally at entry time using independent IANA timezone rules:
- **Asian (`Asia/Tokyo`, no DST):** `09:00–18:00 JST` (`00:00–09:00 UTC`)
- **London (`Europe/London`):** `08:00–16:30` local (`07:00–15:30Z` BST until `2026-10-25 01:00Z`, then `08:00–16:30Z` GMT)
- **New York (`America/New_York`):** `08:00–17:00` local (`12:00–21:00Z` EDT until `2026-11-01 06:00Z`, then `13:00–22:00Z` EST)
- **London/NY Overlap:** Exact intersection (`12:00–15:30Z` BST/EDT; `12:00–16:30Z` GMT/EDT mismatch week Oct 25–31; `13:00–16:30Z` GMT/EST)
- **Holidays Flagged:** `2026-08-31` UK Summer Bank Holiday, `2026-09-07` US Labor Day, `2026-10-12` US Columbus Day, `2026-11-11` US Veterans Day (`H0 n=5 +$85.53`, `H4a n=3 +$218.55` — tiny sample, not a loss driver).

---

## 4. H0 Results (`W1`, `W2`, `W3`)

- `W1`: `118` trades, `55.08%` WR, `+$116.54`, `PF 1.0271`, `Closed DD -$1,333.08`, `Tick-MTM DD 12.79%`
- `W2`: `131` trades, `54.96%` WR, `+$151.69`, `PF 1.0324`, `Closed DD -$1,336.28`, `Tick-MTM DD 12.78%`
- `W3`: `143` trades, `55.24%` WR, `+$211.48`, `PF 1.0414`, `Exp +$1.48` (`95% CI [-$13.73, +$16.72]`), `Closed DD -$1,336.28`, `Tick-MTM DD 12.78%`
- **Primary Time-of-Day Failure:** `NY_POST_LONDON` (`n=26, -$590.60, PF 0.491`), `UTC 16` (`n=27, -$237.70, PF 0.778`), `UTC 09` (`n=16, -$140.13, PF 0.815`), and `Thursday` (`n=29, -$355.35, PF 0.784`).

---

## 5. H4a Results & Direct Comparison vs. H0

- `W1`: `82` trades, `62.20%` WR, `+$892.29`, `PF 1.3467`, `Tick-MTM DD 6.09%`
- `W2`: `91` trades, `61.54%` WR, `+$929.38`, `PF 1.3220`, `Tick-MTM DD 6.18%`
- `W3`: `99` trades, `61.62%` WR, `+$1,257.70`, `PF 1.4005`, `Exp +$12.70` (`95% CI [-$5.49, +$31.81]`), `Closed DD -$648.80`, `Tick-MTM DD 6.18%`
- **Hours Where H4a Improves Most (`W3`):** `UTC 11 (+$473.50, avoids 2W/4L = -$401.49)`, `UTC 13 (+$322.98)`, `UTC 12 (+$137.58)`, `UTC 14 (+$113.76)`, `UTC 10 (+$113.75)`, `UTC 08 (+$69.13)`, `UTC 16 (+$51.40, avoids 6W/12L = -$348.66)`.
- **Hours Where H4a Underperforms H0 (`W3`):** `UTC 09 (-$192.33 delta, H4a -$332.45 PF 0.455)` and `UTC 07 (-$38.26 delta, removes 4W/2L = +$132.01, though H4a PF rises 1.497 -> 1.552)`.
- **Session Profit Concentration:** `LONDON_PRE_OVERLAP` accounts for `67.4%` of `H4a`'s W3 profit (`+$848.28`, `n=63`, `PF 1.452`), `LONDON_NY_OVERLAP` adds `+$258.74` (`n=18`, `PF 1.339`), `ASIAN_ONLY` adds `+$128.78` (`n=2`), and `NY_POST_LONDON` is flat (`+$21.91`, `n=16`, `+$612.51` above `H0`).

---

## 6. BUY vs. SELL Session Analysis

Full table in [task12_buy_sell_session_analysis.csv](file:///d:/gigs/goldedge/backtest_results/audit/task12_session_analysis/task12_buy_sell_session_analysis.csv):
- **W3 `H0`:** `BUY` lost in **every active London/NY session** (`LONDON_PRE_OVERLAP -$382.82`, `LONDON_NY_OVERLAP -$92.93`, `NY_POST_LONDON -$93.38`). `SELL` profited in `LONDON_PRE_OVERLAP (+$698.33)` and `LONDON_NY_OVERLAP (+$365.03)` but collapsed in `NY_POST_LONDON (-$497.22, n=16)`.
- **W3 `H4a`:** `SELL` is strongly positive in `LONDON_PRE_OVERLAP (+$1,034.49, n=45)`, `LONDON_NY_OVERLAP (+$194.57, n=13)`, and `ASIAN_ONLY (+$128.78, n=2)`, and only mildly negative in `NY_POST_LONDON (-$99.39, n=9)`. `BUY` turns positive in `LONDON_NY_OVERLAP (+$64.17, n=5)` and `NY_POST_LONDON (+$121.30, n=7)`, with its remaining loss concentrated in `LONDON_PRE_OVERLAP (-$186.21, n=18, specifically UTC 07–08)`.

---

## 7. Hourly, Session & DST-Regime Performance (`W3`)

Full tables in [task12_hourly_performance.csv](file:///d:/gigs/goldedge/backtest_results/audit/task12_session_analysis/task12_hourly_performance.csv) and [task12_session_performance.json](file:///d:/gigs/goldedge/backtest_results/audit/task12_session_analysis/task12_session_performance.json):

| UTC Hour | `H0` (`n` / P&L / PF) | `H4a` (`n` / P&L / PF) | Delta (`H4a - H0`) |
| :---: | :---: | :---: | ---: |
| `07` | `21` / `+$213.88` / `1.497` | `18` / `+$175.62` / `1.552` | `-$38.26` |
| `08` | `18` / `+$43.51` / `1.072` | `11` / `+$112.64` / `1.306` | `+$69.13` |
| `09` | `16` / `-$140.13` / `0.815` | `11` / `-$332.45` / `0.455` | `-$192.33` |
| `10` | `14` / `+$108.14` / `1.377` | `10` / `+$221.90` / `2.553` | `+$113.75` |
| `11` | `16` / `+$14.98` / `1.020` | `11` / `+$488.49` / `2.488` | `+$473.50` |
| `12` | `15` / `+$48.99` / `1.069` | `10` / `+$186.57` / `1.424` | `+$137.58` |
| `13` | `9` / `+$271.09` / `1.900` | `10` / `+$594.07` / `3.666` | `+$322.98` |
| `14` | `1` / `-$113.76` / `0.000` | `0` / `$0.00` / `—` | `+$113.76` |
| `15` | `5` / `-$38.93` / `0.601` | `4` / `-$2.82` / `0.969` | `+$36.12` |
| `16` | `27` / `-$237.70` / `0.778` | `14` / `-$186.30` / `0.699` | `+$51.40` |
| `17` *(16:55 bar)* | `1` / `+$41.40` / `inf` | `0` / `$0.00` / `—` | `-$41.40` |

- **First 30m of London/NY Open:** `H0 n=24 (-$138.30)` vs non-first-30m `n=119 (+$349.78)`; `H4a n=19 (-$368.91)` vs non-first-30m `n=80 (+$1,626.61)`.
- **Weekdays (`H4a` W3):** `Mon +$64.82`, `Tue -$137.36`, `Wed +$220.36`, `Thu +$728.89`, `Fri +$380.99`.

---

## 8. Intraday Market Behavior (Quote-Based CFD Tick Data)

Full distributions in [task12_intraday_market_behavior.json](file:///d:/gigs/goldedge/backtest_results/audit/task12_session_analysis/task12_intraday_market_behavior.json) *(note: `quote_arrival_rate_per_min` measures CFD tick frequency, NOT exchange traded volume)*:
- **`LONDON_NY_OVERLAP` (`2,916` 5m bars):** Highest `mean_atr` (`$6.11`), highest `quote_arrival_rate` (`305.5 ticks/min`), highest `ADX_14` (`26.1`), and **`73.8%` `HIGH_VOLATILITY`** (peaking at `UTC 14`: `mean_atr $6.92`, `327.7 ticks/min`, **`92.0%` `HIGH_VOLATILITY`** — explaining why almost zero trades fire in `UTC 14`).
- **`LONDON_PRE_OVERLAP` (`4,020` 5m bars):** Balanced volatility (`mean_atr $4.04`, `191.1 ticks/min`, `mean_spread $0.072`), only `14.6%` `HIGH_VOLATILITY` and `36.8%` `SIDEWAYS` — generating the majority of clean `H4a` entries.
- **`NY_POST_LONDON` (`4,349` 5m bars):** Widest average spread (`$0.083`), fading quote arrival (`195.9 ticks/min`), and `43.7%` `LOW_VOLATILITY` — explaining why late-NY breakouts (`UTC 16`) suffer elevated failure rates after London liquidity exits.

---

## 9. Session Hypothesis Results (`W1`, `W2`, `W3`)

Full table in [task12_session_hypothesis_results.csv](file:///d:/gigs/goldedge/backtest_results/audit/task12_session_analysis/task12_session_hypothesis_results.csv):

| Hypothesis | Base | `W3 n` (Rem W/L) | `W3` Net P&L / PF / MTM DD | `W1` P&L / DD | `W2` P&L / DD | `W3` Ex-Best-Hr | `MW p` (`q_FDR`) | Classification |
| :--- | :---: | :---: | :---: | :---: | :---: | ---: | :---: | :---: |
| `H0_BASELINE_V2` | `H0` | `143` (`0/0`) | `+$211.48` / `1.041` / `12.78%` | `+$116.5` / `12.79%` | `+$151.7` / `12.78%` | `-$59.6` | `1.000` (`1.000`) | Control |
| **`H4a_SCORE_CAP_080`** | **`H4a`** | **`99` (`0/0`)** | **`+$1,257.70` / `1.401` / `6.18%`** | **`+$892.3` / `6.09%`** | **`+$929.4` / `6.18%`** | **`+$663.6`** | `1.000` (`1.000`) | **Primary Control** |
| `S05_H4a_NO_UTC16` | `H4a` | `85` (`6W/8L`) | `+$1,463.83` / `1.584` / `4.87%` | `+$1,086.1` / `4.89%` | `+$1,124.0` / `4.87%` | `+$869.8` | `0.775` (`1.000`) | **PROMISING_SHADOW_CANDIDATE** |
| `S06_H4a_CLOSE_15UTC` | `H4a` | `81` (`9W/9L`) | `+$1,479.16` / `1.611` / `5.29%` | `+$1,107.2` / `5.31%` | `+$1,139.3` / `5.29%` | `+$885.1` | `0.776` (`1.000`) | **PROMISING_SHADOW_CANDIDATE** |
| `S17_H4a_NO_FIRST30M` | `H4a` | `83` (`-19 dir, -1 casc, +4 new`) | `+$1,572.09` / `1.626` / `5.07%` | `+$869.2` / `5.12%` | `+$1,122.0` / `5.07%` | `+$1,058.1` | `0.713` (`1.000`) | **PROMISING_SHADOW_CANDIDATE** |
| `S04_H4a_NO_UTC09` | `H4a` | `89` (`-11 dir, -1 casc, +2 new`) | `+$1,713.25` / `1.672` / `4.71%` | `+$1,184.4` / `4.74%` | `+$1,358.1` / `4.71%` | `+$1,115.7` | `0.639` (`1.000`) | **EXPLORATORY** |
| `S10_H4a_BUY_NO_07_08` | `H4a` | `91` (`-8 dir, -1 casc, +1 new`) | `+$1,824.92` / `1.663` / `5.26%` | `+$1,179.9` / `4.52%` | `+$1,475.2` / `4.50%` | `+$1,205.7` | `0.839` (`1.000`) | **EXPLORATORY** |
| `S15_H4a_NO_TRANS_15M` | `H4a` | `72` (`-31 dir, -2 casc, +6 new`) | `+$1,475.06` / `1.801` / `5.25%` | `+$962.8` / `5.23%` | `+$912.3` / `5.25%` | `+$880.3` | `0.565` (`1.000`) | **REQUIRES_MORE_DATA** *(±20m cliff)* |
| `S17_H4a_NO_FIRST30M` | `H4a` | `83` (`-19 dir, -1 casc, +4 new`) | `+$1,572.09` / `1.626` / `5.07%` | `+$869.2` / `5.12%` | `+$1,122.0` / `5.07%` | `+$1,058.1` | `0.713` (`1.000`) | **PROMISING_SHADOW_CANDIDATE** |
| `S07_H0_NO_NY_POST_LDN` | `H0` | `117` (`11W/15L`) | `+$838.07` / `1.203` / `11.18%` | `+$722.8` / `11.11%` | `+$782.3` / `11.18%` | `+$456.2` | `0.933` (`1.000`) | **REJECTED** *(still halts 8% HWM)* |
| `S08_H4a_NO_NY_POST_LDN` | `H4a` | `83` (`9W/7L`) | `+$1,256.54` / `1.478` / `5.29%` | `+$886.3` / `5.31%` | `+$928.2` / `5.29%` | `+$662.5` | `0.932` (`1.000`) | **REQUIRES_MORE_DATA** |
| `S13_H4a_OVERLAP_ONLY` | `H4a` | `19` (`50W/31L`) | `+$243.59` / `1.340` / `3.40%` | `+$152.2` / `3.36%` | `+$152.2` / `3.36%` | `-$320.7` | `0.884` (`1.000`) | **REJECTED** *(tiny `n=19`)* |
| `S19/S20` (Spread cap) | `H0/H4a` | `143 / 99` (`0W/1L`) | `+$239.52` / `+$1,307.04` | — | — | — | `0.984` (`1.000`) | **REJECTED** *(1 trade)* |
| `S21..S23` (Weekdays) | `H0/H4a` | `78..114` | `+$562 / +$1,407 / +$1,214` | Mixed | Mixed | — | `0.711+` (`1.000`) | **REJECTED** *(data-mining risk)* |

### Exact Trade-Count & Cascade Decomposition (`S04`, `S10`, `S15`, `S17` on `W3`, `task12_trade_count_reconciliation.json`)
Every candidate satisfies the exact accounting identity `99 (H4a) - Directly Filtered - Cascade Prevented + Cascade Created = Executed`:

| Candidate | Identity (`Base - Direct - CascPrev + CascNew = Exec`) | Directly Filtered (`W/L`, P&L) | Cascade Prevented (`W/L`, P&L) | Cascade Created (`W/L`, P&L) |
| :--- | :--- | :---: | :---: | :---: |
| `S04_H4a_NO_UTC09` | `99 - 11 - 1 + 2 = 89` | `4W/7L` (`-$332.45`) | `1W/0L` (`+$1.33`) | `2W/0L` (`+$101.69`) |
| `S10_H4a_BUY_NO_07_08` | `99 - 8 - 1 + 1 = 91` | `2W/6L` (`-$221.18`) | `0W/1L` (`-$99.97`) | `1W/0L` (`+$192.18`) |
| `S15_H4a_NO_TRANS_15M` | `99 - 31 - 2 + 6 = 72` | `15W/16L` (`-$463.65`) | `1W/1L` (`-$19.04`) | `1W/5L` (`-$250.55`) |
| `S17_H4a_NO_FIRST30M` | `99 - 19 - 1 + 4 = 83` | `10W/9L` (`-$368.91`) | `1W/0L` (`+$93.51`) | `2W/2L` (`+$23.14`) |

*(Notes: (1) "Directly filtered" = base-`H4a` trades whose entry timestamp/direction fails the session rule. "Cascade" = trades removed or added only because an earlier trade changed position availability, the 15-bar cooldown, or compounded equity/lot size. (2) Net P&L differences vs `H4a` do not equal the simple sum of these buckets because every later trade is re-sized from a different compounded equity. (3) The earlier hypothesis-table "Rem W/L" counts mixed direct and cascade removals and have been replaced by the identity above. (4) `S10`'s gain is the least reliable: its `+$192.18` cascade-created winner and `-$99.97` cascade-prevented loser are not caused by the BUY 07–08 rule itself, reinforcing `EXPLORATORY`.)*

---

## 10. Statistical Validation & Parameter Sensitivity

Full evidence in [task12_statistical_validation.json](file:///d:/gigs/goldedge/backtest_results/audit/task12_session_analysis/task12_statistical_validation.json) and [task12_parameter_sensitivity.csv](file:///d:/gigs/goldedge/backtest_results/audit/task12_session_analysis/task12_parameter_sensitivity.csv):
- **BH-FDR Multiple-Testing Correction:** Across `34` tested configurations, **`0 / 34` achieve `q_FDR < 0.05`** (`min raw p = 0.4587`, `min q_FDR = 1.0000`). All `95%` bootstrap CIs on mean trade P&L cross zero due to modest sample size (`n = 66..143`).
- **Parameter Sensitivity (`H4a` on `W3`):**
  - **Session close cutoff (`14 / 15 / 16 / 17 UTC`):** `+$1,479.16 (5.29% DD)` / `+$1,479.16 (5.29% DD)` / `+$1,463.83 (4.87% DD)` / `+$1,257.70 (6.18% DD)` — **very smooth plateau**.
  - **First-N-minutes open lockout (`0 / 15 / 30 / 45 min`):** `+$1,257.70` -> `+$1,494.01 (5.50% DD)` -> `+$1,572.09 (5.07% DD)` -> `+$1,544.13 (4.70% DD)` — **smooth plateau**.
  - **Transition half-window (`0 / 10 / 15 / 20 min`):** `+$1,257.70` -> `+$1,372.20` -> `+$1,475.06` -> **`+$452.31 (PF 1.238, DD 7.06%, exBestHr -$32.58)`** — **severe sensitivity cliff at `±20m`**.
- **Cost Stress (`1.5×` slip + `$15/M` comm):** `H4a` holds `+$1,219.00 (PF 1.388, DD 6.18%)`; `S05` holds `+$1,421.99 (PF 1.564, DD 4.93%)`; `S17` holds `+$1,538.09 (PF 1.610, DD 5.11%)`.

---

## 11. Task 8 `8%` HWM Risk Analysis

Full metrics in [task12_hwm_comparison.json](file:///d:/gigs/goldedge/backtest_results/audit/task12_session_analysis/task12_hwm_comparison.json):
- **`H0_HWM8`:** Triggers the `8%` HWM kill-switch in **all 3 windows** at **`2026-10-16T07:39:25.668Z`** (`LONDON_PRE_OVERLAP`, `UTC 07`). In `W3`:
  - **`78` executed trades:** trades executed up to and including the trigger trade (closed via `HWM_KILL_SWITCH_8PCT`).
  - **`65` prevented trades:** exact counterfactual difference (`143` unprotected `H0` trades minus `78` executed = `65` trades `H0` would have executed post-halt).
  - **`149` prevented signals:** raw post-halt 5m bars passing score/regime/H1/daily/cooldown gates while halted. This exceeds `65` because the halted portfolio holds no open position (never suppressing overlapping 5m signals during a ~23m hold) and never triggers the 15-bar `(strategy, direction)` cooldown.
  - Peak equity `$11,030.29`, trigger equity `$10,146.75` (`8.01%` DD), final protected net P&L `+$146.64`. Largest pre-halt losing hours: **`UTC 16 (-$952.85)`** (`NY_POST_LONDON -$1,050.43`) and **`UTC 11 (-$735.14)`**.
- **`S07_H0_NO_NYPOST_HWM8`:** Removing `NY_POST_LONDON` delays `H0`'s halt by only 4 days (`2026-10-20T07:48:12.501Z`, `58` trades, `59` trades prevented, `+$506.11`).
- **`H4a_HWM8`, `S15_H4a_NOTRANS_HWM8`, `S17_H4a_NOF30_HWM8`:** **Never trigger** the `8%` HWM kill-switch across `W1`, `W2`, or `W3` (`0` trades prevented; max MTM DD `5.07%–6.18%`).

---

## 12. Independent Raw-Tick Reconciliation

Verified in [task12_independent_reconciliation.json](file:///d:/gigs/goldedge/backtest_results/audit/task12_session_analysis/task12_independent_reconciliation.json) across `9` retained configurations (`H0`, `H4a`, `S04`, `S05`, `S06`, `S07`, `S08`, `S15`, `S17`) × `3` windows (`W1`, `W2`, `W3`) = **`27` candidate-window pairs**:
- **`27 / 27` pairs `FULLY_RECONCILED`** (`0` unverified or mismatched).
- **`2,333` trades** and **`59,898` trailing-stop updates** verified with `0` missing/extra trades, `0` session-classification mismatches, `0` stop-update mismatches, `0` portfolio constraint violations, and `$0.000000` max P&L / equity-path / MTM-DD diff.

---

## 13. Limitations

1. **Low Statistical Power:** 95 days (`99` `H4a` trades) cannot confirm `10–20` trade hourly exclusions at `alpha = 0.05` after BH-FDR (`q = 1.000`).
2. **Macro Bear Market Bias:** `W3` experienced a `-14.59%` secular gold decline; session performance during a multi-month bull market remains untested.
3. **DST Sample Asymmetry:** Only 7 calendar days (`Oct 25–31`) fall in the UK-GMT / US-EDT mismatch week (`n=11` `H4a` trades).

---

## 14. Recommended Candidate(s)

1. **Primary Research Candidate (Unchanged from Tasks 10–11A):** **`H4a_SCORE_CAP_080` (`0.60 <= score < 0.80`)** — Session-resilient, DST-resilient, and never breaches `8%` HWM without needing any stacked time filter.
2. **Secondary Shadow-Telemetry Candidates (`PROMISING_SHADOW_CANDIDATE`, observation-only — do NOT stack into production yet):**
   - **`S05_H4a_NO_UTC16` (`< 16:00 UTC`) / `S06_H4a_CLOSE_15UTC` (`< 15:00 UTC`):** Smooth close-cutoff plateau, cuts late-NY chop, lowers `H4a` MTM DD to `4.87%–5.29%`.
   - **`S17_H4a_NO_FIRST30M` (block first 30m of DST-aware London/NY open):** Smooth plateau (`15–45m`), improves both `BUY` and `SELL`, lowers MTM DD to `5.07%`.
3. **Future Engineering Hygiene (Deferred to Implementation Phase Tasks 27–32):** Replace fixed UTC constants in `SignalGeneratorService` and `TradeScorer._session_score` with DST-aware `Europe/London` and `America/New_York` session helpers.

---

## 15. Final Verdict

**VERIFIED.** All 14 Task 12 deliverables are complete, frozen baselines and Task 8–11A artifacts are untouched, all 27 retained candidate-window pairs are independently reconciled on raw ticks with zero discrepancies, and `SHADOW_MODE = True` / `market_orders = 0` / `market_trades = 0` remain strictly enforced. Ready to proceed to **Task 13 — Entry Signal Quality Audit** upon user instruction.
