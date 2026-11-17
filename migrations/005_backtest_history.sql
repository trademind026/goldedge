CREATE TABLE IF NOT EXISTS backtest_runs (
    id SERIAL PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL DEFAULT 'XAUUSD',
    risk TEXT NOT NULL,
    exit_agent TEXT NOT NULL DEFAULT 'Kalman',
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    trades INTEGER NOT NULL DEFAULT 0,
    win_rate DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    pnl DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    max_dd DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    pf DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    avg_win DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    avg_loss DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    rr DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    equity_curve JSONB NOT NULL DEFAULT '[]'::jsonb,
    trade_list JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_created_at ON backtest_runs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_risk ON backtest_runs (risk);
