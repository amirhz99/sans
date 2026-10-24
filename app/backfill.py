"""Import real sessions from the operator's roster and reconcile the workbook.

SPEC-022.  ``football-account.xlsx`` is the reference document for the period up
to 16 Mordad 1405: it holds the sessions attended, the sessions paid and the
debt of every person.  This module turns that reference into ordinary sessions,
attendance rows and ledger entries, and it never deletes financial history — a
correction is always its own ledger row with its own ``source``.
"""

import csv
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from . import jalali
from .service import (TEHRAN, Service, UserError, integer, normalize,
                      parse_session_datetime, text)

# Every session of this period was played on a Friday at 16:30 in the Teymouri
# Tarasht hall; the operator confirmed the schedule and the hall cost.
FRIDAY = 4
SESSION_HOUR = '16:30'
LOCATION = 'سالن تیموری طرشت'
VENUE_COST = 2500000
PERIOD = '۱۶ مرداد ۱۴۰۵'
SESSIONS_KEY = 'backfill:sessions'
# A stored date this close to a planned Friday is the same session recorded
# with a wrong day: the live row says «۹ خرداد» (a Saturday) for the Friday
# session of 8 Khordad.
NEAR_DAYS = 3

ABSENT = ('', '-', '—', 'غایب', 'خیر', 'no', 'n')
PRESENT = ('x', '✓', 'v', 'حاضر', 'بله', 'yes', 'y')
GUEST_HEADERS = ('guests', 'مهمان', 'تعداد مهمان')
DATE_HEADERS = ('date', 'تاریخ')
NAME_HEADERS = ('name', 'نام')

# ``ledger.source`` is UNIQUE, so a second correction of the same pair needs a
# new key instead of overwriting the first one.
SOURCE_LIMIT = 50


class _DryRun(Exception):
    """Raised inside the transaction so a dry run rolls back every write."""


def xlsx_rows(path):
    """Read the small, plain XLSX workbook used for the football account."""
    namespace = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with ZipFile(path) as workbook:
        shared = []
        if 'xl/sharedStrings.xml' in workbook.namelist():
            root = ET.fromstring(workbook.read('xl/sharedStrings.xml'))
            for item in root.findall('m:si', namespace):
                shared.append(''.join(node.text or '' for node in item.iter(
                    '{%s}t' % namespace['m'])))
        sheet_names = [name for name in workbook.namelist()
                       if name.startswith('xl/worksheets/') and name.endswith('.xml')]
        if not sheet_names:
            raise UserError('فایل Excel هیچ شیتی ندارد.')
        root = ET.fromstring(workbook.read(sorted(sheet_names)[0]))
        rows = []
        for row in root.findall('.//m:sheetData/m:row', namespace):
            values = {}
            for cell in row.findall('m:c', namespace):
                reference = cell.attrib.get('r', '')
                column = ''.join(char for char in reference if char.isalpha())
                value = cell.find('m:v', namespace)
                value = None if value is None else value.text
                if cell.attrib.get('t') == 's' and value is not None:
                    try:
                        value = shared[int(value)]
                    except (IndexError, ValueError):
                        raise UserError('رشتهٔ مشترک نامعتبر در فایل Excel.') from None
                elif cell.attrib.get('t') == 'inlineStr':
                    value = ''.join(node.text or '' for node in cell.iter(
                        '{%s}t' % namespace['m']))
                values[column] = value
            if values:
                rows.append(values)
    if not rows:
        raise UserError('فایل Excel خالی است.')
    return rows


def _header(value):
    """Normalize one workbook header cell for a tolerant column lookup."""
    return ' '.join(str(value).replace('\u200c', ' ').split())


def _column(header, *prefixes):
    for name, column in header.items():
        if any(name.startswith(prefix) for prefix in prefixes):
            return column
    return None


def _sheet_int(value, label):
    """Return a non-negative integer from a sheet cell, ``0`` when empty."""
    if value is None or not str(value).strip():
        return 0
    raw = normalize(value).replace(',', '').replace('٬', '')
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise UserError(f'{label} در Excel عدد نیست.') from None
    if number != number.to_integral_value() or number < 0:
        raise UserError(f'{label} در Excel باید عدد صحیح نامنفی باشد.')
    return int(number)


