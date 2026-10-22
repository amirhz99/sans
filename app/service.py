import difflib
import json
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .content import CONTENT_PATH, DEFAULTS
from . import jalali


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


# SPEC-016: one canonical shape for a shared phone number.  ``normalize`` already
# maps Persian/Arabic digits, so only separators have to be removed here.
PHONE_SEPARATORS = str.maketrans('', '', ' -.()\u200c')


def normalize_phone(value):
    """Return ``+98XXXXXXXXXX`` for an Iranian mobile number.

    Accepts the five shapes a phone card or a human actually produces
    (``+98912…``, ``0098912…``, ``98912…``, ``0912…``, ``912…``) and refuses
    everything else instead of guessing a digit.
    """
    if not isinstance(value, str):
        raise UserError('شمارهٔ تماس معتبر نیست؛ شماره را با دکمهٔ «ارسال شمارهٔ من» بفرستید.')
    digits = normalize(value).translate(PHONE_SEPARATORS)
    if not digits:
        raise UserError('شمارهٔ تماس خالی است؛ شماره را با دکمهٔ «ارسال شمارهٔ من» بفرستید.')
    if digits.startswith('+98'):
        core = digits[3:]
    elif digits.startswith('0098'):
        core = digits[4:]
    elif len(digits) == 12 and digits.startswith('98'):
        core = digits[2:]
    elif len(digits) == 11 and digits.startswith('0'):
        core = digits[1:]
    else:
        core = digits
    if not core.isdigit() or len(core) != 10 or not core.startswith('9'):
        raise UserError('شمارهٔ موبایل ایران باید ۱۰ رقم و با ۹ شروع شود؛ مثلاً ۰۹۱۲۱۲۳۴۵۶۷.')
    return '+98' + core


def setting_value_is_explicit(message, key, value):
    """Return whether a setting value is present in the current user message.

    Conversation history and a reply to an earlier question are deliberately
    not inputs here.  In particular, an acknowledgement such as «آره انجامش
    بده» must not become a card number or silently toggle a safety setting.
    """
    body = normalize(message)
    if not body:
        return False
    if key == 'approval_required':
        if not isinstance(value, bool):
            return False
        if value:
            markers = ('تایید', 'تأیید', 'فعال', 'روشن', 'لازم', 'بررسی شود', 'بررسی بشه')
        else:
            markers = ('خاموش', 'غیرفعال', 'غیر فعال', 'لازم نیست', 'بدون تایید', 'بدون تأیید')
        return any(normalize(marker) in body for marker in markers)
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = normalize(value)
    if candidate in body:
        return True
    # Models sometimes remove separators from card numbers while the user
    # writes them with spaces or dashes; compare the digit run as well.
    if key == 'card':
        digits = ''.join(char for char in candidate if char.isdigit())
        message_digits = ''.join(char for char in body if char.isdigit())
        return bool(digits) and digits in message_digits
    return False


TEHRAN = ZoneInfo('Asia/Tehran')
WEEKDAYS = {
    'دوشنبه': 0, 'سه شنبه': 1, 'سه‌شنبه': 1, 'چهارشنبه': 2,
    'پنجشنبه': 3, 'جمعه': 4, 'شنبه': 5, 'یکشنبه': 6, 'یک‌شنبه': 6,
}
JALALI_MONTHS = jalali.MONTHS


