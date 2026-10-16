import json
import sqlite3
from pathlib import Path


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class Database:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path.resolve()
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys = ON')
        self.conn.execute('PRAGMA journal_mode = WAL')
        self.conn.executescript(Path(__file__).with_name('schema.sql').read_text())
        # Existing installations predate structured session times.  Do not fill
        # this column from old prose: phrases such as «امروز عصر» are not exact
        # timestamps and historical sessions must remain unchanged.
        columns = {row['name'] for row in self.conn.execute('PRAGMA table_info(sessions)')}
        if 'session_at' not in columns:
            self.conn.execute('ALTER TABLE sessions ADD COLUMN session_at TEXT')
        # Every newly inserted session must have a parsed Tehran timestamp.  Keep
        # legacy rows untouched, but do not allow old/direct writers to create
        # another malformed session after the structured-time migration.
        self.conn.execute('''CREATE TRIGGER IF NOT EXISTS sessions_require_session_at
                             BEFORE INSERT ON sessions
                             WHEN NEW.session_at IS NULL OR trim(NEW.session_at) = ''
                             BEGIN
                                 SELECT RAISE(ABORT, 'new sessions require session_at');
                             END''')
        self.conn.commit()

    def close(self):
        self.conn.close()

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def setting(self, key, default=''):
        row = self.one('SELECT value FROM settings WHERE key=?', (key,))
        return row['value'] if row else default

    def set_setting(self, key, value):
        self.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (key, str(value)))

    @property
    def revision(self):
        return int(self.setting('revision', '0'))

    def audit(self, actor, action, details):
        self.execute('INSERT INTO audit_logs(actor, action, details) VALUES (?,?,?)',
                     (str(actor), action, dumps(details)))
        self.set_setting('revision', self.revision + 1)

    def queue(self, method, payload):
        self.execute('INSERT INTO outbox(method,payload) VALUES (?,?)', (method, dumps(payload)))

    def say(self, chat_id, text, buttons=None):
        # Bale's text limit is 4096. Keep chunks small; markup belongs on the last chunk.
        chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)] or ['—']
        for index, chunk in enumerate(chunks):
            payload = {'chat_id': chat_id, 'text': chunk}
            if buttons and index == len(chunks) - 1:
                payload['reply_markup'] = {'inline_keyboard': buttons}
            self.queue('sendMessage', payload)

    def conversation(self, chat_id, user_id):
        row = self.one('SELECT * FROM conversations WHERE chat_id=? AND user_id=?', (chat_id, user_id))
        return (json.loads(row['state']), json.loads(row['history'])) if row else ({}, [])

    def save_conversation(self, chat_id, user_id, state, history):
        self.execute('''INSERT INTO conversations VALUES (?,?,?,?)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET state=excluded.state,history=excluded.history''',
                     (chat_id, user_id, dumps(state), dumps(history[-12:])))