"""LangGraph-powered conversational agent for the football session manager."""

import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import tools_condition

from . import jalali
from .service import (MAX_BATCH, TEHRAN, UserError, integer, normalize,
                      setting_value_is_explicit)

LOG = logging.getLogger('sans.agent')


def _jalali_short(value):
    """Render a date as «۱۴۰۵/۰۸/۰۶» for the model's snapshot (SPEC-026)."""
    jy, jm, jd = jalali.from_gregorian(value)
    return f'{jy:04d}/{jm:02d}/{jd:02d}'


def support_contact(bot):
    """Return the configured support handle for any bot-like object (SPEC-031).

    ``getattr`` keeps lightweight config stand-ins (and the legacy bot paths)
    working: an unset field simply means support is not configured.
    """
    return getattr(getattr(bot, 'config', None), 'support_handle', '') or ''



# SPEC-018: how much conversation the agent may look at.  The database keeps
# HISTORY_LIMIT entries; the agent reads this window and sends a shorter tail to
# the model so a long chat cannot inflate the prompt without bound.
CHAT_MEMORY_WINDOW = 30
SNAPSHOT_MESSAGES = 20
CONVERSATION_HISTORY = 20

# SPEC-031: the prompt never invents a contact.  When no support handle is
# configured, the rule points at the admins instead of showing a bare "@".
SUPPORT_FALLBACK_TEXT = 'با ادمین‌های گروه مدیریتی در تماس باش'


