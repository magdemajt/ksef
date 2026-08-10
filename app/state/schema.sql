-- ksef-bridge: lokalny stan aplikacji (SQLite, WAL)
-- Wersjonowanie: PRAGMA user_version (migracje w app/state/db.py)

CREATE TABLE IF NOT EXISTS sent_invoices (
    id INTEGER PRIMARY KEY,
    company_id TEXT NOT NULL,
    trn_id INTEGER NOT NULL,
    numer_pelny TEXT NOT NULL,
    buyer_nip TEXT,
    status TEXT NOT NULL CHECK (status IN
        ('previewed','queued','sent','accepted','duplicate','rejected','error')),
    xml_path TEXT,
    xml_sha256 TEXT,
    session_ref TEXT,
    invoice_ref TEXT,
    ksef_number TEXT,
    upo_path TEXT,
    error_code INTEGER,
    error_detail TEXT,
    environment TEXT NOT NULL,
    confirmed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (company_id, trn_id, environment)
);

CREATE TABLE IF NOT EXISTS pending_batches (
    id INTEGER PRIMARY KEY,
    company_id TEXT NOT NULL,
    confirm_token TEXT NOT NULL UNIQUE,
    trn_ids_json TEXT NOT NULL,
    totals_json TEXT NOT NULL,
    environment TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS ksef_sessions (
    id INTEGER PRIMARY KEY,
    company_id TEXT NOT NULL,
    ref TEXT NOT NULL UNIQUE,
    environment TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    upo_ref TEXT,
    upo_path TEXT
);

CREATE TABLE IF NOT EXISTS downloaded_invoices (
    id INTEGER PRIMARY KEY,
    company_id TEXT NOT NULL,
    ksef_number TEXT NOT NULL,
    seller_nip TEXT,
    seller_name TEXT,
    invoice_number TEXT,
    issue_date TEXT,
    storage_date TEXT,
    net REAL,
    vat REAL,
    gross REAL,
    currency TEXT,
    xml_path TEXT,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN
        ('new','needs_contractor','already_in_optima','exported','skipped','error')),
    van_id INTEGER,
    exported_at TEXT,
    error_detail TEXT,
    environment TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (company_id, ksef_number, environment)
);

CREATE TABLE IF NOT EXISTS sync_state (
    company_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (company_id, key)
);

-- Przesuwne okno wywolan API (przezywa restart procesu)
CREATE TABLE IF NOT EXISTS rate_limits (
    company_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_limits ON rate_limits (company_id, endpoint, ts);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    company_id TEXT,
    action TEXT NOT NULL,
    entity TEXT,
    entity_id TEXT,
    environment TEXT,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (ts);