def read_overrides(path, fee):
    """Read explicit operator corrections without changing the workbook.

    The CSV has ``name,attended,paid`` columns; ``debt`` is optional and, when
    present, is expressed in thousands of تومان.  An override may also add a
    person absent from the workbook (for example an approved late addition).
    """
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        fields = {normalize(value): value for value in (reader.fieldnames or [])}
        if not fields.get('نام') and not fields.get('name'):
            raise UserError('فایل اصلاحات باید ستون name یا نام داشته باشد.')
        result, seen = {}, set()
        name_field = fields.get('نام') or fields.get('name')
        for raw in reader:
            name = text(raw.get(name_field, ''), 'نام اصلاحات')
            key = normalize(name)
            if key in seen:
                raise UserError('نام تکراری در فایل اصلاحات: ' + name)
            seen.add(key)
            item = {'name': name}
            for key_name, aliases in (('attended', ('جلسات آمده', 'attended')),
                                      ('paid', ('جلسات پرداخت شده', 'جلسات پرداخت', 'paid'))):
                field = next((fields[a] for a in aliases if a in fields), None)
                if field and str(raw.get(field, '')).strip():
                    item[key_name] = _sheet_int(raw[field], f'{key_name} {name}')
            debt_field = next((fields[a] for a in ('مجموع بدهکاری', 'مجموع', 'debt') if a in fields), None)
            if debt_field and str(raw.get(debt_field, '')).strip():
                item['debt'] = _sheet_int(raw[debt_field], f'بدهی {name}') * 1000
            if 'attended' not in item or 'paid' not in item:
                raise UserError(f'اصلاح {name} باید attended و paid داشته باشد.')
            expected = (item['attended'] - item['paid']) * fee
            if 'debt' in item and item['debt'] != expected:
                raise UserError(f'بدهی اصلاحی {name} با attended و paid نمی‌خواند.')
            item['debt'] = expected
            result[key] = item
    return result


def read_workbook(path, fee, overrides=None):
    """Return ``[{'name', 'attended', 'paid', 'debt'}]`` from the account sheet.

    ``overrides`` is an explicit correction CSV path; corrected rows are merged
    after workbook validation and may add approved people.
    """
    rows = xlsx_rows(path)
    header = {_header(value): column for column, value in rows[0].items()
              if value is not None}
    name_column = _column(header, 'نام', 'name')
    attended_column = _column(header, 'جلسات آمده', 'attended')
    paid_column = _column(header, 'جلسات پرداخت', 'paid')
    debt_column = _column(header, 'مجموع', 'debt')
    if not (name_column and attended_column and paid_column):
        raise UserError('Excel باید ستون‌های نام، جلسات آمده و جلسات پرداخت شده را داشته باشد.')
    result, seen = [], set()
    for row in rows[1:]:
        raw_name = row.get(name_column)
        if raw_name is None or not str(raw_name).strip():
            continue
        name = text(str(raw_name), 'نام')
        key = normalize(name)
        if key in seen:
            raise UserError('نام تکراری در اکسل: ' + name)
        seen.add(key)
        attended = _sheet_int(row.get(attended_column), f'جلسات آمدهٔ {name}')
        paid = _sheet_int(row.get(paid_column), f'جلسات پرداخت‌شدهٔ {name}')
        expected = (attended - paid) * fee
        stated = row.get(debt_column) if debt_column else None
        if stated is not None and str(stated).strip():
            # The debt column is written in هزار تومان, like the sheet's own
            # ۱۲۰۰ for 1,200,000 T.
            amount = _sheet_int(stated, f'بدهی {name}') * 1000
            if amount != expected:
                raise UserError(
                    f'بدهی {name} در اکسل {amount:,} تومان است ولی '
                    f'({attended}−{paid})×{fee:,} = {expected:,} می‌دهد؛ ابتدا اکسل را اصلاح کنید.')
        result.append({'name': name, 'attended': attended, 'paid': paid, 'debt': expected})
    if not result:
        raise UserError('اکسل هیچ بازیکنی ندارد.')
    if overrides:
        corrections = read_overrides(overrides, fee)
        by_name = {normalize(row['name']): row for row in result}
        for key, correction in corrections.items():
            if key in by_name:
                by_name[key].update(correction)
            else:
                result.append(correction)
    return result


