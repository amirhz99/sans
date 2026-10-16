PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO settings VALUES ('approval_required', 'true');
INSERT OR IGNORE INTO settings VALUES ('payment_note', 'پس از واریز، تصویر رسید و مبلغ پرداخت را همین‌جا بفرستید.');
INSERT OR IGNORE INTO settings VALUES ('card', '');
INSERT OR IGNORE INTO settings VALUES ('revision', '0');
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    note TEXT NOT NULL DEFAULT '',
    bale_id INTEGER UNIQUE
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL UNIQUE,
    when_text TEXT NOT NULL DEFAULT '',
    session_at TEXT,
    location TEXT NOT NULL DEFAULT '',
    venue_cost INTEGER CHECK(venue_cost >= 0),
    person_fee INTEGER CHECK(person_fee > 0),
    note TEXT NOT NULL DEFAULT '',
    finalized INTEGER NOT NULL DEFAULT 0 CHECK(finalized IN (0, 1))
);
CREATE TRIGGER IF NOT EXISTS sessions_require_session_at
BEFORE INSERT ON sessions
WHEN NEW.session_at IS NULL OR trim(NEW.session_at) = ''
BEGIN
    SELECT RAISE(ABORT, 'new sessions require session_at');
END;
CREATE TABLE IF NOT EXISTS attendance (
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    player_id INTEGER NOT NULL REFERENCES players(id),
    guests INTEGER NOT NULL DEFAULT 0 CHECK(guests >= 0),
    PRIMARY KEY(session_id, player_id)
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(id),
    session_id INTEGER REFERENCES sessions(id),
    amount INTEGER NOT NULL CHECK(amount != 0),
    note TEXT NOT NULL,
    source TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ledger_player ON ledger(player_id, id);
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(id),
    amount INTEGER NOT NULL CHECK(amount > 0),
    file_id TEXT NOT NULL,
    file_key TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL CHECK(media_type IN ('photo','document')),
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')),
    reviewed_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS pending (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    requester INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT '{}',
    history TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS processed_updates (
    update_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    method TEXT NOT NULL,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0
);