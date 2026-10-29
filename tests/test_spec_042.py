"""SPEC-042: the receipt flow must survive a redelivered update.

The regression this module guards is not "receipts are broken".  A player sent a
photo, then ``1050000`` as a second message, and got an invented confirmation
while no receipt row was ever created.  The cause was that Bale redelivered the
same ``update_id`` ten times inside one poll batch, ``TurnBuffer`` appended every
delivery, and the joined body ``"1050000\\n1050000\\n..."`` no longer matched the
single-token amount parser.
"""

import json
import time
import unittest
from unittest.mock import patch

from app.bot import Bot, RECEIPT_EXPIRED_NOTICE, RECEIPT_TTL_SECONDS
from app.turns import TurnBuffer
from tests.test_sans import BaseCase


def message_update(update_id, text, chat=10, user=10):
    return {'update_id': update_id,
            'message': {'message_id': update_id, 'chat': {'id': chat, 'type': 'private'},
                        'from': {'id': user}, 'text': text}}


class TurnBufferDuplicateTests(unittest.TestCase):
    """AC-001..AC-003: one update_id is one fragment, however often it arrives."""

    def test_duplicate_update_counts_once(self):
        buffer = TurnBuffer(1.2)
        for _ in range(10):
            buffer.add(message_update(1051, '1050000'), now=0.0)
        turns = buffer.ready(now=99.0)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]['message']['text'], '1050000')
        self.assertNotIn('_coalesced', turns[0])

    def test_duplicate_does_not_extend_deadline(self):
        buffer = TurnBuffer(2.0)
        self.assertEqual(buffer.add(message_update(1, 'سلام'), now=0.0), 'new')
        self.assertEqual(buffer.add(message_update(1, 'سلام'), now=1.5), 'duplicate')
        # The window still closes at 2.0, not at 3.5.
        self.assertEqual(buffer.ready(now=2.0)[0]['message']['text'], 'سلام')

    def test_distinct_updates_still_coalesce(self):
        buffer = TurnBuffer(1.2)
        buffer.add(message_update(1, 'سانس فردا'), now=0.0)
        buffer.add(message_update(2, 'ساعت ۹ شب لغو شد'), now=0.0)
        turn = buffer.ready(now=99.0)[0]
        self.assertEqual(turn['message']['text'], 'سانس فردا\nساعت ۹ شب لغو شد')
        self.assertEqual(turn['_coalesced'], [1, 2])

    def test_duplicate_ids_are_not_repeated_in_coalesced_list(self):
        buffer = TurnBuffer(1.2)
        buffer.add(message_update(1, 'الف'), now=0.0)
        buffer.add(message_update(2, 'ب'), now=0.0)
        buffer.add(message_update(2, 'ب'), now=0.0)
        self.assertEqual(buffer.ready(now=99.0)[0]['_coalesced'], [1, 2])


class ParseAmountTests(unittest.TestCase):
    """AC-004, AC-005: a repeated numeric line is still one amount."""

    def test_parse_amount_accepts_repeated_body(self):
        self.assertEqual(Bot.parse_amount('1050000\n1050000\n1050000'), 1050000)

    def test_parse_amount_accepts_single_line_forms(self):
        self.assertEqual(Bot.parse_amount('1050000'), 1050000)
        self.assertEqual(Bot.parse_amount('۱٬۰۵۰٬۰۰۰'), 1050000)
        self.assertEqual(Bot.parse_amount(' 1050000 تومان '), 1050000)

    def test_parse_amount_still_rejects_text(self):
        self.assertIsNone(Bot.parse_amount('سلام'))
        self.assertIsNone(Bot.parse_amount(''))
        self.assertIsNone(Bot.parse_amount('مبلغ را 1050000 کردم'))


