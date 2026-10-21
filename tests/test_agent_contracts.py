import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


if __name__ == '__main__':
    unittest.main()