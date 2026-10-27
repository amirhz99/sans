import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent

# SPEC-031: a support contact is only ever a plain Bale handle, never free text.
# The value is injected verbatim into the agent prompt, so anything that is not
# a well-formed handle is rejected at startup instead of reaching the model.
SUPPORT_HANDLE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{2,63}$')


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
    ai_fallback_models: tuple[str, ...] = ()
    # SPEC-031: empty means "no human hand-off is configured".
    support_handle: str = ''

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
        model = os.environ['AI_MODEL']
        fallbacks = parse_fallback_models(
            os.getenv('AI_FALLBACK_MODELS', os.getenv('AI_FALLBACK_MODEL', '')), model)
        support_handle = parse_support_handle(os.getenv('SUPPORT_HANDLE', ''))
        return cls(os.environ['BALE_BOT_TOKEN'], int(os.environ['ADMIN_GROUP_ID']),
                   admins, url, os.environ['AI_API_KEY'], model,
                   database, os.getenv('BOT_NAME', 'مسئول'), hours, access, fallbacks,
                   support_handle)


def parse_support_handle(raw):
    """Return a normalised ``@handle`` or ``''`` when support is not configured.

    SPEC-031: ``amirhz99`` and ``@amirhz99`` are both accepted and stored in the
    same canonical form so the prompt and ``/support`` cannot disagree.  An
    empty value disables the feature; anything else that is not a plain handle
    raises, because it would be injected verbatim into the system prompt.
    """
    if not isinstance(raw, str):
        raise ValueError('SUPPORT_HANDLE must be a string')
    value = raw.strip()
    if not value:
        return ''
    # A bare "@" is a malformed value, not "disabled": an operator who wrote it
    # meant to give a contact, so silently disabling the hand-off would hide it.
    if value.startswith('@'):
        value = value[1:].strip()
    if not value:
        raise ValueError('SUPPORT_HANDLE must be a plain handle such as @amirhz99')
    if not SUPPORT_HANDLE.fullmatch(value):
        raise ValueError('SUPPORT_HANDLE must be a plain handle such as @amirhz99')
    return '@' + value


def parse_fallback_models(raw, primary=''):
    """Parse a comma-separated fallback model list.

    Order is preserved so operators can express preference; blanks, duplicates
    and the primary model are dropped because they would only repeat a failure.
    """
    if not isinstance(raw, str):
        return ()
    models = (item.strip() for item in raw.split(','))
    return tuple(dict.fromkeys(model for model in models if model and model != primary))