def workbook_amount(value, attended, paid, fee):
    """Return the تومان debt of one snapshot row (SPEC-022 fixes the formula).

    The legacy ``--import-players`` path uses this helper: a blank debt cell
    still means «the formula», but the paid column is no longer ignored.
    """
    if value is None or not str(value).strip():
        return (integer(attended, 'جلسات آمده', 0, 10000)
                - integer(paid, 'جلسات پرداخت‌شده', 0, 10000)) * fee
    return _sheet_int(value, 'بدهی') * 1000
def phrase_date(value):
    """Return the Tehran ``date`` inside a session phrase, or ``None``.

    Only an explicit Jalali date counts: a relative phrase such as
    «پنجشنبه ساعت ۲۱» resolves against today and must never match a historical
    session.
    """
    if not value:
        return None
    text_value = str(value)
    if not (any(month in text_value for month in jalali.MONTHS)
            or re.search(r'(?<!\d)1[34]\d{2}[-/]\d{1,2}[-/]\d{1,2}(?!\d)', text_value)):
        return None
    candidates = (text_value, f'{text_value} ساعت {SESSION_HOUR}')
    for candidate in candidates:
        try:
            stamp = parse_session_datetime(candidate)
        except UserError:
            continue
        moment = datetime.fromisoformat(stamp)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=TEHRAN)
        return moment.astimezone(TEHRAN).date()
    return None


def planned_sessions(labels, hour=SESSION_HOUR):
    """Resolve roster date labels into the ten canonical Friday sessions."""
    sessions, seen = [], set()
    for label in labels:
        clean = str(label or '').strip()
        if not clean:
            continue
        phrase = clean if 'ساعت' in clean else f'{clean} ساعت {hour}'
        try:
            stamp = parse_session_datetime(phrase)
        except UserError:
            raise UserError(f'تاریخ «{clean}» در لیست حضور معتبر نیست؛ مثلاً «۸ خرداد».') from None
        moment = datetime.fromisoformat(stamp).astimezone(TEHRAN)
        if moment.weekday() != FRIDAY:
            raise UserError(
                f'{jalali.format_date(moment.date())} جمعه نیست؛ همهٔ سانس‌های این دوره جمعه بوده‌اند.')
        if moment.date() in seen:
            raise UserError(f'تاریخ {jalali.format_date(moment.date())} دو بار در لیست آمده است.')
        seen.add(moment.date())
        label_text = jalali.format_date(moment.date(), with_year=False)
        sessions.append({
            'date': moment.date(),
            'source': clean,
            'label': label_text,
            'title': f'سانس {label_text}',
            'when_text': jalali.format_date(moment.date()),
            'session_at': stamp,
        })
    if not sessions:
        raise UserError('لیست حضور هیچ تاریخی ندارد؛ یک ستون برای هر سانس لازم است.')
    sessions.sort(key=lambda item: item['date'])
    return sessions


def _guests(value):
    """Return the guest count of one attendance cell, ``None`` when absent."""
    if value is None:
        return None
    raw = normalize(value)
    if raw in ABSENT:
        return None
    if raw in PRESENT:
        return 0
    try:
        guests = int(raw)
    except ValueError:
        raise UserError(f'مقدار «{value}» در لیست حضور معتبر نیست؛ خالی، ۰ یا تعداد مهمان بنویسید.') from None
    if not 0 <= guests <= 20:
        raise UserError('تعداد مهمان در هر سانس باید بین ۰ تا ۲۰ باشد.')
    return guests


def _index(header, names):
    for name in names:
        if name in header:
            return header.index(name)
    return None


def read_plan(path, hour=SESSION_HOUR):
    """Read a wide (people × dates) or long (date, name, guests) roster.

    The wide form is what the operator fills in: one row per person and one
    column per session date.  The long form is easier to generate by hand and
    produces exactly the same plan.
    """
    path = Path(path)
    with path.open(encoding='utf-8-sig', newline='') as handle:
        rows = [row for row in csv.reader(handle) if any(str(cell).strip() for cell in row)]
    if not rows:
        raise UserError('لیست حضور خالی است؛ یک سرستون تاریخ لازم است.')
    header = [normalize(cell) for cell in rows[0]]
    date_column = _index(header, DATE_HEADERS)
    name_column = _index(header, NAME_HEADERS)
    guests_column = _index(header, GUEST_HEADERS)
    if date_column is not None and name_column is not None and guests_column is not None:
        labels, entries = _long_plan(rows, date_column, name_column, guests_column)
    else:
        labels, entries = _wide_plan(rows)
    sessions = planned_sessions(labels, hour)
    canonical = {normalize(session['source']): session['label'] for session in sessions}
    for entry in entries:
        key = normalize(entry['label'])
        if key not in canonical:
            raise UserError(f'تاریخ «{entry["label"]}» در سرستون لیست حضور نیست.')
        entry['label'] = canonical[key]
    return {'sessions': sessions, 'entries': entries}


