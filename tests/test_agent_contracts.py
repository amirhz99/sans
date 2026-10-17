import json
import tempfile
import unittest
from pathlib import Path

from app.ai_context import AIContextBuilder
from app.agent import AGENT_SYSTEM_PROMPT
from app.content import SYSTEM_PROMPT
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

    def test_ai_prompts_reject_unrelated_requests_without_tools(self):
        scope_reply = 'این درخواست نامرتبط است؛ من فقط مسئول مدیریت سانس فوتبال هستم و نمی‌توانم به آن پاسخ بدهم.'

        self.assertIn('نامرتبط', AGENT_SYSTEM_PROMPT)
        self.assertIn('هیچ ابزاری را اجرا نکن', AGENT_SYSTEM_PROMPT)
        self.assertIn(scope_reply, AGENT_SYSTEM_PROMPT)

        self.assertIn('نامرتبط', SYSTEM_PROMPT)
        self.assertIn('action=clarify', SYSTEM_PROMPT)
        self.assertIn(scope_reply, SYSTEM_PROMPT)

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

    def test_langgraph_agent_executes_tools_and_proposes_financial_changes(self):
        from langchain_core.messages import AIMessage
        from app.config import Config

        class ScriptedLangGraphLLM:
            def __init__(self, responses):
                self.responses = list(responses)
                self.calls = 0

            def bind_tools(self, tools):
                return self

            def invoke(self, messages):
                idx = min(self.calls, len(self.responses) - 1)
                self.calls += 1
                return self.responses[idx]

        class LangGraphDriver:
            use_langgraph = True

            def __init__(self, responses):
                self.llm_override = ScriptedLangGraphLLM(responses)

        config = Config(
            'fake-token', -100, frozenset({1}), 'https://example.com/v1',
            'fake-key', 'fake-model', Path(self.temp.name) / 'contracts.sqlite3',
        )
        driver = LangGraphDriver([
            AIMessage(
                content='',
                tool_calls=[
                    {
                        'id': 'call_1',
                        'name': 'manage_records',
                        'args': {
                            'operation': 'save_session',
                            'data_json': json.dumps({
                                'title': 'سانس اول مهر',
                                'when_text': 'مهرماه (بدون ساعت دقیق)',
                                'person_fee': 150000,
                            }, ensure_ascii=False),
                        },
                    },
                    {
                        'id': 'call_2',
                        'name': 'manage_records',
                        'args': {
                            'operation': 'set_attendance',
                            'data_json': json.dumps({
                                'session': 'سانس اول مهر',
                                'present': [{'name': 'علی رضایی', 'guests': 1}, 'رضا محمدی'],
                            }, ensure_ascii=False),
                        },
                    },
                    {
                        'id': 'call_3',
                        'name': 'propose_financial_change',
                        'args': {
                            'action': 'finalize',
                            'session': 'سانس اول مهر',
                        },
                    },
                ],
            ),
            AIMessage(content='سانس اول مهر و حاضرانش ثبت شد؛ کارت نهایی‌سازی بدهی را تأیید کنید.'),
        ])
        bot = Bot(self.db, config, driver)
        bot.process({
            'update_id': 1,
            'message': {
                'message_id': 10,
                'chat': {'id': -100, 'type': 'group'},
                'from': {'id': 1, 'first_name': 'ادمین'},
                'text': 'مسئول، سانس اول مهر رو با حضور علی رضایی (+۱ مهمان) و رضا محمدی ثبت کن و ببند.',
            },
        })

        session = self.db.one('SELECT * FROM sessions WHERE title=?', ('سانس اول مهر',))
        self.assertIsNotNone(session)
        self.assertIsNone(session['session_at'])
        attendees = self.db.all('SELECT * FROM attendance WHERE session_id=?', (session['id'],))
        self.assertEqual(len(attendees), 2)
        pending = self.db.one("SELECT * FROM pending WHERE kind='commands' AND status='pending'")
        self.assertIsNotNone(pending)
        self.assertEqual(self.service.balance(1), 0)

        # Approve via inline button callback
        bot.process({
            'update_id': 2,
            'callback_query': {
                'id': 'cb1',
                'data': 'yes:' + pending['id'],
                'from': {'id': 1},
                'message': {'chat': {'id': -100}},
            },
        })
        self.assertEqual(self.service.balance(1), 300000)
        self.assertEqual(self.service.balance(2), 150000)
        self.assertTrue(self.db.recent_messages(-100))
        agent_logs = self.db.recent_agent_logs(10)
        self.assertTrue(any(row['event'] == 'AGENT_REPLY' for row in agent_logs))

    def test_langgraph_agent_unwraps_accidental_json_and_logs_skipped(self):
        from langchain_core.messages import AIMessage
        from app.config import Config

        class ScriptedLangGraphLLM:
            def __init__(self, responses):
                self.responses = list(responses)
                self.calls = 0

            def bind_tools(self, tools):
                return self

            def invoke(self, messages):
                idx = min(self.calls, len(self.responses) - 1)
                self.calls += 1
                return self.responses[idx]

        class LangGraphDriver:
            use_langgraph = True

            def __init__(self, responses):
                self.llm_override = ScriptedLangGraphLLM(responses)

        config = Config(
            token='t',
            admin_group=-100,
            admins=frozenset({1}),
            ai_url='https://example.invalid',
            ai_key='k',
            ai_model='m',
            database=Path(self.temp.name) / 'contracts.sqlite3',
        )
        driver = LangGraphDriver([
            AIMessage(content='{"action":"clarify", "args":{}, "question":"سلام! چطور کمکتون کنم؟"}'),
        ])
        bot = Bot(self.db, config, driver)
        # Unaddressed group message is logged as SKIPPED_NOT_ADDRESSED
        bot.process({
            'update_id': 10,
            'message': {
                'message_id': 11,
                'chat': {'id': -100, 'type': 'group'},
                'from': {'id': 1, 'first_name': 'دانیال'},
                'text': 'سلام بچه ها',
            },
        })
        logs = self.db.recent_agent_logs(10)
        self.assertTrue(any(r['event'] == 'SKIPPED_NOT_ADDRESSED' for r in logs))

        # Addressed group message unwraps accidental JSON into natural text
        bot.process({
            'update_id': 11,
            'message': {
                'message_id': 12,
                'chat': {'id': -100, 'type': 'group'},
                'from': {'id': 1, 'first_name': 'دانیال'},
                'text': 'مسئول سلام',
            },
        })
        outbox = self.db.all('SELECT * FROM outbox ORDER BY id DESC')
        self.assertIn('سلام! چطور کمکتون کنم؟', outbox[0]['payload'])
        self.assertNotIn('"action"', outbox[0]['payload'])


if __name__ == '__main__':
    unittest.main()