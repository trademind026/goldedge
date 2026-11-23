-- Task G3.2 Phase A1: Authoritative ML-Eligible 1-Minute Candle Refresh & Continuous Update Function
-- Strictly non-destructive: preserves raw market_ticks and legacy market_bars_1m untouched.
-- Filters invalid AND quarantined quotes via v_market_ticks_quote_semantics_v1 (is_ml_eligible_quote = TRUE).
-- Never forward-fills missing trading periods.

CREATE OR REPLACE FUNCTION refresh_market_bars_1m_quote_semantics_v1(
    p_symbol VARCHAR DEFAULT 'XAUUSD',
    p_start TIMESTAMPTZ DEFAULT NULL,
    p_end TIMESTAMPTZ DEFAULT NULL
) RETURNS BIGINT AS $$
DECLARE
    v_upserted BIGINT := 0;
BEGIN
    WITH agg AS (
        SELECT
            time_bucket('1 minute', time) AS bucket_time,
            symbol,
            first(bid, time) FILTER (WHERE is_ml_eligible_quote) AS open,
            max(bid)         FILTER (WHERE is_ml_eligible_quote) AS high,
            min(bid)         FILTER (WHERE is_ml_eligible_quote) AS low,
            last(bid, time)  FILTER (WHERE is_ml_eligible_quote) AS close,
            count(*)         FILTER (WHERE is_ml_eligible_quote) AS tick_count,
            (count(*)        FILTER (WHERE is_ml_eligible_quote))::double precision AS tick_rate,
            CASE
                WHEN count(real_volume) FILTER (WHERE is_ml_eligible_quote AND real_volume IS NOT NULL) > 0
                    THEN sum(real_volume) FILTER (WHERE is_ml_eligible_quote)
                ELSE NULL::bigint
            END AS real_volume,
            GREATEST(
                0,
                ROUND((
                    (last(ask, time) FILTER (WHERE is_ml_eligible_quote))
                    - (last(bid, time) FILTER (WHERE is_ml_eligible_quote))
                ) / 0.01)
            )::int AS spread_points,
            count(*) AS raw_quote_count,
            count(*) FILTER (WHERE NOT is_ml_eligible_quote) AS invalid_quote_count,
            FALSE AS is_feed_gap,
            'QUOTE_ARRIVAL_SEMANTICS_V1'::varchar(40) AS semantic_version
        FROM v_market_ticks_quote_semantics_v1
        WHERE symbol = p_symbol
          AND (p_start IS NULL OR time >= p_start)
          AND (p_end   IS NULL OR time <  p_end)
        GROUP BY bucket_time, symbol
        HAVING count(*) FILTER (WHERE is_ml_eligible_quote) > 0
    )
    INSERT INTO market_bars_1m_quote_semantics_v1 (
        time, symbol, open, high, low, close,
        tick_count, tick_rate, real_volume, spread_points,
        raw_quote_count, invalid_quote_count, is_feed_gap, semantic_version
    )
    SELECT
        bucket_time, symbol, open, high, low, close,
        tick_count, tick_rate, real_volume, spread_points,
        raw_quote_count, invalid_quote_count, is_feed_gap, semantic_version
    FROM agg
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

    GET DIAGNOSTICS v_upserted = ROW_COUNT;
    RETURN v_upserted;
END;
$$ LANGUAGE plpgsql;

-- Refresh full XAUUSD history so all 96,862 ML-eligible 1m bars are materialized
SELECT refresh_market_bars_1m_quote_semantics_v1('XAUUSD', NULL, NULL);

-- Update timeline view dynamically (no hardcoded end date, never forward-fills gaps)
CREATE OR REPLACE VIEW v_market_bars_1m_timeline_v1 AS
WITH bounds AS (
    SELECT min(time) AS min_t, max(time) AS max_t
    FROM market_bars_1m_quote_semantics_v1
    WHERE symbol = 'XAUUSD'
),
minute_grid AS (
    SELECT generate_series(b.min_t, b.max_t, INTERVAL '1 minute') AS grid_time
    FROM bounds b
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