class ReceiptFlowTests(BaseCase):
    """AC-006..AC-010: a pending receipt never falls through to the model."""

    def state(self, user=10):
        row = self.db.one('SELECT state FROM conversations WHERE chat_id=? AND user_id=?',
                          (user, user))
        return json.loads(row['state']) if row and row['state'] else {}

    def test_pending_receipt_rejects_unparseable_amount(self):
        self.seed()
        self.receipt('photo-1')
        self.ai.calls.clear()
        self.send('پول رو واریز کردم')
        # The regression: a debt card was built and no receipt was recorded.
        self.assertFalse(self.db.all("SELECT * FROM pending WHERE kind='commands'"))
        self.assertFalse(self.db.all('SELECT * FROM receipts'))
        # ``message()`` turns ``UserError`` into the reply, so assert both: the
        # correction prompt reached the player and the model was never asked.
        self.assertIn('1050000', self.last_reply(10))
        self.assertEqual(self.ai.calls, [])
        # The photo is still pending, so the player can simply retype the amount.
        self.assertEqual(self.state()['receipt']['file_id'], 'photo-1')

    def test_redelivered_amount_still_records_the_receipt(self):
        """The exact production body, after SPEC-042, must submit the receipt."""
        self.seed()
        self.receipt('photo-1')
        self.send('1050000\n1050000\n1050000\n1050000\n1050000')
        row = self.db.one('SELECT * FROM receipts')
        self.assertEqual(row['amount'], 1050000)
        self.assertEqual(row['file_id'], 'photo-1')

    def test_pending_receipt_raises_for_the_dispatcher(self):
        """A non-command body must raise rather than reach the agent."""
        self.seed()
        self.receipt('photo-1')

        def capture(update, acknowledgement=None, client=None):
            raise AssertionError('the model must not run while a receipt waits')

        # ``use_langgraph`` is what routes a free body to the agent; if the guard
        # ever regresses, the body reaches ``agent.run`` instead of raising.
        with patch.object(self.bot.agent, 'run', side_effect=capture), \
                patch.object(type(self.ai), 'use_langgraph', True, create=True):
            self.send('سلام')
        self.assertFalse(self.db.all('SELECT * FROM receipts'))
        self.assertIn('1050000', self.last_reply(10))

    def test_pending_receipt_submits_with_photo(self):
        self.seed()
        self.receipt('photo-1')
        self.send('1050000')
        row = self.db.one('SELECT * FROM receipts')
        self.assertEqual(row['file_id'], 'photo-1')
        self.assertEqual(row['amount'], 1050000)
        self.assertNotIn('receipt', self.state())
        queued = [row['method'] for row in self.db.all('SELECT method FROM outbox')]
        self.assertIn('sendPhoto', queued)

    def test_expired_receipt_is_discarded(self):
        self.seed()
        self.receipt('photo-1')
        stale = self.state()
        stale['receipt']['at'] = int(time.time()) - RECEIPT_TTL_SECONDS - 60
        with self.db.conn:
            self.db.execute('UPDATE conversations SET state=? WHERE chat_id=10 AND user_id=10',
                            (json.dumps(stale),))
        self.send('1050000')
        self.assertFalse(self.db.all('SELECT * FROM receipts'))
        self.assertNotIn('receipt', self.state())
        self.assertIn(RECEIPT_EXPIRED_NOTICE, self.replies(10))

    def test_receipt_without_timestamp_is_kept(self):
        """A receipt stored before SPEC-042 has no ``at``; keep it usable."""
        self.seed()
        self.receipt('photo-1')
        legacy = self.state()
        legacy['receipt'].pop('at', None)
        with self.db.conn:
            self.db.execute('UPDATE conversations SET state=? WHERE chat_id=10 AND user_id=10',
                            (json.dumps(legacy),))
        self.send('1050000')
        self.assertEqual(self.db.one('SELECT file_id FROM receipts')['file_id'], 'photo-1')

    def test_fresh_receipt_is_not_expired(self):
        self.seed()
        self.receipt('photo-1')
        stored = self.state()['receipt']
        self.assertIsInstance(stored['at'], int)
        self.assertFalse(self.bot.expired_receipt({'receipt': dict(stored)}))

    def test_confirmation_requires_receipt_row(self):
        """The words shown to the player come from ``submit_receipt`` itself."""
        self.seed()
        self.receipt('photo-1')
        self.send('1050000')
        receipt_id = self.db.one('SELECT id FROM receipts')['id']
        self.assertIn(f'رسید #{receipt_id}', self.last_reply(10))


if __name__ == '__main__':
    unittest.main()