def _long_plan(rows, date_column, name_column, guests_column):
    labels, entries = [], []
    for row in rows[1:]:
        label = str(row[date_column] if date_column < len(row) else '').strip()
        name = str(row[name_column] if name_column < len(row) else '').strip()
        guests = _guests(row[guests_column] if guests_column < len(row) else '')
        if not label or not name or guests is None:
            continue
        labels.append(label)
        entries.append({'label': label, 'name': text(name, 'نام'), 'guests': guests})
    return labels, entries


def _wide_plan(rows):
    labels = [str(cell).strip() for cell in rows[0][1:]]
    entries = []
    for row in rows[1:]:
        name = str(row[0] if row else '').strip()
        if not name:
            continue
        for position, label in enumerate(labels, 1):
            guests = _guests(row[position] if position < len(row) else '')
            if guests is not None:
                entries.append({'label': label, 'name': text(name, 'نام'), 'guests': guests})
    return labels, entries


def _stored_date(row):
    """Return the date a stored session claims, or ``None``."""
    if row['session_at']:
        try:
            moment = datetime.fromisoformat(row['session_at'])
        except (TypeError, ValueError):
            moment = None
        if moment is not None:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=TEHRAN)
            return moment.astimezone(TEHRAN).date()
    return phrase_date(row['when_text']) or phrase_date(row['title'])


def _match_session(db, planned, used):
    """Find the stored row that already represents one planned Friday.

    An exact date wins; a row within ``NEAR_DAYS`` is the same session recorded
    with a wrong day and is repaired instead of duplicated.
    """
    near = None
    for row in db.all('SELECT * FROM sessions ORDER BY id'):
        if row['id'] in used:
            continue
        stored = _stored_date(row)
        if stored is None:
            continue
        if stored == planned['date']:
            return row
        if near is None and abs((stored - planned['date']).days) <= NEAR_DAYS:
            near = row
    return near


def _repair_session(db, row, planned, venue_cost, fee):
    """Bring one stored session to the canonical Friday, keeping its identity."""
    changes = {}
    if row['title'] != planned['title']:
        clash = db.one('SELECT id FROM sessions WHERE title=? AND id<>?', (planned['title'], row['id']))
        if clash:
            raise UserError(f'سانس #{clash["id"]} قبلاً عنوان «{planned["title"]}» را دارد؛ '
                            'ابتدا آن ردیف تکراری را بررسی کنید.')
        changes['title'] = planned['title']
    if row['when_text'] != planned['when_text']:
        changes['when_text'] = planned['when_text']
    if row['session_at'] != planned['session_at']:
        changes['session_at'] = planned['session_at']
    if row['venue_cost'] != venue_cost:
        changes['venue_cost'] = venue_cost
    if row['person_fee'] != fee:
        changes['person_fee'] = fee
    if not str(row['location'] or '').strip():
        changes['location'] = LOCATION
    for key, value in changes.items():
        db.execute(f'UPDATE sessions SET {key}=? WHERE id=?', (value, row['id']))
    notes = 0
    if changes.get('title'):
        # The amount stays; only the session name inside the ledger note was
        # wrong.  A note is text, not money, so correcting it is safe.
        old = row['title'] + '؛'
        for entry in db.all('SELECT id, note FROM ledger WHERE session_id=?', (row['id'],)):
            if entry['note'].startswith(old):
                db.execute('UPDATE ledger SET note=? WHERE id=?',
                           (planned['title'] + '؛' + entry['note'][len(old):], entry['id']))
                notes += 1
    if changes:
        db.audit('backfill', 'repair_session', {'session': row['id'], 'changes': sorted(changes),
                                                'notes': notes})
    return db.one('SELECT * FROM sessions WHERE id=?', (row['id'],)), sorted(changes), notes


