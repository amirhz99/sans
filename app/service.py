import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .content import CONTENT_PATH, DEFAULTS


class UserError(ValueError):
    """A safe, user-facing validation error."""


def normalize(text):
    text = str(text).translate(str.maketrans('يكى۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', 'یکی01234567890123456789'))
    return ' '.join(text.replace('\u200c', ' ').split()).casefold()


def integer(value, label, minimum=0, maximum=10**12):
    if isinstance(value, bool) or len(str(value)) > 24 or not re.fullmatch(r'-?\d+', normalize(value)):
        raise UserError(f'{label} باید عدد صحیح باشد؛ واحد مبلغ تومان است.')
    value = int(normalize(value))
    if not minimum <= value <= maximum:
        raise UserError(f'{label} باید بین {minimum:,} و {maximum:,} باشد.')
    return value


def text(value, label, required=True, limit=500):
    if not isinstance(value, str) or len(value.strip()) > limit or (required and not value.strip()):
        raise UserError(f'{label} معتبر نیست (حداکثر {limit} نویسه).')
    return value.strip()


TEHRAN = ZoneInfo('Asia/Tehran')
WEEKDAYS = {
    'دوشنبه': 0, 'سه شنبه': 1, 'سه‌شنبه': 1, 'چهارشنبه': 2,
    'پنجشنبه': 3, 'جمعه': 4, 'شنبه': 5, 'یکشنبه': 6, 'یک‌شنبه': 6,
}
JALALI_MONTHS = ('فروردین', 'اردیبهشت', 'خرداد', 'تیر', 'مرداد', 'شهریور',
                 'مهر', 'آبان', 'آذر', 'دی', 'بهمن', 'اسفند')


def parse_session_datetime(value, now=None):
    """Parse an exact Persian session date/time and return a Tehran ISO value.

    Deliberately does not guess a day period: «ساعت ۹» and «امروز عصر» are
    rejected because either morning/night or an exact hour is missing.  A
    weekday means the next occurrence (including today when still upcoming).
    """
    if not isinstance(value, str) or not value.strip():
        raise UserError('تاریخ و ساعت دقیق سانس را اعلام کنید؛ مثلاً «فردا ساعت ۲۱».')
    raw = normalize(value)
    if any(month in raw for month in JALALI_MONTHS):
        raise UserError('تبدیل تاریخ ماه‌های تقویم شمسی در این نسخه انجام نمی‌شود؛ «فردا»، روز هفته یا تاریخ YYYY-MM-DD را بنویسید.')
    now = now or datetime.now(TEHRAN)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TEHRAN)
    else:
        now = now.astimezone(TEHRAN)

    clock = re.search(
        r'(?:ساعت\s*)(\d{1,2})(?:\s*[:٫]\s*(\d{1,2})|\s+و\s+(\d{1,2})\s*(?:دقیقه)?)?'
        r'|(?<!\d)(\d{1,2})\s*[:٫]\s*(\d{1,2})(?!\d)', raw)
    if not clock:
        raise UserError('ساعت دقیق سانس مشخص نیست؛ مثلاً «فردا ساعت ۲۱» یا «پنجشنبه ۲۱:۳۰» بنویسید.')
    hour = int(clock.group(1) or clock.group(4))
    minute = int(clock.group(2) or clock.group(3) or clock.group(5) or 0)
    if hour > 23 or minute > 59:
        raise UserError('ساعت سانس معتبر نیست؛ ساعت باید بین ۰۰ تا ۲۳ و دقیقه بین ۰۰ تا ۵۹ باشد.')

    if 'نیمه شب' in raw:
        periods = ['نیمه شب']
    elif 'بعدازظهر' in raw or 'بعد از ظهر' in raw:
        periods = ['بعدازظهر']
    else:
        periods = [name for name in ('صبح', 'ظهر', 'عصر', 'شب') if name in raw]
    if hour <= 12 and not periods:
        raise UserError('ساعت «۹» مبهم است؛ صبح/شب را مشخص کنید یا از قالب ۲۱ استفاده کنید.')
    if len(periods) > 1:
        raise UserError('بازهٔ زمانی مبهم است؛ فقط یکی از صبح، ظهر، عصر یا شب را بنویسید.')
    if periods:
        period = periods[0]
        if period == 'نیمه شب':
            if hour not in (0, 12):
                raise UserError('برای نیمه‌شب ساعت ۰۰ یا ۱۲ را بنویسید.')
            hour = 0
        elif period == 'صبح':
            if hour == 12:
                hour = 0
        elif period == 'ظهر':
            if hour != 12:
                raise UserError('برای ظهر ساعت ۱۲ را بنویسید.')
        else:  # بعدازظهر، عصر، شب
            if hour < 12:
                hour += 12
    if hour > 23:
        raise UserError('ساعت سانس معتبر نیست.')

    target = now.date()
    explicit = re.search(r'(?<!\d)(20\d{2})[-/]([01]?\d)[-/]([0-3]?\d)(?!\d)', raw)
    if explicit:
        try:
            target = date(int(explicit.group(1)), int(explicit.group(2)), int(explicit.group(3)))
        except ValueError:
            raise UserError('تاریخ سانس معتبر نیست.')
    elif 'پسفردا' in raw:
        target += timedelta(days=2)
    elif 'فردا' in raw:
        target += timedelta(days=1)
    else:
        weekday = next((day for name, day in WEEKDAYS.items() if name in raw), None)
        if weekday is not None:
            delta = (weekday - now.weekday()) % 7
            candidate = target + timedelta(days=delta)
            if candidate == target and time(hour, minute) <= now.timetz().replace(tzinfo=None):
                candidate += timedelta(days=7)
            target = candidate
        elif not any(word in raw for word in ('امروز', 'امشب')):
            # A clock without a date is unambiguously today in the Tehran
            # context supplied to the bot; an explicit relative date is still
            # preferred by the prompt.
            target = now.date()
    return datetime.combine(target, time(hour, minute), tzinfo=TEHRAN).isoformat(timespec='minutes')


