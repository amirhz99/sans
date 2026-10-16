"""Load prompt content while supporting both old and structured content files."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PromptCatalog:
    defaults: dict[str, Any]
    system: str
    help: str
    admin_help: str


class PromptLoader:
    """Read the current JSON format and the planned ``prompts`` format.

    The compatibility path is deliberate: deployments can migrate
    ``bot-content.json`` independently from the Python release.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @staticmethod
    def _text(value: Any, key: str) -> str:
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return '\n'.join(value)
        if isinstance(value, str):
            return value
        raise ValueError(f'content key {key!r} must be a string or list of strings')

    def load(self) -> PromptCatalog:
        content = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(content, dict) or not isinstance(content.get('defaults'), dict):
            raise ValueError('content defaults are missing')
        prompts = content.get('prompts')
        if not isinstance(prompts, dict):
            prompts = content
        return PromptCatalog(
            defaults=content['defaults'],
            system=self._text(prompts.get('system', prompts.get('system_prompt')), 'system_prompt'),
            help=self._text(prompts.get('help'), 'help'),
            admin_help=self._text(prompts.get('admin_help'), 'admin_help'),
        )