def _post_charge(db, session, player_id, target, guests, removed=False):
    """Bring one player's charge for one session to ``target`` تومان.

    The first write uses the same source a finalized session uses; every later
    correction is a new row, because a ledger row is never rewritten.
    """
    prefix = f'session:{session["id"]}:player:{player_id}'
    # ``prefix`` alone must be matched exactly: as a LIKE pattern it would also
    # swallow player 11 when player 1 is being corrected.
    posted = db.one('SELECT COALESCE(SUM(amount),0) n FROM ledger '
                    'WHERE source = ? OR source LIKE ?',
                    (prefix, prefix + ':adjust%'))['n']
    delta = target - posted
    if delta == 0:
        return 0
    source = prefix
    if db.one('SELECT 1 FROM ledger WHERE source=?', (source,)):
        source = prefix + ':adjust'
        attempt = 2
        while db.one('SELECT 1 FROM ledger WHERE source=?', (source,)):
            source = f'{prefix}:adjust:{attempt}'
            attempt += 1
            if attempt > SOURCE_LIMIT:
                raise UserError(f'اصلاح‌های سانس #{session["id"]} از حد مجاز گذشت؛ حساب را دستی بررسی کنید.')
    if source == prefix:
        note = f"{session['title']}؛ خود فرد + {guests} مهمان. {session['note']}".strip()
    elif removed:
        note = f"{session['title']}؛ حذف حضور طبق لیست بازبینی — اصلاح سهم."
    else:
        note = f"{session['title']}؛ اصلاح سهم حضور — خود فرد + {guests} مهمان."
    db.execute('INSERT INTO ledger(player_id,session_id,amount,note,source) VALUES (?,?,?,?,?)',
               (player_id, session['id'], delta, note, source))
    return delta


def _blocked_message(blocked):
    lines = ['این نام‌ها به بازیکن‌های موجود شبیه‌اند و خودکار ثبت نمی‌شوند:']
    for item in blocked:
        near = '، '.join(f"#{c['id']} {c['name']}" for c in item['similar'][:3])
        lines.append(f"• {item['name']} ≈ {near}")
    lines.append('اگر یکی هستند: --alias "نام لیست=نام ذخیره‌شده" — اگر فرد جدید است: --allow-new "نام لیست".')
    return '\n'.join(lines)
def resolve_players(db, names, aliases=None, allow_new=()):
    """Map roster names to player ids, renaming aliases and creating new people.

    Returns ``{'ids': {normalized: id}, 'created': [...], 'renamed': [...],
    'blocked': [...]}``.  A blocked name resembles an existing player, so the
    operator has to say whether it is the same person (``--alias``) or a new one
    (``--allow-new``); nothing is guessed.
    """
    service = Service(db)
    alias_map = {normalize(key): value for key, value in (aliases or {}).items()}
    allowed = {normalize(name) for name in allow_new}
    found = {'ids': {}, 'created': [], 'renamed': [], 'blocked': []}
    for name in names:
        key = normalize(name)
        if key in found['ids']:
            continue
        stored_ref = alias_map.get(key)
        if stored_ref:
            stored = service.player(stored_ref)
            if stored['normalized_name'] != key:
                found['renamed'].append(service.rename_player(stored['id'], name, 'backfill'))
            found['ids'][key] = stored['id']
            continue
        row = db.one('SELECT * FROM players WHERE normalized_name=?', (key,))
        if row:
            found['ids'][key] = row['id']
            continue
        similar = [] if key in allowed else service.similar_players(name)
        if similar:
            found['blocked'].append({'name': name, 'similar': similar})
            continue
        player_id = db.execute('INSERT INTO players(name,normalized_name) VALUES (?,?)',
                               (name, key)).lastrowid
        db.audit('backfill', 'new_player', {'player': player_id, 'name': name})
        found['created'].append({'player': player_id, 'name': name})
        found['ids'][key] = player_id
    return found
