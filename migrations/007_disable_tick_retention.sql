-- Migration 007: Disable destructive 90-day retention drop on market_ticks
-- Preserves 7-day chunk compression while preventing silent deletion of historical backtest ticks.
SELECT remove_retention_policy('market_ticks', if_exists => TRUE);
