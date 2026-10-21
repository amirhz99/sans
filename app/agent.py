"""LangGraph-powered conversational agent for the football session manager."""

import json
import logging
import time
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import tools_condition

from .service import TEHRAN, UserError, integer, setting_value_is_explicit

LOG = logging.getLogger('sans.agent')


AGENT_SYSTEM_PROMPT = """تو «{bot_name}» هستی؛ دستیار هوشمند، صمیمی، دقیق و همه‌فن‌حریفِ مدیریت سانس فوتبال در پیام‌رسان بله.
تو یک ربات خشک و قالبی نیستی؛ مثل یک مسئول واقعی سانس فوتبال فکر می‌کنی، گفتگوهای گروه و پی‌وی را می‌فهمی، با ابزارهایت دیتابیس را می‌خوانی یا آپدیت می‌کنی و همیشه طبیعی، کوتاه و شفاف به فارسی جواب می‌دهی.

قوانین و اختیارات تو:
۱. درک کامل بافت و تاریخچه:
   - پیام‌های اخیر چت و وضعیت فعلی را می‌بینی. اگر کسی تاریخ شمسی، روز هفته یا عبارت محاوره‌ای (مثل «پنجشنبه»، «جلسه قبلی»، «سانس اول»، «هفته پیش») گفت، خودت آن را درک کن.
   - برای ثبت جلسات قدیمی یا ناقص، هیچ اجباری به داشتن ساعت یا تاریخ دقیق میلادی نیست؛ هر اطلاعاتی که کاربر داد (حتی فقط عنوان سانس و اسم حاضران) را با ابزار `manage_records` ثبت کن.

۲. مرز کارهای مستقیم و کارهای نیازمند تأیید ادمین:
   - کارهای خواندنی (`query_database`): همیشه آزاد. قبل از جواب دادن درباره بدهی، سانس یا حاضران، دیتابیس را چک کن.
   - کارهای اجرایی روزمره (`manage_records`):
     * اگر کاربر «ادمین» است: ساخت و ویرایش بازیکنان، ساخت و ویرایش سانس‌ها (جدید یا قدیمی/ناقص)، و ثبت حضور و غیاب (تکی یا دسته‌جمعی برای چند نفر) را مستقیم و فوری با `manage_records` انجام بده و نتیجه را صمیمی گزارش کن.
     * تغییر تنظیمات (`card`، `payment_note`، `approval_required`) را با عملیات `update_settings` فقط به‌صورت پیشنهاد با دکمهٔ تأیید بساز؛ مقدار باید صریحاً در پیام همین نوبت کاربر نوشته شده باشد و هرگز از حافظه، پیام قبلی یا پاسخ‌هایی مثل «آره» برداشت نشود.
     * اگر کاربر «بازیکن عادی» است: می‌تواند با `manage_records` خودش را معرفی کند (`identify`) یا حضور خودش را در سانس باز ثبت/لغو کند (`attendance_self`).
     * هویت بازیکن‌ها را تمیز نگه دار: هرگز برای یک نام کوتاه، ناقص یا با املای متفاوت بازیکن جدید نساز. اگر ابزار فهرست بازیکن‌های مشابه را برگرداند، همان‌ها را با #شناسه نشان بده و نام کامل را بپرس.
     * تغییرنام (`rename_player`) با درخواست صریح ادمین مستقیم انجام می‌شود؛ ادغام (`merge_player`) و حذف (`delete_player`) فقط با شناسه یا نام دقیق و همیشه به‌صورت کارت تأیید.
     * برای یافتن نام‌های تکراری از `query_database` با target='duplicate_players' استفاده کن.
   - تغییرات مالی در دفترکل (`propose_financial_change`):
     * نهایی‌سازی بدهی سانس (`finalize`)، اصلاح دستی بدهی/طلب بازیکن (`adjustment`)، تسویه حساب (`settle`) و اعلام بدهی توسط بازیکن (`declare_debt`) حساس هستند. برای این‌ها از `propose_financial_change` استفاده کن تا کارت تأیید با دکمه شیشه‌ای ساخته شود.
   - پرسش از ادمین‌ها (`ask_admins`):
     * اگر بازیکنی در پی‌وی ادعا یا سوالی مطرح کرد که به نظر ادمین نیاز دارد یا اطلاعات سانس/کارت هنوز مشخص نیست، با `ask_admins` در گروه ادمین بپرس.

۳. لحن و سبک پاسخ:
   - طبیعی، محترمانه، خودمانی و مختصر صحبت کن.
   - فقط و فقط متن طبیعی فارسی بنویس؛ هرگز خروجی JSON یا ساختار کد نده.
   - همه مبالغ به تومان است (مگر کاربر صراحتاً ریال بگوید که به تومان تبدیل می‌کنی).
   - هزینه زمین (`venue_cost`) در بدهی بازیکنان وارد نمی‌شود و بدهی هر نفر در سانس نهایی‌شده برابر است با `سهم هر نفر × (۱ + تعداد مهمان)`.

۴. مرز موضوعی:
   - فقط دربارهٔ مدیریت سانس فوتبال و امور مستقیم آن مثل برنامهٔ سانس، بازیکنان، حضور، بدهی، پرداخت، رسید و راهنمایی ربات پاسخ بده.
   - اگر درخواست دربارهٔ موضوعی نامرتبط است، آن را انجام نده و دربارهٔ آن توضیح یا پاسخ عمومی ارائه نکن؛ هیچ ابزاری را اجرا نکن.
   - در پاسخ به موضوع نامرتبط، کوتاه و روشن بگو: «این درخواست نامرتبط است؛ من فقط مسئول مدیریت سانس فوتبال هستم و نمی‌توانم به آن پاسخ بدهم.»
"""


