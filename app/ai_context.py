"""Build the deliberately small and role-aware context sent to the model."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .domain import ConversationState, EntityRef


class AIContextBuilder:
    # Only these entity types may travel as a follow-up reference.
    _ENTITY_COLUMNS = {'session': ('sessions', 'title'), 'player': ('players', 'name')}

    def __init__(self, db, service=None):
        self.db = db
        self.service = service

    def _entity_context(self, entity: EntityRef | None) -> dict[str, Any] | None:
        """Return the follow-up entity only while it still exists.

        A merge/delete removes a row, and SQLite reuses a freed integer id for
        the next insert, so an unvalidated reference could silently point at a
        different person. An unknown or malformed reference is dropped instead.
        """
        if not entity or entity.type not in self._ENTITY_COLUMNS:
            return None
        if isinstance(entity.id, bool):
            return None
        try:
            ident = int(entity.id)
        except (TypeError, ValueError):
            return None
        table, column = self._ENTITY_COLUMNS[entity.type]
        row = self.db.one(f'SELECT id,{column} label FROM {table} WHERE id=?', (ident,))
        if not row:
            return None
        return {'type': entity.type, 'id': row['id'], 'title': row['label']}

    def _session_context(self, entity: EntityRef | None) -> list[dict[str, Any]]:
        if not entity or entity.type != 'session':
            return []
        try:
            row = self.db.one(
                'SELECT id,title,when_text,session_at,finalized FROM sessions WHERE id=?',
                (int(entity.id),),
            )
        except (TypeError, ValueError):
            row = None
        return [dict(row)] if row else []

    def build(self, chat_id, user_id, admin, state=None, reply_to=None) -> dict[str, Any]:
        conversation = state if isinstance(state, ConversationState) else ConversationState.from_dict(state or {})
        linked = self.service.linked(user_id) if self.service else None
        context: dict[str, Any] = {
            'role': 'admin' if admin else 'player',
            'now_tehran': datetime.now(ZoneInfo('Asia/Tehran')).isoformat(timespec='minutes'),
            'self': {'id': linked['id'], 'name': linked['name']} if linked else None,
            'last_entity': self._entity_context(conversation.last_entity),
            'response_level': conversation.response_level.value,
            'awaiting_receipt_amount': conversation.receipt is not None,
            # Only the entity needed for a follow-up is included.  In particular,
            # never send the complete player directory to the model.
            'sessions': self._session_context(conversation.last_entity),
        }
        if self.service and linked:
            # SPEC-025: id, name and balance of the direct partners only; never a
            # phone number, a receipt, or another player's ledger notes.
            partners = self.service.links(linked['id'])
            if partners:
                context['linked_players'] = partners
        if self.service:
            # SPEC-017: the effective new-session defaults, so the model never
            # relies on numbers hardcoded in the prompt.
            context['session_defaults'] = self.service.session_defaults()
        if reply_to:
            sender = reply_to.get('from', {}) if isinstance(reply_to, dict) else {}
            context['reply_to_message'] = {
                'text': (reply_to.get('text') or reply_to.get('caption') or '')[:2000],
                'sender': {
                    'id': sender.get('id'),
                    'username': sender.get('username'),
                    'display_name': ' '.join(filter(None, (sender.get('first_name'), sender.get('last_name')))),
                },
            }
        return context