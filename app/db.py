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
        self.conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys = ON')
        self.conn.execute('PRAGMA journal_mode = WAL')
        self.conn.executescript(Path(__file__).with_name('schema.sql').read_text())
        columns = {row['name'] for row in self.conn.execute('PRAGMA table_info(sessions)')}
        if 'session_at' not in columns:
            self.conn.execute('ALTER TABLE sessions ADD COLUMN session_at TEXT')
        # Drop legacy trigger if present so incomplete/historical sessions work freely.
        self.conn.execute('DROP TRIGGER IF EXISTS sessions_require_session_at')
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
                     (chat_id, user_id, dumps(state), dumps(history[-20:])))

    def log_message(self, chat_id, user_id, sender_name, text, role='user'):
        if not text or not str(text).strip():
            return
        self.execute(
            'INSERT INTO chat_messages(chat_id, user_id, sender_name, role, text) VALUES (?,?,?,?,?)',
            (int(chat_id), int(user_id or 0), str(sender_name or '')[:120], str(role), str(text).strip()[:2000]),
        )

    def recent_messages(self, chat_id, limit=20):
        rows = self.all(
            'SELECT user_id, sender_name, role, text, created_at FROM chat_messages WHERE chat_id=? ORDER BY id DESC LIMIT ?',
            (int(chat_id), int(limit)),
        )
        return [dict(row) for row in reversed(rows)]

    def log_agent_event(self, chat_id, user_id, sender_name, event, summary='', details=None, duration_ms=0):
        payload = dumps(details or {})
        self.execute(
            'INSERT INTO agent_logs(chat_id, user_id, sender_name, event, summary, details, duration_ms) VALUES (?,?,?,?,?,?,?)',
            (
                int(chat_id or 0),
                int(user_id or 0),
                str(sender_name or '')[:120],
                str(event)[:60],
                str(summary or '')[:1000],
                payload[:4000],
                int(duration_ms or 0),
            ),
        )
        try:
            log_dir = self.path.parent / 'logs'
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / 'agent.log'
            line = (
                f"[{event}] chat={chat_id} user={user_id} ({sender_name}) "
                f"dur={int(duration_ms or 0)}ms | {summary} | {payload}\n"
            )
            with log_file.open('a', encoding='utf-8') as fh:
                fh.write(line)
        except OSError:
            pass

    def recent_agent_logs(self, limit=40):
        rows = self.all('SELECT * FROM agent_logs ORDER BY id DESC LIMIT ?', (int(limit),))
        return [dict(row) for row in reversed(rows)]
