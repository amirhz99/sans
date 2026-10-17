import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent


def load_env(path=ROOT / '.env'):
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


@dataclass(frozen=True)
class Config:
    token: str
    admin_group: int
    admins: frozenset[int]
    ai_url: str
    ai_key: str
    ai_model: str
    database: Path
    bot_name: str = 'مسئول'
    reminder_hours: int = 24
    admin_access: str = 'allowlist'

    @classmethod
    def from_env(cls):
        load_env()
        access = os.getenv('ADMIN_ACCESS', 'allowlist').strip()
        if access not in ('allowlist', 'group_members'):
            raise ValueError('ADMIN_ACCESS must be allowlist or group_members')
        required = ('BALE_BOT_TOKEN', 'ADMIN_GROUP_ID',
                    'AI_BASE_URL', 'AI_API_KEY', 'AI_MODEL')
        missing = [key for key in required if not os.getenv(key, '').strip()]
        if missing:
            raise ValueError('Missing configuration: ' + ', '.join(missing))
        url = os.environ['AI_BASE_URL'].rstrip('/')
        if urlparse(url).scheme != 'https':
            raise ValueError('AI_BASE_URL must use HTTPS')
        admins = frozenset(int(x.strip()) for x in os.getenv('ADMIN_USER_IDS', '').split(',') if x.strip())
        if any(x <= 0 for x in admins):
            raise ValueError('ADMIN_USER_IDS must contain positive numeric IDs')
        database = Path(os.getenv('DATABASE_PATH', 'data/sans.sqlite3'))
        if not database.is_absolute():
            database = ROOT / database
        hours = int(os.getenv('REMINDER_HOURS', '24'))
        if hours < 0:
            raise ValueError('REMINDER_HOURS must be non-negative')
        return cls(os.environ['BALE_BOT_TOKEN'], int(os.environ['ADMIN_GROUP_ID']),
                   admins, url, os.environ['AI_API_KEY'], os.environ['AI_MODEL'],
                   database, os.getenv('BOT_NAME', 'مسئول'), hours, access)