"""SPEC-043: a declared payment must decrease debt, never increase it.

The bug this module guards is not "the receipt path is broken".  A player wrote
«۱۵۰٬۰۰۰ پرداخت کردم» without a receipt, the model routed it to
``declare_debt``, and ``Service.prepare`` capped that action's amount at a
*minimum of one* — so the only writable row was an increase in debt.  In
production this turned a 1,050,000 payment into 2,100,000 of debt.

The fix is structural: a declaration now carries ``direction`` (``owed`` or
``paid``), the ledger sign comes from that direction alone, and nothing can be
written when the direction is missing.
"""

import json
import unittest

from app.domain import DeclaredDirection
from app.service import UserError
from tests.test_sans import BaseCase


class DeclaredDirectionTests(unittest.TestCase):
    """The closed vocabulary itself: no sign guessing, no case folding."""

    def test_only_two_values_are_accepted(self):
        self.assertIs(DeclaredDirection.parse('owed'), DeclaredDirection.OWED)
        self.assertIs(DeclaredDirection.parse('paid'), DeclaredDirection.PAID)

    def test_anything_else_raises(self):
        for value in ('', 'PAID', 'Owed', 'debt', None, 1, True, ['paid']):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    DeclaredDirection.parse(value)

    def test_the_sign_comes_from_the_direction(self):
        self.assertEqual(DeclaredDirection.PAID.sign, -1)
        self.assertEqual(DeclaredDirection.OWED.sign, 1)


class PaymentDirectionTests(BaseCase):
    def seed(self, linked=True):
        """Build one linked player with an outstanding balance.

        Called from each test body rather than ``setUp``: the writes commit
        inside the test's own transaction, exactly like every other module, so
        ``send()`` sees a real ``welcomed`` setting and a real ledger.
        """
        self.apply('new_player', name='علی رضایی')
        if linked:
            with self.db.conn:
                self.service.bind(1, 10, 1)  # chat 10 is this player
        # A real outstanding balance, so "it went down" is observable.
        self.apply('adjustment', player=1, amount=300000, note='بدهی سانس')

    def declare(self, amount=150000, direction='paid', note='اعلام کاربر'):
        return self.service.prepare('declare_debt', {'player': 1, 'amount': amount,
                                                     'direction': direction, 'note': note})

    def ledger_rows(self):
        return self.db.one('SELECT COUNT(*) n FROM ledger')['n']

    def test_prepare_refuses_a_declaration_without_direction(self):
        self.seed()
        # SPEC-043 AC-001: the direction is the whole point; without it the same
        # amount means opposite things, so prepare must fail closed.
        before = self.ledger_rows()
        with self.assertRaises(UserError):
            self.service.prepare('declare_debt', {'player': 1, 'amount': 150000})
        self.assertEqual(self.ledger_rows(), before)

    def test_a_declared_payment_decreases_the_balance(self):
        self.seed()
        # AC-002/AC-003: the production regression, end to end.
        before = self.service.balance(1)
        self.apply('declare_debt', player=1, amount=150000, direction='paid')
        rows = self.db.all("SELECT amount FROM ledger WHERE source=?", ('test:%s' % self.source,))
        self.assertEqual([row['amount'] for row in rows], [-150000])
        self.assertEqual(self.service.balance(1), before - 150000)

    def test_a_declared_debt_increases_the_balance(self):
        self.seed()
        # AC-004: the other direction must still work exactly as before.
        before = self.service.balance(1)
        self.apply('declare_debt', player=1, amount=150000, direction='owed')
        self.assertEqual(self.service.balance(1), before + 150000)

    def test_an_invalid_direction_or_amount_is_refused(self):
        self.seed()
        # AC-005/AC-006: neither a signed amount nor a near-miss direction may
        # slip through; a wrong guess must never reach the ledger.
        before = self.ledger_rows()
        for direction in ('', 'PAID', 'debit', None):
            for amount in (150000, -150000):
                with self.subTest(direction=direction, amount=amount):
                    with self.assertRaises(UserError):
                        self.declare(amount=amount, direction=direction)
        for direction in ('owed', 'paid'):
            with self.subTest(direction=direction):
                with self.assertRaises(UserError):
                    self.declare(amount=-150000, direction=direction)
        self.assertEqual(self.ledger_rows(), before)

    def test_the_card_names_the_direction_and_waits_for_approval(self):
        self.seed()
        # AC-007: an admin must be able to read the direction, and no money may
        # move before the button is pressed.
        self.ai.answer = {'action': 'declare_debt', 'args': {'amount': 150000, 'direction': 'paid'}}
        self.send('۱۵۰٬۰۰۰ تومان پرداخت کردم')
        ident = self.proposal()
        card = self.replies(-100)
        self.assertIn('پرداخت', card)
        self.assertIn('کم می‌شود', card)
        self.assertNotIn('به بدهی بازیکن اضافه می‌شود', card)
        self.assertEqual(self.service.balance(1), 300000)
        self.click('yes:' + ident)
        self.assertEqual(self.service.balance(1), 150000)

    def test_a_rejected_declaration_tells_the_player_nothing_changed(self):
        self.seed()
        # AC-008: the player used to be told nothing at all, so a rejected
        # payment looked recorded.  The balance in the notice comes from the
        # service, not from the model.
        self.ai.answer = {'action': 'declare_debt', 'args': {'amount': 150000, 'direction': 'paid'}}
        self.send('۱۵۰٬۰۰۰ تومان پرداخت کردم')
        ident = self.proposal()
        before = self.service.balance(1)
        self.click('no:' + ident)
        self.assertEqual(self.service.balance(1), before)
        notices = [json.loads(row['payload']).get('text', '')
                   for row in self.db.all("SELECT payload FROM outbox WHERE method='sendMessage' ORDER BY id")
                   if json.loads(row['payload']).get('chat_id') == 10]
        rejection = [text for text in notices if 'تأیید نشد' in text]
        self.assertTrue(rejection, 'the player must be told the request was refused')
        self.assertIn('کم نشد', rejection[-1])
        self.assertIn('300,000', rejection[-1])

    def test_an_open_debt_card_does_not_block_a_payment_card(self):
        self.seed()
        # AC-009: dedupe is per (player, direction).  A player may owe money and
        # have paid part of it in the same conversation.
        self.ai.answer = {'action': 'declare_debt', 'args': {'amount': 150000, 'direction': 'owed'}}
        self.send('۱۵۰٬۰۰۰ تومان بدهکارم')
        first = self.proposal()
        self.ai.answer = {'action': 'declare_debt', 'args': {'amount': 150000, 'direction': 'paid'}}
        self.send('۱۵۰٬۰۰۰ تومان پرداخت کردم')
        self.assertNotEqual(self.proposal(), first)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM pending WHERE status='pending'")['n'], 2)
        # The same direction twice is still a duplicate: it is refused with a
        # message and no third card appears.  (SPEC-042's amount guard answers
        # first when both messages are unparseable-free numbers; either refusal
        # is correct as long as nothing new is created.)
        self.send('۲۰۰٬۰۰۰ تومان پرداخت کردم')
        refusal = [json.loads(row['payload']).get('text', '')
                   for row in self.db.all("SELECT payload FROM outbox WHERE method='sendMessage'")
                   if json.loads(row['payload']).get('chat_id') == 10][-1]
        self.assertIn('هنوز منتظر تأیید است', refusal)
        self.assertNotIn('کد:', refusal)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM pending")['n'], 2)


if __name__ == '__main__':
    unittest.main()