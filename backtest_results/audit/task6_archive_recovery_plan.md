# Task 6 — Archive Recovery Decision & Idempotent Import Plan

## 1. Recovery Decision Summary

**Archive Validated:** [`csv_goldedge_20261117_143146.zip`](file:///d:/gigs/goldedge/backups/csv_goldedge_20261117_143146.zip) (`zip_crc_test_result = OK`, SHA-256 `df275212fb54a91d89ed20b56edcad470e56847d6bb0bf633918da116a4e0e06`, member `market_ticks.csv` = `18,703,145` rows).

| Archived Period | Rows | Integrity & Completeness Status | Decision |
| :--- | ---: | :--- | :--- |
| **`2026-08-20` – `2026-11-16`** | `17,891,183` | `76 / 76` UTC days **bit-for-byte identical** to live TimescaleDB (`stream_sha256` match) | **NO ACTION REQUIRED** (already intact in DB; `0` conflicts) |
| **`2026-11-17`** | `68,863` (arch) vs `72,576` (DB) | Partial snapshot before live ticks appended | **DO NOT OVERWRITE DB** |
| **`2026-08-19` (Wed)** | **`281,902`** | **COMPLETE & VALID** (`00:00:00.112Z .. 23:59:58.861Z`, 1h break `20:59–22:00Z`, `0` invalid, `0` dupes, `1.287s` transition to DB `2026-08-20`) | **SAFE TO RECOVER** (optional idempotent insert via staging table) |
| **`2026-08-13` – `2026-08-18`** | `461,197` (`4` weekdays + Sun open) | **INCOMPLETE / TRUNCATED:** Missing `14.47h` (`Aug 13`), `6.25h` (`Aug 14`), `14.31h` (`Aug 17`), `11.86h` (`Aug 18`) after `00:17:39Z` | **DO NOT APPROVE FOR RESEARCH OR BLIND RESTORE** — requires fresh `CTraderMarketDataProvider.fetch_historical_ticks` re-download before any pre-Aug-19 backtesting |

> [!WARNING]
> **Never use `scripts/import_csv.ps1 -Truncate`** against the live database: truncating `market_ticks` would destroy `2026-11-17`–`2026-11-18` ticks collected after the archive was created (`141,918+` newer rows).

---

## 2. Deterministic, Idempotent, Reversible Recovery Procedure (For `2026-08-19` When Approved)

Because Task 6 requires validation before mutation, **no rows have been imported yet**. When you approve recovering `2026-08-19` (extending continuous uninterrupted history backward from `2026-08-20` to `2026-08-19`):

1. **Extract & Filter Only Verified Complete Range (`2026-08-19` Only):**
   - Stream `market_ticks.csv` from `backups/csv_goldedge_20261117_143146.zip` matching `symbol == 'XAUUSD'` and `time >= '2026-08-19T00:00:00+00' AND time < '2026-08-20T00:00:00+00'` into `/tmp/recover_20260819.csv` (expected **`281,902` rows**, `stream_sha256 = 2346616251f8db936502911ef0ecd0b9c9b693b2f98a0e7b30cf30c752fef7ec`).
2. **Stage & Idempotent Insert (Zero Overwrite of Existing Rows):**
   ```sql
   CREATE TEMP TABLE _stage_ticks (LIKE market_ticks INCLUDING DEFAULTS);
   \copy _stage_ticks FROM '/tmp/recover_20260819.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');
   INSERT INTO market_ticks (time, symbol, bid, ask, last, volume, flags)
   SELECT time, symbol, bid, ask, last, volume, flags FROM _stage_ticks
   ON CONFLICT (symbol, time) DO NOTHING;
   ```
3. **Post-Import Validation (Before Cleaning Stage):**
   - Assert `COUNT(*)` on `2026-08-19` == `281,902` (`281,810` valid, `90` inverted, `2` zero-spread).
   - Recompute `2026-08-19` `day_stream_sha256` against [`task6_daily_fingerprints.json`](file:///d:/gigs/goldedge/backtest_results/audit/task6_daily_fingerprints.json) and verify **Task 4 replay hash (`2026-08-24 .. 2026-11-16`) remains bit-for-byte unchanged (`b5ebba...`)**.
4. **Reversibility:**
   - Rollback command if needed: `DELETE FROM market_ticks WHERE symbol='XAUUSD' AND time >= '2026-08-19 00:00:00+00' AND time < '2026-08-20 00:00:00+00';` (touching zero rows `>= 2026-08-20`).
5. **Minimum Additional Data Required to Cover `2026-08-13` – `2026-08-18`:**
   - Run the remediated [`ctrader_provider.py`](file:///d:/gigs/goldedge/src/goldedge/adapters/secondary/market_data/ctrader_provider.py) fetcher (`2026-08-13 00:00Z` to `2026-08-19 00:00Z`) — **ONLY if cTrader demo server still retains tick history older than ~90 days** (`2026-08-13` is `97` days prior to `2026-11-18`, so broker historical depth must be tested read-only first). Note also `ctrader_provider.py` sets `volume = 1` vs live `NULL` — store `volume = NULL` so schema semantics match identically.