def _jalali_date(value, now, require_day=True):
    """Resolve a Jalali phrase («۹ خرداد» or «1405/03/09») to a Gregorian date.

    The year is optional and defaults to the current Jalali year, because every
    operator writes dates in the Solar Hijri calendar.  ``None`` means the text
    contains no Jalali date at all.  With ``require_day=False`` a month without
    a day is treated as historical text instead of an error, while an impossible
    day still raises.
    """
    numeric = re.search(r'(?<!\d)(1[34]\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)', value)
    if numeric:
        jy, jm, jd = (int(part) for part in numeric.groups())
    else:
        month = next((index for index, name in enumerate(JALALI_MONTHS, 1) if name in value), None)
        if month is None:
            return None
        name = JALALI_MONTHS[month - 1]
        position = value.find(name)
        before = re.search(r'(?<!\d)([0-3]?\d)\s*$', value[:position])
        after = re.search(r'^\s*([0-3]?\d)(?!\d)', value[position + len(name):])
        day = before or after
        if not day:
            if not require_day:
                return None
            raise UserError('روز ماه شمسی مشخص نیست؛ مثلاً «۹ خرداد ساعت ۲۱» بنویسید.')
        year = re.search(r'(?<!\d)(1[34]\d{2})(?!\d)', value)
        jy = int(year.group(1)) if year else jalali.from_gregorian(now.date())[0]
        jm, jd = month, int(day.group(1))
    try:
        return jalali.to_gregorian(jy, jm, jd)
    except ValueError:
        raise UserError('تاریخ سانس معتبر نیست؛ روز و ماه شمسی را بررسی کنید.')


def parse_session_datetime(value, now=None):
    """Parse an exact Persian session date/time and return a Tehran ISO value.

    Jalali dates («۹ خرداد ساعت ۲۱», «1405/03/09 21:00») and Gregorian dates
    (``YYYY-MM-DD``) are both accepted.  Deliberately does not guess a day
    period: «ساعت ۹» and «امروز عصر» are rejected because either
    morning/night or an exact hour is missing.  A weekday means the next
    occurrence (including today when still upcoming).
    """
    if not isinstance(value, str) or not value.strip():
        raise UserError('تاریخ و ساعت دقیق سانس را اعلام کنید؛ مثلاً «فردا ساعت ۲۱» یا «۹ خرداد ساعت ۲۱».')
    raw = normalize(value)
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

    date_text = raw[:clock.start()] + ' ' + raw[clock.end():]
    target = now.date()
    explicit = re.search(r'(?<!\d)(20\d{2})[-/]([01]?\d)[-/]([0-3]?\d)(?!\d)', date_text)
    jalali_target = _jalali_date(date_text, now)
    if explicit:
        try:
            target = date(int(explicit.group(1)), int(explicit.group(2)), int(explicit.group(3)))
        except ValueError:
            raise UserError('تاریخ سانس معتبر نیست.')
    elif jalali_target is not None:
        target = jalali_target
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
                 'finalize', 'adjustment', 'settings', 'player_note', 'settle',
                 'player_rename', 'player_merge', 'player_delete', 'broadcast'}

# SPEC-012/014: one card never carries more than this many commands or
# recipients; a longer request is a sign of a parsing mistake.
MAX_BATCH = 30
MAX_RECIPIENTS = 200
BROADCAST_LIMIT = 3500

SETTING_LABELS = {'card': 'شماره کارت', 'payment_note': 'پیام راهنمای پرداخت',
                  'approval_required': 'بررسی رسید توسط ادمین'}


