-- Migration 003: Configure TimescaleDB Retention & Compression for market_ticks
-- Retain raw ticks for 90 days, compress them after 7 days.
-- Leaves market_bars_1m and market_trades to be retained permanently.

-- Enable compression on the hypertable
ALTER TABLE market_ticks SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol'
);

-- Add policy to compress chunks older than 7 days
SELECT add_compression_policy('market_ticks', INTERVAL '7 days');

-- NOTE: Retention drop policy removed in Migration 007 to avoid deleting historical backtest ticks.
