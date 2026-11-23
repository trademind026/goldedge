-- Task G3.3 Phase A3: Isolated Historical Expansion Staging Table
-- Strictly non-destructive: raw `market_ticks` (2026-08-13 .. 2026-11-22) and frozen H0/H3b
-- research datasets remain 100% untouched.

CREATE TABLE IF NOT EXISTS market_ticks_staged_expansion (
    time TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    bid DOUBLE PRECISION NOT NULL,
    ask DOUBLE PRECISION NOT NULL,
    last DOUBLE PRECISION,
    volume BIGINT,
    flags INTEGER,
    PRIMARY KEY (symbol, time)
);

CREATE INDEX IF NOT EXISTS idx_staged_expansion_time ON market_ticks_staged_expansion (time ASC);
