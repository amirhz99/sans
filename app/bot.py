import json
import re
import sqlite3
import time
import uuid

from .agent import SansAgent
from .ai_context import AIContextBuilder
from .clients import APIError
from .content import ADMIN_HELP, DEFAULTS, HELP, SYSTEM_PROMPT
from .db import dumps
from .domain import ConversationState, EntityRef, Intent, ResponseLevel
from .presentation import SessionView
from .service import ADMIN_ACTIONS, Service, UserError, integer, normalize, text

# A greeting is answered by the model, but it is never allowed to trigger an
# action.  This line is only the fail-closed fallback and the reply for a
# greeting that the model tried to turn into an operation.
GREETING_REPLY = 'سلام! در خدمتم؛ بفرمایید چه کاری برایتان انجام دهم؟ برای راهنما /help را بفرستید.'

# SPEC-011: a proposal stays trustworthy for one day.  Staleness is decided by
# the proposal's own facts (see ``Service.guard``), never by the global revision
# counter, so approving one card cannot expire its siblings.
PROPOSAL_TTL = 86400

# SPEC-015: an admin may close cards younger than the TTL, but the accepted age
# stays inside one year so a typo cannot look like an unbounded request.
MAX_EXPIRY_HOURS = 8760

# SPEC-014: a private reply to a proactive notice is handed back to the admins.
RELAY_CONFIRMATION = 'پیام شما برای ادمین‌ها ارسال شد ✅'


def buttons(yes, no):
    return [[{'text': 'تأیید', 'callback_data': yes}, {'text': 'رد', 'callback_data': no}]]