def apply_sessions(db, plan, aliases=None, allow_new=(), dry_run=False,
                   fee=150000, venue_cost=VENUE_COST):
    """Create or repair the planned sessions and apply the roster as a delta.

    Existing attendance and charges survive: the missing people are inserted, a
    changed guest count posts the difference, and a person dropped from the
    roster gets a reversal row.  Everything happens in one transaction, so a
    validation failure leaves the database untouched.
    """
    report = {'dry_run': bool(dry_run), 'sessions': [], 'created': 0, 'repaired': 0,
              'players_created': [], 'renamed': [], 'blocked': [], 'added': 0,
              'updated': 0, 'removed': 0, 'posted': 0, 'notes': 0}
    names, seen = [], set()
    for entry in plan['entries']:
        key = normalize(entry['name'])
        if key not in seen:
            seen.add(key)
            names.append(entry['name'])
    try:
        with db.conn:
            found = resolve_players(db, names, aliases, allow_new)
            report['players_created'] = found['created']
            report['renamed'] = found['renamed']
            report['blocked'] = found['blocked']
            if found['blocked']:
                raise UserError(_blocked_message(found['blocked']))
            ids, used = {}, set()
            for planned in plan['sessions']:
                row = _match_session(db, planned, used)
                if row is None:
                    session_id = db.execute(
                        '''INSERT INTO sessions(title,when_text,session_at,location,venue_cost,person_fee)
                           VALUES (?,?,?,?,?,?)''',
                        (planned['title'], planned['when_text'], planned['session_at'],
                         LOCATION, venue_cost, fee)).lastrowid
                    row = db.one('SELECT * FROM sessions WHERE id=?', (session_id,))
                    db.audit('backfill', 'new_session', {'session': session_id,
                                                         'title': planned['title']})
                    report['created'] += 1
                    changes = ['created']
                else:
                    row, changes, notes = _repair_session(db, row, planned, venue_cost, fee)
                    report['repaired'] += 1
                    report['notes'] += notes
                used.add(row['id'])
                ids[planned['label']] = row['id']
                report['sessions'].append({
                    'id': row['id'], 'title': row['title'], 'date': str(planned['date']),
                    'session_at': row['session_at'], 'changes': changes})
            wanted = {}
            for entry in plan['entries']:
                player_id = found['ids'][normalize(entry['name'])]
                wanted.setdefault(ids[entry['label']], {})[player_id] = entry['guests']
            _apply_attendance(db, wanted, report, fee)
            session_ids = [ids[planned['label']] for planned in plan['sessions']]
            db.set_setting(SESSIONS_KEY, json.dumps(session_ids))
            db.audit('backfill', 'import_sessions',
                     {'sessions': session_ids, 'added': report['added'],
                      'updated': report['updated'], 'removed': report['removed'],
                      'posted': report['posted']})
            if dry_run:
                raise _DryRun()
    except _DryRun:
        pass
    return report


def _apply_attendance(db, wanted, report, fee):
    """Apply one roster as a delta per session and post the missing amounts."""
    for session_id, target in wanted.items():
        session = db.one('SELECT * FROM sessions WHERE id=?', (session_id,))
        current = {row['player_id']: row['guests'] for row in
                   db.all('SELECT * FROM attendance WHERE session_id=?', (session_id,))}
        for player_id, guests in target.items():
            if player_id not in current:
                db.execute('INSERT INTO attendance VALUES (?,?,?)', (session_id, player_id, guests))
                report['added'] += 1
            elif current[player_id] != guests:
                db.execute('UPDATE attendance SET guests=? WHERE session_id=? AND player_id=?',
                           (guests, session_id, player_id))
                report['updated'] += 1
            if _post_charge(db, session, player_id, (1 + guests) * fee, guests):
                report['posted'] += 1
        for player_id, guests in current.items():
            if player_id in target:
                continue
            db.execute('DELETE FROM attendance WHERE session_id=? AND player_id=?',
                       (session_id, player_id))
            report['removed'] += 1
            if _post_charge(db, session, player_id, 0, guests, removed=True):
                report['posted'] += 1
        db.execute('UPDATE sessions SET finalized=1 WHERE id=?', (session_id,))
_PAID_SOURCE = 'backfill:paid:'


def paid_total(db, player_id):
    """Return how much one person has paid, in تومان.

    A session charge is what the period *asks*, and an ``opening:`` row is a
    snapshot of debt, so neither is a payment.  Every other row (a proposal, a
    receipt, a correction) counts, which keeps the import idempotent even after
    a positive correction raised the debt.
    """
    return -db.one("SELECT COALESCE(SUM(amount),0) n FROM ledger WHERE player_id=? "
                   "AND source NOT LIKE 'session:%' AND source NOT LIKE 'opening:%'",
                   (player_id,))['n']


