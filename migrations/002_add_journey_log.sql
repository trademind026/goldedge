-- Migration 002: Add journey_log JSONB column to market_trades
-- Run this if you already-- Phase 2 additions
-- Add journey log to track the ML score progression per trade

ALTER TABLE market_trades ADD COLUMN IF NOT EXISTS journey_log JSONB;

-- For fast lookup of trades by journey parameters
CREATE INDEX IF NOT EXISTS idx_market_trades_journey ON market_trades USING GIN (journey_log);