class Bot:
    def __init__(self, db, config, ai):
        self.db, self.config, self.ai = db, config, ai
        self.service = Service(db)
        self.context_builder = AIContextBuilder(db, self.service)
        self.session_view = SessionView(self.service)
        self.system_prompt = SYSTEM_PROMPT
        self.content_defaults = DEFAULTS
        self.agent = SansAgent(self)

    def is_admin(self, chat_id, user_id):
        return (isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0
                and isinstance(chat_id, int) and not isinstance(chat_id, bool)
                and chat_id == self.config.admin_group)

    def allowed_chat(self, chat):
        """Return whether an update can be handled in this chat.

        The configured admin group is the only group boundary.  Private chats
        remain available for ordinary user flows; every other group is ignored
        before message, callback, or acknowledgement handling begins.
        """
        if not isinstance(chat, dict):
            return False
        chat_id = chat.get('id')
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            return False
        chat_type = chat.get('type')
        if chat_id == self.config.admin_group:
            # Callback fixtures and some API payloads omit the chat type; the
            # configured group id still identifies the only enabled group.
            return chat_type in (None, 'group', 'supergroup')
        return chat_type == 'private' and chat_id > 0

    def allowed_update(self, update):
        if not isinstance(update, dict):
            return False
        callback = update.get('callback_query')
        if isinstance(callback, dict):
            message = callback.get('message')
        else:
            message = update.get('message')
        return isinstance(message, dict) and self.allowed_chat(message.get('chat'))

    def addressed(self, msg, body):
        """Return whether a group message is directed at this bot.

        The bot identity comes from the values saved by ``getMe``.  Replying to
        another user's message is deliberately not sufficient, and a reply
        must match either this bot's id or its configured username.
        """
        if body.startswith('/'):
            return True
        normalized_body = normalize(body)
        bot_name = normalize(self.config.bot_name).strip()
        bot_username = normalize(self.db.setting('bot_username')).lstrip('@').strip()
        name_match = bool(bot_name and re.search(rf'(?<!\w){re.escape(bot_name)}(?!\w)', normalized_body))
        username_match = bool(bot_username and re.search(rf'(?<!\w)@{re.escape(bot_username)}(?!\w)', normalized_body))
        if name_match or username_match:
            return True

        reply_to = msg.get('reply_to_message')
        if not isinstance(reply_to, dict):
            return False
        sender = reply_to.get('from')
        if not isinstance(sender, dict):
            return False
        try:
            bot_id = int(self.db.setting('bot_id', '0'))
        except (TypeError, ValueError):
            bot_id = 0
        if bot_id > 0 and sender.get('id') == bot_id:
            return True
        reply_username = normalize(sender.get('username', '')).lstrip('@').strip()
        return bool(bot_username and reply_username == bot_username)

    @staticmethod
    def short_greeting(body, bot_name='', bot_username=''):
        """Recognize only a bare greeting, never a greeting plus a request."""
        greeting = normalize(body)
        if not greeting:
            return False
        phrases = ('سلام', 'درود', 'صبح بخیر', 'شب بخیر', 'وقت بخیر', 'خسته نباشید')
        names = [normalize(value).strip().lstrip('@') for value in (bot_name, bot_username)
                 if value and normalize(value).strip()]
        accepted = set(phrases)
        for phrase in phrases:
            for name in names:
                accepted.update((f'{phrase} {name}', f'{name} {phrase}',
                                 f'{phrase} @{name}', f'@{name} {phrase}'))
        return greeting in accepted

    def acknowledgement_chat(self, update):
        """Return the chat that should receive an immediate processing notice."""
        if not isinstance(update, dict) or update.get('callback_query'):
            return None
        msg = update.get('message')
        if not isinstance(msg, dict):
            return None
        chat = msg.get('chat', {})
        sender = msg.get('from', {})
        chat_id = chat.get('id')
        user_id = sender.get('id')
        if (not isinstance(chat_id, int) or not isinstance(user_id, int)
                or sender.get('is_bot') or not self.allowed_chat(chat)):
            return None
        private = chat.get('type') == 'private'
        if (msg.get('text') or msg.get('caption') or msg.get('contact')
                or msg.get('photo') or msg.get('document')):
            body = (msg.get('text') or msg.get('caption') or '').strip()
        else:
            body = ''
        if body.split('@')[0] == '/id':
            return chat_id
        # A bare greeting is answered with a single short message, so an extra
        # "processing" notice would only add noise.
        if (not msg.get('contact') and not msg.get('photo') and not msg.get('document')
                and self.short_greeting(body, self.config.bot_name, self.db.setting('bot_username'))):
            return None
        if private:
            return chat_id
        if not self.addressed(msg, body):
            return None
        return chat_id if (not body.startswith('/') or self.is_admin(chat_id, user_id)) else None

    def proactive_answer(self, msg, chat_id, user_id):
        """Return the outreach row when ``msg`` answers a proactive notice.

        Only a reply to a message this bot delivered as outreach qualifies, and
        only plain text: a photo or a contact is still handled by the receipt
        and identity flows (SPEC-014).
        """
        if user_id in self.config.admins:
            return None
        if msg.get('contact') or msg.get('photo') or msg.get('document'):
            return None
        body = (msg.get('text') or '').strip()
        reply_to = msg.get('reply_to_message')
        if not body or not isinstance(reply_to, dict):
            return None
        message_id = reply_to.get('message_id')
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            return None
        return self.db.one('SELECT * FROM outreach WHERE chat_id=? AND message_id=?',
                           (chat_id, message_id))

    def process(self, update, acknowledgement=None):
        uid = update.get('update_id')
        if not isinstance(uid, int):
            return
        # All domain changes, update offset, and outbound messages commit atomically.
        with self.db.conn:
            # An explicit outer transaction is essential: RELEASE of a root SAVEPOINT
            # would otherwise commit financial changes before the update acknowledgement.
            self.db.execute('BEGIN IMMEDIATE')
            if self.db.one('SELECT 1 FROM processed_updates WHERE update_id=?', (uid,)):
                return
            if not self.allowed_update(update):
                # Advance the polling offset without invoking any bot path for
                # an unauthorized group.  This is infrastructure bookkeeping,
                # not message, conversation, or domain persistence.
                self.db.execute('INSERT INTO processed_updates(update_id) VALUES (?)', (uid,))
                self.db.set_setting('offset', uid + 1)
                return
            callback = update.get('callback_query')
            if callback:
                self.callback(callback)
            elif update.get('message'):
                self.message(update['message'], acknowledgement)
            self.db.execute('INSERT INTO processed_updates(update_id) VALUES (?)', (uid,))
            self.db.set_setting('offset', uid + 1)

    def new_pending(self, kind, payload, user_id, chat_id):
        ident = uuid.uuid4().hex[:16]
        # SPEC-011: store the facts this proposal depends on so a later approval
        # can tell "my facts changed" apart from "somebody else changed anything".
        guard = [self.service.guard(command) for command in payload] if kind == 'commands' else []
        self.db.execute('INSERT INTO pending(id,kind,payload,requester,chat_id,revision,guard,created_at) '
                        'VALUES (?,?,?,?,?,?,?,?)',
                        (ident, kind, dumps(payload), user_id, chat_id, self.db.revision,
                         dumps(guard), int(time.time())))
        return ident

    def propose_settings(self, changes, user_id, chat_id):
        """Create one approval proposal for one or more settings changes.

        ``changes`` maps setting keys to their new values.  Nothing is written
        until an admin presses «تأیید»; the model can only prepare the
        proposal.
        """
        if not isinstance(changes, dict) or not changes:
            raise UserError('مقدار تنظیم مشخص نشد؛ کلید و مقدار را صریحاً بنویسید.')
        commands = [self.service.prepare('settings', {'key': key, 'value': value})
                    for key, value in changes.items()]
        ident = self.new_pending('commands', commands, user_id, chat_id)
        preview = '\n\n'.join(self.service.preview(c) for c in commands)
        return (preview + f'\n\nاین تغییر با تأیید شما اعمال می‌شود. کد: {ident}',
                buttons('yes:' + ident, 'no:' + ident))

    def propose_commands(self, commands, user_id, chat_id):
        """Create one approval card for one or more already prepared commands.

        Both the ordinary route and the multi-player settle route go through
        here, so a batch request produces exactly one card and one click
        (SPEC-011).
        """
        if not isinstance(commands, list) or not 1 <= len(commands) <= 30:
            raise UserError('هر کارت بین ۱ تا ۳۰ دستور می‌پذیرد.')
        # Duplicate targets in a batch are almost always a parsing mistake.
        targets = [(c['action'], c['args'].get('session'), c['args'].get('player'),
                    normalize(c['args'].get('name', ''))) for c in commands]
        if len(set(targets)) != len(targets):
            raise UserError('درخواست مورد تکراری دارد؛ لطفاً اصلاح کنید.')
        ident = self.new_pending('commands', commands, user_id, chat_id)
        preview = '\n\n'.join(self.service.preview(c) for c in commands)
        tail = f'\n\nثبت شود؟ کد: {ident}'
        if any(c['action'] != 'broadcast' for c in commands):
            tail += '\nتمام مبلغ‌ها تومان هستند.'
        return preview + tail, buttons('yes:' + ident, 'no:' + ident)

    @staticmethod
    def card_ref(markup):
        """Return ``proposal:<id>`` when a reply carries an approval card.

        Only the chunk that owns the «تأیید/رد» keyboard is tagged, so the
        delivered message id can be recorded against the proposal (SPEC-013).
        """
        if not isinstance(markup, list) or not markup:
            return None
        try:
            data = markup[0][0]['callback_data']
        except (IndexError, KeyError, TypeError):
            return None
        if not isinstance(data, str):
            return None
        kind, _, ident = data.partition(':')
        return 'proposal:' + ident if kind in ('yes', 'no') and ident else None

    def propose(self, command, user_id, chat_id):
        if command['action'] == 'settings':
            args = command.get('args', {})
            return self.propose_settings({args.get('key'): args.get('value')}, user_id, chat_id)
        if command['action'] == 'batch':
            raw = command.get('args', {}).get('commands')
            if not isinstance(raw, list) or not 1 <= len(raw) <= 20:
                raise UserError('هر بار بین ۱ تا ۲۰ دستور حضور یا ثبت بازیکن بفرستید.')
            commands = []
            for item in raw:
                if not isinstance(item, dict) or item.get('action') not in ('attendance', 'new_player'):
                    raise UserError('درخواست دسته‌ای فقط برای حضورها یا ثبت بازیکن‌ها مجاز است.')
                commands.append(self.service.prepare(item['action'], item.get('args', {})))
        else:
            commands = [self.service.prepare(command['action'], command.get('args', {}))]
        return self.propose_commands(commands, user_id, chat_id)

    def context(self, chat_id, user_id, admin, reply_to=None):
        state, _ = self.db.conversation(chat_id, user_id)
        return self.context_builder.build(
            chat_id, user_id, admin, ConversationState.from_dict(state), reply_to
        )

    @staticmethod
    def response_level(args):
        """Accept detail only when the router explicitly requested it."""
        if not isinstance(args, dict):
            return ResponseLevel.SUMMARY
        raw = args.get('response_level', args.get('detail', args.get('details')))
        if raw is True:
            return ResponseLevel.DETAILS
        try:
            return ResponseLevel(raw)
        except (TypeError, ValueError):
            return ResponseLevel.SUMMARY

    def message(self, msg, acknowledgement=None):
        chat = msg.get('chat', {})
        chat_id = chat.get('id')
        user_id = msg.get('from', {}).get('id')
        if not isinstance(chat_id, int) or not isinstance(user_id, int) or msg.get('from', {}).get('is_bot'):
            return
        private = chat.get('type') == 'private'
        admin = self.is_admin(chat_id, user_id)
        body = (msg.get('text') or msg.get('caption') or '').strip()
        sender = msg.get('from') if isinstance(msg.get('from'), dict) else {}
        sender_name = (' '.join(filter(None, [sender.get('first_name', ''), sender.get('last_name', '')]))
                       or sender.get('username', '') or str(user_id))
        if body and body.split('@')[0] != '/id':
            self.db.log_message(chat_id, user_id, sender_name, body, 'user')
        if private:
            # SPEC-014: remember the private address (and its latest profile
            # name) so the bot can report who it is talking to.
            self.service.touch_contact(user_id, chat_id, sender_name, sender.get('username', ''))
        reply_to = msg.get('reply_to_message') if isinstance(msg.get('reply_to_message'), dict) else None
        reply_body = ((reply_to or {}).get('text') or (reply_to or {}).get('caption') or '').strip()
        processing_body = body
        if reply_body:
            processing_body += '\n\n[متن پیام مورد پاسخ؛ فقط برای زمینه]\n' + reply_body[:2000]
        if body.split('@')[0] == '/id':
            self.db.say(chat_id, f'شناسهٔ گفتگو: {chat_id}\nشناسهٔ شما: {user_id}')
            return
        # SPEC-014: an answer to a proactive notice goes back to the admins
        # instead of being routed to the model as a brand new question.
        relayed = self.proactive_answer(msg, chat_id, user_id) if private else None
        if relayed:
            state, history = self.db.conversation(chat_id, user_id)
            self.db.log_agent_event(
                chat_id, user_id, sender_name, 'RELAYED_TO_ADMIN',
                summary=body[:200],
                details={'outreach': relayed['id'], 'player': relayed['player_id']},
            )
            self.db.say(self.config.admin_group,
                        f'پاسخ {sender_name} (شناسهٔ بله {user_id}) به پیام شما:\n{body[:3000]}')
            self.db.say(chat_id, RELAY_CONFIRMATION)
            history += [{'role': 'user', 'content': body[:4000]},
                        {'role': 'assistant', 'content': RELAY_CONFIRMATION}]
            self.db.save_conversation(chat_id, user_id, state, history)
            self.db.log_message(chat_id, 0, self.config.bot_name, RELAY_CONFIRMATION, 'assistant')
            return
        # In the admin group, don't interpret every ordinary conversation as a command.
        if not private and not self.addressed(msg, body):
            if body:
                self.db.log_agent_event(
                    chat_id,
                    user_id,
                    sender_name,
                    'SKIPPED_NOT_ADDRESSED',
                    summary=body[:200],
                    details={'reason': 'Group message without bot mention or reply to bot'},
                )
            return
        # Public mentions/replies may be answered, but slash commands in a group
        # remain restricted to the admin permission boundary.
        if not private and body.startswith('/') and not admin:
            self.db.log_agent_event(
                chat_id,
                user_id,
                sender_name,
                'SKIPPED_NOT_ADMIN',
                summary=body[:200],
                details={'reason': 'Slash command in non-admin group or by non-admin user'},
            )
            return
        self.db.log_agent_event(
            chat_id,
            user_id,
            sender_name,
            'INCOMING',
            summary=body[:300] or '[media/contact]',
            details={'private': private, 'admin': admin, 'has_reply': bool(reply_to)},
        )
        state, history = self.db.conversation(chat_id, user_id)
        had_receipt_step = bool(state.get('receipt'))
        # A bare greeting is routed to the model like any other free message so
        # the conversation stays natural, but it may only ask a clarifying
        # question; see the dispatch guard below.
        bare_greeting = (not msg.get('contact') and not msg.get('photo') and not msg.get('document')
                         and not state.get('receipt')
                         and self.short_greeting(body, self.config.bot_name, self.db.setting('bot_username')))
        reply, markup = '', None
        try:
            if len(processing_body) > 6000:
                raise UserError('پیام طولانی است؛ لطفاً در چند پیام کوتاه‌تر بفرستید.')
            if private and msg.get('contact'):
                reply = self.receive_contact(msg['contact'], user_id, chat_id, state)
            elif private and (msg.get('photo') or msg.get('document')):
                if not self.service.linked(user_id):
                    raise UserError('قبل از ارسال رسید، با /identify نام کامل خودتان را معرفی کنید و منتظر تأیید ادمین بمانید.')
                if state.get('receipt'):
                    raise UserError('یک رسید منتظر مبلغ است. ابتدا /amount مبلغ را بفرستید یا /cancel کنید.')
                media_type = 'photo' if msg.get('photo') else 'document'
                media = msg['photo'][-1] if media_type == 'photo' else msg['document']
                state['receipt'] = {'file_id': media['file_id'], 'file_key': media.get('file_unique_id') or media['file_id'],
                                    'media_type': media_type, 'chat_id': chat_id, 'message_id': msg['message_id']}
                reply = 'رسید دریافت شد. مبلغ دقیق پرداخت را به تومان بنویسید؛ مثلاً /amount 150000. برای لغو /cancel.'
                # A bare explicit caption is convenient; never infer an amount from the image.
                amount = self.parse_amount(body)
                if amount is not None:
                    reply = self.submit_amount(amount, state, user_id)
            elif admin and (msg.get('photo') or msg.get('document')):
                media_type = 'photo' if msg.get('photo') else 'document'
                media = msg['photo'][-1] if media_type == 'photo' else msg['document']
                command = self.ai.parse(processing_body, history, self.context(chat_id, user_id, admin, reply_to))
                if command.get('action') != 'admin_receipt':
                    raise UserError('برای رسید ادمین، نام بازیکن و مبلغ دقیق تومان را در متن یا کپشن بنویسید.')
                args = command.get('args', {})
                amount = args.get('amount')
                player = self.service.player(args.get('player'))
                receipt_id = self.service.submit_admin_receipt(
                    player['id'], amount,
                    {'file_id': media['file_id'], 'file_key': media.get('file_unique_id') or media['file_id'],
                     'media_type': media_type, 'chat_id': chat_id, 'message_id': msg['message_id']},
                    user_id)
                reply = f'رسید #{receipt_id} برای {player["name"]} ثبت شد؛ پس از بررسی با /approve {receipt_id} تأیید کنید.'
            else:
                command = self.command(body, admin)
                if command is None:
                    amount = self.parse_amount(body) if state.get('receipt') else None
                    if amount is not None:
                        command = {'action': 'payment_amount', 'args': {'amount': amount}}
                    elif getattr(self.ai, 'use_langgraph', False):
                        try:
                            reply, markup, effects = self.agent.run(
                                processing_body,
                                history,
                                chat_id,
                                user_id,
                                admin,
                                sender=sender,
                                llm_override=getattr(self.ai, 'llm_override', None),
                                current_message=body,
                            )
                            if effects.get('last_session_id'):
                                s = self.service.session(effects['last_session_id'])
                                state['last_entity'] = EntityRef('session', s['id'], s['title']).to_dict()
                            elif effects.get('last_player_id'):
                                p = self.service.player(effects['last_player_id'])
                                state['last_entity'] = EntityRef('player', p['id'], p['name']).to_dict()
                            command = {'action': '_agent_handled', 'args': {}}
                        except Exception as exc:
                            raise APIError(f'LangGraph execution error: {exc}') from None
                    else:
                        try:
                            command = Intent.from_dict(self.ai.parse(
                                processing_body, history, self.context(chat_id, user_id, admin, reply_to)
                            )).to_dict()
                        except ValueError:
                            if bare_greeting:
                                raise UserError(GREETING_REPLY) from None
                            raise UserError('هوش مصنوعی فقط می‌تواند یک درخواست معتبر از ربات استخراج کند؛ دوباره واضح‌تر بنویسید.') from None
                        # SPEC-014: `broadcast` is deliberately absent from the
                        # domain Action enum, so the legacy JSON route can never
                        # create one; only the agent tool and an explicit admin
                        # click can.
                        allowed = ADMIN_ACTIONS | {'batch', 'card', 'config', 'debt', 'help', 'session_info', 'clarify',
                                                   'identify', 'payment_amount', 'declare_debt', 'admin_receipt',
                                                   'list_players', 'list_sessions', 'report'}
                        if command['action'] not in allowed:
                            raise UserError('تأیید یا اتصال مستقیم از طریق هوش مصنوعی مجاز نیست؛ از دکمه‌ها یا فرمان‌های راهنما استفاده کنید.')
                action, args = command['action'], command.get('args', {})
                if action == '_agent_handled':
                    pass
                elif bare_greeting:
                    # A greeting is never a request: keep it side-effect free even
                    # when the model tried to derive an operation from it.
                    question = command.get('question')
                    if action != 'clarify' or not isinstance(question, str) or not question.strip():
                        raise UserError(GREETING_REPLY)
                    reply = text(question, 'سؤال', limit=1500)
                elif action == 'cancel':
                    state.clear()
                    history.clear()
                    self.db.execute("UPDATE pending SET status='cancelled' WHERE requester=? AND chat_id=? AND status='pending'",
                                    (user_id, chat_id))
                    reply = 'مرحلهٔ فعلی و پیشنهادهای تأییدنشدهٔ شما لغو شدند؛ رسید ثبت‌شده یا حساب مالی تغییر نکرد.'
                elif action == 'help':
                    reply = HELP + ('\n\n' + ADMIN_HELP if admin else '')
                    if private and self.service.linked(user_id):
                        reply += '\n\n' + self.service.debt(self.service.linked(user_id)['id'])
                elif action == 'config':
                    if not admin:
                        raise UserError('نمایش تنظیمات فقط برای ادمین‌های مجاز در گروه مدیریتی است.')
                    reply = self.service.configuration()
                elif action == 'clarify':
                    reply = text(command.get('question'), 'سؤال', limit=1500)
                elif action == 'card':
                    reply = self.service.card()
                    if not self.db.setting('card'):
                        self.ask_admin('card', 'لطفاً شماره کارت و نام صاحب حساب را اعلام کنید؛ مثلاً «مسئول، شماره کارت ...».')
                elif action == 'debt':
                    p = self.service.player(args.get('player')) if admin and args.get('player') else self.service.linked(user_id)
                    if not p:
                        raise UserError('ابتدا با /identify نام کامل خود را معرفی کنید و منتظر تأیید اتصال حساب بمانید.')
                    reply = self.service.debt(p['id'])
                elif action == 'session_info':
                    try:
                        conversation = ConversationState.from_dict(state)
                        ref = args.get('session')
                        if ref in (None, '') and conversation.last_entity and conversation.last_entity.type == 'session':
                            ref = conversation.last_entity.id
                        session = self.service.session(ref)
                        level = self.response_level(args)
                        reply = self.session_view.render(session, level, admin)
                        state['last_entity'] = EntityRef('session', session['id'], session['title']).to_dict()
                        state['response_level'] = level.value
                    except UserError:
                        self.ask_admin('session', 'زمان، عنوان سانس بعدی و سهم ثابت هر نفر چقدر است؟ لطفاً با خطاب «مسئول» پاسخ دهید.')
                        raise
                elif action == 'identify' and private:
                    reply = self.identify(args.get('name'), user_id, chat_id, msg.get('from'),
                                          state.pop('identity_phone', None))
                elif action == 'payment_amount' and private:
                    reply = self.submit_amount(args.get('amount'), state, user_id)
                elif action == 'declare_debt' and private:
                    reply, markup = self.request_self_debt(args, user_id, chat_id)
                elif not admin:
                    raise UserError('این عملیات فقط برای ادمین‌های مجاز در گروه مدیریتی است.')
                elif action == 'duplicates':
                    reply = self.service.duplicates_report()
                elif action == 'player_rename':
                    reply = self.rename_player(args.get('player'), args.get('name'), user_id)
                elif action in ADMIN_ACTIONS or action == 'batch':
                    if action == 'settings':
                        self.service.require_explicit_setting(body, args)
                    reply, markup = self.propose(command, user_id, chat_id)
                elif action == 'list_players':
                    reply = '\n'.join(f"#{p['id']} {p['name']}" for p in self.db.all('SELECT * FROM players')) or 'بازیکنی ثبت نشده.'
                    if admin and self.service.duplicate_players():
                        reply += '\n\n⚠️ نام‌های مشکوک به تکرار پیدا شد؛ /duplicates را ببینید.'
                elif action == 'list_sessions':
                    reply = '\n'.join(f"#{s['id']} {s['title']} — {self.service.session_time_display(s)} — {'نهایی' if s['finalized'] else 'باز'}" for s in self.db.all('SELECT * FROM sessions ORDER BY id DESC')) or 'سانسی ثبت نشده.'
                elif action == 'report':
                    reply = self.service.report()
                elif action == 'expire':
                    # SPEC-015: the admin asked in plain language or with
                    # /expire; close the cards instead of answering that the
                    # tool is missing. The slash-command path already required
                    # admin, and this guard also covers the agent tool.
                    if not admin:
                        raise UserError('بستن کارت‌ها فقط برای ادمین‌های مجاز در گروه مدیریتی است.')
                    hours = self.expiry_hours(args.get('older_than_hours'))
                    reply = self.expiry_report(hours, self.expire_proposals(hours, user_id))
                elif action == 'pending':
                    closed = self.expire_stale_proposals()
                    rows = self.db.all("SELECT * FROM pending WHERE status='pending' ORDER BY created_at DESC LIMIT 30")
                    reply = '\n\n'.join(self.pending_summary(r) for r in rows) or 'درخواست منتظری نیست.'
                    if closed:
                        reply += f'\n\n{closed} کارت قدیمی‌تر از ۲۴ ساعت بسته شد.'
                    if rows:
                        reply += '\n\nبرای بستن کارت‌ها /expire [ساعت] را بفرستید؛ /expire 0 همه را می‌بندد.'
                elif action in ('confirm', 'deny'):
                    reply = self.confirm(args.get('id'), action == 'confirm', user_id)
                elif action in ('approve', 'reject'):
                    reply = self.review(integer(args.get('id'), 'شناسهٔ رسید', 1), action == 'approve', user_id)
                elif action == 'link':
                    reply, markup = self.resolve_claim(args['id'], args['player'], user_id)
                elif action == 'bind':
                    reply, markup = self.bind_profile(args, msg, user_id, chat_id)
                else:
                    raise UserError('درخواست شناخته نشد؛ /help را ببینید یا واضح‌تر بنویسید.')
        except (UserError, APIError, sqlite3.IntegrityError) as exc:
            if isinstance(exc, APIError):
                # A greeting has a short, friendly fallback instead of a technical notice.
                reply = GREETING_REPLY if bare_greeting else 'سرویس هوش مصنوعی فعلاً پاسخ معتبر نداد. دوباره تلاش کنید یا از فرمان‌های /help استفاده کنید؛ تغییری اعمال نشد.'
            elif isinstance(exc, sqlite3.IntegrityError):
                # Transactional mutations are protected by a savepoint in confirm().
                reply = 'اطلاعات تکراری یا ناسازگار است؛ ثبت انجام نشد.'
            else:
                reply = str(exc)
        history += [{'role': 'user', 'content': processing_body[:4000]}, {'role': 'assistant', 'content': reply[:2500]}]
        self.db.save_conversation(chat_id, user_id, state, history)
        self.db.log_message(chat_id, 0, self.config.bot_name, reply, 'assistant')
        # The polling loop may have sent a short-lived processing notice before
        # entering this transaction. Replace it with the final response when
        # possible; otherwise preserve the established chunking/send behavior.
        edit = (acknowledgement if isinstance(acknowledgement, dict) else {})
        edit_chat = edit.get('chat_id')
        edit_message = edit.get('message_id')
        can_edit = (isinstance(edit_chat, int) and not isinstance(edit_chat, bool)
                    and edit_chat == chat_id
                    and isinstance(edit_message, int) and not isinstance(edit_message, bool)
                    and edit_message > 0 and len(reply) <= 3500
                    and not had_receipt_step
                    and not msg.get('contact') and not msg.get('photo') and not msg.get('document'))
        if can_edit:
            payload = {'chat_id': edit_chat, 'message_id': edit_message, 'text': reply}
            if markup:
                payload['reply_markup'] = {'inline_keyboard': markup}
            self.db.queue('editMessageText', payload, self.card_ref(markup))
        else:
            self.db.say(chat_id, reply, markup, self.card_ref(markup))

    @staticmethod
    def parse_amount(body):
        value = normalize(body).replace(',', '').replace('٬', '')
        match = re.fullmatch(r'(\d+)\s*(?:تومان)?', value)
        return integer(match.group(1), 'مبلغ پرداخت', 1) if match else None

    def command(self, body, admin):
        if not body.startswith('/'):
            return None
        parts = body.split(maxsplit=1)
        name = parts[0].split('@')[0][1:]
        rest = parts[1].strip() if len(parts) > 1 else ''
        simple = {'start': 'help', 'help': 'help', 'card': 'card', 'cancel': 'cancel', 'config': 'config', 'report': 'report',
                  'players': 'list_players', 'sessions': 'list_sessions', 'pending': 'pending'}
        if name in simple:
            return {'action': simple[name], 'args': {}}
        if name in ('debt', 'session', 'identify', 'amount', 'confirm', 'deny', 'approve', 'reject'):
            key = {'debt': 'player', 'session': 'session', 'identify': 'name', 'amount': 'amount'}.get(name, 'id')
            return {'action': {'session': 'session_info', 'amount': 'payment_amount'}.get(name, name),
                    'args': {key: rest} if rest else {}}
        if name == 'setcard' and admin:
            if not rest:
                raise UserError('/setcard شماره کارت و نام صاحب حساب')
            return {'action': 'settings', 'args': {'key': 'card', 'value': rest}}
        if name == 'expire' and admin:
            # SPEC-015: closing cards is bookkeeping, so it needs no approval
            # card, but it stays inside the admin-group boundary like /setcard.
            return {'action': 'expire', 'args': {'older_than_hours': rest} if rest else {}}
        if name == 'link' and admin:
            pieces = rest.split(maxsplit=1)
            if len(pieces) != 2:
                raise UserError('/link کد_درخواست شناسه_بازیکن')
            return {'action': 'link', 'args': {'id': pieces[0], 'player': pieces[1]}}
        if name == 'bind' and admin:
            if not rest:
                raise UserError('/bind شناسه یا نام بازیکن [@username] و ریپلای به پروفایل')
            pieces = rest.rsplit(maxsplit=1)
            if len(pieces) == 2 and pieces[1].startswith('@'):
                player, username = pieces
                if not player.strip():
                    raise UserError('/bind شناسه یا نام بازیکن [@username] و ریپلای به پروفایل')
                return {'action': 'bind', 'args': {'player': player, 'username': username}}
            return {'action': 'bind', 'args': {'player': rest}}
        if name in ('merge', 'rename', 'delplayer', 'newplayer', 'duplicates') and admin:
            if name == 'duplicates':
                return {'action': 'duplicates', 'args': {}}
            pieces = rest.split(maxsplit=1)
            if name == 'merge':
                if len(pieces) != 2:
                    raise UserError('/merge رکورد_تکراری رکورد_اصلی — مثلاً /merge 12 18 '
                                    '(برای نام‌های چندکلمه‌ای از شناسه استفاده کنید)')
                return {'action': 'player_merge', 'args': {'source': pieces[0], 'target': pieces[1]}}
            if name == 'rename':
                if len(pieces) != 2:
                    raise UserError('/rename شناسه_یا_نام_فعلی نام کامل جدید — مثلاً /rename 15 محمد مهدی موحد')
                return {'action': 'player_rename', 'args': {'player': pieces[0], 'name': pieces[1]}}
            if name == 'delplayer':
                if len(pieces) != 1:
                    raise UserError('/delplayer شناسه_یا_نام_بازیکن')
                return {'action': 'player_delete', 'args': {'player': pieces[0]}}
            if not rest:
                raise UserError('/newplayer نام کامل بازیکن')
            return {'action': 'new_player', 'args': {'name': rest}}
        if name == 'do' and admin:
            try:
                obj = json.loads(rest)
                if not isinstance(obj, dict) or not isinstance(obj.get('action'), str) or not isinstance(obj.get('args', {}), dict):
                    raise ValueError()
                return obj
            except ValueError:
                raise UserError('پس از /do یک JSON معتبر شامل action و args بفرستید.') from None
        return {'action': 'help', 'args': {}}

    def rename_player(self, player_ref, new_name, actor):
        """Rename directly: it is reversible and keeps the id and history."""
        player = self.service.player(player_ref)
        result = self.service.rename_player(player['id'], new_name, actor)
        return (f"نام #{result['id']} از «{result['old']}» به «{result['new']}» تغییر کرد؛ "
                'شناسه و کل تاریخچه حفظ شد.')

    def identify(self, name, user_id, chat_id, sender=None, phone_number=None):
        if self.service.linked(user_id):
            return 'حساب شما قبلاً شناسایی شده است. برای مشاهدهٔ بدهی /debt را بفرستید.'
        name = text(name, 'نام کامل')
        existing = self.db.one("SELECT id FROM pending WHERE requester=? AND kind IN ('identity','claim') AND status='pending'", (user_id,))
        if existing:
            return 'درخواست شناسایی شما منتظر ادمین است. برای اصلاح نام ابتدا /cancel کنید.'
        try:
            player = self.service.player(name)
        except UserError:
            player = None
        payload = {'name': name, 'user_id': user_id, 'player': player['id'] if player else None,
                   **self.identity_profile(sender, phone_number)}
        ident = self.new_pending('identity' if player else 'claim', payload, user_id, chat_id)
        note = self.identity_note(ident, payload, player)
        markup = None
        if player:
            markup = buttons('yes:' + ident, 'no:' + ident)
        else:
            note += f'\nتطبیق قطعی پیدا نشد. با /players بررسی کنید؛ در صورت نیاز بازیکن بسازید و سپس:\n/link {ident} شناسه_بازیکن'
        self.db.say(self.config.admin_group, note, markup)
        return 'معرفی شما برای بررسی به ادمین‌ها ارسال شد؛ بعد از تأیید، بدهی حساب خودتان قابل مشاهده است.'

    @staticmethod
    def identity_profile(sender, phone_number=None):
        sender = sender if isinstance(sender, dict) else {}
        first_name = sender.get('first_name') if isinstance(sender.get('first_name'), str) else ''
        last_name = sender.get('last_name') if isinstance(sender.get('last_name'), str) else ''
        display_name = ' '.join(part.strip() for part in (first_name, last_name) if part.strip())
        username = sender.get('username') if isinstance(sender.get('username'), str) else ''
        username = username.strip().lstrip('@') or None
        profile = {'display_name': display_name[:200] or None, 'username': username[:100] if username else None}
        if phone_number:
            profile['phone_number'] = phone_number
        return profile

    @staticmethod
    def _contact_phone(contact, user_id):
        if not isinstance(contact, dict) or contact.get('user_id') != user_id:
            raise UserError('فقط contact متعلق به خودتان پذیرفته می‌شود؛ شمارهٔ شخص دیگری قابل ثبت نیست.')
        phone = contact.get('phone_number')
        if not isinstance(phone, str) or not phone.strip():
            raise UserError('contact شمارهٔ قابل استفاده‌ای ندارد.')
        return phone.strip()[:100]

    def receive_contact(self, contact, user_id, chat_id, state):
        phone = self._contact_phone(contact, user_id)
        # SPEC-014: keep the verified phone in the address book as well.
        self.service.touch_contact(user_id, chat_id, phone=phone)
        pending = self.db.one("SELECT * FROM pending WHERE requester=? AND chat_id=? "
                              "AND kind IN ('identity','claim') AND status='pending' "
                              "ORDER BY created_at DESC LIMIT 1", (user_id, chat_id))
        if not pending:
            state['identity_phone'] = phone
            return 'شمارهٔ شما دریافت شد. حالا /identify نام کامل را بفرستید تا username و شماره برای ادمین‌ها ارسال شود.'
        payload = json.loads(pending['payload'])
        payload['phone_number'] = phone
        self.db.execute('UPDATE pending SET payload=? WHERE id=?', (dumps(payload), pending['id']))
        player = self.db.one('SELECT id,name FROM players WHERE id=?', (payload.get('player'),)) if payload.get('player') else None
        markup = buttons('yes:' + pending['id'], 'no:' + pending['id']) if pending['kind'] == 'identity' else None
        self.db.say(self.config.admin_group, self.identity_note(pending['id'], payload, player) +
                    ('' if player else f'\nتطبیق قطعی پیدا نشد. با /players بررسی کنید؛ در صورت نیاز بازیکن بسازید و سپس:\n/link {pending["id"]} شناسه_بازیکن'),
                    markup)
        return 'شمارهٔ شما به درخواست شناسایی اضافه و برای ادمین‌ها ارسال شد.'

    def identity_note(self, ident, payload, player=None):
        username = '@' + payload['username'] if payload.get('username') else 'ارسال نشده'
        phone = payload.get('phone_number') or 'ارسال نشده'
        display_name = payload.get('display_name') or 'ارسال نشده'
        note = (f'درخواست شناسایی {ident}\n'
                f'نام اعلامی: «{payload["name"]}»\n'
                f'نام نمایشی: {display_name}\n'
                f'username: {username}\n'
                f'شماره تماس: {phone}\n'
                f'شناسهٔ داخلی بله: {payload["user_id"]}')
        if player:
            note += (f"\nتطبیق پیشنهادی: #{player['id']} {player['name']}"
                     '\nلطفاً هویت را مستقلاً بررسی کنید؛ نام، username و شماره به‌تنهایی اثبات هویت نیستند.')
        return note

    def pending_summary(self, row):
        if row['kind'] == 'binding':
            payload = json.loads(row['payload'])
            return (f"درخواست اتصال {row['id']}\nبازیکن: #{payload['player']} {payload['player_name']}\n"
                    f"شناسهٔ داخلی بله: {payload['user_id']}\n"
                    f"username: @{payload['username']}\nآیا این حساب به این بازیکن متصل شود؟")
        if row['kind'] == 'commands':
            try:
                commands = json.loads(row['payload'])
                preview = '\n\n'.join(self.service.preview(c) for c in commands)
            except (TypeError, ValueError, UserError, KeyError):
                return f"{row['id']} | {row['kind']} | {row['payload']}"
            return f"درخواست {row['id']} — منتظر تأیید ادمین\n{preview}"
        if row['kind'] not in ('identity', 'claim'):
            return f"{row['id']} | {row['kind']} | {row['payload']}"
        payload = json.loads(row['payload'])
        player = self.db.one('SELECT id,name FROM players WHERE id=?', (payload.get('player'),)) if payload.get('player') else None
        return self.identity_note(row['id'], payload, player)

    def resolve_claim(self, ident, player_ref, actor):
        ident = text(ident, 'کد درخواست', limit=32)
        player_ref = text(player_ref, 'شناسه یا نام بازیکن')
        row = self.db.one("SELECT * FROM pending WHERE id=? AND status='pending' AND kind='claim'", (ident,))
        if not row:
            raise UserError('درخواست هویت منتظر پیدا نشد.')
        player = self.service.player('#' + player_ref.lstrip('#') if player_ref.lstrip('#').isdigit() else player_ref)
        payload = json.loads(row['payload'])
        payload['player'] = player['id']
        self.db.execute("UPDATE pending SET kind='identity',payload=? WHERE id=?", (dumps(payload), ident))
        return (self.identity_note(ident, payload, player) + '\nآیا این حساب به این بازیکن متصل شود؟',
                buttons('yes:' + ident, 'no:' + ident))

    @staticmethod
    def _profile_user(profile):
        if not isinstance(profile, dict):
            return None
        user_id = profile.get('id')
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            return None
        if profile.get('is_bot') is True:
            return None
        username = profile.get('username')
        if username is not None and (not isinstance(username, str) or not username.strip()):
            return None
        return user_id, (username.strip().lstrip('@').casefold() if isinstance(username, str) else '')

    @classmethod
    def extract_profile_user(cls, message):
        """Return a trusted structured profile, never resolving message text."""
        if not isinstance(message, dict):
            return None
        contact = message.get('contact')
        if isinstance(contact, dict) and 'id' not in contact:
            contact = dict(contact)
            contact['id'] = contact.get('user_id')
        # A contact/forward identifies its target, while ``from`` identifies
        # the person who sent the wrapper message. Prefer the target fields to
        # avoid binding the admin who forwarded a contact.
        candidates = [contact, message.get('forward_from')]
        for key in ('forward_origin', 'forwarded_from'):
            origin = message.get(key)
            if isinstance(origin, dict):
                candidates.append(origin.get('sender_user'))
                candidates.append(origin.get('from'))
        candidates.append(message.get('from'))
        for candidate in candidates:
            profile = cls._profile_user(candidate)
            if profile:
                return profile
        return None

    def bind_profile(self, args, message, actor, chat_id):
        if not self.is_admin(chat_id, actor):
            raise UserError('اتصال حساب فقط برای ادمین‌های گروه مدیریتی مجاز است.')
        reply = message.get('reply_to_message') if isinstance(message, dict) else None
        profile = self.extract_profile_user(reply)
        if not profile:
            raise UserError('برای /bind باید به پیام پروفایل کاربر ریپلای کنید؛ شناسهٔ عددی معتبر و غیررباتی لازم است.')
        user_id, profile_username = profile
        requested_username = args.get('username')
        if requested_username is not None:
            if not isinstance(requested_username, str) or not requested_username.strip():
                raise UserError('username معتبر نیست.')
            requested_username = requested_username.strip().lstrip('@').casefold()
            if not profile_username or requested_username != profile_username:
                raise UserError('username اعلام‌شده با username پروفایل یکسان نیست.')
        player = self.service.player(args.get('player'))
        if player['bale_id'] is not None or self.service.linked(user_id):
            raise UserError('این بازیکن یا حساب بله قبلاً متصل شده است؛ اتصال دوباره مجاز نیست.')
        for pending in self.db.all("SELECT payload FROM pending WHERE kind='binding' AND status='pending'"):
            try:
                pending_payload = json.loads(pending['payload'])
            except (TypeError, ValueError):
                continue
            if pending_payload.get('player') == player['id'] or pending_payload.get('user_id') == user_id:
                raise UserError('برای این بازیکن یا حساب بله، درخواست اتصال دیگری منتظر بررسی است.')
        ident = self.new_pending('binding', {
            'player': player['id'], 'player_name': player['name'], 'user_id': user_id,
            'username': profile_username or 'ارسال نشده',
        }, actor, chat_id)
        note = (f'درخواست اتصال {ident}\nبازیکن: #{player["id"]} {player["name"]}\n'
                f'شناسهٔ داخلی بله: {user_id}\nusername: @{profile_username or "ارسال نشده"}\n'
                'آیا این حساب به این بازیکن متصل شود؟')
        self.db.say(self.config.admin_group, note, buttons('yes:' + ident, 'no:' + ident))
        return 'درخواست اتصال برای تأیید ادمین ثبت شد؛ هنوز حسابی متصل نشده.', buttons('yes:' + ident, 'no:' + ident)

    def repair_last_entity(self, chat_id, user_id, commands):
        """Point follow-up memory at the surviving row after a merge/delete.

        ``AIContextBuilder`` already drops stale references, but keeping the
        stored state accurate means the next «بدهی‌اش؟» still refers to the
        person the admin was working on.
        """
        state, history = self.db.conversation(chat_id, user_id)
        try:
            ref = ConversationState.from_dict(state).last_entity
        except ValueError:
            ref = None
            stale = True
        else:
            stale = False
        if not ref or ref.type != 'player':
            if stale:
                state.pop('last_entity', None)
                self.db.save_conversation(chat_id, user_id, state, history)
            return
        try:
            ident = None if isinstance(ref.id, bool) else int(ref.id)
        except (TypeError, ValueError):
            ident = None
        if ident is None:
            state.pop('last_entity', None)
            self.db.save_conversation(chat_id, user_id, state, history)
            return
        changed = False
        for command in commands if isinstance(commands, list) else []:
            if not isinstance(command, dict):
                continue
            args = command.get('args') if isinstance(command.get('args'), dict) else {}
            action = command.get('action')
            if action == 'player_merge' and args.get('source') == ident:
                target = self.db.one('SELECT id,name FROM players WHERE id=?', (args.get('target'),))
                if target:
                    state['last_entity'] = EntityRef('player', target['id'], target['name']).to_dict()
                    changed = True
            elif action == 'player_delete' and args.get('player') == ident:
                state.pop('last_entity', None)
                changed = True
        if changed:
            self.db.save_conversation(chat_id, user_id, state, history)

    def expire_proposal(self, ident):
        """Mark one proposal expired without touching any business state."""
        self.db.execute("UPDATE pending SET status='expired' WHERE id=? AND status='pending'", (ident,))

    def expire_stale_proposals(self, now=None):
        """Close proposals older than ``PROPOSAL_TTL`` and report how many.

        Called by /pending and by the periodic sweep so both share one rule
        (SPEC-011 AC-007, SPEC-013).
        """
        return self.sweep_proposals(now)

    def proposal_staleness(self, now=None, cutoff=None):
        """Return the cards the sweep must close (SPEC-013, SPEC-015)."""
        if cutoff is None:
            cutoff = int(time.time() if now is None else now) - PROPOSAL_TTL
        rows = self.db.all("SELECT id, chat_id, message_id FROM pending "
                           "WHERE status='pending' AND kind='commands' AND created_at < ? "
                           "ORDER BY created_at", (cutoff,))
        return [dict(row) for row in rows]

    def sweep_proposals(self, now=None, cutoff=None):
        """Expire every stale card and remove the buttons of delivered ones.

        Bookkeeping only: no ledger, player, session, or receipt row is touched.
        The caller owns the transaction, so this can run inside ``process``.
        """
        stale = self.proposal_staleness(now, cutoff)
        for row in stale:
            self.expire_proposal(row['id'])
            message_id = row.get('message_id')
            if isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0:
                self.db.queue('editMessageReplyMarkup', {
                    'chat_id': row['chat_id'], 'message_id': message_id,
                    'reply_markup': {'inline_keyboard': []},
                })
        if stale:
            self.db.set_setting('last_sweep', int(time.time() if now is None else now))
        return len(stale)

    def expiry_hours(self, value=None):
        """Validate an admin supplied card age in hours (SPEC-015).

        ``None`` or an empty value means the 24 hour TTL, so ``/expire`` alone
        behaves exactly like the background sweep.  ``0`` is the explicit
        "every open card" form.
        """
        if value is None or (isinstance(value, str) and not value.strip()):
            return PROPOSAL_TTL // 3600
        if isinstance(value, bool):
            raise UserError('بازهٔ زمانی باید عدد صحیح ساعت باشد؛ مثلاً /expire 0')
        normalized = normalize(value).strip()
        if not re.fullmatch(r'\d+', normalized):
            raise UserError('بازهٔ زمانی باید عدد صحیح ساعت باشد؛ مثلاً /expire 12')
        hours = int(normalized)
        if hours > MAX_EXPIRY_HOURS:
            raise UserError(f'بازهٔ زمانی حداکثر {MAX_EXPIRY_HOURS:,} ساعت (یک سال) است.')
        return hours

    def expiry_cutoff(self, hours, now=None):
        """Turn an accepted age into an epoch cutoff (SPEC-015).

        ``0`` means "every open card", so the cutoff sits one second ahead: a
        card created in the same second as the request is not literally older
        than zero hours, yet the admin clearly asked for it to be closed.
        """
        now = int(time.time() if now is None else now)
        return now + 1 if hours == 0 else now - hours * 3600

    def expire_proposals(self, older_than_hours=None, actor=None):
        """Close open ``commands`` cards on an explicit admin request (SPEC-015).

        Bookkeeping only: no ledger, player, session, attendance, or receipt row
        is touched.  Identity, claim, and binding cards are a human decision and
        are deliberately left ``pending``.
        """
        hours = self.expiry_hours(older_than_hours)
        closed = self.sweep_proposals(cutoff=self.expiry_cutoff(hours))
        if actor is not None:
            self.db.audit(actor, 'expire_proposals', {'older_than_hours': hours, 'closed': closed})
        return closed

    def expiry_report(self, hours, closed):
        """Describe what a manual expiry changed and what stayed open (SPEC-015)."""
        cutoff = self.expiry_cutoff(hours)
        remaining = self.db.one(
            "SELECT COUNT(*) n FROM pending WHERE status='pending' AND kind='commands' "
            "AND created_at >= ?", (cutoff,))['n']
        identity = self.db.one(
            "SELECT COUNT(*) n FROM pending WHERE status='pending' "
            "AND kind IN ('identity','claim','binding')")['n']
        lines = [f'{closed} کارت پیشنهاد بسته شد؛ هیچ حساب، سانس یا رسیدی تغییر نکرد.' if closed
                 else 'کارت پیشنهادی برای بستن نبود؛ هیچ چیزی تغییر نکرد.']
        if remaining:
            lines.append(f'{remaining} کارت تازه‌تر از {hours} ساعت باز ماند؛ '
                         'برای بستن همه /expire 0 را بفرستید.')
        if identity:
            lines.append(f'{identity} درخواست هویت/اتصال دست نخورد و برای بررسی ادمین باقی ماند.')
        return '\n'.join(lines)

    def assert_proposal_current(self, row, payload):
        """Fail closed when a card is too old or its own facts changed.

        Two distinct reasons produce two distinct messages, so the admin knows
        whether to ask again or simply to approve a sibling card.
        """
        if time.time() - row['created_at'] > PROPOSAL_TTL:
            self.expire_proposal(row['id'])
            raise UserError('این کارت بیش از ۲۴ ساعت پیش ساخته شده و منقضی شده است؛ '
                            'درخواست را دوباره بفرستید تا کارت تازه ساخته شود.')
        try:
            stored = json.loads(row['guard'] or '[]')
        except (TypeError, ValueError):
            stored = []
        if not isinstance(stored, list):
            stored = []
        if not stored:
            # Rows written before SPEC-011 have no fingerprint; the TTL above is
            # the only rule they can be held to.
            return
        try:
            current = [self.service.guard(command) for command in payload]
        except (UserError, KeyError, TypeError):
            current = None
        if stored != current:
            self.expire_proposal(row['id'])
            raise UserError('از زمان ساخت این کارت، مانده یا اطلاعات مرتبط تغییر کرده است؛ '
                            'برای دیدن وضعیت تازه دوباره درخواست بدهید.')

    def confirm(self, ident, approve, actor):
        ident = text(ident, 'کد درخواست', limit=32)
        row = self.db.one('SELECT * FROM pending WHERE id=?', (ident,))
        if not row or row['status'] != 'pending':
            raise UserError('این درخواست موجود نیست یا قبلاً بررسی/لغو شده است.')
        payload = json.loads(row['payload'])
        settings_change = (row['kind'] == 'commands' and isinstance(payload, list)
                           and any(isinstance(c, dict) and c.get('action') == 'settings' for c in payload))
        if approve and row['kind'] == 'commands':
            self.assert_proposal_current(row, payload)
            self.db.execute('SAVEPOINT apply_commands')
            try:
                for index, command in enumerate(payload):
                    self.service.apply(command, actor, f'proposal:{ident}:{index}')
            except Exception:
                self.db.execute('ROLLBACK TO apply_commands')
                raise
            finally:
                self.db.execute('RELEASE apply_commands')
            self.repair_last_entity(row['chat_id'], row['requester'], payload)
        elif approve and row['kind'] in ('identity', 'binding'):
            self.service.bind(payload['player'], payload['user_id'], actor)
        elif approve:
            raise UserError('ابتدا تطبیق بازیکن را با /link مشخص کنید.')
        self.db.execute('UPDATE pending SET status=? WHERE id=?', ('approved' if approve else 'rejected', ident))
        if row['kind'] == 'binding':
            self.db.say(row['chat_id'], 'اتصال حساب تأیید شد ✅')
        elif row['kind'] in ('identity', 'claim'):
            self.db.say(row['chat_id'], 'هویت شما تأیید شد.\n' + self.service.debt(payload['player']) if approve
                        else 'درخواست شناسایی تأیید نشد. لطفاً با /identify نام کامل دقیق را دوباره بفرستید.')
        if approve and row['kind'] == 'commands':
            for command in payload:
                if command['action'] == 'finalize':
                    for player in self.db.all('''SELECT p.id,p.bale_id FROM attendance a JOIN players p ON p.id=a.player_id
                                                WHERE a.session_id=? AND p.bale_id IS NOT NULL''', (command['args']['session'],)):
                        self.db.say(player['bale_id'], 'حساب سانس ثبت شد.\n' + self.service.debt(player['id']))
            if not settings_change and not any(c.get('action') == 'broadcast' for c in payload):
                self.ask_missing()
        if settings_change:
            return 'تنظیمات اعمال شد ✅' if approve else 'تغییر تنظیمات رد شد؛ هیچ مقداری تغییر نکرد.'
        if approve and row['kind'] == 'commands' and any(
                c.get('action') in ('player_merge', 'player_delete') for c in payload):
            return 'انجام شد ✅ فهرست بازیکنان به‌روز شد؛ برای بررسی /players و /duplicates را بفرستید.'
        return 'ثبت شد ✅' if approve else 'درخواست رد شد؛ تغییری در حساب ایجاد نشد.'

    def request_self_debt(self, args, user_id, chat_id):
        player = self.service.linked(user_id)
        if not player:
            raise UserError('برای اعلام بدهی ابتدا با /identify نام کامل خودتان را معرفی و منتظر تأیید ادمین بمانید.')
        pending = self.db.all("SELECT payload FROM pending WHERE requester=? AND chat_id=? "
                              "AND kind='commands' AND status='pending'", (user_id, chat_id))
        for row in pending:
            commands = json.loads(row['payload'])
            if any(c.get('action') == 'declare_debt' and c.get('args', {}).get('player') == player['id']
                   for c in commands):
                raise UserError('یک اعلام بدهی از شما هنوز منتظر تأیید است؛ ابتدا همان درخواست را تأیید یا رد کنید.')
        command = self.service.prepare('declare_debt', {
            'player': player['id'], 'amount': args.get('amount'),
            'note': 'بدهی اعلام‌شده توسط خود فرد'})
        ident = self.new_pending('commands', [command], user_id, chat_id)
        return (self.service.preview(command) +
                f'\n\nبرای ثبت بدهی و ارسال روش پرداخت تأیید کنید. کد: {ident}',
                buttons('yes:' + ident, 'no:' + ident))

    def submit_amount(self, amount, state, user_id):
        player = self.service.linked(user_id)
        if not player:
            raise UserError('ابتدا معرفی و تأیید هویت لازم است.')
        media = state.get('receipt')
        if not media:
            raise UserError('اول عکس یا فایل رسید را بفرستید، سپس مبلغ دقیق تومان را اعلام کنید.')
        receipt_id = self.service.submit_receipt(player['id'], amount, media, user_id)
        receipt = self.db.one('SELECT * FROM receipts WHERE id=?', (receipt_id,))
        self.db.queue('sendPhoto' if media['media_type'] == 'photo' else 'sendDocument',
                      {'chat_id': self.config.admin_group, media['media_type']: media['file_id'],
                       'caption': f"رسید #{receipt_id} | {player['name']} | {receipt['amount']:,} تومان (اعلام کاربر)"})
        pending = receipt['status'] == 'pending'
        self.db.say(self.config.admin_group,
                    f"رسید #{receipt_id} از {player['name']}\nمبلغ اعلامی: {receipt['amount']:,} تومان\n" +
                    ('لطفاً مبلغ، مقصد و تکراری نبودن انتقال را بررسی کنید.' if pending else 'طبق تنظیمات، خودکار ثبت شد؛ بررسی بانکی انجام نشده.'),
                    buttons(f'pay:{receipt_id}', f'reject:{receipt_id}') if pending else None)
        state.pop('receipt', None)
        return (f'رسید #{receipt_id} برای تأیید ادمین ارسال شد؛ هنوز از بدهی کم نشده.' if pending else
                f'رسید #{receipt_id} در حالت اعتماد ثبت شد.\n' + self.service.debt(player['id']))

    def review(self, receipt_id, approve, actor):
        row = self.service.review_receipt(receipt_id, approve, actor)
        self.db.say(row['chat_id'], f'رسید #{receipt_id} ' + ('تأیید شد.\n' + self.service.debt(row['player_id']) if approve
                    else 'تأیید نشد؛ مبلغ از بدهی کم نشد. برای توضیح یا اصلاح با ادمین تماس بگیرید.'))
        return f'رسید #{receipt_id} ' + ('تأیید شد ✅' if approve else 'رد شد.')

    def callback(self, callback):
        chat_id = callback.get('message', {}).get('chat', {}).get('id')
        user_id = callback.get('from', {}).get('id')
        if not self.is_admin(chat_id, user_id):
            self.db.queue('answerCallbackQuery', {'callback_query_id': callback['id'], 'text': 'فقط ادمین مجاز در گروه مدیریتی.'})
            return
        try:
            kind, ident = callback.get('data', '').split(':', 1)
            if kind in ('yes', 'no'):
                result = self.confirm(ident, kind == 'yes', user_id)
            elif kind in ('pay', 'reject'):
                result = self.review(integer(ident, 'شناسهٔ رسید', 1), kind == 'pay', user_id)
            else:
                raise UserError('دکمه معتبر نیست.')
        except (ValueError, sqlite3.IntegrityError) as exc:
            result = str(exc) if isinstance(exc, UserError) else 'درخواست معتبر نیست یا تکراری است.'
        self.db.queue('answerCallbackQuery', {'callback_query_id': callback['id'], 'text': result[:180]})
        self.db.say(chat_id, result)

    def ask_admin(self, key, question):
        now = time.time()
        if now - float(self.db.setting('asked:' + key, '0')) >= 6 * 3600:
            self.db.say(self.config.admin_group, question)
            self.db.set_setting('asked:' + key, now)

    def ask_missing(self):
        if not self.db.setting('card'):
            self.ask_admin('card', 'شماره کارت و نام صاحب حساب برای واریز چیست؟ لطفاً با خطاب «مسئول» اعلام کنید.')
        sessions = self.db.all('SELECT * FROM sessions WHERE finalized=0 ORDER BY id')
        if not sessions:
            self.ask_admin('session', 'سانس بعدی چه زمان و کجاست؟ سهم ثابت هر نفر و در صورت تمایل هزینهٔ زمین را اعلام کنید. با «مسئول» خطابم کنید.')
        for s in sessions:
            missing = []
            for key, label in (('session_at', 'تاریخ و ساعت دقیق تهران'), ('location', 'محل'), ('person_fee', 'سهم ثابت هر نفر')):
                if not s[key]:
                    missing.append(label)
            if s['venue_cost'] is None:
                missing.append('هزینهٔ زمین (اختیاری)')
            if not self.db.one('SELECT 1 FROM attendance WHERE session_id=?', (s['id'],)):
                missing.append('بعد از برگزاری، فهرست حاضران و مهمان‌ها')
            missing.append('پس از تکمیل حضورها، تأیید نهایی‌سازی حساب')
            self.ask_admin(f"session:{s['id']}", f"برای سانس #{s['id']} {s['title']} نیاز دارم به: " + '، '.join(missing))

    def remind(self):
        hours = self.config.reminder_hours
        if not hours or time.time() - float(self.db.setting('last_reminder', '0')) < hours * 3600:
            return
        with self.db.conn:
            self.ask_missing()
            count = self.db.one("SELECT COUNT(*) n FROM receipts WHERE status='pending'")['n']
            identities = self.db.one("SELECT COUNT(*) n FROM pending WHERE kind IN ('identity','claim') AND status='pending'")['n']
            if count or identities:
                self.db.say(self.config.admin_group, f'{count} رسید و {identities} درخواست هویت منتظر بررسی است. /report و /pending را ببینید.')
            self.db.set_setting('last_reminder', time.time())