def apply_payments(db, rows, fee, dry_run=False, aliases=None, allow_new=()):
    """Bring every person's payments to the workbook's paid-session count.

    A person who paid more than the sheet records gets a positive correction
    row, so the balance rises to the sheet's debt; nobody's history is deleted.
    A name the roster did not create is created here from the sheet, because the
    sheet is the reference for who owes what; an ambiguous name still blocks the
    whole import.  A second run computes a zero difference and writes nothing.
    """
    report = {'dry_run': bool(dry_run), 'adjusted': [], 'unchanged': 0,
              'created': [], 'renamed': [], 'blocked': []}
    try:
        with db.conn:
            found = resolve_players(db, [row['name'] for row in rows], aliases, allow_new)
            report['created'] = found['created']
            report['renamed'] = found['renamed']
            report['blocked'] = found['blocked']
            if found['blocked']:
                raise UserError(_blocked_message(found['blocked']))
            for row in rows:
                player = db.one('SELECT * FROM players WHERE normalized_name=?',
                                (normalize(row['name']),))
                target = row['paid'] * fee
                current = paid_total(db, player['id'])
                delta = current - target
                if delta == 0:
                    report['unchanged'] += 1
                    continue
                source = f'{_PAID_SOURCE}{player["id"]}'
                attempt = 2
                while db.one('SELECT 1 FROM ledger WHERE source=?', (source,)):
                    source = f'{_PAID_SOURCE}{player["id"]}:{attempt}'
                    attempt += 1
                    if attempt > SOURCE_LIMIT:
                        raise UserError('اصلاح پرداخت تکراری از حد مجاز گذشت؛ حساب را دستی بررسی کنید.')
                note = (f'پرداخت طبق اکسل {PERIOD}' if delta < 0
                        else f'اصلاح پرداخت طبق اکسل {PERIOD}')
                db.execute('INSERT INTO ledger(player_id,amount,note,source) VALUES (?,?,?,?)',
                           (player['id'], delta, note, source))
                db.audit('backfill', 'payment_adjust', {'player': player['id'],
                                                        'paid': target, 'amount': delta})
                report['adjusted'].append({'name': player['name'], 'amount': delta})
            if dry_run:
                raise _DryRun()
    except _DryRun:
        pass
    return report


def marked_sessions(db):
    """Return the session ids the last import covered (SPEC-022)."""
    try:
        ids = json.loads(db.setting(SESSIONS_KEY, '[]'))
    except (TypeError, ValueError):
        return []
    return [int(ident) for ident in ids if isinstance(ident, int) or str(ident).isdigit()]


def reconcile(db, rows, fee, session_ids=None):
    """Compare the ledger with the workbook.  Read-only: it never writes."""
    if session_ids is None:
        session_ids = marked_sessions(db)
    covered = set(session_ids) if session_ids else None
    # The workbook counts slots, not rows: a player with two guests counts as
    # three attended sessions, which is how the operator keeps the sheet.
    attended = {}
    for row in db.all('SELECT session_id, player_id, guests FROM attendance'):
        if covered is not None and row['session_id'] not in covered:
            continue
        attended[row['player_id']] = attended.get(row['player_id'], 0) + 1 + row['guests']
    result = {'rows': [], 'missing': [], 'extra': [], 'ok': True,
              'session_ids': list(session_ids or []), 'sheet_total': 0}
    seen = set()
    for row in rows:
        player = db.one('SELECT * FROM players WHERE normalized_name=?', (normalize(row['name']),))
        if not player:
            result['missing'].append(row['name'])
            result['ok'] = False
            continue
        seen.add(player['id'])
        balance = db.one('SELECT COALESCE(SUM(amount),0) n FROM ledger WHERE player_id=?',
                         (player['id'],))['n']
        paid = paid_total(db, player['id'])
        count = attended.get(player['id'], 0)
        item = {'name': player['name'], 'attended': count, 'sheet_attended': row['attended'],
                'paid': paid, 'sheet_paid': row['paid'] * fee, 'balance': balance,
                'sheet_debt': row['debt'], 'diff': balance - row['debt']}
        result['rows'].append(item)
        result['sheet_total'] += row['debt']
        if item['diff'] or count != row['attended'] or paid != row['paid'] * fee:
            result['ok'] = False
    for player in db.all('SELECT * FROM players ORDER BY id'):
        if player['id'] in seen:
            continue
        balance = db.one('SELECT COALESCE(SUM(amount),0) n FROM ledger WHERE player_id=?',
                         (player['id'],))['n']
        if balance:
            result['extra'].append({'name': player['name'], 'balance': balance})
            result['ok'] = False
    result['ledger_total'] = db.one('SELECT COALESCE(SUM(amount),0) n FROM ledger')['n']
    return result
