-- Phase 1 Foundation Schema for GoldEdge (Multi-Asset)
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- 1. System Configuration
CREATE TABLE IF NOT EXISTS system_config (
    key VARCHAR(255) PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Market 1-Minute Bars
CREATE TABLE IF NOT EXISTS market_bars_1m (
    time TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    tick_volume BIGINT NOT NULL,
    real_volume BIGINT,
    spread INTEGER NOT NULL,
    PRIMARY KEY (symbol, time)
);

-- Convert to TimescaleDB hypertable
SELECT create_hypertable('market_bars_1m', 'time', if_not_exists => TRUE);

-- 3. Market Tick Data (High Frequency)
CREATE TABLE IF NOT EXISTS market_ticks (
    time TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    bid DOUBLE PRECISION NOT NULL,
    ask DOUBLE PRECISION NOT NULL,
    last DOUBLE PRECISION,
    volume BIGINT,
    flags INTEGER,
    PRIMARY KEY (symbol, time)
);

SELECT create_hypertable('market_ticks', 'time', if_not_exists => TRUE);

-- 4. Signals Journal
CREATE TABLE IF NOT EXISTS market_signals (
    id SERIAL PRIMARY KEY,
    time TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    direction VARCHAR(10) NOT NULL, -- BUY, SELL
    strategy VARCHAR(50) NOT NULL,
    score DOUBLE PRECISION,
    regime VARCHAR(20)
);

CREATE INDEX idx_market_signals_time ON market_signals(time);

-- 5. Orders Journal
CREATE TABLE IF NOT EXISTS market_orders (
    ticket BIGINT PRIMARY KEY,
    signal_id INTEGER REFERENCES market_signals(id),
    time_setup TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    type VARCHAR(10) NOT NULL, -- BUY, SELL
    volume DOUBLE PRECISION NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    sl DOUBLE PRECISION NOT NULL,
    tp DOUBLE PRECISION NOT NULL,
    status VARCHAR(20) NOT NULL -- PLACED, REJECTED, FILLED
);

CREATE INDEX idx_market_orders_time ON market_orders(time_setup);

-- 6. Trades Journal
CREATE TABLE IF NOT EXISTS market_trades (
    ticket BIGINT PRIMARY KEY REFERENCES market_orders(ticket),
    time_execution TIMESTAMPTZ NOT NULL,
    time_closing TIMESTAMPTZ,
    symbol VARCHAR(20) NOT NULL,
    type VARCHAR(10) NOT NULL,
    volume DOUBLE PRECISION NOT NULL,
    price_open DOUBLE PRECISION NOT NULL,
    price_sl DOUBLE PRECISION NOT NULL,
    price_tp DOUBLE PRECISION NOT NULL,
    price_close DOUBLE PRECISION,
    commission DOUBLE PRECISION DEFAULT 0.0,
    swap DOUBLE PRECISION DEFAULT 0.0,
    profit DOUBLE PRECISION,
    status VARCHAR(20) NOT NULL, -- OPEN, CLOSED, CANCELLED
    ml_score DOUBLE PRECISION,
    risk_profile VARCHAR(20),
    journey_log JSONB
);

CREATE INDEX idx_market_trades_time ON market_trades(time_execution);
CREATE INDEX idx_market_trades_status ON market_trades(status);

-- 7. ML Features (Aligned to 1M Bars)
CREATE TABLE IF NOT EXISTS market_features (
    time TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    atr_14 DOUBLE PRECISION,
    rsi_14 DOUBLE PRECISION,
    macd_line DOUBLE PRECISION,
    macd_signal DOUBLE PRECISION,
    session_asia BOOLEAN DEFAULT FALSE,
    session_london BOOLEAN DEFAULT FALSE,
    session_ny BOOLEAN DEFAULT FALSE,
    session_overlap BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (symbol, time),
    FOREIGN KEY (symbol, time) REFERENCES market_bars_1m (symbol, time) ON DELETE CASCADE
);