class Service:
    def __init__(self, db):
        self.db = db

    def require_explicit_setting(self, message, args):
        if not isinstance(args, dict):
            raise UserError('کلید و مقدار تنظیم را صریحاً در همین پیام بنویسید.')
        key = args.get('key')
        value = args.get('value')
        if key not in ('card', 'payment_note', 'approval_required') or not setting_value_is_explicit(message, key, value):
            raise UserError('مقدار تنظیم باید صریحاً در همین پیام نوشته شود؛ پاسخ‌هایی مثل «آره انجامش بده» کافی نیستند.')

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

    def similar_players(self, name, exclude_id=None):
        """Return existing players whose name strongly matches ``name``.

        The result is advisory: it stops the agent from creating a second row
        for a shortened or misspelled name, but a human still decides whether
        to rename, merge, or force a genuinely new player.
        """
        clean = normalize(name)
        if not clean:
            return []
        found = []
        for row in self.db.all('SELECT * FROM players ORDER BY id'):
            if exclude_id is not None and row['id'] == exclude_id:
                continue
            other = row['normalized_name']
            ratio = difflib.SequenceMatcher(None, clean, other).ratio()
            if other == clean:
                reason = 'نام یکسان'
            elif min(len(clean), len(other)) >= 3 and (
                    f' {clean} ' in f' {other} ' or f' {other} ' in f' {clean} '):
                reason = 'بخشی از نام کامل'
            elif ratio >= 0.85:
                # Two full names that only share a family name are different
                # people («مهیار قهرمان زاده» vs «مهراد قهرمان زاده»); require
                # the given name to match as well before suggesting a merge.
                if len(clean.split()) > 1 and len(other.split()) > 1:
                    given = difflib.SequenceMatcher(None, clean.split()[0], other.split()[0]).ratio()
                    if given < 0.85:
                        continue
                reason = 'املای نزدیک'
            else:
                continue
            found.append({'id': row['id'], 'name': row['name'], 'reason': reason,
                          'ratio': round(ratio, 2)})
        found.sort(key=lambda item: (-item['ratio'], item['id']))
        return found

    def player_usage(self, player_id, ignore_pending=None):
        """Count every record that refers to a player (used by merge/delete)."""
        usage = {table: self.db.one(f'SELECT COUNT(*) n FROM {table} WHERE player_id=?',
                                    (player_id,))['n']
                 for table in ('attendance', 'ledger', 'receipts')}
        usage['balance'] = self.balance(player_id)
        usage['pending'] = self._pending_reference(player_id, ignore=ignore_pending)
        return usage

    def _pending_reference(self, *player_ids, ignore=None):
        """Return a pending proposal id that points at one of the players."""
        wanted = set(player_ids)
        for row in self.db.all("SELECT id,payload FROM pending WHERE status='pending'"):
            if ignore is not None and row['id'] == ignore:
                # The proposal currently being applied refers to the player by
                # definition; it must not block its own operation.
                continue
            try:
                payload = json.loads(row['payload'])
            except (TypeError, ValueError):
                continue
            stack = [payload]
            while stack:
                item = stack.pop()
                if isinstance(item, dict):
                    for key, value in item.items():
                        if (key in ('player', 'source', 'target') and isinstance(value, int)
                                and not isinstance(value, bool) and value in wanted):
                            return row['id']
                        stack.append(value)
                elif isinstance(item, list):
                    stack.extend(item)
        return None

    def _merge_conflict(self, source, target, ignore_pending=None):
        """Return why two players must not be merged, or ``None`` when safe."""
        if source['bale_id'] is not None and target['bale_id'] is not None:
            return 'هر دو بازیکن به یک حساب بله متصل هستند؛ ادغام دو حساب متصل مجاز نیست.'
        pending = self._pending_reference(source['id'], target['id'], ignore=ignore_pending)
        if pending:
            return f'درخواست منتظر {pending} به یکی از این دو بازیکن اشاره می‌کند؛ اول آن را تأیید یا رد کنید.'
        guests = {}
        for row in self.db.all('SELECT session_id,player_id,guests FROM attendance WHERE player_id IN (?,?)',
                               (source['id'], target['id'])):
            guests.setdefault(row['session_id'], {})[row['player_id']] = row['guests']
        for session_id, per_player in guests.items():
            if (source['id'] in per_player and target['id'] in per_player
                    and per_player[source['id']] > per_player[target['id']]):
                return (f'در سانس #{session_id} برای «{source["name"]}» {per_player[source["id"]]} مهمان '
                        f'و برای «{target["name"]}» {per_player[target["id"]]} مهمان ثبت شده؛ '
                        'ادغام باعث گم‌شدن مهمان می‌شود.')
        return None

    def rename_player(self, player_id, new_name, actor='agent'):
        """Rename a player while keeping the id and the whole history."""
        player = self.player(player_id)
        clean = text(new_name, 'نام جدید')
        norm = normalize(clean)
        if norm == player['normalized_name']:
            raise UserError('نام جدید با نام فعلی یکسان است.')
        clash = self.db.one('SELECT * FROM players WHERE normalized_name=? AND id<>?', (norm, player['id']))
        if clash:
            raise UserError(f'این نام قبلاً برای #{clash["id"]} {clash["name"]} ثبت شده؛ '
                            'برای یکی‌کردن دو رکورد از ادغام استفاده کنید.')
        self.db.execute('UPDATE players SET name=?, normalized_name=? WHERE id=?',
                        (clean, norm, player['id']))
        self.db.audit(actor, 'player_rename', {'player': player['id'], 'old': player['name'], 'new': clean})
        return {'id': player['id'], 'old': player['name'], 'new': clean}

    def merge_players(self, source_id, target_id, actor='agent', ignore_pending=None):
        """Move all history from one player to another, then delete the source."""
        source, target = self.player(source_id), self.player(target_id)
        if source['id'] == target['id']:
            raise UserError('مبدأ و مقصد ادغام یک نفر است؛ دو بازیکن متفاوت انتخاب کنید.')
        conflict = self._merge_conflict(source, target, ignore_pending=ignore_pending)
        if conflict:
            raise UserError(conflict)
        moved = {'attendance': 0, 'ledger': 0, 'receipts': 0}
        for row in self.db.all('SELECT session_id FROM attendance WHERE player_id=?', (source['id'],)):
            existing = self.db.one('SELECT 1 FROM attendance WHERE session_id=? AND player_id=?',
                                   (row['session_id'], target['id']))
            if existing:
                self.db.execute('DELETE FROM attendance WHERE session_id=? AND player_id=?',
                                (row['session_id'], source['id']))
            else:
                self.db.execute('UPDATE attendance SET player_id=? WHERE session_id=? AND player_id=?',
                                (target['id'], row['session_id'], source['id']))
            moved['attendance'] += 1
        for table in ('ledger', 'receipts'):
            # ``ledger.source`` stays untouched on purpose: it is a unique
            # audit key for the original transaction, not an ownership field.
            moved[table] = self.db.execute(
                f'UPDATE {table} SET player_id=? WHERE player_id=?', (target['id'], source['id'])).rowcount
        note = target['note'] or ''
        if source['note'] and source['note'] not in note:
            note = ((note + ' | ') if note else '') + source['note']
        bale_id = target['bale_id'] if target['bale_id'] is not None else source['bale_id']
        if source['bale_id'] is not None and target['bale_id'] is None:
            # Release the unique bale_id before moving it to the target.
            self.db.execute('UPDATE players SET bale_id=NULL WHERE id=?', (source['id'],))
        self.db.execute('UPDATE players SET note=?, bale_id=? WHERE id=?', (note[:500], bale_id, target['id']))
        self.db.execute('DELETE FROM players WHERE id=?', (source['id'],))
        self.db.audit(actor, 'player_merge', {'source': source['id'], 'source_name': source['name'],
                                              'target': target['id'], 'target_name': target['name'],
                                              **moved})
        return {'source': source['id'], 'target': target['id'],
                'target_name': target['name'], 'moved': moved}

    def _assert_deletable(self, player, ignore_pending=None):
        usage = self.player_usage(player['id'], ignore_pending=ignore_pending)
        if player['bale_id'] is not None or usage['attendance'] or usage['ledger'] or usage['receipts']:
            raise UserError(f"«{player['name']}» حضور، تراکنش یا رسید ثبت‌شده دارد و حذف نمی‌شود؛ "
                            'برای حفظ سابقه آن را با بازیکن درست ادغام کنید.')
        if usage['pending']:
            raise UserError(f"برای «{player['name']}» درخواست منتظر {usage['pending']} وجود دارد؛ "
                            'اول آن را تأیید یا رد کنید.')

    def delete_player(self, player_id, actor='agent', ignore_pending=None):
        """Delete a player only when no record references it."""
        player = self.player(player_id)
        self._assert_deletable(player, ignore_pending=ignore_pending)
        self.db.execute('DELETE FROM players WHERE id=?', (player['id'],))
        self.db.audit(actor, 'player_delete', {'player': player['id'], 'name': player['name']})
        return {'id': player['id'], 'name': player['name']}

    def _player_weight(self, row):
        usage = self.player_usage(row['id'])
        return (usage['attendance'] + usage['ledger'] + usage['receipts'],
                len(row['normalized_name']), -row['id'])

    def duplicate_players(self):
        """Return suspected duplicate pairs, keeping the richer record."""
        pairs, seen = [], set()
        for row in self.db.all('SELECT * FROM players ORDER BY id'):
            for match in self.similar_players(row['name'], exclude_id=row['id']):
                key = tuple(sorted((row['id'], match['id'])))
                if key in seen:
                    continue
                seen.add(key)
                other = self.db.one('SELECT * FROM players WHERE id=?', (match['id'],))
                keep, drop = ((row, other) if self._player_weight(row) >= self._player_weight(other)
                              else (other, row))
                pairs.append({'keep': {'id': keep['id'], 'name': keep['name']},
                              'drop': {'id': drop['id'], 'name': drop['name']},
                              'reason': match['reason'], 'ratio': match['ratio']})
        return pairs

    def duplicates_report(self):
        pairs = self.duplicate_players()
        if not pairs:
            return 'هیچ نام تکراری یا ناقصی پیدا نشد؛ هویت بازیکن‌ها یکدست است.'
        lines = ['نام‌های مشکوک به تکرار:']
        for item in pairs:
            keep, drop = item['keep'], item['drop']
            lines.append(f"• #{drop['id']} {drop['name']} ← #{keep['id']} {keep['name']} "
                         f"({item['reason']}، شباهت {item['ratio']}) | پیشنهاد: /merge {drop['id']} {keep['id']}")
        return '\n'.join(lines)

    def ensure_player(self, name, note='', actor='agent', allow_similar=False):
        clean = text(name, 'نام')
        norm = normalize(clean)
        row = self.db.one('SELECT * FROM players WHERE normalized_name=?', (norm,))
        if row:
            if note and note.strip() and note.strip() not in (row['note'] or ''):
                updated_note = ((row['note'] + ' | ' if row['note'] else '') + note.strip())[:500]
                self.db.execute('UPDATE players SET note=? WHERE id=?', (updated_note, row['id']))
            return self.db.one('SELECT * FROM players WHERE id=?', (row['id'],))
        similar = [] if allow_similar else self.similar_players(clean)
        if similar:
            raise UserError('بازیکن جدید ساخته نشد؛ این نام با بازیکن‌های موجود مشابه است:\n' + '\n'.join(
                f"#{item['id']} {item['name']}" for item in similar) +
                '\nاگر منظورتان یکی از آن‌هاست همان را با نام کامل یا شناسه انتخاب کنید؛ '
                'اگر واقعاً فرد جدیدی است، نام کامل‌تری بفرستید یا صریحاً بگویید «بازیکن جدید بساز».')
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
            phrase = str(when_text).strip()
            # Historical text may lack a day or an exact hour, but a Jalali day
            # that does not exist must never be stored silently.
            _jalali_date(normalize(phrase), datetime.now(TEHRAN), require_day=False)
            try:
                session_at = parse_session_datetime(phrase)
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
        """Return the readable Jalali time, or the untouched legacy text."""
        if row['session_at']:
            return Service.session_at_display(row['session_at'], with_zone=False)
        return row['when_text'] or 'نامشخص'

    @staticmethod
    def session_time_display(row):
        prefix = 'زمان تهران: ' if row['session_at'] else 'زمان (متن قدیمی): '
        return prefix + Service.session_time(row)

    @staticmethod
    def session_at_display(value, with_zone=True):
        """Render a stored timestamp in the Jalali calendar Tehran time."""
        try:
            parsed = datetime.fromisoformat(value).astimezone(TEHRAN)
        except (TypeError, ValueError):
            return str(value)
        text = f'{jalali.format_date(parsed.date())}، ساعت {jalali.persian_digits(f"{parsed:%H:%M}")}'
        return text + ' (Asia/Tehran)' if with_zone else text

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

    def broadcast_recipients(self, player_ids=None):
        """Return (recipients, unreachable names) for a proactive message.

        Only a player with a linked Bale account can be addressed directly; an
        unlinked name is reported instead of being guessed (SPEC-014).
        """
        if player_ids is None:
            rows = self.db.all('SELECT id, name, bale_id FROM players ORDER BY id')
        else:
            rows = [self.player(ident) for ident in player_ids]
        recipients, unreachable = [], []
        for row in rows:
            chat_id = row['bale_id']
            if isinstance(chat_id, int) and not isinstance(chat_id, bool) and chat_id > 0:
                recipients.append({'player': row['id'], 'name': row['name'], 'chat_id': chat_id})
            else:
                unreachable.append(row['name'])
        return recipients, unreachable

    def touch_contact(self, user_id, chat_id, display_name='', username='', phone=None):
        """Remember a private chat address without touching domain state."""
        if (isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0
                or isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id <= 0):
            return None
        self.db.execute(
            '''INSERT INTO contacts(user_id,chat_id,display_name,username,phone) VALUES (?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,
                 display_name=excluded.display_name, username=excluded.username,
                 phone=COALESCE(NULLIF(excluded.phone,''), contacts.phone),
                 updated_at=CURRENT_TIMESTAMP''',
            (user_id, chat_id, str(display_name or '')[:200], str(username or '').lstrip('@')[:100],
             str(phone or '')[:100]),
        )
        return user_id

    def set_player_phone(self, player_id, phone, actor):
        """Store one canonical phone number on a player (SPEC-016).

        Idempotent: repeating the stored value writes nothing and records no
        audit row.  The audit row keeps the player id only, so the number is
        not copied into a second table.
        """
        player = self.player(player_id)
        canonical = normalize_phone(phone)
        if player['phone'] == canonical:
            return {'id': player['id'], 'name': player['name'], 'phone': canonical, 'changed': False}
        self.db.execute('UPDATE players SET phone=? WHERE id=?', (canonical, player['id']))
        self.db.audit(actor, 'set_player_phone', {'player': player['id']})
        return {'id': player['id'], 'name': player['name'], 'phone': canonical, 'changed': True}

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

    def prepare(self, action, args, ignore_pending=None):
        """Resolve ambiguous names now, validate all input, return an immutable proposal.

        ``ignore_pending`` is the proposal currently being applied; without it a
        merge/delete proposal would block itself because it references its own
        target player.
        """
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
        elif action == 'player_rename':
            p = self.player(args.get('player', ''))
            a = {'player': p['id'], 'name': text(args.get('name'), 'نام جدید')}
            norm = normalize(a['name'])
            if norm == p['normalized_name']:
                raise UserError('نام جدید با نام فعلی یکسان است.')
            if self.db.one('SELECT id FROM players WHERE normalized_name=? AND id<>?', (norm, p['id'])):
                raise UserError('این نام قبلاً ثبت شده؛ برای یکی‌کردن دو رکورد از ادغام استفاده کنید.')
        elif action == 'player_merge':
            source = self.player(args.get('source', ''))
            target = self.player(args.get('target', ''))
            if source['id'] == target['id']:
                raise UserError('مبدأ و مقصد ادغام باید دو بازیکن متفاوت باشند.')
            conflict = self._merge_conflict(source, target, ignore_pending=ignore_pending)
            if conflict:
                raise UserError(conflict)
            a = {'source': source['id'], 'target': target['id']}
        elif action == 'player_delete':
            p = self.player(args.get('player', ''))
            self._assert_deletable(p, ignore_pending=ignore_pending)
            a = {'player': p['id']}
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
        elif action == 'broadcast':
            # SPEC-014: the audience is resolved from the database here, never
            # taken from the model, and nothing is delivered before approval.
            a['text'] = text(args.get('text'), 'متن پیام', limit=BROADCAST_LIMIT)
            raw = args.get('players')
            if raw is None or (isinstance(raw, str) and not raw.strip()) or raw == []:
                a['players'] = None
            elif isinstance(raw, str) and normalize(raw) in ('all', 'همه', 'همه بازیکن ها', 'همه بازیکن‌ها'):
                a['players'] = None
            else:
                refs = (raw if isinstance(raw, list)
                        else [ref for ref in re.split(r'[,،\n]+', str(raw)) if ref.strip()])
                if len(refs) > MAX_RECIPIENTS:
                    raise UserError(f'در هر ارسال حداکثر {MAX_RECIPIENTS} بازیکن مجاز است.')
                ids = []
                for ref in refs:
                    ident = self.player(ref)['id']
                    if ident not in ids:
                        ids.append(ident)
                if not ids:
                    raise UserError('گیرنده‌ای مشخص نشد؛ نام بازیکن‌ها را بفرستید یا «همه» را بگویید.')
                a['players'] = ids
            recipients, _ = self.broadcast_recipients(a['players'])
            if not recipients:
                raise UserError('هیچ بازیکن متصلی برای دریافت پیام وجود ندارد؛ ابتدا بازیکن‌ها با /identify یا /bind به بله متصل شوند.')
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

    def guard(self, command):
        """Return the facts a proposal preview depends on (SPEC-011).

        The fingerprint is stored with the proposal and recomputed right before
        it is applied.  A mismatch means the world moved on since the admin saw
        the preview, so the proposal expires instead of applying a stale amount.
        Only identifiers, amounts, counts, and current values are returned; no
        message text or private note is included.
        """
        action, a = command['action'], command['args']
        if action == 'settle':
            return {'balance': self.balance(a['player'])}
        if action == 'finalize':
            session = self.session(a['session'])
            attendance = [[row['player_id'], row['guests']] for row in self.db.all(
                'SELECT player_id, guests FROM attendance WHERE session_id=? ORDER BY player_id',
                (session['id'],))]
            return {'finalized': int(session['finalized']), 'person_fee': session['person_fee'],
                    'attendance': attendance}
        if action == 'settings':
            return {'value': self.db.setting(a['key'])}
        if action == 'attendance':
            row = self.db.one('SELECT guests FROM attendance WHERE session_id=? AND player_id=?',
                              (a['session'], a['player']))
            return {'finalized': int(self.session(a['session'])['finalized']),
                    'guests': None if row is None else row['guests']}
        if action == 'update_session':
            session = self.session(a['session'])
            return {key: session[key] for key in sorted(a) if key != 'session'}
        if action in ('player_note', 'player_rename'):
            key = 'note' if action == 'player_note' else 'name'
            return {key: self.player(a['player'])[key]}
        if action == 'player_merge':
            return {'source': a['source'], 'target': a['target']}
        if action in ('adjustment', 'declare_debt', 'player_delete'):
            return {'player': a['player']}
        if action == 'broadcast':
            recipients, _ = self.broadcast_recipients(a['players'])
            return {'recipients': sorted(r['player'] for r in recipients), 'text': a['text']}
        return {}

    def setting_display(self, key, value):
        """Render a stored setting value for a human reader."""
        if key == 'approval_required':
            return ('فعال؛ هر رسید توسط ادمین بررسی می‌شود' if value == 'true'
                    else 'غیرفعال؛ رسیدها خودکار تأیید می‌شوند')
        return value or '—'

    def settings_preview(self, a):
        """Describe one settings change without applying it."""
        key, value = a['key'], a['value']
        lines = ['تغییر تنظیمات',
                 f'{SETTING_LABELS[key]}: {self.setting_display(key, value)}']
        current = self.db.setting(key)
        if current is not None and current != value:
            lines.append(f'مقدار فعلی: {self.setting_display(key, current)}')
        if key == 'approval_required' and value == 'false':
            lines.append('هشدار: بدون بررسی بانکی، هر رسید با مبلغ اعلامی کاربر از بدهی کم خواهد شد.')
        return '\n'.join(lines)

    def preview(self, command):
        action, a = command['action'], command['args']
        if action == 'settings':
            return self.settings_preview(a)
        if action == 'player_merge':
            source, target = self.player(a['source']), self.player(a['target'])
            usage = self.player_usage(source['id'])
            return '\n'.join([
                'ادغام بازیکن',
                f"حذف‌شونده: #{source['id']} {source['name']}",
                f"باقی‌مانده: #{target['id']} {target['name']}",
                f"تاریخچهٔ منتقل‌شده: {usage['attendance']} حضور، {usage['ledger']} تراکنش، {usage['receipts']} رسید",
                'شناسهٔ باقی‌مانده و همهٔ سوابقش حفظ می‌شود؛ رکورد تکراری حذف می‌شود.',
            ])
        if action == 'player_rename':
            p = self.player(a['player'])
            return '\n'.join(['تغییرنام بازیکن', f"#{p['id']} «{p['name']}» ← «{a['name']}»",
                              'شناسه و کل تاریخچه حفظ می‌شود.'])
        if action == 'player_delete':
            p = self.player(a['player'])
            return '\n'.join(['حذف بازیکن', f"#{p['id']} {p['name']}",
                              'این بازیکن هیچ حضور، تراکنش یا رسیدی ندارد.'])
        if action == 'broadcast':
            recipients, unreachable = self.broadcast_recipients(a['players'])
            names = '، '.join(r['name'] for r in recipients[:10])
            if len(recipients) > 10:
                names += f' و {len(recipients) - 10} نفر دیگر'
            lines = ['ارسال پیام به بازیکنان', a['text'], '',
                     f'گیرندگان ({len(recipients)} نفر): {names}']
            if unreachable:
                lines.append(f'بدون حساب متصل و بدون دریافت پیام ({len(unreachable)} نفر): ' +
                             '، '.join(unreachable[:10]))
            lines.append('پیام فقط پس از تأیید شما ارسال می‌شود.')
            return '\n'.join(lines)
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
        return '\n'.join(lines)

    def apply(self, command, actor, source):
        """Caller must hold a transaction and check proposal guard / authorization."""
        action, a = command['action'], command['args']
        # Revalidate after confirmation as a defense in depth.
        raw = dict(a)
        if action == 'settings' and a['key'] == 'approval_required':
            raw['value'] = a['value'] == 'true'
        ignored = None
        if isinstance(source, str) and source.startswith('proposal:'):
            ignored = source.split(':')[1] or None
        a = self.prepare(action, raw, ignore_pending=ignored)['args']
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
        elif action == 'broadcast':
            # SPEC-014: one outreach row per recipient so delivery is traceable,
            # and one private outbox row per message. Nothing is sent here.
            recipients, _ = self.broadcast_recipients(a['players'])
            for recipient in recipients:
                outreach_id = self.db.execute(
                    "INSERT INTO outreach(player_id,chat_id,text,status) VALUES (?,?,?,'queued')",
                    (recipient['player'], recipient['chat_id'], a['text'])).lastrowid
                self.db.queue('sendMessage', {'chat_id': recipient['chat_id'], 'text': a['text']},
                              ref=f'outreach:{outreach_id}')
            self.db.audit(actor, action, {'recipients': len(recipients), 'players': a['players'],
                                          'text_length': len(a['text'])})
            return
        elif action == 'settings':
            self.db.set_setting(a['key'], a['value'])
        elif action == 'player_note':
            self.db.execute('UPDATE players SET note=? WHERE id=?', (a['note'], a['player']))
        elif action == 'player_rename':
            self.rename_player(a['player'], a['name'], actor)
        elif action == 'player_merge':
            self.merge_players(a['source'], a['target'], actor, ignore_pending=ignored)
        elif action == 'player_delete':
            self.delete_player(a['player'], actor, ignore_pending=ignored)
        else:
            # Unknown actions must never be acknowledged as applied.
            raise UserError('درخواست مدیریتی معتبر نیست.')
        if action not in ('player_rename', 'player_merge', 'player_delete'):
            # The three identity operations record their own detailed audit row.
            self.db.audit(actor, action, a)

    def bind(self, player_id, user_id, actor):
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise UserError('شناسهٔ کاربر بله معتبر نیست.')
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