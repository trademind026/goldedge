-- Task 8: Persistent 8% High-Water-Mark (HWM) Hard Risk Kill-Switch State & Audit Log

CREATE TABLE IF NOT EXISTS risk_hwm_state (
    account_scope VARCHAR(128) PRIMARY KEY,
    account_id VARCHAR(64) NOT NULL DEFAULT '',
    account_currency VARCHAR(12) NOT NULL DEFAULT 'USD',
    initial_equity DOUBLE PRECISION NOT NULL,
    hwm_equity DOUBLE PRECISION NOT NULL,
    current_equity DOUBLE PRECISION NOT NULL,
    drawdown_pct DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    max_drawdown_pct DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    halted BOOLEAN NOT NULL DEFAULT FALSE,
    halt_timestamp TIMESTAMPTZ,
    halt_reason TEXT,
    trigger_hwm DOUBLE PRECISION,
    trigger_equity DOUBLE PRECISION,
    trigger_drawdown_pct DOUBLE PRECISION,
    open_positions_at_trigger INTEGER NOT NULL DEFAULT 0,
    emergency_exit_status VARCHAR(64) NOT NULL DEFAULT 'NONE',
    emergency_exit_details JSONB NOT NULL DEFAULT '[]'::jsonb,
    reset_count INTEGER NOT NULL DEFAULT 0,
    last_reset_at TIMESTAMPTZ,
    last_reset_by VARCHAR(128),
    last_reset_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS risk_hwm_audit_log (
    id BIGSERIAL PRIMARY KEY,
    event_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    account_scope VARCHAR(128) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    hwm_equity DOUBLE PRECISION,
    current_equity DOUBLE PRECISION,
    drawdown_pct DOUBLE PRECISION,
    authorized_by VARCHAR(128),
    reason TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_risk_hwm_audit_scope_time
    ON risk_hwm_audit_log (account_scope, event_time DESC);
