-- Task 7B (Phases 2, 3, 5): QUOTE_ARRIVAL_SEMANTICS_V1
-- Strictly non-destructive: preserves original market_ticks and market_bars_1m untouched.
-- Separates nullable real_volume from valid quote tick_count and quote-arrival tick_rate,
-- enforces consistent (bid > 0 AND ask > 0 AND bid < ask) validity, and distinguishes
-- missing feed intervals / outages from genuine zero market activity.

-- 1. Corrected read view over raw market_ticks (never substitutes 1 for missing real_volume)
CREATE OR REPLACE VIEW v_market_ticks_quote_semantics_v1 AS
SELECT
    time,
    symbol,
    bid,
    ask,
    last AS price,
    volume AS real_volume,
    flags,
    (bid IS NOT NULL AND ask IS NOT NULL AND bid > 0 AND ask > 0 AND bid < ask) AS is_valid_quote,
    CASE
        WHEN bid IS NOT NULL AND ask IS NOT NULL AND bid > 0 AND ask > 0 AND bid < ask THEN 1
        ELSE 0
    END AS tick_count_unit
FROM market_ticks;

-- 2. Versioned corrected 1-minute dataset (separate from legacy market_bars_1m)
CREATE TABLE IF NOT EXISTS market_bars_1m_quote_semantics_v1 (
    time TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    tick_count BIGINT NOT NULL,
    tick_rate DOUBLE PRECISION NOT NULL,
    real_volume BIGINT,
    spread_points INTEGER NOT NULL,
    raw_quote_count BIGINT NOT NULL,
    invalid_quote_count BIGINT NOT NULL,
    is_feed_gap BOOLEAN NOT NULL DEFAULT FALSE,
    semantic_version VARCHAR(40) NOT NULL DEFAULT 'QUOTE_ARRIVAL_SEMANTICS_V1',
    PRIMARY KEY (symbol, time)
);

-- 3. Idempotent historical reconstruction over the Task 6 approved continuous research window
--    (2026-08-20 00:00:00Z warmup through 2026-11-16 20:01:23Z; excludes post-Nov-16 live outages).
INSERT INTO market_bars_1m_quote_semantics_v1 (
    time, symbol, open, high, low, close,
    tick_count, tick_rate, real_volume, spread_points,
    raw_quote_count, invalid_quote_count, is_feed_gap, semantic_version
)
SELECT
    time_bucket('1 minute', time) AS bucket_time,
    symbol,
    first(bid, time) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS open,
    max(bid)         FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS high,
    min(bid)         FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS low,
    last(bid, time)  FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS close,
    count(*)         FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) AS tick_count,
    (count(*)        FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask))::double precision AS tick_rate,
    CASE
        WHEN count(volume) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask AND volume IS NOT NULL) > 0
            THEN sum(volume) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask)
        ELSE NULL::bigint
    END AS real_volume,
    GREATEST(
        0,
        ROUND((
            (last(ask, time) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask))
            - (last(bid, time) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask))
        ) / 0.01)
    )::int AS spread_points,
    count(*) AS raw_quote_count,
    count(*) FILTER (WHERE NOT (bid > 0 AND ask > 0 AND bid < ask)) AS invalid_quote_count,
    FALSE AS is_feed_gap,
    'QUOTE_ARRIVAL_SEMANTICS_V1' AS semantic_version
FROM market_ticks
WHERE symbol = 'XAUUSD'
  AND time >= '2026-08-20T00:00:00Z'
  AND time <  '2026-11-16T20:02:00Z'
GROUP BY bucket_time, symbol
HAVING count(*) FILTER (WHERE bid > 0 AND ask > 0 AND bid < ask) > 0
ON CONFLICT (symbol, time) DO UPDATE SET
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    tick_count = EXCLUDED.tick_count,
    tick_rate = EXCLUDED.tick_rate,
    real_volume = EXCLUDED.real_volume,
    spread_points = EXCLUDED.spread_points,
    raw_quote_count = EXCLUDED.raw_quote_count,
    invalid_quote_count = EXCLUDED.invalid_quote_count,
    is_feed_gap = EXCLUDED.is_feed_gap,
    semantic_version = EXCLUDED.semantic_version;

-- 4. Timeline view distinguishing missing feed intervals / outages (is_feed_gap = TRUE, tick_rate = NULL)
--    from observed quote-arrival intervals.
CREATE OR REPLACE VIEW v_market_bars_1m_timeline_v1 AS
WITH minute_grid AS (
    SELECT generate_series(
        '2026-08-24T00:00:00Z'::timestamptz,
        '2026-11-16T20:01:00Z'::timestamptz,
        INTERVAL '1 minute'
    ) AS grid_time
)
SELECT
    g.grid_time AS time,
    COALESCE(b.symbol, 'XAUUSD') AS symbol,
    b.open,
    b.high,
    b.low,
    b.close,
    b.tick_count,
    b.tick_rate,
    b.real_volume,
    b.spread_points,
    COALESCE(b.raw_quote_count, 0) AS raw_quote_count,
    COALESCE(b.invalid_quote_count, 0) AS invalid_quote_count,
    (b.time IS NULL) AS is_feed_gap,
    'QUOTE_ARRIVAL_SEMANTICS_V1'::varchar(40) AS semantic_version
FROM minute_grid g
LEFT JOIN market_bars_1m_quote_semantics_v1 b
  ON b.symbol = 'XAUUSD' AND b.time = g.grid_time;