ADMIN_ACTIONS = {'new_player', 'new_session', 'update_session', 'attendance',
                 'finalize', 'adjustment', 'settings', 'player_note', 'settle'}


class Service:
    def __init__(self, db):
        self.db = db

    def player(self, ref):
        if isinstance(ref, bool) or not isinstance(ref, (str, int)):
            raise UserError('نام کامل یا شناسهٔ بازیکن را مشخص کنید.')
        if isinstance(ref, int) or (isinstance(ref, str) and normalize(ref).lstrip('#').isdigit()):
            ident = integer(normalize(ref).lstrip('#'), 'شناسهٔ بازیکن', 1, 2**63 - 1)
            row = self.db.one('SELECT * FROM players WHERE id=?', (ident,))
            if row:
                return row
            raise UserError('بازیکن با این شناسه پیدا نشد.')
        name = normalize(ref)
        rows = self.db.all('SELECT * FROM players ORDER BY id')
        exact = [row for row in rows if row['normalized_name'] == name]
        matches = exact or [row for row in rows if name and name in row['normalized_name']]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise UserError('نام مبهم است؛ نام کامل یا شناسه را بگویید:\n' + '\n'.join(
                f"#{r['id']} {r['name']}" for r in matches))
        raise UserError('بازیکن پیدا نشد؛ ابتدا نام کامل او را ثبت کنید.')

    def ensure_player(self, name, note='', actor='agent'):
        clean = text(name, 'نام')
        norm = normalize(clean)
        row = self.db.one('SELECT * FROM players WHERE normalized_name=?', (norm,))
        if row:
            if note and note.strip() and note.strip() not in (row['note'] or ''):
                updated_note = ((row['note'] + ' | ' if row['note'] else '') + note.strip())[:500]
                self.db.execute('UPDATE players SET note=? WHERE id=?', (updated_note, row['id']))
            return self.db.one('SELECT * FROM players WHERE id=?', (row['id'],))
        cur = self.db.execute(
            'INSERT INTO players(name, normalized_name, note) VALUES (?,?,?)',
            (clean, norm, text(note or '', 'توضیح', False)),
        )
        self.db.audit(actor, 'new_player', {'name': clean, 'note': note or ''})
        return self.db.one('SELECT * FROM players WHERE id=?', (cur.lastrowid,))

    def save_session_flexible(self, title=None, when_text=None, location=None,
                              person_fee=None, venue_cost=None, note=None,
                              session_ref=None, actor='agent'):
        """Create or update a session without requiring an exact timestamp (supports historical/incomplete sessions)."""
        existing = None
        if session_ref not in (None, '', 0, '0'):
            try:
                existing = self.session(session_ref, draft=False)
            except UserError:
                existing = None
        if existing is None and title:
            existing = self.db.one('SELECT * FROM sessions WHERE title=?', (str(title).strip(),))

        session_at = None
        if when_text and str(when_text).strip():
            try:
                session_at = parse_session_datetime(str(when_text).strip())
            except UserError:
                session_at = None

        if existing is None:
            clean_title = text(title or (f'سانس {when_text}' if when_text else f'سانس #{int(self.db.one("SELECT COUNT(*)+1 AS n FROM sessions")["n"])}'), 'عنوان سانس')
            if self.db.one('SELECT id FROM sessions WHERE title=?', (clean_title,)):
                clean_title = f"{clean_title} ({int(self.db.one('SELECT COUNT(*)+1 AS n FROM sessions')['n'])})"
            w_text = text(str(when_text or ''), 'تاریخ سانس', False)
            loc = text(str(location if location is not None else DEFAULTS['location']), 'محل', False)
            fee = integer(person_fee if person_fee is not None else DEFAULTS['person_fee'], 'سهم هر نفر', 0)
            v_cost = None if venue_cost is None else integer(venue_cost, 'هزینه زمین', 0)
            s_note = text(str(note or ''), 'توضیح', False)
            cur = self.db.execute(
                '''INSERT INTO sessions(title,when_text,session_at,location,venue_cost,person_fee,note)
                   VALUES (?,?,?,?,?,?,?)''',
                (clean_title, w_text, session_at, loc, v_cost, fee, s_note),
            )
            self.db.audit(actor, 'new_session_flexible', {'id': cur.lastrowid, 'title': clean_title})
            return self.db.one('SELECT * FROM sessions WHERE id=?', (cur.lastrowid,))
        else:
            updates = {}
            if title and str(title).strip() and str(title).strip() != existing['title']:
                updates['title'] = text(str(title), 'عنوان سانس')
            if when_text is not None:
                updates['when_text'] = text(str(when_text), 'تاریخ سانس', False)
                if session_at:
                    updates['session_at'] = session_at
            if location is not None:
                updates['location'] = text(str(location), 'محل', False)
            if person_fee is not None:
                updates['person_fee'] = integer(person_fee, 'سهم هر نفر', 0)
            if venue_cost is not None:
                updates['venue_cost'] = integer(venue_cost, 'هزینه زمین', 0)
            if note is not None:
                updates['note'] = text(str(note), 'توضیح', False)
            for k, v in updates.items():
                self.db.execute(f'UPDATE sessions SET {k}=? WHERE id=?', (v, existing['id']))
            if updates:
                self.db.audit(actor, 'update_session_flexible', {'id': existing['id'], **updates})
            return self.db.one('SELECT * FROM sessions WHERE id=?', (existing['id'],))

    def linked(self, user_id):
        return self.db.one('SELECT * FROM players WHERE bale_id=?', (user_id,))

    @staticmethod
    def session_time(row):
        """Return the exact new format, or the untouched legacy text."""
        return row['session_at'] or row['when_text'] or 'نامشخص'

    @staticmethod
    def session_time_display(row):
        prefix = 'زمان تهران: ' if row['session_at'] else 'زمان (متن قدیمی): '
        return prefix + Service.session_time(row)

    @staticmethod
    def session_at_display(value):
        """Render a stored timestamp as an unambiguous Tehran date and time."""
        try:
            parsed = datetime.fromisoformat(value).astimezone(TEHRAN)
        except (TypeError, ValueError):
            return str(value)
        return f'{parsed:%Y-%m-%d}، ساعت {parsed:%H:%M} (Asia/Tehran)'

    def session(self, ref=None, draft=False):
        if isinstance(ref, bool) or (ref is not None and not isinstance(ref, (str, int))):
            raise UserError('عنوان یا شناسهٔ سانس معتبر نیست.')
        if ref in (None, '', 'current', 'جاری'):
            rows = self.db.all('SELECT * FROM sessions WHERE finalized=0 ORDER BY id DESC')
            if not rows and not draft:
                rows = self.db.all('SELECT * FROM sessions ORDER BY id DESC LIMIT 1')
        elif isinstance(ref, int) or normalize(ref).lstrip('#').isdigit():
            ident = integer(normalize(ref).lstrip('#'), 'شناسهٔ سانس', 1, 2**63 - 1)
            rows = self.db.all('SELECT * FROM sessions WHERE id=?', (ident,))
        else:
            rows = self.db.all('SELECT * FROM sessions WHERE title=?', (ref,))
        if not rows:
            raise UserError('سانسی پیدا نشد؛ عنوان و زمان سانس جدید را اعلام کنید.')
        if len(rows) > 1:
            raise UserError('کدام سانس؟\n' + '\n'.join(f"#{r['id']} {r['title']}" for r in rows))
        if draft and rows[0]['finalized']:
            raise UserError('حساب این سانس بسته شده؛ برای اصلاح مالی از اصلاح حساب با توضیح استفاده کنید.')
        return rows[0]

    def balance(self, player_id):
        return self.db.one('SELECT COALESCE(SUM(amount),0) AS balance FROM ledger WHERE player_id=?',
                           (player_id,))['balance']

    def debt(self, player_id):
        player = self.player(player_id)
        balance = self.balance(player_id)
        status = 'بدهی' if balance > 0 else 'بستانکاری' if balance < 0 else 'مانده'
        lines = [player['name'], f'{status}: {abs(balance):,} تومان', '', 'گردش حساب (آخرین ۳۰ مورد؛ مثبت = بدهی):']
        entries = self.db.all('SELECT * FROM ledger WHERE player_id=? ORDER BY id DESC LIMIT 30', (player_id,))
        for row in reversed(entries):
            lines.append(f"{row['created_at'][:10]} | {row['amount']:+,} تومان | {row['note']}")
        if not entries:
            lines.append('تراکنشی ثبت نشده است.')
        pending = self.db.one("SELECT COALESCE(SUM(amount),0) AS total FROM receipts WHERE player_id=? AND status='pending'",
                              (player_id,))['total']
        if pending:
            lines.append(f'رسیدهای منتظر تأیید: {pending:,} تومان (هنوز از بدهی کم نشده).')
        # Allocate credits oldest first for an understandable outstanding-session report.
        rows = self.db.all('SELECT * FROM ledger WHERE player_id=? ORDER BY id', (player_id,))
        credit = -sum(r['amount'] for r in rows if r['amount'] < 0)
        outstanding = []
        for row in rows:
            if row['amount'] <= 0:
                continue
            used = min(credit, row['amount'])
            credit -= used
            if row['amount'] > used:
                outstanding.append(f"• {row['note']}: {row['amount'] - used:,} تومان")
        if outstanding:
            lines.extend(['', 'ماندهٔ بدهی‌ها (پرداخت‌ها از قدیمی‌ترین بدهی کسر می‌شوند):', *outstanding])
        if player['note']:
            lines.extend(['', 'توضیح شما: ' + player['note']])
        if self.db.setting('payment_note'):
            lines.extend(['', self.db.setting('payment_note')])
        return '\n'.join(lines)

    def card(self):
        return self.db.setting('card') or 'شماره کارت هنوز ثبت نشده؛ از ادمین‌ها می‌پرسم.'

    def session_info(self, ref=None, admin=False, level='summary'):
        """Render session information through the presentation layer.

        ``level`` is intentionally summary by default.  Callers that need
        attendance or administrative accounting details must opt in explicitly.
        """
        from .presentation import SessionView

        return SessionView(self).for_ref(ref, level, admin)

    def configuration(self):
        """Return the admin-only business settings and the open-session prices."""
        card = self.db.setting('card') or 'ثبت نشده'
        payment_note = self.db.setting('payment_note') or 'ثبت نشده'
        approval = 'فعال؛ رسید باید تأیید شود' if self.db.setting('approval_required', 'true') != 'false' else 'خاموش؛ رسید خودکار ثبت می‌شود'
        lines = [
            'تنظیمات مرکزی ربات',
            f'فایل متن و prompt: {CONTENT_PATH}',
            '',
            'پیش‌فرض سانس جدید (قابل تغییر برای هر سانس):',
            f"• محل: {DEFAULTS['location']}",
            f"• هزینهٔ زمین: {DEFAULTS['venue_cost']:,} تومان",
            f"• سهم هر نفر: {DEFAULTS['person_fee']:,} تومان",
            f"• فرمول بدهی: {DEFAULTS['formula']}",
            '• هزینهٔ زمین در بدهی بازیکن محاسبه نمی‌شود.',
            '',
            'پرداخت:',
            f'• شماره کارت: {card}',
            f'• متن پرداخت: {payment_note}',
            f'• تأیید رسید: {approval}',
            '',
            'زمان سانس‌های جدید: ISO با timezone Asia/Tehran؛ زمان‌های قدیمی بدون بازسازی خودکار باقی می‌مانند.',
            '',
            'سانس‌های باز:'
        ]
        sessions = self.db.all('SELECT * FROM sessions WHERE finalized=0 ORDER BY id DESC')
        if sessions:
            for session in sessions:
                venue = 'نامشخص' if session['venue_cost'] is None else f"{session['venue_cost']:,}"
                fee = 'اعلام نشده' if session['person_fee'] is None else f"{session['person_fee']:,}"
                lines.append(f"• #{session['id']} {session['title']}؛ {self.session_time_display(session)}؛ {fee} تومان برای هر نفر؛ زمین {venue} تومان؛ محل {session['location'] or 'نامشخص'}")
        else:
            lines.append('• سانس بازی وجود ندارد.')
        return '\n'.join(lines)

    def report(self):
        lines = ['گزارش حساب‌ها (تومان):']
        for p in self.db.all('SELECT * FROM players ORDER BY name'):
            b = self.balance(p['id'])
            lines.append(f"#{p['id']} {p['name']}: {b:,} {'بدهکار' if b > 0 else 'بستانکار' if b < 0 else 'تسویه'}")
        totals = self.db.one('''SELECT COALESCE(SUM(CASE WHEN amount>0 THEN amount ELSE 0 END),0) charges,
            COALESCE(SUM(CASE WHEN amount<0 THEN -amount ELSE 0 END),0) credits FROM ledger''')
        lines += [f"جمع بدهکارسازی: {totals['charges']:,}", f"جمع پرداخت/بستانکارسازی: {totals['credits']:,}"]
        for s in self.db.all('SELECT * FROM sessions ORDER BY id DESC LIMIT 15'):
            charged = self.db.one('SELECT COALESCE(SUM(amount),0) n FROM ledger WHERE session_id=?', (s['id'],))['n']
            lines.append(f"سانس #{s['id']} {s['title']}؛ {self.session_time_display(s)}؛ سهم ثبت‌شده {charged:,}؛ هزینهٔ زمین {s['venue_cost']}")
        for r in self.db.all("SELECT r.*,p.name FROM receipts r JOIN players p ON p.id=r.player_id WHERE status='pending'"):
            lines.append(f"رسید منتظر #{r['id']}: {r['name']}، {r['amount']:,} تومان؛ /approve {r['id']} یا /reject {r['id']}")
        return '\n'.join(lines)

    def prepare(self, action, args):
        """Resolve ambiguous names now, validate all input, return an immutable proposal."""
        if action not in ADMIN_ACTIONS | {'declare_debt'} or not isinstance(args, dict):
            raise UserError('درخواست مدیریتی معتبر نیست.')
        a = {}
        if action == 'new_player':
            a = {'name': text(args.get('name'), 'نام'), 'note': text(args.get('note', ''), 'توضیح', False)}
            if self.db.one('SELECT id FROM players WHERE normalized_name=?', (normalize(a['name']),)):
                raise UserError('این نام قبلاً ثبت شده است.')
        elif action in ('new_session', 'update_session'):
            if action == 'new_session':
                a['title'] = text(args.get('title'), 'عنوان سانس')
                if self.db.one('SELECT id FROM sessions WHERE title=?', (a['title'],)):
                    raise UserError('عنوان سانس تکراری است؛ یک عنوان متمایز انتخاب کنید.')
                a.update({'when_text': text(args.get('when_text'), 'تاریخ و ساعت سانس'),
                          'location': DEFAULTS['location'], 'note': '',
                          'venue_cost': DEFAULTS['venue_cost'], 'person_fee': DEFAULTS['person_fee']})
            else:
                a['session'] = self.session(args.get('session'), draft=True)['id']
            for key in ('when_text', 'location', 'note'):
                if key in args:
                    a[key] = text(args[key], key, False)
            if action == 'new_session' or 'when_text' in a:
                a['session_at'] = parse_session_datetime(a['when_text'])
            for key in ('venue_cost', 'person_fee'):
                if key in args and args[key] is not None:
                    a[key] = integer(args[key], key, 1 if key == 'person_fee' else 0)
        elif action in ('attendance', 'finalize'):
            s = self.session(args.get('session'), draft=True)
            a['session'] = s['id']
            if action == 'attendance':
                a['player'] = self.player(args.get('player', ''))['id']
                a['guests'] = integer(args.get('guests', 0), 'تعداد مهمان', maximum=100)
                a['present'] = args.get('present', True)
                if not isinstance(a['present'], bool):
                    raise UserError('وضعیت حضور معتبر نیست.')
            else:
                if not s['person_fee']:
                    raise UserError('سهم ثابت هر نفر برای این سانس چقدر است؟')
                if not self.db.one('SELECT 1 FROM attendance WHERE session_id=?', (s['id'],)):
                    raise UserError('ابتدا حاضران سانس را ثبت کنید.')
        elif action in ('adjustment', 'player_note', 'declare_debt'):
            a['player'] = self.player(args.get('player', ''))['id']
            a['note'] = text(args.get('note', 'بدهی اعلام‌شده توسط خود فرد'), 'توضیح', False)
            if action in ('adjustment', 'declare_debt'):
                a['amount'] = integer(args.get('amount'), 'اصلاح حساب', 1 if action == 'declare_debt' else -10**12)
                if not a['amount']:
                    raise UserError('مبلغ اصلاح نباید صفر باشد.')
        elif action == 'settle':
            a['player'] = self.player(args.get('player', ''))['id']
            balance = self.balance(a['player'])
            if balance <= 0:
                raise UserError('این حساب بدهی مثبت ندارد و قبلاً تسویه است.')
            a['amount'] = balance
            a['note'] = 'تسویه کامل توسط ادمین'
        elif action == 'settings':
            key = args.get('key')
            if key not in ('card', 'payment_note', 'approval_required'):
                raise UserError('تنظیم مجاز: card، payment_note، approval_required')
            value = args.get('value')
            if key == 'approval_required':
                if not isinstance(value, bool):
                    raise UserError('وضعیت تأیید باید true یا false باشد.')
                value = 'true' if value else 'false'
            else:
                value = text(value, 'مقدار', False)
            a = {'key': key, 'value': value}
        return {'action': action, 'args': a}

    def preview(self, command):
        action, a = command['action'], command['args']
        labels = {'new_player': 'ثبت بازیکن', 'new_session': 'ثبت سانس', 'update_session': 'تکمیل سانس',
                  'attendance': 'ثبت/اصلاح حضور', 'finalize': 'نهایی‌سازی بدهی سانس',
                  'adjustment': 'اصلاح حساب', 'declare_debt': 'ثبت بدهی اعلام‌شده',
                  'settle': 'تسویه کامل حساب', 'settings': 'تنظیمات', 'player_note': 'یادداشت بازیکن'}
        fields = {'name': 'نام', 'title': 'عنوان', 'note': 'توضیح', 'when_text': 'عبارت زمان',
                  'location': 'محل', 'venue_cost': 'هزینهٔ زمین (تومان)', 'person_fee': 'سهم هر نفر (تومان)',
                  'guests': 'تعداد مهمان', 'present': 'حاضر', 'amount': 'مبلغ (مثبت: بدهکار؛ منفی: بستانکار)',
                  'key': 'تنظیم', 'value': 'مقدار'}
        lines = [labels[action]]
        for key, value in a.items():
            if key == 'player':
                lines.append('بازیکن: ' + self.player(value)['name'])
            elif key == 'session':
                lines.append('سانس: ' + self.session(value)['title'])
            elif key == 'session_at':
                lines.append('زمان تهران: ' + self.session_at_display(value))
            else:
                lines.append(f'{fields.get(key, key)}: {value}')
        if action == 'finalize':
            s = self.session(a['session'])
            total = 0
            for row in self.db.all('''SELECT a.*,p.name FROM attendance a JOIN players p ON p.id=a.player_id
                                      WHERE session_id=? ORDER BY p.id''', (s['id'],)):
                amount = (1 + row['guests']) * s['person_fee']
                total += amount
                lines.append(f"{row['name']}: {row['guests']} مهمان، {amount:,} تومان")
            lines.append(f'جمع بدهی جدید: {total:,} تومان؛ هزینهٔ زمین در محاسبه دخالت ندارد.')
        if action == 'settings' and a == {'key': 'approval_required', 'value': 'false'}:
            lines.append('هشدار: بدون بررسی بانکی، هر رسید با مبلغ اعلامی کاربر از بدهی کم خواهد شد.')
        return '\n'.join(lines)

    def apply(self, command, actor, source):
        """Caller must hold a transaction and check proposal revision / authorization."""
        action, a = command['action'], command['args']
        # Revalidate after confirmation as a defense in depth.
        raw = dict(a)
        if action == 'settings' and a['key'] == 'approval_required':
            raw['value'] = a['value'] == 'true'
        a = self.prepare(action, raw)['args']
        if action == 'new_player':
            self.db.execute('INSERT INTO players(name, normalized_name, note) VALUES (?,?,?)',
                            (a['name'], normalize(a['name']), a['note']))
        elif action == 'declare_debt':
            self.db.execute('INSERT INTO ledger(player_id,amount,note,source) VALUES (?,?,?,?)',
                            (a['player'], a['amount'], a['note'], source))
        elif action == 'settle':
            current = self.balance(a['player'])
            if current <= 0:
                raise UserError('این حساب بدهی مثبت ندارد و قبلاً تسویه است.')
            self.db.execute('INSERT INTO ledger(player_id,amount,note,source) VALUES (?,?,?,?)',
                            (a['player'], -current, 'تسویه کامل توسط ادمین', source))
        elif action == 'new_session':
            self.db.execute('''INSERT INTO sessions(title,when_text,session_at,location,venue_cost,person_fee,note)
                               VALUES (?,?,?,?,?,?,?)''', tuple(a.get(k, '' if k in ('when_text','location','note') else None)
                               for k in ('title', 'when_text', 'session_at', 'location', 'venue_cost', 'person_fee', 'note')))
        elif action == 'update_session':
            for key, value in a.items():
                if key != 'session':  # Keys came from the fixed allowlist in prepare().
                    self.db.execute(f'UPDATE sessions SET {key}=? WHERE id=?', (value, a['session']))
        elif action == 'attendance':
            if a['present']:
                self.db.execute('''INSERT INTO attendance VALUES (?,?,?) ON CONFLICT(session_id,player_id)
                                   DO UPDATE SET guests=excluded.guests''', (a['session'], a['player'], a['guests']))
            else:
                self.db.execute('DELETE FROM attendance WHERE session_id=? AND player_id=?', (a['session'], a['player']))
        elif action == 'finalize':
            s = self.session(a['session'], draft=True)
            for row in self.db.all('SELECT * FROM attendance WHERE session_id=?', (s['id'],)):
                self.db.execute('INSERT INTO ledger(player_id,session_id,amount,note,source) VALUES (?,?,?,?,?)',
                                (row['player_id'], s['id'], (1 + row['guests']) * s['person_fee'],
                                 f"{s['title']}؛ خود فرد + {row['guests']} مهمان. {s['note']}",
                                 f"session:{s['id']}:player:{row['player_id']}"))
            self.db.execute('UPDATE sessions SET finalized=1 WHERE id=?', (s['id'],))
        elif action == 'adjustment':
            self.db.execute('INSERT INTO ledger(player_id,amount,note,source) VALUES (?,?,?,?)',
                            (a['player'], a['amount'], a['note'], source))
        elif action == 'settings':
            self.db.set_setting(a['key'], a['value'])
        elif action == 'player_note':
            self.db.execute('UPDATE players SET note=? WHERE id=?', (a['note'], a['player']))
        self.db.audit(actor, action, a)

    def bind(self, player_id, user_id, actor):
        p = self.player(player_id)
        if p['bale_id'] is not None or self.linked(user_id):
            raise UserError('این بازیکن یا حساب بله قبلاً متصل شده است؛ اتصال دوباره مجاز نیست.')
        self.db.execute('UPDATE players SET bale_id=? WHERE id=?', (user_id, player_id))
        self.db.audit(actor, 'bind', {'player': player_id, 'user': user_id})

    def submit_receipt(self, player_id, amount, media, user_id):
        amount = integer(amount, 'مبلغ پرداخت', 1)
        key = media['file_key']
        if self.db.one('SELECT id FROM receipts WHERE file_key=?', (key,)):
            raise UserError('این فایل رسید قبلاً ارسال شده است؛ دوباره از بدهی کسر نمی‌شود.')
        receipt_id = self.db.execute('''INSERT INTO receipts
            (player_id,amount,file_id,file_key,media_type,chat_id,message_id,status) VALUES (?,?,?,?,?,?,?,'pending')''',
            (player_id, amount, media['file_id'], key, media['media_type'], media['chat_id'], media['message_id'])).lastrowid
        self.db.audit(user_id, 'submit_receipt', {'receipt': receipt_id, 'amount': amount})
        if self.db.setting('approval_required') == 'false':
            self.review_receipt(receipt_id, True, 'auto')
        return receipt_id

    def submit_admin_receipt(self, player_id, amount, media, actor):
        """Store an admin-submitted receipt; approval is still required."""
        amount = integer(amount, 'مبلغ پرداخت', 1)
        key = media['file_key']
        if self.db.one('SELECT id FROM receipts WHERE file_key=?', (key,)):
            raise UserError('این فایل رسید قبلاً ارسال شده است؛ دوباره از بدهی کسر نمی‌شود.')
        receipt_id = self.db.execute('''INSERT INTO receipts
            (player_id,amount,file_id,file_key,media_type,chat_id,message_id,status) VALUES (?,?,?,?,?,?,?,'pending')''',
            (player_id, amount, media['file_id'], key, media['media_type'], media['chat_id'], media['message_id'])).lastrowid
        self.db.audit(actor, 'submit_admin_receipt', {'receipt': receipt_id, 'player': player_id, 'amount': amount})
        return receipt_id

    def review_receipt(self, receipt_id, approve, actor):
        row = self.db.one('SELECT * FROM receipts WHERE id=?', (receipt_id,))
        if not row:
            raise UserError('رسید پیدا نشد.')
        if row['status'] != 'pending':
            raise UserError('این رسید قبلاً بررسی شده است؛ تغییر تکراری انجام نشد.')
        if approve:
            self.db.execute('INSERT INTO ledger(player_id,amount,note,source) VALUES (?,?,?,?)',
                            (row['player_id'], -row['amount'], f'پرداخت رسید #{receipt_id}', f'receipt:{receipt_id}'))
        self.db.execute('UPDATE receipts SET status=?,reviewed_by=? WHERE id=?',
                        ('approved' if approve else 'rejected', None if actor == 'auto' else actor, receipt_id))
        self.db.audit(actor, 'approve_receipt' if approve else 'reject_receipt', {'receipt': receipt_id})
        return row