from pathlib import Path

import json

from .prompt_loader import PromptLoader


ROOT = Path(__file__).resolve().parent.parent
CONTENT_PATH = ROOT / 'content' / 'bot-content.json'


def _lines(content, key):
    value = content.get(key)
    if not isinstance(value, list) or not all(isinstance(line, str) for line in value):
        raise ValueError(f'content key {key!r} must be a list of strings')
    return '\n'.join(value)


def load_content(path=CONTENT_PATH):
    content = json.loads(Path(path).read_text(encoding='utf-8'))
    defaults = content.get('defaults')
    if not isinstance(defaults, dict):
        raise ValueError('content defaults are missing')
    for key in ('venue_cost', 'person_fee', 'location', 'formula'):
        if key not in defaults:
            raise ValueError(f'content default {key!r} is missing')
    return content


CONTENT = load_content()
DEFAULTS = CONTENT['defaults']
_PROMPTS = PromptLoader(CONTENT_PATH).load()
SYSTEM_PROMPT = _PROMPTS.system
HELP = _PROMPTS.help
ADMIN_HELP = _PROMPTS.admin_help