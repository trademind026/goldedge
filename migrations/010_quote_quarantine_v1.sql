-- Task G3.1 Corrective Verification Pass: Non-Destructive Quote Quarantine (V1)
-- Preserves raw market_ticks untouched as an immutable audit log.
-- Explicitly quarantines off-session weekend reconnect snapshots (e.g., Sunday 2026-11-22 16:00:02.669772Z
-- which arrived hours before Fusion Markets' Sunday ~22:00-23:00 UTC XAUUSD session open because
-- LiveTickerService stamped a SpotEvent reconnect snapshot with local UTC wall clock).

CREATE TABLE IF NOT EXISTS market_ticks_quarantine (
    symbol VARCHAR(20) NOT NULL,
    time TIMESTAMPTZ NOT NULL,
    reason VARCHAR(120) NOT NULL,
    quarantined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, time)
);

-- Quarantine all Saturday UTC ticks and Sunday < 21:00 UTC off-session ticks idempotently
INSERT INTO market_ticks_quarantine (symbol, time, reason)
SELECT
    symbol,
    time,
    'OFF_SESSION_WEEKEND_QUOTE_BEFORE_SUNDAY_21_UTC'
FROM market_ticks
WHERE symbol = 'XAUUSD'
  AND (
      EXTRACT(DOW FROM time AT TIME ZONE 'UTC') = 6
      OR (
          EXTRACT(DOW FROM time AT TIME ZONE 'UTC') = 0
          AND EXTRACT(HOUR FROM time AT TIME ZONE 'UTC') < 21
      )
  )
ON CONFLICT (symbol, time) DO NOTHING;

-- Extend v_market_ticks_quote_semantics_v1 non-destructively (append columns at end to keep existing view consumers intact)
CREATE OR REPLACE VIEW v_market_ticks_quote_semantics_v1 AS
SELECT
    t.time,
    t.symbol,
    t.bid,
    t.ask,
    t.last AS price,
    t.volume AS real_volume,
    t.flags,
    (t.bid IS NOT NULL AND t.ask IS NOT NULL AND t.bid > 0 AND t.ask > 0 AND t.bid < t.ask) AS is_valid_quote,
    CASE
        WHEN t.bid IS NOT NULL AND t.ask IS NOT NULL AND t.bid > 0 AND t.ask > 0 AND t.bid < t.ask THEN 1
        ELSE 0
    END AS tick_count_unit,
    (
        q.time IS NOT NULL
        OR EXTRACT(DOW FROM t.time AT TIME ZONE 'UTC') = 6
        OR (
            EXTRACT(DOW FROM t.time AT TIME ZONE 'UTC') = 0
            AND EXTRACT(HOUR FROM t.time AT TIME ZONE 'UTC') < 21
        )
    ) AS is_quarantined,
    (
        t.bid IS NOT NULL AND t.ask IS NOT NULL AND t.bid > 0 AND t.ask > 0 AND t.bid < t.ask
        AND q.time IS NULL
        AND NOT (
            EXTRACT(DOW FROM t.time AT TIME ZONE 'UTC') = 6
            OR (
                EXTRACT(DOW FROM t.time AT TIME ZONE 'UTC') = 0
                AND EXTRACT(HOUR FROM t.time AT TIME ZONE 'UTC') < 21
            )
        )
    ) AS is_ml_eligible_quote
FROM market_ticks t
LEFT JOIN market_ticks_quarantine q
  ON q.symbol = t.symbol AND q.time = t.time;
