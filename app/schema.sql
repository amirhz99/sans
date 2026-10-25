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
    phone TEXT NOT NULL DEFAULT '',
    bale_id INTEGER UNIQUE
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL UNIQUE,
    when_text TEXT NOT NULL DEFAULT '',
    session_at TEXT,
    location TEXT NOT NULL DEFAULT '',
    venue_cost INTEGER CHECK(venue_cost IS NULL OR venue_cost >= 0),
    person_fee INTEGER NOT NULL DEFAULT 0 CHECK(person_fee >= 0),
    note TEXT NOT NULL DEFAULT '',
    finalized INTEGER NOT NULL DEFAULT 0 CHECK(finalized IN (0, 1))
);
CREATE TABLE IF NOT EXISTS attendance (
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    player_id INTEGER NOT NULL REFERENCES players(id),
    guests INTEGER NOT NULL DEFAULT 0 CHECK(guests >= 0),
    -- SPEC-024: 1 when the player attended himself, 0 when the row only carries
    -- his guests.  The default keeps every pre-existing row on today's rule.
    own INTEGER NOT NULL DEFAULT 1 CHECK(own IN (0, 1)),
    PRIMARY KEY(session_id, player_id)
);
-- SPEC-024: who the guests were, when the operator knows it.  Descriptive only:
-- ``attendance.guests`` stays authoritative for the money.
CREATE TABLE IF NOT EXISTS attendance_guests (
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    host_id INTEGER NOT NULL REFERENCES players(id),
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(session_id, host_id, normalized_name)
);
-- SPEC-024: idempotency registry of the explicit guest migration.  A migrated
-- guest keeps its name, so the key is the name and the original session.
CREATE TABLE IF NOT EXISTS guest_migrations (
    guest_name TEXT NOT NULL,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    host_id INTEGER NOT NULL REFERENCES players(id),
    destination_session_id INTEGER NOT NULL REFERENCES sessions(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(guest_name, session_id)
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
-- SPEC-025: a symmetric link between two players who pay for each other.  One
-- row per pair (``low_id < high_id``), so a link can never be stored twice.
CREATE TABLE IF NOT EXISTS player_links (
    low_id INTEGER NOT NULL REFERENCES players(id),
    high_id INTEGER NOT NULL REFERENCES players(id),
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(low_id < high_id),
    PRIMARY KEY(low_id, high_id)
);
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
    guard TEXT NOT NULL DEFAULT '[]',
    message_id INTEGER,
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
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user',
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_chat ON chat_messages(chat_id, id);
CREATE TABLE IF NOT EXISTS agent_logs (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL DEFAULT 0,
    user_id INTEGER NOT NULL DEFAULT 0,
    sender_name TEXT NOT NULL DEFAULT '',
    event TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '{}',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_agent_logs_id ON agent_logs(id DESC);
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
    next_attempt REAL NOT NULL DEFAULT 0,
    ref TEXT
);
CREATE TABLE IF NOT EXISTS contacts (
    user_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS outreach (
    id INTEGER PRIMARY KEY,
    -- SPEC-019: a recipient is a private chat, not necessarily a player, so
    -- ``player_id`` is nullable and the Bale user id is stored next to it.
    player_id INTEGER REFERENCES players(id),
    user_id INTEGER,
    chat_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'sent', 'failed')),
    message_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_outreach_chat ON outreach(chat_id, message_id);