AGENT_SYSTEM_PROMPT = """تو «{bot_name}» هستی؛ دستیار هوشمند، صمیمی، دقیق و همه‌فن‌حریفِ مدیریت سانس فوتبال در پیام‌رسان بله.
تو یک ربات خشک و قالبی نیستی؛ مثل یک مسئول واقعی سانس فوتبال فکر می‌کنی، گفتگوهای گروه و پی‌وی را می‌فهمی، با ابزارهایت دیتابیس را می‌خوانی یا آپدیت می‌کنی و همیشه طبیعی، کوتاه و شفاف به فارسی جواب می‌دهی.

قوانین و اختیارات تو:
۱. درک کامل بافت و تاریخچه:
   - پیام‌های اخیر چت و وضعیت فعلی را می‌بینی. اگر کسی تاریخ شمسی، روز هفته یا عبارت محاوره‌ای (مثل «پنجشنبه»، «جلسه قبلی»، «سانس اول»، «هفته پیش») گفت، خودت آن را درک کن.
   - در بافت `now_tehran` تاریخ امروز به تقویم شمسی و با نام روز هفته آمده است؛ همین را مبنا بگیر و خودت تقویم میلادی را به شمسی تبدیل نکن.
   - تاریخ‌های نسبی («فردا»، «پس‌فردا»، «هفته بعد»، «آخر هفته»، «جمعه هفته بعد») را **دقیقاً همان‌طور که کاربر نوشت** در `when_text` بده؛ خودت تاریخ را حساب نکن و میلادی ننویس. ابزار خودش با ساعت تهران آن را حل می‌کند.
   - اگر ربات گفت روز هفته با تاریخ نمی‌خواند، همان پیشنهاد را به کاربر بگو و منتظر تأیید بمان؛ هرگز خودت تاریخ را عوض نکن و دوباره با روز دیگری نفرست. هر روزی برای سانس مجاز است.
   - برای ثبت جلسات قدیمی یا ناقص، هیچ اجباری به داشتن ساعت یا تاریخ دقیق میلادی نیست؛ هر اطلاعاتی که کاربر داد (حتی فقط عنوان سانس و اسم حاضران) را با ابزار `manage_records` ثبت کن.

۲. مرز کارهای مستقیم و کارهای نیازمند تأیید ادمین:
   - کارهای خواندنی (`query_database`): همیشه آزاد. قبل از جواب دادن درباره بدهی، سانس یا حاضران، دیتابیس را چک کن.
   - کارهای اجرایی روزمره (`manage_records`):
     * اگر کاربر «ادمین» است: ساخت و ویرایش بازیکنان، ساخت و ویرایش سانس‌ها (جدید یا قدیمی/ناقص)، و ثبت حضور و غیاب (تکی یا دسته‌جمعی برای چند نفر) را مستقیم و فوری با `manage_records` انجام بده و نتیجه را صمیمی گزارش کن.
     * تغییر تنظیمات (`card`، `payment_note`، `approval_required`) را با عملیات `update_settings` فقط به‌صورت پیشنهاد با دکمهٔ تأیید بساز؛ مقدار باید صریحاً در پیام همین نوبت کاربر نوشته شده باشد و هرگز از حافظه، پیام قبلی یا پاسخ‌هایی مثل «آره» برداشت نشود.
     * اگر کاربر «بازیکن عادی» است: می‌تواند با `manage_records` خودش را معرفی کند (`identify`) یا حضور خودش را در سانس باز ثبت/لغو کند (`attendance_self`).
     * کاربر تازه‌وارد: ربات خودش پیام اول را فرستاده و نام کامل و شمارهٔ موبایل را خواسته است. اگر در بافت `needs_introduction` درست است، همان توضیح را تکرار نکن، دوباره شماره نخواه و از واژه‌های داخلی مثل «اتصال حساب»، «تطبیق هویت» یا «بازیکن» استفاده نکن.
     * وقتی کاربر نام و نام خانوادگی‌اش را گفت، اول با `query_database` (target='players') ببین چنین بازیکنی هست یا نه؛ اگر یک تطبیق روشن بود همان را با #شناسه پیشنهاد بده و بعد از تأیید کاربر `identify` را با همان نام صدا بزن. اگر چند نام مشابه بود، نام کامل‌تر را بپرس. هرگز خودت نام نساز و بدون گفتن کاربر کارت هویت نساز. username را هم از او نپرس؛ خودت از پروفایل می‌خوانی.
     * هویت بازیکن‌ها را تمیز نگه دار: هرگز برای یک نام کوتاه، ناقص یا با املای متفاوت بازیکن جدید نساز. اگر ابزار فهرست بازیکن‌های مشابه را برگرداند، همان‌ها را با #شناسه نشان بده و نام کامل را بپرس.
     * فهرست کامل بازیکنان از قبل در بافت نمی‌آید؛ هر وقت به نام یا شناسهٔ بازیکنان نیاز داشتی، `query_database` با target='players' را صدا بزن.
     * تغییرنام (`rename_player`) با درخواست صریح ادمین مستقیم انجام می‌شود؛ ادغام (`merge_player`) و حذف (`delete_player`) فقط با شناسه یا نام دقیق و همیشه به‌صورت کارت تأیید.
     * برای یافتن نام‌های تکراری از `query_database` با target='duplicate_players' استفاده کن.
     * بستن کارت‌های تأیید باز (`expire_proposals`): اگر ادمین گفت «کارت‌های منتظر را ببند»، «پیشنهادها را کنسل کن» یا «کارت‌های منقضی را ببند»، این عملیات را صدا بزن. `older_than_hours` را خالی بگذار یعنی فقط کارت‌های قدیمی‌تر از ۲۴ ساعت، و برای «همه» مقدار ۰ بده. این کار فقط کارت را می‌بندد و هیچ مالی اعمال نمی‌شود؛ همین را شفاف بگو. کارت هویت/اتصال را دست نزن.
   - تغییرات مالی در دفترکل (`propose_financial_change`):
     * نهایی‌سازی بدهی سانس (`finalize`)، اصلاح دستی بدهی/طلب بازیکن (`adjustment`)، تسویه حساب (`settle`) و اعلام بدهی توسط بازیکن (`declare_debt`) حساس هستند. برای این‌ها از `propose_financial_change` استفاده کن تا کارت تأیید با دکمه شیشه‌ای ساخته شود.
     * اگر درخواست تسویه چند نفره است («بدهی همه را صاف کن»، «حساب این سه نفر را تسویه کن»)، همهٔ نام‌ها را در آرگومان `players` و جداشده با کاما بده تا **یک کارت واحد** ساخته شود. هرگز برای هر نفر کارت جدا نساز و برای هر نفر جداگانه ابزار را صدا نزن.
     * اصلاح حساب چند نفره با مبلغ یکسان: نام‌ها را در `players` و مبلغ مشترک را در `amount` بده.
     * اصلاح حساب با مبلغ متفاوت برای هر نفر: `entries` را به‌صورت JSON بده؛ مثلاً `[{{"player":"مهدی","amount":-150000}},{{"player":"رضا","amount":50000}}]`. هرگز `entries` را با `players` یا `amount` ترکیب نکن.
     * در تسویهٔ گروهی، اعضایی که بدهی مثبت ندارند خودکار کنار گذاشته می‌شوند؛ لازم نیست آن‌ها را حذف کنی یا برایشان کارت بسازی.
     * برای هر درخواست، حتی با ده نفر، فقط **یک بار** این ابزار را صدا بزن.
   - پیام به بازیکنان و مخاطبان (`message_players`):
     * این ربات می‌تواند و می‌کند که خودش شروع‌کنندهٔ پیام باشد: بعد از اینکه ادمین دکمهٔ «تأیید» کارت را بزند، پیام در پی‌وی گیرنده ارسال می‌شود. ربات «فقط پاسخ‌دهنده» نیست؛ هرگز به ادمین نگو «من نمی‌توانم پیام بفرستم»، «فقط به پیام‌هایی که به من می‌رسد جواب می‌دهم» یا «این کار از عهدهٔ من خارج است».
     * اگر ادمین خواست پیامی به یک یا چند نفر برسد («به همه بگو سانس فردا لغو شد»، «به مهدی و رضا بگو کارت عوض شد»)، متن دقیق پیام را در `text` و نام‌ها/شناسه‌ها را در `players` بده.
     * مخاطب دو حالت دارد و باید صریح انتخاب شود:
       - `audience='players'` (پیش‌فرض): فقط بازیکن‌ها؛ هم بازیکن متصل به بله و هم بازیکنی که درخواست شناسایی باز دارد.
       - `audience='all'`: هر کسی که ربات را در پی‌وی استارت کرده، حتی اگر بازیکن نباشد. برای «همهٔ مخاطبان»، «هر کسی که ربات را استارت کرده»، «به همه در پی‌وی بگو» این حالت را بده و `players` را خالی بگذار. این حالت را با فهرست نام‌ها ترکیب نکن.
     * بازیکنی که هنوز به بله متصل نشده ولی درخواست شناسایی باز دارد، پیام می‌گیرد. کارت تأیید این گیرنده‌ها را با برچسب «بدون بازیکن تأییدشده» نشان می‌دهد؛ لازم نیست خودت هشدار بدهی، فقط کارت را بساز.
     * اگر ادمین خواست همراه پیام شماره هم گرفته شود («شماره‌شان را بگیر»، «ازشون شماره بخواه»)، `request_phone=true` بده تا دکمهٔ «ارسال شمارهٔ من» هم با پیام برود.
     * این ابزار فقط کارت تأیید می‌سازد؛ ارسال واقعی بعد از تأیید ادمین انجام می‌شود. پیام را خودت دوباره نفرست و برای هر نفر جداگانه صدا نزن. اگر ادمین پرسید چه کسی پیام می‌گیرد، `query_database` با target='contacts' یا target='players' را ببین و همان اعداد را بگو.
   - پرسش از ادمین‌ها (`ask_admins`):
     * اگر بازیکنی در پی‌وی ادعا یا سوالی مطرح کرد که به نظر ادمین نیاز دارد یا اطلاعات سانس/کارت هنوز مشخص نیست، با `ask_admins` در گروه ادمین بپرس.
   - شمارهٔ تماس، username و شناسهٔ بلهٔ بازیکن:
     * اگر کاربر «ادمین» است، این اطلاعات را آزادانه و کامل نشان بده: فهرست بازیکن‌ها (`query_database` target='players')، درخواست‌های شناسایی باز (target='identities') و مخاطبان ربات (target='contacts'). هرگز به ادمین نگو «نمی‌توانم شماره را نشان بدهم» یا «این اطلاعات محرمانه است».
     * اگر کاربر «بازیکن عادی» است، شماره و username و شناسهٔ بلهٔ هیچ بازیکن دیگری را بازگو نکن و فقط شمارهٔ خودش را با /phone نشان بده.
     * شماره فقط با دکمهٔ «ارسال شمارهٔ من» ثبت می‌شود. هرگز شماره‌ای را از متن پیام حدس نزن و از کاربر نخواه شماره را تایپ کند. اگر کسی شماره می‌خواهد، به /phone در پی‌وی راهنمایی کن.
     * اگر ادمین شماره‌ای را در پیام خودش نوشت و خواست ثبت شود («شمارهٔ علی ۰۹۱۲۱۲۳۴۵۶۷ است، ثبتش کن»)، با `manage_records` و عملیات `set_phone` همان عدد را ثبت کن. شماره‌ای که در همین پیام نوشته نشده باشد قابل ثبت نیست.
     * کد کارت تأیید باز (مثل `9b7898198d6e47ac`) را از target='pending' یا target='identities' بخوان؛ اگر ادمین به یک کارت ریپلای کرد، همان کد داخل متن کارت است.

۳. لحن و سبک پاسخ:
   - طبیعی، محترمانه، خودمانی و مختصر صحبت کن.
   - فقط و فقط متن طبیعی فارسی بنویس؛ هرگز خروجی JSON یا ساختار کد نده.
   - همه مبالغ به تومان است (مگر کاربر صراحتاً ریال بگوید که به تومان تبدیل می‌کنی).
   - هزینه زمین (`venue_cost`) در بدهی بازیکنان وارد نمی‌شود و بدهی هر نفر در سانس نهایی‌شده برابر است با `سهم هر نفر × (۱ + تعداد مهمان)`.

۵. ارجاع به پشتیبان انسانی ({support_contact}):
   - اگر کاربر گفت مشکلی پیش آمده که خودت نتوانستی حل کنی، یا صریحاً پرسید «با کی صحبت کنم»، «پشتیبان کیه»، «کجا پیام بدم» یا «با آدم واقعی حرف بزنم»، آیدی {support_contact} را همان‌طور که نوشته شده بده و بگو برای پیگیری مستقیم به همین آیدی پیام بدهد.
   - این آیدی را از خودت نساز، تغییر نده، به شکل لینک یا یوزرنیم دیگری بازنویسی نکن و به کاربر دیگری نسبت نده. فقط همین یک مقدار درست است.
   - اگر خودت توانستی مشکل را حل کنی، اول راه‌حل را بده؛ لازم نیست هر پاسخ را با آیدی پشتیبان تمام کنی. در پاسخ‌های نامرتبط به این آیدی اشاره نکن.

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
        moment = datetime.now(TEHRAN)
        today = moment.date()
        # SPEC-026: the model reads a Jalali «now» with a Persian weekday, so it
        # never has to convert «Tuesday» or a Gregorian date into what the
        # operator writes.
        today_text = (f'{jalali.format_date(today)}، {jalali.weekday_name(today)}، '
                      f'ساعت {jalali.persian_digits(f"{moment:%H:%M}")}')
        linked = service.linked(user_id)
        open_sessions = db.all('SELECT * FROM sessions WHERE finalized=0 ORDER BY id DESC LIMIT 5')
        recent_sessions = db.all('SELECT * FROM sessions ORDER BY id DESC LIMIT 8')
        players_count = db.one('SELECT COUNT(*) n FROM players')['n']
        chat_log = db.recent_messages(chat_id, limit=CHAT_MEMORY_WINDOW)
        defaults = service.session_defaults()
        profile = sender if isinstance(sender, dict) else {}
        sender_name = ' '.join(
            part.strip() for part in (profile.get('first_name', ''), profile.get('last_name', ''))
            if isinstance(part, str) and part.strip())
        has_pending_identity = bool(db.one(
            "SELECT 1 FROM pending WHERE requester=? AND kind IN ('identity','claim') "
            "AND status='pending'", (user_id,)))

        snapshot = {
            'now_tehran': today_text,
            # SPEC-026: precomputed Jalali relatives so the model never converts
            # a calendar or a English weekday name itself.
            'tomorrow': _jalali_short(today + timedelta(days=1)),
            'day_after': _jalali_short(today + timedelta(days=2)),
            'caller': {
                'user_id': user_id,
                'chat_id': chat_id,
                'role': 'admin' if admin else 'player',
                'sender_name': sender_name,
                'linked_player': ({'id': linked['id'], 'name': linked['name']} if linked else None),
                # SPEC-021: the bot already sent the first-contact text, so the
                # model must not repeat it.  Only these two facts are exposed;
                # the address book and any number stay out of the snapshot.
                'needs_introduction': not admin and linked is None,
                'has_pending_identity': has_pending_identity,
            },
            'settings': {
                'card': db.setting('card') or 'ثبت نشده',
                'payment_note': db.setting('payment_note') or '',
                'default_location': defaults['location'],
                'default_person_fee': defaults['person_fee'],
                'default_venue_cost': defaults['venue_cost'],
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
            # AGENTS.md: never send the complete player directory to the model.
            # The agent reads names or balances on demand through query_database.
            'players_count': players_count,
            'recent_chat_messages': chat_log[-SNAPSHOT_MESSAGES:],
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
            target: 'overview', 'players', 'sessions', 'session_details', 'debt', 'pending',
                    'duplicate_players', 'identities', 'contacts'.
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
                                f"متصل: {'بله' if p['bale_id'] else 'خیر'} | "
                                f"شماره: {p['phone'] or '-'} | یادداشت: {p['note'] or '-'}"
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
                if target == 'identities':
                    # SPEC-019: the admin may read the phone number, username, and
                    # Bale user id of an open identity request.  A player never can.
                    if not admin:
                        return 'فقط ادمین به درخواست‌های شناسایی دسترسی دارد.'
                    rows = db.all("SELECT id, payload FROM pending "
                                  "WHERE kind IN ('identity','claim') AND status='pending' "
                                  "ORDER BY created_at LIMIT 50")
                    if not rows:
                        return 'درخواست شناسایی باز وجود ندارد.'
                    lines = []
                    for row in rows:
                        try:
                            payload = json.loads(row['payload'])
                        except (TypeError, ValueError):
                            payload = {}
                        if not isinstance(payload, dict):
                            payload = {}
                        player = payload.get('player')
                        lines.append(
                            f"کد {row['id']} | نام اعلامی: {payload.get('name') or '-'} | "
                            f"بازیکن پیشنهادی: {('#' + str(player)) if player else 'ندارد'} | "
                            f"نام نمایشی: {payload.get('display_name') or '-'} | "
                            f"username: {('@' + payload['username']) if payload.get('username') else '-'} | "
                            f"شماره: {payload.get('phone_number') or '-'} | "
                            f"شناسهٔ بله: {payload.get('user_id')}")
                    return '\n'.join(lines)
                if target == 'contacts':
                    # SPEC-019: everybody who ever started the bot in private.
                    if not admin:
                        return 'فقط ادمین به فهرست مخاطبان دسترسی دارد.'
                    rows = db.all('SELECT user_id, chat_id, display_name, username, phone, updated_at '
                                  'FROM contacts ORDER BY updated_at DESC, user_id LIMIT 50')
                    if not rows:
                        return 'هنوز هیچ مخاطبی ربات را در پی‌وی استارت نکرده است.'
                    bound = {p['bale_id']: p for p in
                             db.all('SELECT id, name, bale_id FROM players WHERE bale_id IS NOT NULL')}
                    lines = []
                    for row in rows:
                        player = bound.get(row['user_id'])
                        lines.append(
                            f"شناسهٔ بله: {row['user_id']} | "
                            f"بازیکن: {(('#' + str(player['id']) + ' ' + player['name']) if player else 'غیربازیکن')} | "
                            f"نام نمایشی: {row['display_name'] or '-'} | "
                            f"username: {('@' + row['username']) if row['username'] else '-'} | "
                            f"شماره: {row['phone'] or '-'} | آخرین تماس: {row['updated_at']}")
                    return '\n'.join(lines)
                return 'هدف جستجو نامعتبر است.'
            except Exception as exc:
                return f'خطا در خواندن اطلاعات: {exc}'

        @tool
        def manage_records(operation: str, data_json: str) -> str:
            """Directly create or update non-financial records.
            Operations:
            - 'identify': data_json='{"name": "نام کامل"}' — شمارهٔ کاربر، اگر قبلاً با دکمهٔ
              «ارسال شمارهٔ من» فرستاده شده باشد، خودکار روی کارت ادمین می‌آید؛ آن را از
              کاربر دوباره نپرس و در data_json نگذار.
            - 'attendance_self': data_json='{"present": true, "guests": 0, "guest_names": ["علی"], "session": ""}'
              guest_names is optional: only the names the caller actually stated.
            - 'upsert_players': (admin) data_json='{"players": [{"name": "علی رضایی", "note": ""}]}'; a name similar to an existing player is refused unless force=true.
            - 'save_session': (admin) create/update new or historical session; date is accepted as an alias for when_text. Jalali («۹ خرداد ساعت ۲۱», «1405/03/09 21:00») and Gregorian (YYYY-MM-DD) dates are both accepted; a Jalali date without a year uses the current Jalali year.
            - 'set_attendance': (admin) set present/absent players for a session; unknown names are refused, no player is ever created here. Each attendee may carry guest_names: [{"name":"علی","guests":2,"guest_names":["رضا"]}].
            - 'rename_player': (admin) data_json='{"player": "#15", "name": "نام کامل"}' — keeps the id and the whole history.
            - 'merge_player': (admin) data_json='{"source": "#4", "target": "#15"}' — moves history and builds an approval card.
            - 'delete_player': (admin) data_json='{"player": "#4"}' — only for a player with no history; builds an approval card.
            - 'duplicates': (admin) list suspected duplicate or partial names.
            - 'set_phone': (admin) data_json='{"player": "#15", "phone": "09121234567"}'
              records a number the admin typed in this same message; a number from
              memory or an earlier message is refused.
            - 'expire_proposals': (admin) close open approval cards; data_json='{"older_than_hours": 0}'
              where 0 closes every open card and an omitted value means the 24 hour TTL.
              This only removes the cards; nothing financial is applied.
            - 'links': (admin) list the existing player links.
            - 'suggest_links': (admin) propose pairs that share a family name; every
              pair becomes an approval card, and nothing links without a click.
            - 'link_players' / 'unlink_players': (admin) data_json='{"player":"#12","partner":"#18"}'
              builds ONE approval card; it never links or unlinks directly.
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
                    names = service._clean_guest_names(data.get('guest_names'))
                    if names is not None and len(names) > guests:
                        return 'تعداد نام مهمان‌ها از تعداد مهمان بیشتر است.'
                    if present:
                        db.execute(
                            '''INSERT INTO attendance(session_id,player_id,guests,own)
                               VALUES (?,?,?,1) ON CONFLICT(session_id,player_id)
                               DO UPDATE SET guests=excluded.guests, own=excluded.own''',
                            (s['id'], linked['id'], guests),
                        )
                        if names is not None:
                            service._store_guest_names(s['id'], linked['id'], names)
                    else:
                        db.execute('DELETE FROM attendance WHERE session_id=? AND player_id=?',
                                   (s['id'], linked['id']))
                        db.execute('DELETE FROM attendance_guests WHERE session_id=? AND host_id=?',
                                   (s['id'], linked['id']))
                    db.audit(user_id, 'attendance_self', {'session': s['id'], 'player': linked['id'],
                                                          'present': present, 'guests': guests,
                                                          'guest_names': names or []})
                    effects['last_session_id'] = s['id']
                    extra = (' مهمان‌ها: ' + '، '.join(names)) if names else ''
                    return (f"حضور {linked['name']} در «{s['title']}» با وضعیت present={present} "
                            f"و مهمان={guests} ثبت شد.{extra}")

                if not admin:
                    return 'خطا: این عملیات مدیریتی فقط برای ادمین مجاز است.'

                if operation == 'set_phone':
                    # SPEC-020: an admin may record a number the player already
                    # gave him.  The digits must be in this message, so a number
                    # from memory or an earlier turn can never be stored.
                    unknown = set(data) - {'player', 'phone'}
                    if unknown:
                        return 'کلید ناشناخته در درخواست شماره: ' + '، '.join(sorted(unknown))
                    p = service.player(data.get('player', ''))
                    phone = data.get('phone')
                    if not isinstance(phone, str) or not phone.strip():
                        return 'شمارهٔ تماس را صریحاً در همین پیام بنویسید.'
                    digits = re.sub(r'\D', '', normalize(phone))
                    if (len(digits) < 10
                            or digits[-10:] not in re.sub(r'\D', '', normalize(current_message))):
                        return ('شمارهٔ تماس باید صریحاً در همین پیام نوشته شود؛ شماره از حافظه '
                                'یا پیام قبلی برداشت نمی‌شود.')
                    result = service.set_player_phone(p['id'], phone, user_id)
                    return (f"شمارهٔ {result['phone']} برای #{result['id']} «{result['name']}» ثبت شد."
                            if result['changed'] else
                            f"شمارهٔ «{result['name']}» از قبل همین بود؛ تغییری ثبت نشد.")

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
                        names = None
                        if isinstance(item, str):
                            p_name, guests = item, 0
                        elif isinstance(item, dict):
                            extra = set(item) - {'name', 'player', 'guests', 'guest_names'}
                            if extra:
                                return 'کلید ناشناخته در حاضران: ' + '، '.join(sorted(extra))
                            p_name = item.get('name') or item.get('player') or ''
                            guests = integer(item.get('guests', 0), 'مهمان', 0, 50)
                            try:
                                names = service._clean_guest_names(item.get('guest_names'))
                            except UserError as exc:
                                return str(exc)
                            if names is not None and len(names) > guests:
                                return 'تعداد نام مهمان‌ها از تعداد مهمان بیشتر است.'
                        else:
                            continue
                        try:
                            planned.append((service.player(p_name), guests, names))
                        except UserError:
                            missing.append(str(p_name).strip() or '—')
                    if missing:
                        return ('حضور ثبت نشد؛ این نام‌ها در فهرست بازیکنان نیست:\n' + '، '.join(missing) +
                                '\nاول با upsert_players بازیکن را با نام کامل ثبت کنید یا نام درست را از فهرست انتخاب کنید.')
                    added = []
                    for p, guests, names in planned:
                        db.execute(
                            '''INSERT INTO attendance(session_id,player_id,guests,own)
                               VALUES (?,?,?,1) ON CONFLICT(session_id,player_id)
                               DO UPDATE SET guests=excluded.guests, own=excluded.own''',
                            (s['id'], p['id'], guests),
                        )
                        if names is not None:
                            service._store_guest_names(s['id'], p['id'], names)
                        label = f"{p['name']}" + (f" (+{guests} مهمان)" if guests else '')
                        if names:
                            label += ' [' + '، '.join(names) + ']'
                        added.append(label)
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

                if operation in ('suggest_links', 'links', 'link_players', 'unlink_players'):
                    # SPEC-025: a player may ask; only the admin card links.
                    if not admin:
                        return 'خطا: پیوند حساب‌ها فقط توسط ادمین قابل پیشنهاد است.'
                    if operation == 'suggest_links':
                        pairs = service.link_candidates()
                        preview = service.format_link_candidates(pairs)
                        if not pairs:
                            return preview
                        for item in pairs[:10]:
                            bot.propose({'action': 'player_link',
                                         'args': {'player': item['left']['id'],
                                                  'partner': item['right']['id']}},
                                        user_id, chat_id)
                        return preview + '\nیک کارت تأیید برای این زوج‌ها ساخته شد؛ بدون تأیید شما پیوندی ساخته نمی‌شود.'
                    if operation == 'links':
                        rows = db.all('SELECT * FROM player_links ORDER BY low_id, high_id')
                        if not rows:
                            return 'هیچ پیوندی ثبت نشده.'
                        return '\n'.join(
                            f"#{r['low_id']} {service.player(r['low_id'])['name']}"
                            f" ↔ #{r['high_id']} {service.player(r['high_id'])['name']}"
                            for r in rows)
                    unknown = set(data) - {'player', 'partner'}
                    if unknown:
                        return 'کلید ناشناخته در پیوند: ' + '، '.join(sorted(unknown))
                    left = service.player(data.get('player', ''))
                    right = service.player(data.get('partner', ''))
                    action = 'player_link' if operation == 'link_players' else 'player_unlink'
                    if action == 'player_link' and service.are_linked(left['id'], right['id']):
                        return 'این دو حساب از قبل به هم وصل هستند.'
                    if action == 'player_unlink' and not service.are_linked(left['id'], right['id']):
                        return 'این دو حساب به هم وصل نیستند.'
                    reply, _ = bot.propose({'action': action,
                                            'args': {'player': left['id'], 'partner': right['id']}},
                                           user_id, chat_id)
                    return reply

                if operation == 'expire_proposals':
                    # SPEC-015: closing cards is bookkeeping, not a financial
                    # change, so it needs no approval card. ``older_than_hours``
                    # defaults to the 24 hour TTL; 0 closes every open card.
                    unknown = set(data) - {'older_than_hours', 'hours'}
                    if unknown:
                        return 'کلید ناشناخته در بستن کارت‌ها: ' + '، '.join(sorted(unknown))
                    hours = bot.expiry_hours(data.get('older_than_hours', data.get('hours')))
                    closed = bot.expire_proposals(hours, user_id)
                    return bot.expiry_report(hours, closed)

                return f'عملیات {operation} شناخته نشد.'
            except UserError as exc:
                return str(exc)
            except Exception as exc:
                return f'خطا در اجرای عملیات: {exc}'

        # SPEC-012: one card can carry several players.  ``entries`` exists
        # because ``integer()`` rejects thousands separators, so a comma list
        # cannot hold an amount per name.
        def batch_refs(raw):
            if isinstance(raw, list):
                raw = '\n'.join(str(item) for item in raw)
            return [ref.strip() for ref in re.split(r'[,،\n]+', str(raw or '')) if ref.strip()]

        def entry_amount(value):
            if isinstance(value, bool) or value is None:
                raise UserError('مبلغ هر نفر باید عدد تومان باشد.')
            cleaned = str(value).replace(',', '').replace('٬', '').strip()
            return integer(cleaned, 'مبلغ هر نفر', -10**12)

        def batch_entries(raw):
            if isinstance(raw, str):
                raw = raw.strip()
                if not raw:
                    return []
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raise UserError('قالب entries باید JSON معتبر باشد؛ مثلاً '
                                    '[{"player":"مهدی","amount":-150000}].') from None
            if not isinstance(raw, list):
                raise UserError('entries باید آرایه‌ای از {"player":…,"amount":…} باشد.')
            if not raw:
                raise UserError('entries خالی است؛ برای هر نفر نام و مبلغ را مشخص کنید.')
            if len(raw) > MAX_BATCH:
                raise UserError(f'در هر کارت حداکثر {MAX_BATCH} نفر جا می‌شود؛ فهرست را کوتاه‌تر کنید.')
            parsed = []
            for item in raw:
                if not isinstance(item, dict):
                    raise UserError('هر مورد entries باید شامل player و amount باشد.')
                extra = set(item) - {'player', 'amount'}
                if extra:
                    raise UserError('کلید ناشناخته در entries: ' + '، '.join(sorted(extra)))
                ref = item.get('player')
                if isinstance(ref, bool) or not isinstance(ref, (str, int)) or not str(ref).strip():
                    raise UserError('نام یا شناسهٔ بازیکن در entries مشخص نیست.')
                value = entry_amount(item.get('amount'))
                if not value:
                    raise UserError('مبلغ هر نفر در entries نباید صفر باشد.')
                parsed.append((ref, value))
            return parsed

        def batch_reply(commands, skipped=()):
            preview, markup = bot.propose_commands(commands, user_id, chat_id)
            effects['markup'] = markup
            effects['proposal_preview'] = preview
            reply = (f'پیشنهاد برای {len(commands)} نفر آماده شد و دکمه‌های تأیید/رد ضمیمه گردید:'
                     f'\n{preview}')
            if skipped:
                reply += '\nبدون بدهی و خارج از کارت: ' + '، '.join(skipped)
            return reply

        @tool
        def propose_financial_change(action: str, player: str = '', session: str = '',
                                     amount: int = 0, note: str = '',
                                     players: str = '', entries: str = '') -> str:
            """Create an admin-approval proposal (with inline Yes/No buttons) for financial mutations.
            action: 'finalize', 'adjustment', 'settle', 'declare_debt'.
            One request is always ONE card, even for many players; never call this per person.
            adjustment:
              - `player` + `amount`: a single player.
              - `players` (names or ids separated by , ، or newline) + one shared `amount`.
              - `entries`: JSON array like [{"player":"مهدی","amount":-150000}] for a
                different amount per person; it cannot be combined with `players`/`amount`.
            settle: put every name in `players`; members without debt are skipped automatically.
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
                    refs = batch_refs(players)
                    has_entries = ((isinstance(entries, (list, tuple)) and bool(entries))
                                   or (isinstance(entries, str) and bool(entries.strip())))
                    if has_entries:
                        if refs or amount:
                            return ('برای اصلاح چندنفره یا entries بدهید یا players و amount؛ '
                                    'ترکیب این دو مجاز نیست.')
                        parsed = batch_entries(entries)
                        if not parsed:
                            return 'entries خالی است؛ برای هر نفر نام و مبلغ را مشخص کنید.'
                        return batch_reply([service.prepare('adjustment', {
                            'player': service.player(ref)['id'],
                            'amount': value,
                            'note': note or 'اصلاح حساب توسط ادمین',
                        }) for ref, value in parsed])
                    if refs:
                        if len(refs) > MAX_BATCH:
                            return (f'در هر کارت حداکثر {MAX_BATCH} بازیکن اصلاح می‌شود؛ '
                                    'فهرست را کوتاه‌تر کنید.')
                        value = entry_amount(amount)
                        if not value:
                            return 'مبلغ اصلاح نباید صفر باشد.'
                        return batch_reply([service.prepare('adjustment', {
                            'player': service.player(ref)['id'],
                            'amount': value,
                            'note': note or 'اصلاح حساب توسط ادمین',
                        }) for ref in refs])
                    # A financial correction never creates a player as a side
                    # effect; an unknown or partial name must be clarified.
                    p = service.player(player)
                    value = entry_amount(amount)
                    if not value:
                        return 'مبلغ اصلاح نباید صفر باشد.'
                    cmd = {'action': 'adjustment', 'args': {'player': p['id'], 'amount': value,
                                                           'note': note or 'اصلاح حساب توسط ادمین'}}
                elif action == 'settle':
                    if ((isinstance(entries, (list, tuple)) and entries)
                            or (isinstance(entries, str) and entries.strip())):
                        return 'برای تسویه، مبلغ از ماندهٔ حساب محاسبه می‌شود؛ entries مجاز نیست.'
                    # SPEC-011: a multi-player request becomes ONE card with one
                    # command per player, so a single «تأیید» settles the batch.
                    refs = batch_refs(players)
                    if not refs and isinstance(player, str) and player.strip():
                        refs = [player.strip()]
                    if not refs:
                        return 'کدام بازیکن تسویه شود؟ نام بازیکن را بفرستید.'
                    if len(refs) > MAX_BATCH:
                        return (f'در هر کارت حداکثر {MAX_BATCH} بازیکن تسویه می‌شود؛ '
                                'فهرست را کوتاه‌تر کنید.')
                    # SPEC-012: one already-settled member must not abort the
                    # whole batch; it is reported and left out of the card.
                    commands, skipped = [], []
                    for ref in refs:
                        p = service.player(ref)
                        if service.balance(p['id']) <= 0:
                            skipped.append(p['name'])
                            continue
                        commands.append(service.prepare('settle', {'player': p['id']}))
                    if not commands:
                        return 'این حساب‌ها بدهی مثبت ندارند و از قبل تسویه‌اند: ' + '، '.join(skipped)
                    return batch_reply(commands, skipped)
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
        def message_players(text: str, players: str = '', audience: str = '',
                            request_phone: bool = False) -> str:
            """Propose one proactive message to private chats (admin approval required).

            The bot does send this after the admin presses «تأیید»; it is not a
            reply-only bot.

            text: the exact Persian message to deliver.
            players: names or #ids separated by , ، or newline; empty means every player.
            audience: 'players' (default) for players only, or 'all' for every person
              who ever started the bot, including people who are not players.
            request_phone: true to attach the «ارسال شمارهٔ من» button to the message.
            """
            if not admin:
                return 'ارسال پیام به بازیکنان فقط توسط ادمین ممکن است.'
            try:
                command = service.prepare('broadcast', {
                    'text': text, 'players': players or None,
                    'audience': audience, 'request_phone': request_phone})
                preview, markup = bot.propose_commands([command], user_id, chat_id)
            except UserError as exc:
                return str(exc)
            effects['markup'] = markup
            effects['proposal_preview'] = preview
            return f'پیشنهاد ارسال پیام آماده شد و دکمه‌های تأیید/رد ضمیمه گردید:\n{preview}'

        @tool
        def ask_admins(question: str) -> str:
            """Send a question or escalation note to the admin group when something is ambiguous or needs admin input."""
            if not question or not str(question).strip():
                return 'متن سوال خالی است.'
            prefix = f'❓ پرسش دستیار هوشمند (مربوط به کاربر {user_id}):\n' if chat_id != bot.config.admin_group else '❓ پرسش:\n'
            db.say(bot.config.admin_group, prefix + str(question).strip())
            return 'سوال برای ادمین‌ها در گروه مدیریتی ارسال شد.'

        return [query_database, manage_records, propose_financial_change, message_players, ask_admins]

    @staticmethod
    def _finalize_reply(reply: str, effects: dict) -> str:
        """Attach the approval card to the model reply at most once (SPEC-030).

        The model usually repeats the tool result, and the whole card body lives
        in ``effects['proposal_preview']``.  The old guard asked whether the word
        «کد:» appeared in the reply, but the card itself always contains that
        word, so a model that echoed the card still got it pasted on top: the
        same block appeared two or three times in one message.  Compare the block
        itself instead.
        """
        preview = effects.get('proposal_preview')
        if not preview:
            return reply
        if not reply:
            return preview
        if preview.strip() in reply:
            return reply
        return f"{reply}\n\n{preview}"

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
        progress: Any = None,
    ) -> tuple[str, list | None, dict[str, Any]]:
        """Execute the LangGraph StateGraph for one conversational turn.

        ``progress`` is an optional SPEC-028 feed: a callable that receives one of
        the fixed ``PROGRESS_*`` step texts.  It is called from inside the graph
        nodes, so the acknowledgement message can advance while ``graph.invoke``
        is still running, and it never affects the result of the turn.
        """
        # SPEC-028: the step texts live in ``bot`` to keep the catalogue in one
        # place; importing locally avoids a circular import at module load.
        from .bot import PROGRESS_ANSWERING, PROGRESS_READING, PROGRESS_START, PROGRESS_WRITING

        def report(step_text):
            if progress is None:
                return
            try:
                progress(step_text)
            except Exception:  # a progress line must never break a turn.
                LOG.debug('Progress feed raised; ignoring', exc_info=True)

        # SPEC-028: tools that only read may announce the read step; anything else
        # may change state, so it announces the write step.  The split is a fixed
        # catalogue keyed by a fixed set of names, so no tool name reaches a user.
        read_only_tools = {'query_database', 'list_sessions', 'get_session',
                           'list_players', 'get_player', 'player_debt', 'report',
                           'duplicates', 'pending_proposals', 'session_info'}
        tool_steps_announced = set()
        tool_results = {'count': 0}
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
            AGENT_SYSTEM_PROMPT.format(bot_name=self.bot.config.bot_name,
                                       support_contact=support_contact(self.bot)
                                       or SUPPORT_FALLBACK_TEXT)
            + '\n\nوضعیت زنده دیتابیس و چت (JSON):\n'
            + snapshot
        )

        working = {'index': 0}

        def call_model(state: MessagesState):
            t0 = time.monotonic()
            # SPEC-028: the first model call is the start of the work; a call that
            # follows tool results is the composition of the answer.
            report(PROGRESS_ANSWERING if tool_results['count'] else PROGRESS_START)
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
                # SPEC-028: announce what this tool is about to do.  Only the fixed
                # catalogue text is sent, never the tool name or its arguments.
                if tool_fn is not None:
                    if t_name in read_only_tools:
                        report(PROGRESS_READING)
                    else:
                        report(PROGRESS_WRITING)
                    tool_steps_announced.add(t_name)
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
                # SPEC-028: the next model call composes the answer from results.
                if tool_fn is not None:
                    tool_results['count'] += 1
            return {'messages': out_messages}

        builder = StateGraph(MessagesState)
        builder.add_node('agent', call_model)
        builder.add_node('tools', run_tools_sequentially)
        builder.add_edge(START, 'agent')
        builder.add_conditional_edges('agent', tools_condition)
        builder.add_edge('tools', 'agent')
        graph = builder.compile()

        lc_messages = []
        for item in history[-CONVERSATION_HISTORY:]:
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
        reply = self._finalize_reply(reply, effects)

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
