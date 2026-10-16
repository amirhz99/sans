import json
import re
import sqlite3
import time
import uuid

from .ai_context import AIContextBuilder
from .clients import APIError
from .content import ADMIN_HELP, HELP
from .db import dumps
from .domain import ConversationState, EntityRef, Intent, ResponseLevel
from .presentation import SessionView
from .service import ADMIN_ACTIONS, Service, UserError, integer, normalize, text

# A greeting is answered by the model, but it is never allowed to trigger an
# action.  This line is only the fail-closed fallback and the reply for a
# greeting that the model tried to turn into an operation.
GREETING_REPLY = 'سلام! در خدمتم؛ بفرمایید چه کاری برایتان انجام دهم؟ برای راهنما /help را بفرستید.'


def buttons(yes, no):
    return [[{'text': 'تأیید', 'callback_data': yes}, {'text': 'رد', 'callback_data': no}]]


class Bot:
    def __init__(self, db, config, ai):
        self.db, self.config, self.ai = db, config, ai
        self.service = Service(db)
        self.context_builder = AIContextBuilder(db, self.service)
        self.session_view = SessionView(self.service)

    def is_admin(self, chat_id, user_id):
        return (isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0
                and chat_id == self.config.admin_group
                and (self.config.admin_access == 'group_members' or user_id in self.config.admins))

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
                or sender.get('is_bot')):
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

    def process(self, update):
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
            callback = update.get('callback_query')
            if callback:
                self.callback(callback)
            elif update.get('message'):
                self.message(update['message'])
            self.db.execute('INSERT INTO processed_updates(update_id) VALUES (?)', (uid,))
            self.db.set_setting('offset', uid + 1)

    def new_pending(self, kind, payload, user_id, chat_id):
        ident = uuid.uuid4().hex[:16]
        self.db.execute('INSERT INTO pending(id,kind,payload,requester,chat_id,revision,created_at) VALUES (?,?,?,?,?,?,?)',
                        (ident, kind, dumps(payload), user_id, chat_id, self.db.revision, int(time.time())))
        return ident

    def propose(self, command, user_id, chat_id):
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
        # Duplicate targets in a batch are almost always a parsing mistake.
        targets = [(c['action'], c['args'].get('session'), c['args'].get('player'),
                    normalize(c['args'].get('name', ''))) for c in commands]
        if len(set(targets)) != len(targets):
            raise UserError('درخواست دسته‌ای مورد تکراری دارد؛ لطفاً اصلاح کنید.')
        ident = self.new_pending('commands', commands, user_id, chat_id)
        preview = '\n\n'.join(self.service.preview(c) for c in commands)
        return preview + f'\n\nثبت شود؟ کد: {ident}\nتمام مبلغ‌ها تومان هستند.', buttons('yes:' + ident, 'no:' + ident)

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

    def message(self, msg):
        chat = msg.get('chat', {})
        chat_id = chat.get('id')
        user_id = msg.get('from', {}).get('id')
        if not isinstance(chat_id, int) or not isinstance(user_id, int) or msg.get('from', {}).get('is_bot'):
            return
        private = chat.get('type') == 'private'
        admin = self.is_admin(chat_id, user_id)
        body = (msg.get('text') or msg.get('caption') or '').strip()
        reply_to = msg.get('reply_to_message') if isinstance(msg.get('reply_to_message'), dict) else None
        reply_body = ((reply_to or {}).get('text') or (reply_to or {}).get('caption') or '').strip()
        processing_body = body
        if reply_body:
            processing_body += '\n\n[متن پیام مورد پاسخ؛ فقط برای زمینه]\n' + reply_body[:2000]
        if body.split('@')[0] == '/id':
            self.db.say(chat_id, f'شناسهٔ گفتگو: {chat_id}\nشناسهٔ شما: {user_id}')
            return
        # In the admin group, don't interpret every ordinary conversation as a command.
        if not private and not self.addressed(msg, body):
            return
        # Public mentions/replies may be answered, but slash commands in a group
        # remain restricted to the admin permission boundary.
        if not private and body.startswith('/') and not admin:
            return
        state, history = self.db.conversation(chat_id, user_id)
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
                    else:
                        try:
                            command = Intent.from_dict(self.ai.parse(
                                processing_body, history, self.context(chat_id, user_id, admin, reply_to)
                            )).to_dict()
                        except ValueError:
                            if bare_greeting:
                                raise UserError(GREETING_REPLY) from None
                            raise UserError('هوش مصنوعی فقط می‌تواند یک درخواست معتبر از ربات استخراج کند؛ دوباره واضح‌تر بنویسید.') from None
                        allowed = ADMIN_ACTIONS | {'batch', 'card', 'config', 'debt', 'help', 'session_info', 'clarify',
                                                   'identify', 'payment_amount', 'declare_debt', 'admin_receipt',
                                                   'list_players', 'list_sessions', 'report'}
                        if command['action'] not in allowed:
                            raise UserError('تأیید یا اتصال مستقیم از طریق هوش مصنوعی مجاز نیست؛ از دکمه‌ها یا فرمان‌های راهنما استفاده کنید.')
                action, args = command['action'], command.get('args', {})
                if bare_greeting:
                    # A greeting is never a request: keep it side-effect free even
                    # when the model tried to derive an operation from it.
                    question = command.get('question')
                    if action != 'clarify' or not isinstance(question, str) or not question.strip():
                        raise UserError(GREETING_REPLY)
                if action == 'cancel':
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
                elif action in ADMIN_ACTIONS or action == 'batch':
                    reply, markup = self.propose(command, user_id, chat_id)
                elif action == 'list_players':
                    reply = '\n'.join(f"#{p['id']} {p['name']}" for p in self.db.all('SELECT * FROM players')) or 'بازیکنی ثبت نشده.'
                elif action == 'list_sessions':
                    reply = '\n'.join(f"#{s['id']} {s['title']} — {self.service.session_time_display(s)} — {'نهایی' if s['finalized'] else 'باز'}" for s in self.db.all('SELECT * FROM sessions ORDER BY id DESC')) or 'سانسی ثبت نشده.'
                elif action == 'report':
                    reply = self.service.report()
                elif action == 'pending':
                    rows = self.db.all("SELECT * FROM pending WHERE status='pending' ORDER BY created_at DESC LIMIT 30")
                    reply = '\n\n'.join(self.pending_summary(r) for r in rows) or 'درخواست منتظری نیست.'
                elif action in ('confirm', 'deny'):
                    reply = self.confirm(args.get('id'), action == 'confirm', user_id)
                elif action in ('approve', 'reject'):
                    reply = self.review(integer(args.get('id'), 'شناسهٔ رسید', 1), action == 'approve', user_id)
                elif action == 'link':
                    reply, markup = self.resolve_claim(args['id'], args['player'], user_id)
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
        self.db.say(chat_id, reply, markup)

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
        if name == 'link' and admin:
            pieces = rest.split(maxsplit=1)
            if len(pieces) != 2:
                raise UserError('/link کد_درخواست شناسه_بازیکن')
            return {'action': 'link', 'args': {'id': pieces[0], 'player': pieces[1]}}
        if name == 'do' and admin:
            try:
                obj = json.loads(rest)
                if not isinstance(obj, dict) or not isinstance(obj.get('action'), str) or not isinstance(obj.get('args', {}), dict):
                    raise ValueError()
                return obj
            except ValueError:
                raise UserError('پس از /do یک JSON معتبر شامل action و args بفرستید.') from None
        return {'action': 'help', 'args': {}}

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

    def confirm(self, ident, approve, actor):
        ident = text(ident, 'کد درخواست', limit=32)
        row = self.db.one('SELECT * FROM pending WHERE id=?', (ident,))
        if not row or row['status'] != 'pending':
            raise UserError('این درخواست موجود نیست یا قبلاً بررسی/لغو شده است.')
        payload = json.loads(row['payload'])
        if approve and row['kind'] == 'commands':
            if time.time() - row['created_at'] > 86400 or row['revision'] != self.db.revision:
                self.db.execute("UPDATE pending SET status='expired' WHERE id=?", (ident,))
                raise UserError('از زمان پیشنهاد، اطلاعات تغییر کرده یا ۲۴ ساعت گذشته است؛ برای دیدن خلاصهٔ تازه دوباره درخواست بدهید.')
            self.db.execute('SAVEPOINT apply_commands')
            try:
                for index, command in enumerate(payload):
                    self.service.apply(command, actor, f'proposal:{ident}:{index}')
            except Exception:
                self.db.execute('ROLLBACK TO apply_commands')
                raise
            finally:
                self.db.execute('RELEASE apply_commands')
        elif approve and row['kind'] == 'identity':
            self.service.bind(payload['player'], payload['user_id'], actor)
        elif approve:
            raise UserError('ابتدا تطبیق بازیکن را با /link مشخص کنید.')
        self.db.execute('UPDATE pending SET status=? WHERE id=?', ('approved' if approve else 'rejected', ident))
        if row['kind'] in ('identity', 'claim'):
            self.db.say(row['chat_id'], 'هویت شما تأیید شد.\n' + self.service.debt(payload['player']) if approve
                        else 'درخواست شناسایی تأیید نشد. لطفاً با /identify نام کامل دقیق را دوباره بفرستید.')
        if approve and row['kind'] == 'commands':
            for command in payload:
                if command['action'] == 'finalize':
                    for player in self.db.all('''SELECT p.id,p.bale_id FROM attendance a JOIN players p ON p.id=a.player_id
                                                WHERE a.session_id=? AND p.bale_id IS NOT NULL''', (command['args']['session'],)):
                        self.db.say(player['bale_id'], 'حساب سانس ثبت شد.\n' + self.service.debt(player['id']))
            self.ask_missing()
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