TABLE_HEAD = 'نام                          آمده  اکسل   پرداخت     اکسل      مانده       اکسل    تفاضل'
TABLE_RULE = '-' * 96


def format_reconcile(result):
    """Render the reconcile table the operator reads before trusting a total."""
    lines = []
    if result['session_ids']:
        lines.append('سانس‌های بررسی‌شده: ' + '، '.join(f'#{ident}' for ident in result['session_ids']))
    else:
        lines.append('سانسی علامت نخورده؛ همهٔ سانس‌های دیتابیس شمرده شد.')
    lines.append(TABLE_HEAD)
    for row in result['rows']:
        lines.append('%-26s %5d %5d %9s %9s %10s %10s %8s' % (
            row['name'], row['attended'], row['sheet_attended'],
            f"{row['paid']:,}", f"{row['sheet_paid']:,}",
            f"{row['balance']:,}", f"{row['sheet_debt']:,}", f"{row['diff']:,}"))
    lines.append(TABLE_RULE)
    lines.append(f"جمع اکسل: {result['sheet_total']:,} تومان — جمع دفتر: {result['ledger_total']:,} تومان")
    if result['missing']:
        lines.append('در دیتابیس نیستند: ' + '، '.join(result['missing']))
    for item in result['extra']:
        lines.append(f"خارج از اکسل: {item['name']} با {item['balance']:,} تومان")
    lines.append('نتیجه: ' + ('همهٔ ردیف‌ها با اکسل می‌خوانند.' if result['ok']
                             else 'اختلاف هست؛ تا صفر‌شدن تفاضل‌ها ورود اطلاعات تمام نیست.'))
    return '\n'.join(lines)


def format_sessions(report):
    lines = []
    if report['dry_run']:
        lines.append('حالت آزمایشی: هیچ تغییری در دیتابیس ثبت نشد.')
    for item in report['sessions']:
        state = 'ساخته شد' if item['changes'] == ['created'] else 'به‌روز شد'
        lines.append(f"#{item['id']} {item['title']} — {state} ({item['date']})")
    lines.append(f"سانس جدید: {report['created']}؛ اصلاح‌شده: {report['repaired']}؛ "
                 f"حضور افزوده: {report['added']}؛ اصلاح حضور: {report['updated']}؛ "
                 f"حذف حضور: {report['removed']}؛ ردیف دفتر: {report['posted']}")
    if report['notes']:
        lines.append(f"متن دفتر {report['notes']} ردیف با عنوان درست سانس هم‌گام شد.")
    for item in report['renamed']:
        lines.append(f"نام #{item['id']} از «{item['old']}» به «{item['new']}» اصلاح شد.")
    if report['players_created']:
        lines.append(f"بازیکن جدید: {len(report['players_created'])} نفر — "
                     + '، '.join(item['name'] for item in report['players_created']))
    if report['blocked']:
        lines.append(_blocked_message(report['blocked']))
    return '\n'.join(lines)


def format_payments(report):
    lines = []
    if report['dry_run']:
        lines.append('حالت آزمایشی: هیچ تغییری در دیتابیس ثبت نشد.')
    for item in report['adjusted']:
        verb = 'پرداخت ثبت شد' if item['amount'] < 0 else 'اصلاح بدهی ثبت شد'
        lines.append(f"{item['name']}: {verb} {abs(item['amount']):,} تومان")
    lines.append(f"اصلاح‌شده: {len(report['adjusted'])}؛ بی‌تغییر: {report['unchanged']}")
    if report['created']:
        lines.append(f"بازیکن جدید از اکسل: {len(report['created'])} نفر — "
                     + '، '.join(item['name'] for item in report['created']))
    for item in report['renamed']:
        lines.append(f"نام #{item['id']} از «{item['old']}» به «{item['new']}» اصلاح شد.")
    if report['blocked']:
        lines.append(_blocked_message(report['blocked']))
    return '\n'.join(lines)