def _unwrap_accidental_json(reply: str) -> str:
    """If a model accidentally returns a legacy JSON intent string, extract readable Persian text."""
    text = (reply or '').strip()
    if not text:
        return ''
    if text.startswith('```'):
        lines = [line for line in text.splitlines() if not line.strip().startswith('```')]
        text = '\n'.join(lines).strip()
    if text.startswith('{') and text.endswith('}'):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                for key in ('question', 'reply', 'message', 'text'):
                    val = obj.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
                action = obj.get('action')
                if action == 'help':
                    return 'من می‌توانم سانس‌ها، بازیکنان، حضور و غیاب، رسیدها و بدهی‌ها را مدیریت کنم. هر کاری دارید به زبان ساده بگویید!'
        except Exception:
            pass
    return reply.strip()



class SansAgent:
    """Standard LangGraph ReAct agent bound to the SQLite service layer."""

    def __init__(self, bot):
        self.bot = bot

    def _build_live_snapshot(self, chat_id: int, user_id: int, admin: bool, sender: dict | None) -> str:
        db = self.bot.db
        service = self.bot.service
        now = datetime.now(TEHRAN).strftime('%Y-%m-%d %H:%M (%A)')
        linked = service.linked(user_id)
        open_sessions = db.all('SELECT * FROM sessions WHERE finalized=0 ORDER BY id DESC LIMIT 5')
        recent_sessions = db.all('SELECT * FROM sessions ORDER BY id DESC LIMIT 8')
        players = db.all('SELECT id, name, bale_id, note FROM players ORDER BY id')
        chat_log = db.recent_messages(chat_id, limit=14)

        snapshot = {
            'now_tehran': now,
            'caller': {
                'user_id': user_id,
                'chat_id': chat_id,
                'role': 'admin' if admin else 'player',
                'sender_name': (sender or {}).get('first_name', ''),
                'linked_player': dict(linked) if linked else None,
            },
            'settings': {
                'card': db.setting('card') or 'ثبت نشده',
                'payment_note': db.setting('payment_note') or '',
                'default_location': self.bot.content_defaults.get('location', ''),
                'default_person_fee': self.bot.content_defaults.get('person_fee', 150000),
                'default_venue_cost': self.bot.content_defaults.get('venue_cost', 2400000),
            },
            'open_sessions': [dict(s) for s in open_sessions],
            'recent_sessions': [
                {
                    'id': s['id'],
                    'title': s['title'],
                    'when_text': s['when_text'],
                    'person_fee': s['person_fee'],
                    'finalized': bool(s['finalized']),
                }
                for s in recent_sessions
            ],
            'players_summary': (
                [{'id': p['id'], 'name': p['name'], 'balance': service.balance(p['id'])} for p in players]
                if admin
                else [{'id': p['id'], 'name': p['name']} for p in players]
            ),
            'recent_chat_messages': chat_log[-10:],
        }
        return json.dumps(snapshot, ensure_ascii=False)

    def _make_tools(self, chat_id: int, user_id: int, admin: bool, sender: dict | None,
                    effects: dict[str, Any], current_message: str = ''):
        bot = self.bot
        service = bot.service
        db = bot.db

        @tool
        def query_database(target: str, reference: str = '') -> str:
            """Read data from the database.
            target: 'overview', 'players', 'sessions', 'session_details', 'debt', 'pending', 'duplicate_players'.
            reference: optional player name/ID or session title/ID.
            """
            try:
                if target == 'overview':
                    return service.report() if admin else service.session_info(None, admin=False, level='details')
                if target == 'players':
                    rows = db.all('SELECT * FROM players ORDER BY id')
                    if not rows:
                        return 'هنوز هیچ بازیکنی ثبت نشده است.'
                    lines = []
                    for p in rows:
                        if admin:
                            bal = service.balance(p['id'])
                            lines.append(
                                f"#{p['id']} {p['name']} | مانده: {bal:,} تومان | "
                                f"متصل: {'بله' if p['bale_id'] else 'خیر'} | یادداشت: {p['note'] or '-'}"
                            )
                        else:
                            lines.append(f"#{p['id']} {p['name']}")
                    return '\n'.join(lines)
                if target == 'sessions':
                    rows = db.all('SELECT * FROM sessions ORDER BY id DESC LIMIT 30')
                    if not rows:
                        return 'هنوز هیچ سانسی ثبت نشده است.'
                    lines = []
                    for s in rows:
                        att = db.all(
                            'SELECT p.name, a.guests FROM attendance a JOIN players p ON p.id=a.player_id WHERE a.session_id=? ORDER BY p.id',
                            (s['id'],),
                        )
                        att_str = '، '.join(f"{r['name']}" + (f" (+{r['guests']})" if r['guests'] else '') for r in att) or 'بدون حاضر'
                        lines.append(
                            f"سانس #{s['id']} «{s['title']}» | زمان: {s['when_text'] or s['session_at'] or 'نامشخص'} | "
                            f"سهم هر نفر: {s['person_fee']:,} | وضعیت: {'نهایی‌شده' if s['finalized'] else 'باز'} | حاضران ({len(att)}): {att_str}"
                        )
                    return '\n'.join(lines)
                if target == 'session_details':
                    s = service.session(reference or None)
                    effects['last_session_id'] = s['id']
                    att = db.all(
                        'SELECT p.id, p.name, a.guests FROM attendance a JOIN players p ON p.id=a.player_id WHERE a.session_id=? ORDER BY p.id',
                        (s['id'],),
                    )
                    info = {
                        'id': s['id'],
                        'title': s['title'],
                        'when_text': s['when_text'],
                        'session_at': s['session_at'],
                        'location': s['location'],
                        'person_fee': s['person_fee'],
                        'venue_cost': s['venue_cost'] if admin else None,
                        'finalized': bool(s['finalized']),
                        'note': s['note'],
                        'attendees': [dict(r) for r in att],
                    }
                    return json.dumps(info, ensure_ascii=False)
                if target == 'debt':
                    if admin and reference:
                        p = service.player(reference)
                        effects['last_player_id'] = p['id']
                        return service.debt(p['id'])
                    if admin and not reference:
                        return service.report()
                    linked = service.linked(user_id)
                    if not linked:
                        return 'حساب بله این کاربر هنوز به بازیکنی متصل نشده است. ابتدا باید نام کاملش را بگوید تا با ابزار identify ثبت شود.'
                    return service.debt(linked['id'])
                if target == 'pending':
                    if not admin:
                        return 'فقط ادمین به لیست انتظار دسترسی دارد.'
                    rows = db.all("SELECT * FROM pending WHERE status='pending' ORDER BY created_at")
                    receipts = db.all("SELECT r.*, p.name FROM receipts r JOIN players p ON p.id=r.player_id WHERE r.status='pending'")
                    return json.dumps(
                        {'pending': [dict(r) for r in rows], 'receipts': [dict(r) for r in receipts]},
                        ensure_ascii=False,
                    )
                if target == 'duplicate_players':
                    if not admin:
                        return 'فقط ادمین به فهرست نام‌های تکراری دسترسی دارد.'
                    return service.duplicates_report()
                return 'هدف جستجو نامعتبر است.'
            except Exception as exc:
                return f'خطا در خواندن اطلاعات: {exc}'

        @tool
        def manage_records(operation: str, data_json: str) -> str:
            """Directly create or update non-financial records.
            Operations:
            - 'identify': data_json='{"name": "نام کامل"}'
            - 'attendance_self': data_json='{"present": true, "guests": 0, "session": ""}'
            - 'upsert_players': (admin) data_json='{"players": [{"name": "علی رضایی", "note": ""}]}'; a name similar to an existing player is refused unless force=true.
            - 'save_session': (admin) create/update new or historical session; date is accepted as an alias for when_text. Jalali («۹ خرداد ساعت ۲۱», «1405/03/09 21:00») and Gregorian (YYYY-MM-DD) dates are both accepted; a Jalali date without a year uses the current Jalali year.
            - 'set_attendance': (admin) set present/absent players for a session; unknown names are refused, no player is ever created here.
            - 'rename_player': (admin) data_json='{"player": "#15", "name": "نام کامل"}' — keeps the id and the whole history.
            - 'merge_player': (admin) data_json='{"source": "#4", "target": "#15"}' — moves history and builds an approval card.
            - 'delete_player': (admin) data_json='{"player": "#4"}' — only for a player with no history; builds an approval card.
            - 'duplicates': (admin) list suspected duplicate or partial names.
            - 'update_settings': (admin) propose setting changes with approval buttons; data_json='{"card": "...", "payment_note": "...", "approval_required": true}'
            """
            try:
                data = json.loads(data_json) if isinstance(data_json, str) and data_json.strip() else {}
                if not isinstance(data, dict):
                    return 'فرمت data_json باید یک آبجکت JSON باشد.'

                if operation == 'identify':
                    return bot.identify(data.get('name', ''), user_id, chat_id, sender)

                if operation == 'attendance_self':
                    linked = service.linked(user_id)
                    if not linked:
                        return 'ابتدا باید هویت شما به عنوان بازیکن تایید و متصل شود.'
                    s = service.session(data.get('session') or None, draft=True)
                    present = bool(data.get('present', True))
                    guests = integer(data.get('guests', 0), 'تعداد مهمان', 0, 20)
                    if present:
                        db.execute(
                            '''INSERT INTO attendance VALUES (?,?,?) ON CONFLICT(session_id,player_id)
                               DO UPDATE SET guests=excluded.guests''',
                            (s['id'], linked['id'], guests),
                        )
                    else:
                        db.execute('DELETE FROM attendance WHERE session_id=? AND player_id=?', (s['id'], linked['id']))
                    db.audit(user_id, 'attendance_self', {'session': s['id'], 'player': linked['id'], 'present': present, 'guests': guests})
                    effects['last_session_id'] = s['id']
                    return f"حضور {linked['name']} در «{s['title']}» با وضعیت present={present} و مهمان={guests} ثبت شد."

                if not admin:
                    return 'خطا: این عملیات مدیریتی فقط برای ادمین مجاز است.'

                if operation == 'upsert_players':
                    unknown = set(data) - {'players', 'name', 'note', 'force'}
                    if unknown:
                        return 'کلید ناشناخته در درخواست بازیکن: ' + '، '.join(sorted(unknown))
                    raw_list = data.get('players') or ([data] if data.get('name') else [])
                    if not isinstance(raw_list, list):
                        return 'فهرست بازیکنان باید آرایه باشد.'
                    force = data.get('force') is True
                    created = []
                    for item in raw_list:
                        if isinstance(item, str):
                            p = service.ensure_player(item, actor=user_id, allow_similar=force)
                        elif isinstance(item, dict) and item.get('name'):
                            extra = set(item) - {'name', 'note'}
                            if extra:
                                return 'کلید ناشناخته در بازیکن: ' + '، '.join(sorted(extra))
                            p = service.ensure_player(item['name'], item.get('note', ''), actor=user_id,
                                                      allow_similar=force)
                        else:
                            continue
                        created.append(f"#{p['id']} {p['name']}")
                        effects['last_player_id'] = p['id']
                    return 'بازیکنان ثبت/به‌روز شدند: ' + ('، '.join(created) if created else 'موردی نبود')

                if operation == 'save_session':
                    unknown = set(data) - {'title', 'when_text', 'date', 'location', 'person_fee',
                                           'venue_cost', 'note', 'session', 'session_id'}
                    if unknown:
                        return 'کلید ناشناخته در سانس: ' + '، '.join(sorted(unknown))
                    s = service.save_session_flexible(
                        title=data.get('title'),
                        when_text=data.get('when_text') or data.get('date'),
                        location=data.get('location'),
                        person_fee=data.get('person_fee'),
                        venue_cost=data.get('venue_cost'),
                        note=data.get('note'),
                        session_ref=data.get('session') or data.get('session_id'),
                        actor=user_id,
                    )
                    effects['last_session_id'] = s['id']
                    return f"سانس #{s['id']} «{s['title']}» با موفقیت ذخیره شد (زمان: {s['when_text'] or s['session_at'] or 'بدون تاریخ'}، سهم هر نفر: {s['person_fee']:,} تومان)."

                if operation == 'set_attendance':
                    unknown = set(data) - {'session', 'session_id', 'present', 'attendees',
                                           'players', 'absent'}
                    if unknown:
                        return 'کلید ناشناخته در ثبت حضور: ' + '، '.join(sorted(unknown))
                    present = data.get('present', data.get('attendees', data.get('players', [])))
                    absent = data.get('absent', [])
                    if not isinstance(present, list) or not isinstance(absent, list):
                        return 'فهرست حاضران و غایبان باید آرایه باشد.'
                    s_ref = data.get('session') or data.get('session_id') or effects.get('last_session_id')
                    try:
                        s = service.session(s_ref, draft=False)
                    except UserError:
                        if s_ref and isinstance(s_ref, str):
                            s = service.save_session_flexible(title=s_ref, actor=user_id)
                        else:
                            raise
                    if s['finalized']:
                        return 'این سانس نهایی شده است؛ برای تغییر مالی از propose_financial_change استفاده کنید.'
                    # Resolve every name first: a half-applied attendance list is
                    # worse than a clear error, and no player is created here.
                    planned, missing = [], []
                    for item in present:
                        if isinstance(item, str):
                            p_name, guests = item, 0
                        elif isinstance(item, dict):
                            extra = set(item) - {'name', 'player', 'guests'}
                            if extra:
                                return 'کلید ناشناخته در حاضران: ' + '، '.join(sorted(extra))
                            p_name = item.get('name') or item.get('player') or ''
                            guests = integer(item.get('guests', 0), 'مهمان', 0, 50)
                        else:
                            continue
                        try:
                            planned.append((service.player(p_name), guests))
                        except UserError:
                            missing.append(str(p_name).strip() or '—')
                    if missing:
                        return ('حضور ثبت نشد؛ این نام‌ها در فهرست بازیکنان نیست:\n' + '، '.join(missing) +
                                '\nاول با upsert_players بازیکن را با نام کامل ثبت کنید یا نام درست را از فهرست انتخاب کنید.')
                    added = []
                    for p, guests in planned:
                        db.execute(
                            '''INSERT INTO attendance VALUES (?,?,?) ON CONFLICT(session_id,player_id)
                               DO UPDATE SET guests=excluded.guests''',
                            (s['id'], p['id'], guests),
                        )
                        added.append(f"{p['name']}" + (f" (+{guests} مهمان)" if guests else ''))
                    removed = []
                    for item in data.get('absent', []):
                        p_name = item.get('name') if isinstance(item, dict) else str(item)
                        try:
                            p = service.player(p_name)
                            db.execute('DELETE FROM attendance WHERE session_id=? AND player_id=?', (s['id'], p['id']))
                            removed.append(p['name'])
                        except UserError:
                            continue
                    db.audit(user_id, 'set_attendance', {'session': s['id'], 'added': added, 'removed': removed})
                    effects['last_session_id'] = s['id']
                    return f"در سانس #{s['id']} «{s['title']}» حاضران ثبت شدند: {', '.join(added) or '-'}" + (
                        f" | حذف‌شده: {', '.join(removed)}" if removed else ''
                    )

                if operation == 'update_settings':
                    if not admin:
                        return 'خطا: این عملیات مدیریتی فقط برای ادمین مجاز است.'
                    allowed = ('card', 'payment_note', 'approval_required')
                    if any(key not in allowed for key in data):
                        return 'کلید تنظیم نامعتبر است.'
                    changes = {}
                    for key in allowed:
                        if key not in data or data[key] is None:
                            continue
                        value = data[key]
                        if key == 'approval_required' and not isinstance(value, bool):
                            return 'وضعیت تأیید باید true یا false باشد.'
                        if not setting_value_is_explicit(current_message, key, value):
                            return 'مقدار تنظیم باید صریحاً در همین پیام نوشته شود؛ پاسخ‌هایی مثل «آره انجامش بده» کافی نیستند.'
                        changes[key] = value
                    if not changes:
                        return 'مقدار تنظیمی برای تغییر مشخص نشد.'
                    # Settings are never written by the model; the admin must
                    # press «تأیید» on the attached inline keyboard.
                    preview, markup = bot.propose_settings(changes, user_id, chat_id)
                    effects['markup'] = markup
                    effects['proposal_preview'] = preview
                    return f'پیشنهاد تغییر تنظیمات آماده شد و دکمه‌های تأیید/رد ضمیمه گردید:\n{preview}'

                if operation == 'rename_player':
                    unknown = set(data) - {'player', 'name'}
                    if unknown:
                        return 'کلید ناشناخته در تغییرنام: ' + '، '.join(sorted(unknown))
                    result = service.rename_player(data.get('player', ''), data.get('name'), actor=user_id)
                    effects['last_player_id'] = result['id']
                    return (f"نام بازیکن #{result['id']} از «{result['old']}» به «{result['new']}» تغییر کرد؛ "
                            'شناسه و کل تاریخچه حفظ شد.')

                if operation in ('merge_player', 'merge_players'):
                    unknown = set(data) - {'source', 'target', 'source_id', 'target_id'}
                    if unknown:
                        return 'کلید ناشناخته در ادغام: ' + '، '.join(sorted(unknown))
                    cmd = {'action': 'player_merge', 'args': {
                        'source': data.get('source', data.get('source_id')),
                        'target': data.get('target', data.get('target_id'))}}
                    preview, markup = bot.propose(cmd, user_id, chat_id)
                    effects['markup'] = markup
                    effects['proposal_preview'] = preview
                    return f'پیشنهاد ادغام آماده شد و دکمه‌های تأیید/رد ضمیمه گردید:\n{preview}'

                if operation == 'delete_player':
                    unknown = set(data) - {'player'}
                    if unknown:
                        return 'کلید ناشناخته در حذف بازیکن: ' + '، '.join(sorted(unknown))
                    cmd = {'action': 'player_delete', 'args': {'player': data.get('player', '')}}
                    preview, markup = bot.propose(cmd, user_id, chat_id)
                    effects['markup'] = markup
                    effects['proposal_preview'] = preview
                    return f'پیشنهاد حذف بازیکن آماده شد و دکمه‌های تأیید/رد ضمیمه گردید:\n{preview}'

                if operation == 'duplicates':
                    return service.duplicates_report()

                return f'عملیات {operation} شناخته نشد.'
            except UserError as exc:
                return str(exc)
            except Exception as exc:
                return f'خطا در اجرای عملیات: {exc}'

        @tool
        def propose_financial_change(action: str, player: str = '', session: str = '', amount: int = 0, note: str = '') -> str:
            """Create an admin-approval proposal (with inline Yes/No buttons) for financial mutations.
            action: 'finalize', 'adjustment', 'settle', 'declare_debt'.
            """
            try:
                if action == 'declare_debt':
                    linked = service.linked(user_id)
                    if not linked:
                        return 'ابتدا باید حساب کاربری به نام بازیکن متصل شود.'
                    cmd = {'action': 'declare_debt', 'args': {'player': linked['id'], 'amount': amount, 'note': note or 'بدهی اعلامی کاربر'}}
                    preview, markup = bot.propose(cmd, user_id, chat_id)
                    db.say(bot.config.admin_group, f"درخواست ثبت بدهی از {linked['name']}:\n\n" + preview, markup)
                    return 'پیشنهاد ثبت بدهی برای تأیید به گروه ادمین‌ها ارسال شد.'

                if not admin:
                    return 'تغییرات مالی فقط توسط ادمین قابل پیشنهاد است.'

                if action == 'finalize':
                    cmd = {'action': 'finalize', 'args': {'session': session or effects.get('last_session_id') or 'current'}}
                elif action == 'adjustment':
                    # A financial correction never creates a player as a side
                    # effect; an unknown or partial name must be clarified.
                    p = service.player(player)
                    cmd = {'action': 'adjustment', 'args': {'player': p['id'], 'amount': amount, 'note': note or 'اصلاح حساب توسط ادمین'}}
                elif action == 'settle':
                    p = service.player(player)
                    cmd = {'action': 'settle', 'args': {'player': p['id']}}
                else:
                    return 'نوع تغییر مالی نامعتبر است (finalize, adjustment, settle, declare_debt).'

                preview, markup = bot.propose(cmd, user_id, chat_id)
                effects['markup'] = markup
                effects['proposal_preview'] = preview
                return f'پیشنهاد مالی آماده شد و دکمه‌های تأیید/رد ضمیمه گردید:\n{preview}'
            except UserError as exc:
                return str(exc)
            except Exception as exc:
                return f'خطا در ساخت پیشنهاد مالی: {exc}'

        @tool
        def ask_admins(question: str) -> str:
            """Send a question or escalation note to the admin group when something is ambiguous or needs admin input."""
            if not question or not str(question).strip():
                return 'متن سوال خالی است.'
            prefix = f'❓ پرسش دستیار هوشمند (مربوط به کاربر {user_id}):\n' if chat_id != bot.config.admin_group else '❓ پرسش:\n'
            db.say(bot.config.admin_group, prefix + str(question).strip())
            return 'سوال برای ادمین‌ها در گروه مدیریتی ارسال شد.'

        return [query_database, manage_records, propose_financial_change, ask_admins]

    def run(
        self,
        body: str,
        history: list[dict[str, str]],
        chat_id: int,
        user_id: int,
        admin: bool,
        sender: dict | None = None,
        llm_override: Any = None,
        current_message: str | None = None,
    ) -> tuple[str, list | None, dict[str, Any]]:
        """Execute the LangGraph StateGraph for one conversational turn."""
        turn_start = time.monotonic()
        sender_name = (
            ' '.join(filter(None, [(sender or {}).get('first_name', ''), (sender or {}).get('last_name', '')]))
            or (sender or {}).get('username', '')
            or str(user_id)
        )
        effects: dict[str, Any] = {'tool_calls': []}
        raw_tools = self._make_tools(chat_id, user_id, admin, sender, effects, current_message or body)

        # Wrap tools to log every tool invocation and its output
        tools = raw_tools

        if llm_override is not None:
            llm = llm_override.bind_tools(tools) if hasattr(llm_override, 'bind_tools') else llm_override
            llms = [llm]
            models = ['override']
        else:
            llm = ChatOpenAI(
                model=self.bot.config.ai_model,
                api_key=self.bot.config.ai_key,
                base_url=self.bot.config.ai_url,
                temperature=0.2,
                max_retries=1,
                timeout=35,
            )
            models = list(dict.fromkeys(
                (self.bot.config.ai_model,) + tuple(self.bot.config.ai_fallback_models)))
            llms = [llm.bind_tools(tools)]
            for fallback_model in models[1:]:
                llms.append(type(llm)(
                    model=fallback_model,
                    api_key=self.bot.config.ai_key,
                    base_url=self.bot.config.ai_url,
                    temperature=0.2,
                    max_retries=1,
                    timeout=35,
                ).bind_tools(tools))

        snapshot = self._build_live_snapshot(chat_id, user_id, admin, sender)
        # Do NOT append self.bot.system_prompt (which instructs legacy JSON-only output).
        sys_text = (
            AGENT_SYSTEM_PROMPT.format(bot_name=self.bot.config.bot_name)
            + '\n\nوضعیت زنده دیتابیس و چت (JSON):\n'
            + snapshot
        )

        working = {'index': 0}

        def call_model(state: MessagesState):
            t0 = time.monotonic()
            messages = [SystemMessage(content=sys_text)] + state['messages']
            # Start from the model that last answered; within one turn there are
            # usually two model calls (tool round + final answer) and there is no
            # reason to retry a model that already failed in this turn.
            order = list(range(len(llms)))
            start = working['index']
            order = order[start:] + order[:start]
            response = None
            last_error = None
            for position, index in enumerate(order):
                try:
                    response = llms[index].invoke(messages)
                    working['index'] = index
                    break
                except Exception as exc:
                    last_error = exc
                    if position + 1 >= len(order):
                        break
                    failed, nxt = models[index], models[order[position + 1]]
                    LOG.warning('LLM model %s failed (%s); trying fallback %s', failed, exc, nxt)
                    self.bot.db.log_agent_event(
                        chat_id,
                        user_id,
                        sender_name,
                        'LLM_FALLBACK',
                        summary=f'{failed} -> {nxt}',
                        details={'failed_model': failed, 'fallback_model': nxt,
                                 'error': f'{type(exc).__name__}: {exc}'[:300]},
                    )
            if response is None:
                if last_error is not None:
                    raise last_error
                raise RuntimeError('no AI model is configured')
            dt_ms = int((time.monotonic() - t0) * 1000)
            tcalls = getattr(response, 'tool_calls', None) or []
            if tcalls:
                for tc in tcalls:
                    effects['tool_calls'].append({'name': tc.get('name'), 'args': tc.get('args')})
                LOG.info(
                    'LLM tool_calls chat=%s user=%s (%dms): %s',
                    chat_id,
                    user_id,
                    dt_ms,
                    json.dumps(tcalls, ensure_ascii=False),
                )
                self.bot.db.log_agent_event(
                    chat_id,
                    user_id,
                    sender_name,
                    'LLM_TOOL_CALL',
                    summary=', '.join(str(tc.get('name')) for tc in tcalls),
                    details={'tool_calls': tcalls},
                    duration_ms=dt_ms,
                )
            return {'messages': [response]}

        tool_map = {t.name: t for t in tools}

        def run_tools_sequentially(state: MessagesState):
            """Execute tool calls strictly in order so dependent DB mutations (e.g. save_session -> set_attendance -> finalize) never race."""
            last_msg = state['messages'][-1]
            tcalls = getattr(last_msg, 'tool_calls', None) or []
            out_messages = []
            for tc in tcalls:
                t_name = tc.get('name', '')
                t_args = tc.get('args') or {}
                t_id = tc.get('id', '')
                t0 = time.monotonic()
                tool_fn = tool_map.get(t_name)
                if tool_fn is None:
                    res_text = f'ابزار ناشناخته: {t_name}'
                else:
                    try:
                        res_text = str(tool_fn.invoke(t_args))
                    except Exception as exc:
                        res_text = f'خطا در اجرای ابزار {t_name}: {exc}'
                dt_ms = int((time.monotonic() - t0) * 1000)
                self.bot.db.log_agent_event(
                    chat_id,
                    user_id,
                    sender_name,
                    'TOOL_RESULT',
                    summary=f"{t_name}: {res_text[:200]}",
                    details={'tool': t_name, 'args': t_args, 'result': res_text[:800]},
                    duration_ms=dt_ms,
                )
                out_messages.append(ToolMessage(content=res_text, name=t_name, tool_call_id=t_id))
            return {'messages': out_messages}

        builder = StateGraph(MessagesState)
        builder.add_node('agent', call_model)
        builder.add_node('tools', run_tools_sequentially)
        builder.add_edge(START, 'agent')
        builder.add_conditional_edges('agent', tools_condition)
        builder.add_edge('tools', 'agent')
        graph = builder.compile()

        lc_messages = []
        for item in history[-10:]:
            role = item.get('role')
            content = str(item.get('content', ''))
            if not content:
                continue
            if role == 'user':
                lc_messages.append(HumanMessage(content=content))
            elif role == 'assistant':
                lc_messages.append(AIMessage(content=_unwrap_accidental_json(content)))
        lc_messages.append(HumanMessage(content=body))

        result = graph.invoke({'messages': lc_messages}, config={'recursion_limit': 12})
        final_messages = result.get('messages', [])
        reply = ''
        for msg in reversed(final_messages):
            if isinstance(msg, AIMessage) and msg.content:
                if isinstance(msg.content, list):
                    reply = '\n'.join(
                        part.get('text', '') if isinstance(part, dict) else str(part)
                        for part in msg.content
                    ).strip()
                else:
                    reply = str(msg.content).strip()
                if reply:
                    break

        reply = _unwrap_accidental_json(reply)

        if not reply and effects.get('proposal_preview'):
            reply = effects['proposal_preview']
        elif effects.get('proposal_preview') and 'کد:' not in reply:
            reply = f"{reply}\n\n{effects['proposal_preview']}"

        total_ms = int((time.monotonic() - turn_start) * 1000)
        final_reply = reply or 'انجام شد.'
        LOG.info(
            'AGENT_TURN chat=%s user=%s (%s) admin=%s dur=%dms tools=%d | in=%r -> out=%r',
            chat_id,
            user_id,
            sender_name,
            admin,
            total_ms,
            len(effects['tool_calls']),
            body[:120],
            final_reply[:160],
        )
        self.bot.db.log_agent_event(
            chat_id,
            user_id,
            sender_name,
            'AGENT_REPLY',
            summary=final_reply[:300],
            details={
                'input': body[:500],
                'admin': admin,
                'tool_calls': effects['tool_calls'],
                'has_markup': bool(effects.get('markup')),
            },
            duration_ms=total_ms,
        )
        return final_reply, effects.get('markup'), effects
