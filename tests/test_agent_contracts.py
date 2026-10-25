import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.ai_context import AIContextBuilder
from app.agent import AGENT_SYSTEM_PROMPT, SansAgent
from app.clients import AIClient
from app.config import Config
from app.content import DEFAULTS, SYSTEM_PROMPT
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

    def test_agent_snapshot_withholds_complete_player_directory(self):
        self.apply('new_player', name='علی رضایی')
        self.apply('new_player', name='بازیکن مخفی')
        with self.db.conn:
            self.db.execute("UPDATE players SET note='یادداشت خصوصی' WHERE id=2")
            self.service.bind(1, 10, 1)
        agent = SansAgent(SimpleNamespace(db=self.db, service=self.service, content_defaults={}))

        for admin, user_id in ((True, 1), (False, 10)):
            with self.subTest(admin=admin):
                raw = agent._build_live_snapshot(10, user_id, admin, {'first_name': 'تست'})
                snapshot = json.loads(raw)
                self.assertEqual(snapshot['players_count'], 2)
                self.assertNotIn('players_summary', snapshot)
                self.assertNotIn('بازیکن مخفی', raw)
                self.assertNotIn('یادداشت خصوصی', raw)
                self.assertNotIn('balance', raw)

        own = json.loads(agent._build_live_snapshot(10, 10, False, None))
        self.assertEqual(own['caller']['linked_player']['name'], 'علی رضایی')

        effects = {}
        tools = {t.name: t for t in agent._make_tools(10, 1, True, None, effects)}
        self.assertIn('بازیکن مخفی', tools['query_database'].invoke({'target': 'players'}))

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
                        'id': 'call_0',
                        'name': 'manage_records',
                        'args': {
                            'operation': 'upsert_players',
                            'data_json': json.dumps({
                                'players': [{'name': 'علی رضایی'}, {'name': 'رضا محمدی'}],
                            }, ensure_ascii=False),
                        },
                    },
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

    def test_langgraph_rejects_ambiguous_setting_acknowledgement(self):
        from langchain_core.messages import AIMessage
        from app.config import Config

        class ScriptedLangGraphLLM:
            def __init__(self):
                self.calls = 0

            def bind_tools(self, tools):
                return self

            def invoke(self, messages):
                self.calls += 1
                if self.calls == 1:
                    return AIMessage(content='', tool_calls=[{
                        'id': 'settings-1',
                        'name': 'manage_records',
                        'args': {
                            'operation': 'update_settings',
                            'data_json': json.dumps({'card': '6037997512345678'}),
                        },
                    }])
                return AIMessage(content='مقدار کارت باید در همین پیام نوشته شود.')

        class LangGraphDriver:
            use_langgraph = True

            def __init__(self):
                self.llm_override = ScriptedLangGraphLLM()

        config = Config(
            'fake-token', -100, frozenset({1}), 'https://example.invalid',
            'fake-key', 'fake-model', Path(self.temp.name) / 'contracts.sqlite3',
        )
        bot = Bot(self.db, config, LangGraphDriver())
        bot.process({
            'update_id': 20,
            'message': {
                'message_id': 20,
                'chat': {'id': -100, 'type': 'group'},
                'from': {'id': 1},
                'text': 'مسئول آره انجامش بده',
            },
        })

        self.assertEqual(self.db.setting('card'), '')
        self.assertIn('همین پیام', self.db.all("SELECT payload FROM outbox")[-1]['payload'])

    def test_langgraph_update_settings_creates_approval_proposal(self):
        from langchain_core.messages import AIMessage
        from app.config import Config

        class ScriptedLangGraphLLM:
            def __init__(self):
                self.calls = 0

            def bind_tools(self, tools):
                return self

            def invoke(self, messages):
                self.calls += 1
                if self.calls == 1:
                    return AIMessage(content='', tool_calls=[{
                        'id': 'settings-1',
                        'name': 'manage_records',
                        'args': {
                            'operation': 'update_settings',
                            'data_json': json.dumps({
                                'card': '6037997512345678',
                                'approval_required': True,
                            }),
                        },
                    }])
                return AIMessage(content='پیشنهاد تغییر تنظیمات آماده شد؛ لطفاً تأیید کنید.')

        class LangGraphDriver:
            use_langgraph = True

            def __init__(self):
                self.llm_override = ScriptedLangGraphLLM()

        config = Config(
            'fake-token', -100, frozenset({1}), 'https://example.invalid',
            'fake-key', 'fake-model', Path(self.temp.name) / 'contracts.sqlite3',
        )
        bot = Bot(self.db, config, LangGraphDriver())
        with patch.object(self.db, 'set_setting', wraps=self.db.set_setting) as set_setting:
            bot.process({
                'update_id': 30,
                'message': {
                    'message_id': 30,
                    'chat': {'id': -100, 'type': 'group'},
                    'from': {'id': 1},
                    'text': 'مسئول شماره کارت 6037997512345678 را ثبت کن و بررسی رسید را فعال کن',
                },
            })

        # The model must never write the setting directly.
        self.assertEqual(self.db.setting('card'), '')
        self.assertNotIn('card', [call.args[0] for call in set_setting.call_args_list if call.args])

        pending = self.db.all("SELECT * FROM pending WHERE kind='commands' AND status='pending'")
        self.assertEqual(len(pending), 1)
        commands = json.loads(pending[0]['payload'])
        self.assertEqual([c['args']['key'] for c in commands], ['card', 'approval_required'])
        self.assertEqual(self.db.setting('approval_required'), 'true')

        payload = json.loads(self.db.all(
            "SELECT payload FROM outbox WHERE method='sendMessage' ORDER BY id")[-1]['payload'])
        self.assertEqual(payload['reply_markup']['inline_keyboard'], [[
            {'text': 'تأیید', 'callback_data': 'yes:' + pending[0]['id']},
            {'text': 'رد', 'callback_data': 'no:' + pending[0]['id']},
        ]])
        self.assertIn('شماره کارت', payload['text'])
        self.assertIn('بررسی رسید توسط ادمین', payload['text'])
        self.assertIn(pending[0]['id'], payload['text'])

    def test_langgraph_setting_proposal_applies_only_on_admin_approval(self):
        from langchain_core.messages import AIMessage
        from app.config import Config

        class ScriptedLangGraphLLM:
            def __init__(self):
                self.calls = 0

            def bind_tools(self, tools):
                return self

            def invoke(self, messages):
                self.calls += 1
                if self.calls == 1:
                    return AIMessage(content='', tool_calls=[{
                        'id': 'settings-1',
                        'name': 'manage_records',
                        'args': {
                            'operation': 'update_settings',
                            'data_json': json.dumps({'card': '6037997512345678'}),
                        },
                    }])
                return AIMessage(content='منتظر تأیید ادمین هستم.')

        class LangGraphDriver:
            use_langgraph = True

            def __init__(self):
                self.llm_override = ScriptedLangGraphLLM()

        config = Config(
            'fake-token', -100, frozenset({1}), 'https://example.invalid',
            'fake-key', 'fake-model', Path(self.temp.name) / 'contracts.sqlite3',
        )
        bot = Bot(self.db, config, LangGraphDriver())
        bot.process({
            'update_id': 31,
            'message': {
                'message_id': 31,
                'chat': {'id': -100, 'type': 'group'},
                'from': {'id': 1},
                'text': 'مسئول شماره کارت 6037997512345678 را ثبت کن',
            },
        })
        ident = self.db.one("SELECT id FROM pending WHERE kind='commands' AND status='pending'")['id']
        self.assertEqual(self.db.setting('card'), '')

        bot.process({
            'update_id': 32,
            'callback_query': {
                'id': 'cb-settings',
                'data': 'yes:' + ident,
                'from': {'id': 1},
                'message': {'chat': {'id': -100}},
            },
        })
        self.assertEqual(self.db.setting('card'), '6037997512345678')
        self.assertEqual(self.db.one('SELECT status FROM pending WHERE id=?', (ident,))['status'], 'approved')

    def test_config_parses_fallback_models(self):
        from app.config import Config, parse_fallback_models

        self.assertEqual(parse_fallback_models(' a , b ,a, , c ', 'a'), ('b', 'c'))
        self.assertEqual(parse_fallback_models('', 'primary'), ())
        self.assertEqual(parse_fallback_models(None), ())

        env = {
            'BALE_BOT_TOKEN': 'test', 'ADMIN_GROUP_ID': '-100',
            'AI_BASE_URL': 'https://example.com/v1', 'AI_API_KEY': 'test',
            'AI_MODEL': 'primary-model',
            'AI_FALLBACK_MODEL': ' fallback-one , primary-model,fallback-two ',
        }
        with patch.dict(os.environ, env, clear=True), patch('app.config.load_env'):
            config = Config.from_env()
        self.assertEqual(config.ai_model, 'primary-model')
        self.assertEqual(config.ai_fallback_models, ('fallback-one', 'fallback-two'))

        with patch.dict(os.environ, {**env, 'AI_FALLBACK_MODELS': 'plural-model'},
                        clear=True), patch('app.config.load_env'):
            config = Config.from_env()
        self.assertEqual(config.ai_fallback_models, ('plural-model',))

    def test_legacy_ai_client_falls_back_to_secondary_model(self):
        from app.clients import AIClient, APIError
        from app.config import Config

        config = Config(
            'fake-token', -100, frozenset({1}), 'https://example.invalid/v1',
            'fake-key', 'primary-model', Path(self.temp.name) / 'contracts.sqlite3',
            ai_fallback_models=('fallback-model',),
        )
        seen = []

        def fake_post(url, payload, headers=None, timeout=45):
            seen.append(payload['model'])
            if payload['model'] == 'primary-model':
                raise APIError('HTTP 402', 60)
            return {'choices': [{'message': {'content': '{"action":"help","args":{}}'}}]}

        with patch('app.clients.post_json', side_effect=fake_post):
            self.assertEqual(AIClient(config).parse('سلام', [], {'role': 'player'}),
                             {'action': 'help', 'args': {}})
        self.assertEqual(seen, ['primary-model', 'fallback-model'])

    def test_legacy_ai_client_raises_when_every_model_fails(self):
        from app.clients import AIClient, APIError
        from app.config import Config

        config = Config(
            'fake-token', -100, frozenset({1}), 'https://example.invalid/v1',
            'fake-key', 'primary-model', Path(self.temp.name) / 'contracts.sqlite3',
            ai_fallback_models=('fallback-model',),
        )
        with patch('app.clients.post_json', side_effect=APIError('HTTP 402', 60)) as post:
            with self.assertRaises(APIError):
                AIClient(config).parse('سلام', [], {})
        self.assertEqual(post.call_count, 2)

    def test_langgraph_falls_back_to_secondary_model(self):
        from langchain_core.messages import AIMessage
        from app.config import Config
        from app import agent as agent_module

        used = []

        class ScriptedModel:
            """Fails for the primary model and answers for the fallback.

            The fallback first asks for a tool call so the graph invokes the
            model a second time in the same turn.
            """

            def __init__(self, **kwargs):
                self.model = kwargs.get('model')

            def bind_tools(self, tools):
                return self

            def invoke(self, messages):
                used.append(self.model)
                if self.model == 'primary-model':
                    raise RuntimeError('HTTP 402')
                if not any(getattr(message, 'type', '') == 'tool' for message in messages):
                    return AIMessage(content='', tool_calls=[{
                        'id': 'q1', 'name': 'query_database', 'args': {'target': 'overview'},
                    }])
                return AIMessage(content='پاسخ از مدل جایگزین')

        def factory(**kwargs):
            return ScriptedModel(**kwargs)

        config = Config(
            'fake-token', -100, frozenset({1}), 'https://example.invalid/v1',
            'fake-key', 'primary-model', Path(self.temp.name) / 'contracts.sqlite3',
            ai_fallback_models=('fallback-model',),
        )
        bot = Bot(self.db, config, SimpleNamespace(use_langgraph=True))
        # The provider class name is resolved from the module so the test does
        # not depend on how the import is spelled.
        chat_model = next(name for name in dir(agent_module)
                          if name.startswith('Chat') and name.endswith('AI'))
        with patch.object(agent_module, chat_model, side_effect=factory):
            bot.process({
                'update_id': 40,
                'message': {
                    'message_id': 40,
                    'chat': {'id': -100, 'type': 'group'},
                    'from': {'id': 1},
                    'text': 'مسئول گزارش مالی را بگو',
                },
            })

        # The failed primary model is not retried within the same turn.
        self.assertEqual(used, ['primary-model', 'fallback-model', 'fallback-model'])
        payloads = [json.loads(row['payload']) for row in self.db.all(
            "SELECT payload FROM outbox WHERE method='sendMessage' ORDER BY id")]
        self.assertIn('پاسخ از مدل جایگزین', payloads[-1]['text'])
        events = [row['event'] for row in self.db.recent_agent_logs(30)]
        self.assertIn('LLM_FALLBACK', events)

    def test_fallback_reuses_the_same_provider_configuration(self):
        """SPEC-006 AC-005: only the model name changes on the fallback path."""
        from langchain_core.messages import AIMessage
        from app.config import Config
        from app import agent as agent_module

        created = []

        class RecordingModel:
            def __init__(self, **kwargs):
                created.append(kwargs)
                self.model = kwargs.get('model')

            def bind_tools(self, tools):
                return self

            def invoke(self, messages):
                if self.model == 'primary-model':
                    raise RuntimeError('HTTP 402')
                return AIMessage(content='پاسخ از مدل جایگزین')

        tables_before = {row['name'] for row in self.db.all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        config = Config('fake-token', -100, frozenset({1}), 'https://example.invalid/v1',
                        'fake-key', 'primary-model',
                        Path(self.temp.name) / 'contracts.sqlite3',
                        ai_fallback_models=('fallback-model', 'third-model'))
        bot = Bot(self.db, config, SimpleNamespace(use_langgraph=True))
        chat_model = next(name for name in dir(agent_module)
                          if name.startswith('Chat') and name.endswith('AI'))
        with patch.object(agent_module, chat_model, side_effect=lambda **kwargs: RecordingModel(**kwargs)):
            bot.process({
                'update_id': 42,
                'message': {'message_id': 42, 'chat': {'id': -100, 'type': 'group'},
                            'from': {'id': 1}, 'text': 'مسئول گزارش مالی را بگو'},
            })

        self.assertEqual([item['model'] for item in created],
                         ['primary-model', 'fallback-model', 'third-model'])
        shared = {key: item[key] for key in ('api_key', 'base_url', 'temperature', 'timeout', 'max_retries')
                  for item in created[:1]}
        for item in created:
            for key, value in shared.items():
                self.assertEqual(item[key], value, key)
        self.assertEqual(set(shared), {'api_key', 'base_url', 'temperature', 'timeout', 'max_retries'})
        tables_after = {row['name'] for row in self.db.all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(tables_before, tables_after)

    def test_langgraph_raises_when_every_model_fails(self):
        from app.config import Config
        from app import agent as agent_module

        class FailingModel:
            def __init__(self, **kwargs):
                self.model = kwargs.get('model')

            def bind_tools(self, tools):
                return self

            def invoke(self, messages):
                raise RuntimeError('HTTP 402')

        config = Config(
            'fake-token', -100, frozenset({1}), 'https://example.invalid/v1',
            'fake-key', 'primary-model', Path(self.temp.name) / 'contracts.sqlite3',
            ai_fallback_models=('fallback-model',),
        )
        bot = Bot(self.db, config, SimpleNamespace(use_langgraph=True))
        chat_model = next(name for name in dir(agent_module)
                          if name.startswith('Chat') and name.endswith('AI'))
        with patch.object(agent_module, chat_model, side_effect=lambda **kwargs: FailingModel()):
            bot.process({
                'update_id': 41,
                'message': {
                    'message_id': 41,
                    'chat': {'id': -100, 'type': 'group'},
                    'from': {'id': 1},
                    'text': 'مسئول گزارش مالی را بگو',
                },
            })
        payloads = [json.loads(row['payload']) for row in self.db.all(
            "SELECT payload FROM outbox WHERE method='sendMessage' ORDER BY id")]
        self.assertIn('سرویس هوش مصنوعی', payloads[-1]['text'])
        self.assertEqual(self.db.setting('card'), '')


    def test_manage_records_is_strict_about_players_and_keys(self):
        from app.config import Config

        config = Config('t', -100, frozenset({1}), 'https://example.invalid/v1', 'k', 'm',
                        Path(self.temp.name) / 'contracts.sqlite3')
        bot = Bot(self.db, config, SimpleNamespace(use_langgraph=True))
        effects = {}
        tools = {t.name: t for t in bot.agent._make_tools(
            -100, 1, True, {'first_name': 'ادمین'}, effects)}
        manage = tools['manage_records']
        self.apply('new_session', title='سانس', when_text='فردا ساعت ۲۱')

        # An unknown name is refused and no player is created as a side effect.
        result = manage.invoke({'operation': 'set_attendance', 'data_json': json.dumps(
            {'session': 1, 'attendees': ['علی رضایی']}, ensure_ascii=False)})
        self.assertIn('در فهرست بازیکنان نیست', result)
        self.assertFalse(self.db.all('SELECT * FROM players'))
        self.assertFalse(self.db.all('SELECT * FROM attendance'))

        # Unknown keys are rejected instead of silently ignored.
        result = manage.invoke({'operation': 'set_attendance', 'data_json': json.dumps(
            {'session': 1, 'players_list': ['علی رضایی']}, ensure_ascii=False)})
        self.assertIn('کلید ناشناخته', result)

        # A partial name never becomes a second player.
        result = manage.invoke({'operation': 'upsert_players', 'data_json': json.dumps(
            {'name': 'علی رضایی'}, ensure_ascii=False)})
        self.assertIn('#1', result)
        result = manage.invoke({'operation': 'upsert_players', 'data_json': json.dumps(
            {'name': 'علی'}, ensure_ascii=False)})
        self.assertIn('مشابه', result)
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM players')['n'], 1)

        # The attendees alias works once the player exists.
        result = manage.invoke({'operation': 'set_attendance', 'data_json': json.dumps(
            {'session': 1, 'attendees': [{'name': 'علی رضایی', 'guests': 1}]}, ensure_ascii=False)})
        self.assertIn('ثبت شدند', result)
        self.assertEqual(self.db.one('SELECT guests FROM attendance')['guests'], 1)

        # date is accepted as an alias for when_text.
        result = manage.invoke({'operation': 'save_session', 'data_json': json.dumps(
            {'title': 'سانس قدیمی', 'date': 'پنجشنبه ساعت ۲۱'}, ensure_ascii=False)})
        self.assertIn('ذخیره شد', result)
        self.assertEqual(self.db.one('SELECT when_text FROM sessions WHERE title=?',
                                     ('سانس قدیمی',))['when_text'], 'پنجشنبه ساعت ۲۱')

    def test_set_attendance_takes_optional_guest_names(self):
        """SPEC-024: names are descriptive; an impossible list fails closed."""
        from app.config import Config

        config = Config('t', -100, frozenset({1}), 'https://example.invalid/v1', 'k', 'm',
                        Path(self.temp.name) / 'contracts.sqlite3')
        bot = Bot(self.db, config, SimpleNamespace(use_langgraph=True))
        tools = {t.name: t for t in bot.agent._make_tools(-100, 1, True, {'first_name': 'ادمین'}, {})}
        manage = tools['manage_records']
        self.apply('new_session', title='سانس', when_text='فردا ساعت ۲۱')
        manage.invoke({'operation': 'upsert_players', 'data_json': json.dumps(
            {'name': 'علی رضایی'}, ensure_ascii=False)})
        player = self.service.player('علی رضایی')['id']

        # More names than guests is a contradiction, not a silent truncation.
        result = manage.invoke({'operation': 'set_attendance', 'data_json': json.dumps(
            {'session': 1, 'attendees': [{'name': 'علی رضایی', 'guests': 1,
                                          'guest_names': ['رضا', 'سارا']}]}, ensure_ascii=False)})
        self.assertIn('بیشتر است', result)
        self.assertFalse(self.db.all('SELECT * FROM attendance'))

        result = manage.invoke({'operation': 'set_attendance', 'data_json': json.dumps(
            {'session': 1, 'attendees': [{'name': 'علی رضایی', 'guests': 2,
                                          'guest_names': ['رضا', 'سارا']}]}, ensure_ascii=False)})
        self.assertIn('ثبت شدند', result)
        self.assertEqual(self.service.guest_names(1, player), ['رضا', 'سارا'])
        self.assertEqual(self.db.one('SELECT guests FROM attendance')['guests'], 2)

    def test_merge_tool_only_proposes_and_duplicates_are_admin_only(self):
        from app.config import Config

        config = Config('t', -100, frozenset({1}), 'https://example.invalid/v1', 'k', 'm',
                        Path(self.temp.name) / 'contracts.sqlite3')
        bot = Bot(self.db, config, SimpleNamespace(use_langgraph=True))
        self.apply('new_player', name='سید امین هاشمی')
        self.apply('new_player', name='هاشمی')
        self.apply('new_session', title='سانس', when_text='فردا ساعت ۲۱')
        self.apply('attendance', session=1, player=1)

        effects = {}
        tools = {t.name: t for t in bot.agent._make_tools(
            -100, 1, True, {'first_name': 'ادمین'}, effects)}
        result = tools['manage_records'].invoke({'operation': 'merge_player', 'data_json': json.dumps(
            {'source': '#2', 'target': '#1'}, ensure_ascii=False)})
        self.assertIn('پیشنهاد ادغام', result)
        self.assertTrue(effects.get('markup'))
        self.assertIsNotNone(self.db.one('SELECT 1 FROM players WHERE id=2'))

        report = tools['query_database'].invoke({'target': 'duplicate_players'})
        self.assertIn('هاشمی', report)
        self.assertIn('/merge', report)

        # A non-admin sees no player directory analysis.
        player_tools = {t.name: t for t in bot.agent._make_tools(
            -100, 99, False, {'first_name': 'بازیکن'}, {})}
        self.assertIn('فقط ادمین', player_tools['query_database'].invoke({'target': 'duplicate_players'}))
        self.assertIn('فقط برای ادمین', player_tools['manage_records'].invoke(
            {'operation': 'merge_player', 'data_json': json.dumps({'source': 2, 'target': 1})}))


    def test_agent_logs_every_step_and_excludes_the_legacy_prompt(self):
        """SPEC-003 AC-005: message, skipped reason, tool call, and reply are logged."""
        from langchain_core.messages import AIMessage
        from app.config import Config

        class ScriptedLangGraphLLM:
            def __init__(self):
                self.calls = 0

            def bind_tools(self, tools):
                return self

            def invoke(self, messages):
                self.calls += 1
                if self.calls == 1:
                    return AIMessage(content='', tool_calls=[{
                        'id': 'read-1', 'name': 'query_database',
                        'args': {'target': 'players'},
                    }])
                return AIMessage(content='فهرست بازیکن‌ها را خواندم.')

        class LangGraphDriver:
            use_langgraph = True

            def __init__(self):
                self.llm_override = ScriptedLangGraphLLM()

        self.apply('new_player', name='علی رضایی')
        config = Config('fake-token', -100, frozenset({1}), 'https://example.invalid',
                        'fake-key', 'fake-model', Path(self.temp.name) / 'contracts.sqlite3')
        bot = Bot(self.db, config, LangGraphDriver())

        bot.process({
            'update_id': 71,
            'message': {'message_id': 71, 'chat': {'id': -100, 'type': 'group'},
                        'from': {'id': 1, 'first_name': 'ادمین'},
                        'text': 'مسئول چند بازیکن داریم؟'},
        })

        events = [row['event'] for row in self.db.recent_agent_logs(30)]
        for expected in ('INCOMING', 'LLM_TOOL_CALL', 'TOOL_RESULT', 'AGENT_REPLY'):
            self.assertIn(expected, events)
        # The legacy JSON-only prompt must never reach the agent.
        self.assertIn('فقط یک JSON بدون markdown بده', SYSTEM_PROMPT)
        self.assertNotIn('فقط یک JSON بدون markdown بده', AGENT_SYSTEM_PROMPT)
        self.assertTrue((Path(self.temp.name) / 'logs' / 'agent.log').is_file())

    def test_langgraph_multi_player_settle_creates_one_card(self):
        from langchain_core.messages import AIMessage
        from app.config import Config

        class ScriptedLangGraphLLM:
            def __init__(self):
                self.calls = 0

            def bind_tools(self, tools):
                return self

            def invoke(self, messages):
                self.calls += 1
                if self.calls == 1:
                    return AIMessage(content='', tool_calls=[{
                        'id': 'settle-1',
                        'name': 'propose_financial_change',
                        'args': {'action': 'settle', 'players': 'علی رضایی، رضا محمدی'},
                    }])
                return AIMessage(content='یک کارت تسویه برای هر دو نفر ساختم.')

        class LangGraphDriver:
            use_langgraph = True

            def __init__(self):
                self.llm_override = ScriptedLangGraphLLM()

        self.apply('new_player', name='علی رضایی')
        self.apply('new_player', name='رضا محمدی')
        self.apply('adjustment', player=1, amount=200000, note='بدهی قبلی')
        self.apply('adjustment', player=2, amount=300000, note='بدهی قبلی')
        config = Config(
            'fake-token', -100, frozenset({1}), 'https://example.invalid',
            'fake-key', 'fake-model', Path(self.temp.name) / 'contracts.sqlite3',
        )
        bot = Bot(self.db, config, LangGraphDriver())

        bot.process({
            'update_id': 41,
            'message': {
                'message_id': 41,
                'chat': {'id': -100, 'type': 'group'},
                'from': {'id': 1},
                'text': 'مسئول بدهی همهٔ بازیکن‌ها رو صاف کن',
            },
        })

        pending = self.db.all("SELECT * FROM pending WHERE kind='commands' AND status='pending'")
        self.assertEqual(len(pending), 1)
        commands = json.loads(pending[0]['payload'])
        self.assertEqual([(c['action'], c['args']['player']) for c in commands],
                         [('settle', 1), ('settle', 2)])
        # Creating the card must not move any balance.
        self.assertEqual(self.service.balance(1), 200000)
        self.assertEqual(self.service.balance(2), 300000)

        with self.db.conn:
            bot.confirm(pending[0]['id'], True, 1)

        self.assertEqual(self.service.balance(1), 0)
        self.assertEqual(self.service.balance(2), 0)


class BatchFinancialToolTests(unittest.TestCase):
    """SPEC-012: one card for a multi-player balance correction."""

    def setUp(self):
        from app.config import Config

        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'batch.sqlite3'
        self.db = Database(self.path)
        self.service = Service(self.db)
        self.config = Config('fake-token', -100, frozenset({1}), 'https://example.invalid',
                             'fake-key', 'fake-model', self.path)
        self.bot = Bot(self.db, self.config, SimpleNamespace(use_langgraph=False))
        self.source = 0
        self.apply('new_player', name='علی رضایی')
        self.apply('new_player', name='رضا محمدی')
        self.apply('new_player', name='سعید مرادی')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def apply(self, action, **args):
        self.source += 1
        with self.db.conn:
            self.service.apply(self.service.prepare(action, args), 1, f'batch-test:{self.source}')

    def tools(self, admin=True, user=1):
        effects = {}
        agent = SansAgent(self.bot)
        return {t.name: t for t in agent._make_tools(-100, user, admin, None, effects)}, effects

    def call(self, **args):
        tools, effects = self.tools()
        return tools['propose_financial_change'].invoke(args), effects

    def cards(self):
        return self.db.all("SELECT * FROM pending WHERE kind='commands' AND status='pending'")

    def commands(self):
        cards = self.cards()
        self.assertEqual(len(cards), 1)
        return [(c['action'], c['args']['player'], c['args'].get('amount'))
                for c in json.loads(cards[0]['payload'])]

    def test_adjustment_with_shared_amount_creates_one_card(self):
        reply, _ = self.call(action='adjustment', players='علی رضایی، رضا محمدی', amount=50000)

        self.assertEqual(self.commands(),
                         [('adjustment', 1, 50000), ('adjustment', 2, 50000)])
        self.assertIn('2 نفر', reply)
        # Nothing moves before the admin presses the button.
        self.assertEqual([self.service.balance(p) for p in (1, 2, 3)], [0, 0, 0])
    def test_adjustment_with_entries_uses_per_person_amounts(self):
        entries = json.dumps([{'player': 'علی رضایی', 'amount': -150000},
                              {'player': 'رضا محمدی', 'amount': 50000}], ensure_ascii=False)
        self.call(action='adjustment', entries=entries)

        self.assertEqual(self.commands(),
                         [('adjustment', 1, -150000), ('adjustment', 2, 50000)])
        with self.db.conn:
            self.bot.confirm(self.cards()[0]['id'], True, 1)
        self.assertEqual([self.service.balance(p) for p in (1, 2, 3)],
                         [-150000, 50000, 0])

    def test_entries_are_mutually_exclusive_with_players_and_amount(self):
        entries = json.dumps([{'player': 'علی رضایی', 'amount': 1000}], ensure_ascii=False)
        for args in ({'entries': entries, 'players': 'رضا محمدی'},
                     {'entries': entries, 'amount': 1000}):
            with self.subTest(args=args):
                reply, _ = self.call(action='adjustment', **args)
                self.assertIn('ترکیب', reply)
                self.assertEqual(self.cards(), [])

    def test_invalid_entries_fail_closed(self):
        invalid = (
            'not-json',
            '[]',
            '{"player":"علی رضایی","amount":1000}',
            json.dumps([{'player': 'علی رضایی', 'amount': 0}], ensure_ascii=False),
            json.dumps([{'player': 'علی رضایی', 'amount': 'abc'}], ensure_ascii=False),
            json.dumps([{'player': 'علی رضایی', 'amount': 1000, 'extra': 1}], ensure_ascii=False),
            json.dumps([{'amount': 1000}], ensure_ascii=False),
            json.dumps([{'player': f'بازیکن {n}', 'amount': 1000} for n in range(31)],
                       ensure_ascii=False),
        )
        for entries in invalid:
            with self.subTest(entries=entries[:40]):
                reply, _ = self.call(action='adjustment', entries=entries)
                self.assertTrue(reply)
                self.assertEqual(self.cards(), [])

    def test_settle_skips_settled_players_and_still_builds_one_card(self):
        self.apply('adjustment', player=1, amount=200000, note='بدهی قبلی')

        reply, _ = self.call(action='settle', players='علی رضایی، رضا محمدی')

        self.assertEqual(self.commands(), [('settle', 1, 200000)])
        self.assertIn('رضا محمدی', reply)
        self.assertIn('بدون بدهی', reply)
        self.assertEqual(self.service.balance(2), 0)

    def test_settle_without_positive_balance_creates_no_card(self):
        reply, _ = self.call(action='settle', players='علی رضایی، رضا محمدی')

        self.assertIn('تسویه', reply)
        self.assertEqual(self.cards(), [])

    def test_batch_card_moves_nothing_until_one_approval(self):
        self.call(action='adjustment', players='علی رضایی، رضا محمدی، سعید مرادی',
                  amount=100000)
        self.assertEqual([self.service.balance(p) for p in (1, 2, 3)], [0, 0, 0])

        ident = self.cards()[0]['id']
        with self.db.conn:
            self.bot.confirm(ident, True, 1)

        self.assertEqual([self.service.balance(p) for p in (1, 2, 3)],
                         [100000, 100000, 100000])
        self.assertEqual(self.db.one('SELECT status FROM pending WHERE id=?', (ident,))['status'],
                         'approved')

    def test_unknown_player_fails_closed_without_a_card(self):
        reply, _ = self.call(action='adjustment', players='علی رضایی، بازیکن ناشناس',
                             amount=100000)

        self.assertIn('پیدا نشد', reply)
        self.assertEqual(self.cards(), [])

    def test_settle_rejects_entries_because_amount_is_derived(self):
        entries = json.dumps([{'player': 'علی رضایی', 'amount': 1000}], ensure_ascii=False)
        reply, _ = self.call(action='settle', entries=entries)
        self.assertIn('entries', reply)
        self.assertEqual(self.cards(), [])


class ExpireProposalsToolTests(unittest.TestCase):
    """SPEC-015: the assistant closes cards instead of saying it lacks a tool."""

    def setUp(self):
        from app.config import Config

        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'expire.sqlite3'
        self.db = Database(self.path)
        self.service = Service(self.db)
        config = Config('t', -100, frozenset({1}), 'https://example.invalid/v1', 'k', 'm', self.path)
        self.bot = Bot(self.db, config, SimpleNamespace(use_langgraph=True))
        with self.db.conn:
            for source, action, args in (
                ('expire:1', 'new_player', {'name': 'علی رضایی'}),
                ('expire:2', 'adjustment', {'player': 1, 'amount': 200000, 'note': 'بدهی قبلی'}),
                ('expire:3', 'new_session', {'title': 'سانس', 'when_text': 'فردا ساعت ۲۱'}),
            ):
                self.service.apply(self.service.prepare(action, args), 1, source)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def tools(self, admin=True, user=1):
        return {tool.name: tool for tool in self.bot.agent._make_tools(-100, user, admin, None, {})}

    def card(self):
        with self.db.conn:
            _, markup = self.bot.propose_commands(
                [self.service.prepare('settle', {'player': 1})], 1, -100)
        return markup[0][0]['callback_data'].split(':', 1)[1]

    def status(self, ident):
        return self.db.one('SELECT status FROM pending WHERE id=?', (ident,))['status']

    def test_admin_closes_cards_through_the_tool(self):
        ident = self.card()

        reply = self.tools()['manage_records'].invoke(
            {'operation': 'expire_proposals', 'data_json': json.dumps({'older_than_hours': 0})})

        self.assertIn('1 کارت پیشنهاد بسته شد', reply)
        self.assertIn('هیچ حساب، سانس یا رسیدی تغییر نکرد', reply)
        self.assertEqual(self.status(ident), 'expired')
        self.assertEqual(self.service.balance(1), 200000)

    def test_tool_defaults_to_the_ttl(self):
        ident = self.card()

        reply = self.tools()['manage_records'].invoke(
            {'operation': 'expire_proposals', 'data_json': '{}'})

        self.assertIn('کارت پیشنهادی برای بستن نبود', reply)
        self.assertEqual(self.status(ident), 'pending')

    def test_non_admin_cannot_close_cards(self):
        ident = self.card()

        reply = self.tools(admin=False, user=10)['manage_records'].invoke(
            {'operation': 'expire_proposals', 'data_json': json.dumps({'older_than_hours': 0})})

        self.assertIn('فقط برای ادمین', reply)
        self.assertEqual(self.status(ident), 'pending')

    def test_unknown_keys_and_bad_hours_are_refused(self):
        ident = self.card()
        manage = self.tools()['manage_records']

        self.assertIn('کلید ناشناخته', manage.invoke(
            {'operation': 'expire_proposals', 'data_json': json.dumps({'everything': True})}))
        self.assertIn('عدد صحیح ساعت', manage.invoke(
            {'operation': 'expire_proposals', 'data_json': json.dumps({'older_than_hours': 'soon'})}))
        self.assertEqual(self.status(ident), 'pending')


class PlayerPhoneContextTests(unittest.TestCase):
    """SPEC-016: a shared phone number never reaches the model."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'phone.sqlite3')
        self.service = Service(self.db)
        with self.db.conn:
            player = self.service.ensure_player('علی رضایی', actor='test')
            self.player_id = player['id']
            self.service.bind(self.player_id, 10, 'test')
            self.service.set_player_phone(self.player_id, '09121234567', 'test')
        self.bot = Bot(self.db, SimpleNamespace(admin_group=-100, bot_name='sans',
                                               reminder_hours=0, admins=frozenset({1})), None)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_phone_is_not_part_of_the_model_context(self):
        self.assertEqual(self.db.one('SELECT phone FROM players WHERE id=?', (self.player_id,))['phone'],
                         '+989121234567')

        context = AIContextBuilder(self.db, self.service).build(10, 10, False)
        self.assertEqual(context['self'], {'id': self.player_id, 'name': 'علی رضایی'})
        snapshot = SansAgent(self.bot)._build_live_snapshot(10, 10, False, None)

        for payload in (context, snapshot):
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn('+989121234567', serialized)
            self.assertNotIn('9121234567', serialized)

    def test_admin_player_listing_exposes_the_number_to_admins_only(self):
        effects = {}
        admin_tools = {tool.name: tool for tool in SansAgent(self.bot)._make_tools(
            -100, 1, True, None, effects)}
        player_tools = {tool.name: tool for tool in SansAgent(self.bot)._make_tools(
            10, 10, False, {}, {})}

        self.assertIn('+989121234567', admin_tools['query_database'].invoke(
            {'target': 'players'}))
        self.assertNotIn('+989121234567', player_tools['query_database'].invoke(
            {'target': 'players'}))


class SessionDefaultsContextTests(unittest.TestCase):
    """SPEC-017: the model receives the effective defaults, not file contents."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'defaults.sqlite3')
        self.service = Service(self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_context_reports_effective_defaults(self):
        with self.db.conn:
            for key, value in (('default_location', 'سالن الغدیر'),
                               ('default_venue_cost', '3000000'),
                               ('default_person_fee', '200000')):
                self.service.apply(self.service.prepare('settings', {'key': key, 'value': value}),
                                   'test', f'defaults:{key}')

        context = AIContextBuilder(self.db, self.service).build(10, 1, True)

        self.assertEqual(context['session_defaults'],
                         {'location': 'سالن الغدیر', 'venue_cost': 3000000, 'person_fee': 200000})
        self.assertNotIn('150000', json.dumps(context, ensure_ascii=False))

    def test_context_falls_back_to_content_defaults(self):
        context = AIContextBuilder(self.db, self.service).build(10, 1, True)

        self.assertEqual(context['session_defaults']['location'], DEFAULTS['location'])
        self.assertEqual(context['session_defaults']['person_fee'], DEFAULTS['person_fee'])


class ChatMemoryWindowTests(unittest.TestCase):
    """SPEC-018: memory depth is bounded and named, not scattered."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'memory.sqlite3')
        self.service = Service(self.db)
        self.bot = Bot(self.db, SimpleNamespace(admin_group=-100, bot_name='sans',
                                               reminder_hours=0, admins=frozenset({1})), None)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_snapshot_is_bounded(self):
        with self.db.conn:
            for index in range(60):
                self.db.log_message(-100, 1, 'ادمین', f'پیام {index}', 'user')

        snapshot = json.loads(SansAgent(self.bot)._build_live_snapshot(-100, 1, True, None))

        self.assertEqual(len(snapshot['recent_chat_messages']), 20)
        self.assertEqual(snapshot['recent_chat_messages'][-1]['text'], 'پیام 59')
        self.assertLessEqual(len(snapshot['recent_chat_messages']), 30)

    def test_legacy_route_sends_a_deeper_but_capped_history(self):
        config = SimpleNamespace(ai_url='https://example.com/v1', ai_key='fake-key',
                                 ai_model='fake-model', ai_fallback_models=())
        history = [{'role': 'user' if index % 2 == 0 else 'assistant',
                    'content': f'{index} ' + ('x' * 2000)} for index in range(20)]

        with patch('app.clients.post_json',
                   return_value={'choices': [{'message': {'content': '{"action":"card","args":{}}'}}]}) as post:
            AIClient(config).parse('کارت', history, {'role': 'player'})

        payload = json.loads(post.call_args.args[1]['messages'][1]['content'])
        self.assertEqual(len(payload['history']), 8)
        self.assertTrue(all(len(item['content']) <= 1000 for item in payload['history']))
        # The tail, not the head, of the conversation is replayed.
        self.assertTrue(payload['history'][0]['content'].startswith('12 '))
        self.assertTrue(payload['history'][-1]['content'].startswith('19 '))


class OutreachPromptTests(unittest.TestCase):
    """SPEC-019: the prompt and the tool must not invent a reply-only bot."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'outreach.sqlite3')
        self.service = Service(self.db)
        self.config = Config('fake-token', -100, frozenset({1}), 'https://example.com/v1',
                             'fake-key', 'fake-model', Path(self.temp.name) / 'unused.sqlite3')
        self.bot = Bot(self.db, self.config, SimpleNamespace())
        with self.db.conn:
            self.service.apply(self.service.prepare('new_player', {'name': 'علی رضایی'}), 1, 'test:1')
            self.service.apply(self.service.prepare('new_player', {'name': 'مهدی کریمی'}), 1, 'test:2')
            self.service.bind(1, 10, 1)
            self.bot.new_pending('identity', {'name': 'مهدی کریمی', 'user_id': 40, 'player': 2,
                                              'username': 'mehdi'}, 40, 40)
            self.service.touch_contact(50, 50, 'مهمان')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def tools(self, admin=True, user=1):
        effects = {}
        agent = SansAgent(self.bot)
        return {t.name: t for t in agent._make_tools(-100, user, admin, None, effects)}, effects

    def test_prompt_allows_admins_and_proactive_sending(self):
        # The bot does send proactively after an admin click, and the prompt says so.
        self.assertIn('می‌تواند و می‌کند که خودش شروع‌کنندهٔ پیام باشد', AGENT_SYSTEM_PROMPT)
        self.assertIn('فقط پاسخ‌دهنده» نیست', AGENT_SYSTEM_PROMPT)
        # The admin exception is explicit, and the absolute rule is gone.
        self.assertIn('اگر کاربر «ادمین» است', AGENT_SYSTEM_PROMPT)
        self.assertIn('نمی‌توانم شماره را نشان بدهم', AGENT_SYSTEM_PROMPT)
        self.assertNotIn('و شمارهٔ یک بازیکن را برای کاربر دیگری بازگو نکن', AGENT_SYSTEM_PROMPT)
        # Both audiences and the discovery targets are documented.
        self.assertIn("audience='all'", AGENT_SYSTEM_PROMPT)
        self.assertIn("target='identities'", AGENT_SYSTEM_PROMPT)
        self.assertIn("target='contacts'", AGENT_SYSTEM_PROMPT)
        self.assertIn('request_phone=true', AGENT_SYSTEM_PROMPT)

    def test_message_players_exposes_both_audiences(self):
        tools, _ = self.tools(admin=True)
        self.assertIn('audience', tools['message_players'].args)
        self.assertIn('request_phone', tools['message_players'].args)

        reply = tools['message_players'].invoke({'text': 'سانس فردا لغو شد', 'audience': 'all'})

        self.assertIn('حالت: همهٔ مخاطبان ربات', reply)
        self.assertIn('گیرندگان (3 نفر)', reply)
        self.assertIn('مهمان', reply)
        self.assertIn('تأیید', reply)
        # A proposal only: nothing is delivered or recorded before the click.
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM outreach')['n'], 0)

    def test_request_phone_is_reported_on_the_card(self):
        tools, _ = self.tools(admin=True)

        reply = tools['message_players'].invoke({'text': 'شماره‌تان را بفرستید',
                                                 'audience': 'all', 'request_phone': True})

        self.assertIn('دکمهٔ «ارسال شمارهٔ من» هم فرستاده می‌شود', reply)


class OnboardingAgentContextTests(unittest.TestCase):
    """SPEC-021: the model sees the newcomer state, never the address book."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'onboarding.sqlite3')
        self.service = Service(self.db)
        self.config = Config('fake-token', -100, frozenset({1}), 'https://example.com/v1',
                             'fake-key', 'fake-model', Path(self.temp.name) / 'unused.sqlite3')
        self.bot = Bot(self.db, self.config, SimpleNamespace())
        with self.db.conn:
            self.service.apply(self.service.prepare('new_player', {'name': 'علی رضایی'}), 1, 'test:1')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def snapshot(self, user_id=50, sender=None):
        agent = SansAgent(self.bot)
        return json.loads(agent._build_live_snapshot(user_id, user_id, False, sender))

    def test_prompt_tells_the_model_not_to_repeat_the_introduction(self):
        self.assertIn('needs_introduction', AGENT_SYSTEM_PROMPT)
        self.assertIn('ربات خودش پیام اول را فرستاده', AGENT_SYSTEM_PROMPT)
        self.assertIn('username را هم از او نپرس', AGENT_SYSTEM_PROMPT)

    def test_a_stranger_needs_introduction_and_no_number_is_exposed(self):
        with self.db.conn:
            self.service.touch_contact(50, 50, 'مهمان', 'guest', phone='09121234567')

        caller = self.snapshot(50, {'id': 50, 'first_name': 'علی', 'last_name': 'رضایی'})['caller']

        self.assertEqual(caller['sender_name'], 'علی رضایی')
        self.assertTrue(caller['needs_introduction'])
        self.assertFalse(caller['has_pending_identity'])
        self.assertNotIn('989121234567', json.dumps(caller, ensure_ascii=False))

    def test_a_linked_caller_and_an_open_request_are_reported(self):
        with self.db.conn:
            self.service.bind(1, 10, 1)
            self.bot.new_pending('identity', {'name': 'علی رضایی', 'user_id': 10, 'player': 1},
                                 10, 10)

        caller = self.snapshot(10)['caller']

        self.assertFalse(caller['needs_introduction'])
        self.assertTrue(caller['has_pending_identity'])
        self.assertEqual(caller['linked_player']['id'], 1)


if __name__ == '__main__':
    unittest.main()
