"""Coalesce rapid back-to-back messages into one conversational turn (SPEC-018).

A person rarely writes one complete sentence: they send «سانس فردا» and then
«ساعت ۹ شب لغو شد» as two messages.  Answering each fragment separately wastes a
model call and produces two contradictory replies.  ``TurnBuffer`` holds such
fragments for a very short window and hands the bot a single synthetic update.

It is deliberately small and side-effect free: no database, no network, no
Bale payload knowledge beyond ``update_id`` and ``message``.
"""


class TurnBuffer:
    """Hold plain-text fragments of one user for ``window`` seconds."""

    def __init__(self, window=1.2, max_messages=10):
        self.window = max(0.0, float(window))
        self.max_messages = max(1, int(max_messages))
        self._pending = {}

    @staticmethod
    def _key(update):
        """Return ``(chat_id, user_id)`` for a message update, else ``None``."""
        if not isinstance(update, dict) or update.get('callback_query'):
            return None
        message = update.get('message')
        if not isinstance(message, dict):
            return None
        chat_id = (message.get('chat') or {}).get('id')
        user_id = (message.get('from') or {}).get('id')
        for value in (chat_id, user_id):
            if isinstance(value, bool) or not isinstance(value, int):
                return None
        return chat_id, user_id

    @staticmethod
    def _eligible(update):
        """Only plain text is merged; media, contacts, and commands keep their flow."""
        message = update['message']
        if message.get('photo') or message.get('document') or message.get('contact'):
            return None
        body = (message.get('text') or '').strip()
        if not body or body.startswith('/'):
            return None
        return body

    def add(self, update, now):
        """Buffer ``update`` and return ``'new'``, ``'joined'``, or ``None``.

        ``None`` means the caller must handle the update immediately: either the
        window is disabled, or the update is not a mergeable plain-text message.
        """
        if self.window <= 0:
            return None
        key = self._key(update)
        body = self._eligible(update) if key else None
        if not key or body is None:
            return None
        entry = self._pending.get(key)
        if entry is None:
            self._pending[key] = {
                'update': update,
                'first': update,
                'texts': [body],
                'ids': [update.get('update_id')],
                'deadline': now + self.window,
            }
            return 'new'
        entry['texts'].append(body)
        entry['ids'].append(update.get('update_id'))
        entry['update'] = update
        entry['deadline'] = now + self.window
        return 'joined'

    def ready(self, now):
        """Return the turns whose window closed, oldest first."""
        due = [key for key, entry in self._pending.items()
               if now >= entry['deadline'] or len(entry['texts']) >= self.max_messages]
        turns = [self._pop(key) for key in due]
        turns.sort(key=lambda update: update.get('update_id') or 0)
        return turns

    def flush(self):
        """Return every buffered turn regardless of the window."""
        turns = [self._pop(key) for key in list(self._pending)]
        turns.sort(key=lambda update: update.get('update_id') or 0)
        return turns

    def flush_key(self, update):
        """Return the buffered turn of this update's sender, if any.

        Used before handling a message that cannot be coalesced (a command, a
        receipt, a contact) so that a later message never overtakes an earlier
        one from the same person.
        """
        key = self._key(update)
        if key is None or key not in self._pending:
            return []
        return [self._pop(key)]

    def __len__(self):
        return len(self._pending)

    def _pop(self, key):
        entry = self._pending.pop(key)
        first = entry.get('first') or {}
        update = dict(entry['update'])
        if len(entry['texts']) > 1:
            # One turn, one reply: the model sees the whole thought in order.
            message = dict(update.get('message') or {})
            message['text'] = '\n'.join(entry['texts'])
            update['message'] = message
            update['_coalesced'] = [ident for ident in entry['ids']
                                    if isinstance(ident, int) and not isinstance(ident, bool)]
            # SPEC-028: the progress acknowledgement is attached to the *first*
            # fragment, but this merged turn is built from the *last* one.  Carry
            # it over so a coalesced turn still has a progress message to edit.
            if update.get('_ack') is None and first.get('_ack') is not None:
                update['_ack'] = first['_ack']
        return update
