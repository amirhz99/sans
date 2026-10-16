import json
import tempfile
import unittest
from pathlib import Path

from app.ai_context import AIContextBuilder
from app.db import Database
from app.domain import ConversationState, EntityRef, EntityType, Intent, ResponseLevel
from app.bot import Bot
from app.presentation import SessionView
from app.prompt_loader import PromptLoader
from app.service import Service


class AgentContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'contracts.sqlite3')
        self.service = Service(self.db)
        self.source = 0

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def apply(self, action, **args):
        self.source += 1
        with self.db.conn:
            command = self.service.prepare(action, args)
            self.service.apply(command, 1, f'agent-contract:{self.source}')

    def test_domain_contracts_coerce_and_round_trip(self):
        entity = EntityRef(EntityType.SESSION, 7, 'پنجشنبه')
        state = ConversationState(last_entity=entity, response_level='details')
        restored = ConversationState.from_dict(state.to_dict())
        self.assertEqual(restored.last_entity, entity)
        self.assertEqual(restored.response_level, ResponseLevel.DETAILS)
        self.assertEqual(Intent.from_dict({'action': 'card', 'args': {}}).to_dict(),
                         {'action': 'card', 'args': {}})
        self.assertEqual(Bot.response_level({'response_level': 'full'}), ResponseLevel.FULL)
        self.assertEqual(Bot.response_level({'response_level': 'not-a-level'}), ResponseLevel.SUMMARY)
        self.assertEqual(Bot.response_level({'details': True}), ResponseLevel.DETAILS)

    def test_invalid_intents_and_entities_fail_closed(self):
        invalid_intents = (
            {'action': 'drop_database', 'args': {}},
            {'action': 'card', 'args': []},
            {'action': 'card', 'args': {}, 'question': 12},
            [],
        )
        for value in invalid_intents:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Intent.from_dict(value)
        for value in ({'type': 'session'}, {'type': 'session', 'id': True}, {'type': 1, 'id': 1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                EntityRef.from_dict(value)
        self.assertEqual(ConversationState.from_dict({'response_level': 'unsafe'}).response_level,
                         ResponseLevel.SUMMARY)

    def test_context_is_role_aware_and_entity_scoped(self):
        self.apply('new_player', name='علی رضایی')
        with self.db.conn:
            self.service.bind(1, 10, 1)
        self.apply('new_session', title='سانس آزمایشی', when_text='فردا ساعت ۲۱',
                   location='زمین آزادی', venue_cost=1, person_fee=2)

        builder = AIContextBuilder(self.db, self.service)
        player = builder.build(10, 10, False, ConversationState())
        admin = builder.build(10, 1, True,
                              ConversationState(last_entity=EntityRef(EntityType.SESSION, 1)))
        missing = builder.build(10, 1, True,
                                ConversationState(last_entity=EntityRef(EntityType.SESSION, 999)))

        self.assertEqual(player['role'], 'player')
        self.assertEqual(admin['role'], 'admin')
        self.assertEqual(player['self']['id'], 1)
        self.assertNotIn('players', player)
        self.assertEqual([row['id'] for row in admin['sessions']], [1])
        self.assertEqual(missing['sessions'], [])

    def test_prompt_loader_supports_legacy_and_structured_shapes(self):
        legacy = {
            'defaults': {}, 'system_prompt': ['legacy-system'],
            'help': ['legacy-help'], 'admin_help': ['legacy-admin'],
        }
        structured = {
            'defaults': {},
            'prompts': {'system': ['new-system'], 'help': ['new-help'], 'admin_help': ['new-admin']},
        }
        for name, content, expected in (
            ('legacy.json', legacy, 'legacy-system'),
            ('structured.json', structured, 'new-system'),
        ):
            path = Path(self.temp.name) / name
            path.write_text(json.dumps(content), encoding='utf-8')
            with self.subTest(name=name):
                prompts = PromptLoader(path).load()
                self.assertEqual(prompts.system, expected)
                self.assertTrue(prompts.help)
                self.assertTrue(prompts.admin_help)

        missing_defaults = Path(self.temp.name) / 'missing-defaults.json'
        missing_defaults.write_text(json.dumps({'prompts': structured['prompts']}), encoding='utf-8')
        with self.assertRaises(ValueError):
            PromptLoader(missing_defaults).load()
        bad_prompt = Path(self.temp.name) / 'bad-prompt.json'
        bad_prompt.write_text(json.dumps({'defaults': {}, 'prompts': {
            'system': 1, 'help': 'help', 'admin_help': 'admin',
        }}), encoding='utf-8')
        with self.assertRaises(ValueError):
            PromptLoader(bad_prompt).load()

    def test_session_view_has_explicit_levels(self):
        session = {
            'id': 1, 'title': 'سانس آزمایشی', 'when_text': 'فردا ساعت ۲۱',
            'session_at': '2026-10-17T21:00+03:30', 'location': 'زمین آزادی',
            'person_fee': 150000, 'venue_cost': 2400000,
            'note': 'یادداشت داخلی', 'finalized': 0,
        }
        view = SessionView(self.service)
        summary = view.render(session)
        details = view.render(session, ResponseLevel.DETAILS)
        full_for_player = view.render(session, ResponseLevel.FULL, admin=False)
        full_for_admin = view.render(session, ResponseLevel.FULL, admin=True)

        self.assertIn('سانس آزمایشی', summary)
        self.assertNotIn('یادداشت داخلی', summary)
        self.assertIn('توضیح: یادداشت داخلی', details)
        self.assertNotIn('هزینهٔ زمین:', full_for_player)
        self.assertIn('هزینهٔ زمین:', full_for_admin)

        incomplete = dict(session, session_at=None, when_text='', location='', person_fee=0,
                          venue_cost=None, note='')
        rendered = view.render(incomplete)
        self.assertIn('نامشخص', rendered)
        self.assertIn('هنوز اعلام نشده', rendered)


if __name__ == '__main__':
    unittest.main()