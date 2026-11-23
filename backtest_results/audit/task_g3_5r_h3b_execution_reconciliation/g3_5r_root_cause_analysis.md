# TASK G3.5R — Root-Cause Analysis: H3b Application Baseline vs. G3.5 Research Simulator Discrepancy

**Generated (UTC):** `2026-11-23T11:01:03Z`  
**Attribution:** **`CASE_B` — Confirmed Research Simulator & ATR Reconstruction Defects ONLY (`0` defects in `RunBacktestUseCase`)**

---

## 1. Exact Discrepancy Reproduced (Unrounded)

| Evaluation Path | Candidate Opportunities | Unique Days | Total Net `R` | Mean Net `R` | Cum-R MaxDD |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Frozen H3b Application (`RunBacktestUseCase`)** | `112` | `39` | `+0.772510 R` | `+0.006897 R` | `-10.9091 R` |
| **Original G3.5 Research Simulator (Before Fix)** | `112` | `39` | `-13.018770 R` | `-0.116239 R` | — |
| **Original Discrepancy (`Sim - App`)** | — | — | **`-13.791280 R`** | **`-0.123136 R`** | — |
| **Repaired Canonical Simulator (After Fix)** | `112` | `39` | **`+0.772510 R`** | **`+0.006897 R`** | **`-10.9091 R`** |
| **Repaired Discrepancy (`Sim - App`)** | — | — | **`+0.000000 R`** | **`+0.000000 R`** | **`0.0000 R`** |
| **Clean-Room Independent Auditor (Zero `goldedge` imports)** | `112` | `39` | **`+0.772480 R`** | **`+0.006897 R`** | **`-10.9091 R`** |

Both evaluations used the **exact same `112` opportunities** (`100%` identical UTC timestamps and BUY/SELL directions across `39` unique OOT days).

---

## 2. Exact Decomposition of All `112` Trades (`0` `UNKNOWN` Trades)

| Mismatch Class | Trades (`n`) | Original `Sum(Sim - App) R` | Root Cause |
| :--- | :---: | :---: | :--- |
| **`SIZING_MISMATCH`** *(Corrupted Initial ATR)* | `54` (`48.2%`) | **`-14.100440 R`** | `task_g3_5_phase_cdefg_utility.py` reconstructed `atr = abs(entry_price - sl) / 1.5` using `tr["theoretical_stop_price"]` (which in `RunBacktestUseCase` is the **final ratcheted exit SL**, not the initial `1.5x ATR` stop!). On `13` `BREAKEVEN` trades, `abs(entry - sl)` was `\$0.20` → bogus `atr = \$0.133` (~`25x` too small) → `PositionSizingService` sized ~`25x` too large (`comm = \$17.12` vs `\$0.84`!) and created a bogus `\$0.20` SL that immediately stopped out (`-16.91 R` across `13` `BREAKEVEN -> SL_HIT` trades alone). Also corrupted all `TRAILING_SL` and `TIME_SQUEEZE` trades. |
| **`E1_MISMATCH`** | `18` (`16.1%`) | `+0.134100 R` | Original simulator checked `E1` continuously on first tick `elapsed >= 900s`, whereas canonical `RunBacktestUseCase` checks `EARLY_FAIL_15M` at the **end of the 15th completed post-entry 1-minute bar** (`bars_held == 15`), after scanning all ticks in that bar for SL/TP/MFE. |
| **`COST_MISMATCH`** *(TP Exit Slippage)* | `10` (`8.9%`) | `+0.174780 R` | Original simulator subtracted exit slippage on limit `TP_HIT` fills, whereas canonical `RunBacktestUseCase` fills limit `TARGET` orders at exact `float(self.tp)` with zero exit slippage. |
| **`EXACT_MATCH`** (`SL -> SL_HIT`) | `30` (`26.8%`) | `+0.000280 R` | Trades where `SL` was never ratcheted had uncorrupted ATR and matched to `0.000009 R/trade`. |
| **TOTAL** | **`112`** | **`-13.791280 R`** | **100.000% explained (`0` unexplained trades).** |

---

## 3. Additional Secondary Defects Fixed in `calibration_utility.py`

1. **`TIME_SQUEEZE` formula & session window:** Replaced invented `1.10 - 0.70*frac` formula and `7 <= hour < 19` with canonical `RunBacktestUseCase._ratchet_trailing` (`sl + progress * (price - sl)` for `hold_pct >= 0.75` and `7 <= hour < 20` for `60m` max hold).
2. **Zero-spread quote guard & per-leg commission rounding:** Enforced `b >= a` (skipping zero-spread AND inverted quotes) and 2-decimal per-leg commission rounding (`round(fee, 2)`) matching `CommissionSchedule.calculate_side_commission`.
3. **Sub-second entry tick alignment:** Matched exact entry tick inside `[entry_ts, entry_ts + 1.0)` to eliminate whole-second `trade_list` truncation drift.

---

## 4. Downstream Impact on G3.5 Delayed-Entry Claims (`INVALIDATED`)

The corrupted-ATR bug (`atr = \$0.133` on `BREAKEVEN` trades → `0.5 * atr = \$0.066` invalidation threshold) caused the G3.5 Phase D `> 0.5 ATR` fast-spike guard to falsely invalidate `21–27` trades — disproportionately canceling the bogus `-1.5 R` to `-2.25 R` fake `SL_HIT` trades and turning them into `0.0 R` missed fills, **fabricating `+13.18 R` to `+21.81 R` of phantom delayed-entry gains**. Once repaired to `0.000000 R` canonical baseline reconciliation, **every single delayed-entry and fast-spike guard policy collapses to statistical noise (`BH-FDR q = 1.0000` across all `15/15` tested policies)**.
