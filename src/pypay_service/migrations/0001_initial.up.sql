CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    api_key_id TEXT NOT NULL UNIQUE,
    api_secret_enc TEXT NOT NULL,
    notify_secret_enc TEXT NOT NULL,
    notify_url TEXT NOT NULL,
    allowed_return_urls TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE channel_accounts (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL CHECK (channel IN ('wechat', 'alipay')),
    name TEXT NOT NULL,
    env_prefix TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (channel, name)
);

CREATE TABLE project_channels (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK (channel IN ('wechat', 'alipay')),
    account_id TEXT NOT NULL REFERENCES channel_accounts(id),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    PRIMARY KEY (project_id, channel)
);

CREATE TABLE payment_orders (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    business_order_no TEXT NOT NULL,
    subject TEXT NOT NULL,
    amount_fen INTEGER NOT NULL CHECK (amount_fen > 0),
    currency TEXT NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'paid', 'closed', 'expired', 'refunded', 'failed')),
    cashier_token TEXT NOT NULL UNIQUE,
    return_url TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    paid_at TEXT,
    closed_at TEXT,
    refunded_amount_fen INTEGER NOT NULL DEFAULT 0 CHECK (refunded_amount_fen >= 0),
    UNIQUE (project_id, business_order_no),
    CHECK (refunded_amount_fen <= amount_fen)
);

CREATE INDEX idx_payment_orders_project_created
    ON payment_orders (project_id, created_at DESC);
CREATE INDEX idx_payment_orders_status_expires
    ON payment_orders (status, expires_at);

CREATE TABLE payment_attempts (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES payment_orders(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK (channel IN ('wechat', 'alipay')),
    account_id TEXT NOT NULL REFERENCES channel_accounts(id),
    out_trade_no TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'paid', 'closed', 'failed', 'refunded')),
    amount_fen INTEGER NOT NULL CHECK (amount_fen > 0),
    currency TEXT NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
    qr_code_url TEXT,
    form_html TEXT,
    redirect_url TEXT,
    transaction_id TEXT,
    channel_status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    paid_at TEXT,
    closed_at TEXT,
    UNIQUE (order_id, channel)
);

CREATE INDEX idx_payment_attempts_order ON payment_attempts (order_id);
CREATE INDEX idx_payment_attempts_status ON payment_attempts (status);

CREATE TABLE refunds (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES payment_orders(id),
    attempt_id TEXT NOT NULL REFERENCES payment_attempts(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    out_refund_no TEXT NOT NULL UNIQUE,
    amount_fen INTEGER NOT NULL CHECK (amount_fen > 0),
    total_amount_fen INTEGER NOT NULL CHECK (total_amount_fen > 0),
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'succeeded', 'failed', 'closed')),
    provider_refund_id TEXT,
    idempotency_key TEXT,
    requested_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    succeeded_at TEXT,
    UNIQUE (project_id, idempotency_key)
);

CREATE INDEX idx_refunds_order ON refunds (order_id);
CREATE INDEX idx_refunds_status ON refunds (status);

CREATE TABLE channel_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    notification_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    out_trade_no TEXT NOT NULL,
    out_refund_no TEXT,
    payload_hash TEXT,
    received_at TEXT NOT NULL,
    UNIQUE (channel, notification_id)
);

CREATE INDEX idx_channel_notifications_order
    ON channel_notifications (out_trade_no);
CREATE INDEX idx_channel_notifications_refund
    ON channel_notifications (out_refund_no);

CREATE TABLE outbox_events (
    event_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    order_id TEXT NOT NULL REFERENCES payment_orders(id),
    event_type TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'delivered', 'dead')),
    ack_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (ack_status IN ('pending', 'acked', 'rejected')),
    attempts INTEGER NOT NULL DEFAULT 0,
    manual_resends INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    lease_until TEXT,
    last_error TEXT,
    delivered_at TEXT,
    acked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, dedupe_key)
);

CREATE INDEX idx_outbox_events_due ON outbox_events (status, next_attempt_at);
CREATE INDEX idx_outbox_events_order ON outbox_events (order_id);

CREATE TABLE outbox_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES outbox_events(event_id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('delivered', 'retry', 'dead')),
    http_status INTEGER,
    response_excerpt TEXT,
    error TEXT,
    duration_ms INTEGER,
    sent_at TEXT NOT NULL
);

CREATE INDEX idx_outbox_deliveries_event ON outbox_deliveries (event_id);

CREATE TABLE api_nonces (
    key_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (key_id, nonce)
);

CREATE INDEX idx_api_nonces_created ON api_nonces (created_at);

CREATE TABLE audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    project_id TEXT,
    order_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_audit_logs_created ON audit_logs (created_at DESC);
CREATE INDEX idx_audit_logs_order ON audit_logs (order_id);
