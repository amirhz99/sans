import json
import urllib.error
import urllib.request

from .content import SYSTEM_PROMPT
from .domain import Intent


class APIError(RuntimeError):
    def __init__(self, message, retry_after=5):
        super().__init__(message)
        self.retry_after = retry_after


def post_json(url, payload, headers=None, timeout=45):
    req = urllib.request.Request(url, json.dumps(payload).encode(),
                                 {'Content-Type': 'application/json', **(headers or {})}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read(4 * 1024 * 1024))
    except urllib.error.HTTPError as exc:
        retry = 5
        try:
            body = json.loads(exc.read(8192))
            retry = min(3600, max(1, int(body.get('parameters', {}).get('retry_after', 5))))
        except (ValueError, TypeError, AttributeError):
            pass
        # Never log URLs, headers, receipt text or provider bodies: they can contain secrets.
        raise APIError(f'HTTP {exc.code}', retry) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise APIError('Network error or invalid JSON response') from None


class BaleClient:
    def __init__(self, token):
        self.base = f'https://tapi.bale.ai/bot{token}'

    def call(self, method, payload):
        result = post_json(f'{self.base}/{method}', payload)
        if not isinstance(result, dict) or not result.get('ok'):
            code = result.get('error_code', 'unknown') if isinstance(result, dict) else 'unknown'
            raise APIError(f'Bale API error {code}')
        return result.get('result')


# Kept as a module-level alias for callers that imported the old name.
PROMPT = SYSTEM_PROMPT


class AIClient:
    def __init__(self, config):
        self.url = config.ai_url + '/chat/completions'
        self.key = config.ai_key
        self.model = config.ai_model

    def parse(self, message, history, context):
        # The model only needs a short tail for linguistic continuity.  The
        # structured context is the source of truth for identity and entities.
        compact_history = [
            {'role': item.get('role'), 'content': str(item.get('content', ''))[:1000]}
            for item in (history or [])[-4:]
            if isinstance(item, dict) and item.get('role') in ('user', 'assistant')
        ]
        result = post_json(self.url, {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': json.dumps({'context': context, 'history': compact_history,
                                                      'message': message}, ensure_ascii=False)},
            ],
            'temperature': 0,
            'max_tokens': 2000,
        }, {'Authorization': 'Bearer ' + self.key})
        try:
            content = result['choices'][0]['message']['content'].strip()
            if content.startswith('```') and content.endswith('```'):
                content = content.split('\n', 1)[1].rsplit('```', 1)[0]
            obj = json.loads(content)
            return Intent.from_dict(obj).to_dict()
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise APIError('AI returned an invalid command') from None