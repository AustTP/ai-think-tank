CREATE TABLE IF NOT EXISTS confirmation_proposals (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    target_json TEXT NOT NULL,
    args_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    actor TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'web',
    phrase TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    confirmed_at REAL,
    confirmed_by TEXT,
    executed_at REAL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_confirmation_actor_status
    ON confirmation_proposals(actor, status, created_at);
CREATE INDEX IF NOT EXISTS idx_confirmation_status_expiry
    ON confirmation_proposals(status, expires_at);
