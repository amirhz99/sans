"""Import real sessions from the operator's roster and migrate named guests.

SPEC-022 introduced this module to turn a spreadsheet of the 1405 season into
ordinary sessions, attendance rows and ledger entries.  SPEC-039 retired the
spreadsheet half: the workbook reader, the payments importer and the reconcile
gate existed to close a single migration, which has since run, and they can no
longer pass against a ledger that has moved past their cut-off.

What remains is general and reusable for any club:

- ``read_plan``/``apply_sessions`` — create and repair real sessions from a
  roster CSV (``--import-sessions``).
- ``read_guest_links``/``migrate_guests`` — move explicit named guests onto
  their host's account (``--migrate-guests``).

The module never deletes financial history — a correction is always its own
ledger row with its own ``source``.
"""

import csv
import json
import re
from datetime import datetime
from pathlib import Path

from . import jalali
from .service import (TEHRAN, Service, UserError, normalize,
                      parse_session_datetime, text)

# Fallback defaults used when a roster row does not carry a hall.
# These are placeholders: pass ``--venue-cost`` and edit the location (or set
# the ``location`` default in ``content/bot-content.json``) for your own club.
FRIDAY = 4
SESSION_HOUR = '16:30'
LOCATION = 'سالن فوتسال نمونه'
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


def read_guest_links(path):
    """Read the explicit guest/host links for the SPEC-024 migration.

    The file is deliberately declarative: no relationship is inferred from a
    name.  ``guest`` and ``host`` are required; ``status`` is optional and is
    retained for operator review.
    """
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        fields = {normalize(value): value for value in (reader.fieldnames or [])}
        guest_field = fields.get('guest') or fields.get('مهمان')
        host_field = fields.get('host') or fields.get('میزبان')
        if not guest_field or not host_field:
            raise UserError('فایل مهمان‌ها باید ستون guest و host داشته باشد.')
        status_field = fields.get('status') or fields.get('وضعیت')
        result = []
        seen = set()
        for raw in reader:
            guest = text(raw.get(guest_field, ''), 'نام مهمان')
            host = text(raw.get(host_field, ''), 'نام میزبان')
            key = normalize(guest)
            if key in seen:
                raise UserError('مهمان تکراری در فایل پیوندها: ' + guest)
            if key == normalize('مهمان نواز'):
                raise UserError('«مهمان نواز» نباید به‌عنوان مهمان ثبت شود.')
            if key == normalize(host):
                raise UserError('مهمان و میزبان نمی‌توانند یک نفر باشند: ' + guest)
            seen.add(key)
            result.append({'guest': guest, 'host': host,
                           'status': (raw.get(status_field, '').strip() if status_field else '')})
    return result


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
            # SPEC-026: the shared helper names the real weekday, so the message
            # cannot claim «جمعه نیست» without saying what it actually is.
            raise UserError(
                f'{jalali.format_date(moment.date())} {jalali.weekday_name(moment.date())} است، '
                'نه جمعه؛ همهٔ سانس‌های این دوره جمعه بوده‌اند.')
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
                # SPEC-024: a roster row means the person attended himself.
                db.execute('INSERT INTO attendance(session_id,player_id,guests,own) VALUES (?,?,?,1)',
                           (session_id, player_id, guests))
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


def marked_sessions(db):
    """Return the session ids the last import covered (SPEC-022)."""
    try:
        ids = json.loads(db.setting(SESSIONS_KEY, '[]'))
    except (TypeError, ValueError):
        return []
    return [int(ident) for ident in ids if isinstance(ident, int) or str(ident).isdigit()]


def _migrated(db, guest_name, session_id):
    """Return ``True`` when this guest already left this session (SPEC-024)."""
    return db.one('SELECT 1 FROM guest_migrations WHERE guest_name=? AND session_id=?',
                  (guest_name, session_id)) is not None


def _session_distance(left, right):
    """Distance between ISO session timestamps, with deterministic fallback."""
    if not left or not right:
        return 10**18
    try:
        return abs((datetime.fromisoformat(left) - datetime.fromisoformat(right)).total_seconds())
    except ValueError:
        return 10**18


def _guest_host_session(db, host_id, session_at):
    """Return the host's own attendance row closest to ``session_at``.

    ``own=1`` rows only: a session the host himself played.  An equal distance is
    resolved by the earlier timestamp, so the previous session always wins.
    """
    rows = db.all('''SELECT a.session_id, a.guests, a.own, s.session_at
                     FROM attendance a JOIN sessions s ON s.id=a.session_id
                     WHERE a.player_id=? AND a.own=1''', (host_id,))
    if not rows:
        return None
    return min(rows, key=lambda item: (_session_distance(session_at, item['session_at']),
                                       item['session_at'] or '', item['session_id']))


def _retire_guest(db, guest):
    """Delete an emptied guest account, keeping its name and history (SPEC-024).

    The name stays in ``attendance_guests`` and in the ledger notes, so the
    operator loses nothing.  An account with a bound chat, a receipt or a pending
    request is never deleted.
    """
    if guest['bale_id'] is not None:
        return 0
    for table in ('attendance', 'ledger', 'receipts'):
        if db.one(f'SELECT 1 FROM {table} WHERE player_id=?', (guest['id'],)):
            return 0
    if Service(db).pending_reference(guest['id']):
        return 0
    db.execute('DELETE FROM players WHERE id=?', (guest['id'],))
    db.audit('backfill', 'guest_account_retired', {'player': guest['id'], 'name': guest['name']})
    return 1


def migrate_guests(db, links, dry_run=False):
    """Move explicitly linked guests under their host's account (SPEC-024).

    A guest never keeps an account of its own.  Its attendance row becomes one
    guest slot on the host's row in the closest session the host himself
    attended, and its ledger rows move to the host unchanged, so the ledger total
    cannot grow.  A host who never attended at all keeps the guest in the guest's
    own session with ``own=0``, which charges the guest share only.
    """
    report = {'dry_run': bool(dry_run), 'moved': [], 'created_hosts': [],
              'guest_only_rows': [], 'already_done': 0, 'posted': 0, 'removed_players': 0}
    try:
        with db.conn:
            for link in links:
                guest_name = link['guest']
                guest = db.one('SELECT * FROM players WHERE normalized_name=?',
                               (normalize(guest_name),))
                if not guest:
                    # A previous run already moved this guest and retired it.
                    if db.one('SELECT 1 FROM guest_migrations WHERE guest_name=?', (guest_name,)):
                        report['already_done'] += 1
                        continue
                    raise UserError('مهمان در دیتابیس نیست: ' + guest_name)
                host = db.one('SELECT * FROM players WHERE normalized_name=?',
                              (normalize(link['host']),))
                if not host:
                    host_id = db.execute(
                        'INSERT INTO players(name,normalized_name) VALUES (?,?)',
                        (link['host'], normalize(link['host']))).lastrowid
                    db.audit('backfill', 'guest_host_created',
                             {'host': host_id, 'name': link['host'], 'guest': guest['id']})
                    host = db.one('SELECT * FROM players WHERE id=?', (host_id,))
                    report['created_hosts'].append(link['host'])
                attendance = db.all(
                    '''SELECT a.session_id, s.title, s.session_at FROM attendance a
                       JOIN sessions s ON s.id=a.session_id
                       WHERE a.player_id=? ORDER BY s.session_at, a.session_id''',
                    (guest['id'],))
                for row in attendance:
                    if _migrated(db, guest_name, row['session_id']):
                        report['already_done'] += 1
                        continue
                    target = _guest_host_session(db, host['id'], row['session_at'])
                    if target:
                        destination_id = target['session_id']
                        db.execute('UPDATE attendance SET guests=guests+1 '
                                   'WHERE session_id=? AND player_id=?',
                                   (destination_id, host['id']))
                    else:
                        destination_id = row['session_id']
                        # A guest-only row for this host may already exist here.
                        existing = db.one('SELECT 1 FROM attendance '
                                          'WHERE session_id=? AND player_id=?',
                                          (destination_id, host['id']))
                        if existing:
                            db.execute('UPDATE attendance SET guests=guests+1 '
                                       'WHERE session_id=? AND player_id=?',
                                       (destination_id, host['id']))
                        else:
                            db.execute('INSERT INTO attendance(session_id,player_id,guests,own) '
                                       'VALUES (?,?,1,0)', (destination_id, host['id']))
                        report['guest_only_rows'].append(f"{link['host']} در {row['title']}")
                    db.execute('''INSERT OR IGNORE INTO attendance_guests
                                  (session_id,host_id,name,normalized_name) VALUES (?,?,?,?)''',
                               (destination_id, host['id'], guest['name'],
                                normalize(guest['name'])))
                    # The money travels with the person: the rows keep their
                    # amount and their unique audit key, so the total is fixed.
                    moved = db.execute(
                        '''UPDATE ledger SET player_id=?, session_id=?
                           WHERE player_id=? AND (session_id=? OR session_id IS NULL)''',
                        (host['id'], destination_id, guest['id'], row['session_id'])).rowcount
                    report['posted'] += moved
                    db.execute('DELETE FROM attendance WHERE session_id=? AND player_id=?',
                               (row['session_id'], guest['id']))
                    db.execute('''INSERT INTO guest_migrations
                                  (guest_name,session_id,host_id,destination_session_id)
                                  VALUES (?,?,?,?)''',
                               (guest_name, row['session_id'], host['id'], destination_id))
                    report['moved'].append({'guest': guest_name, 'host': host['name'],
                                            'from': row['session_id'], 'to': destination_id})
                report['removed_players'] += _retire_guest(db, guest)
            if dry_run:
                raise _DryRun()
    except _DryRun:
        pass
    return report


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


def format_guest_migration(report):
    lines = []
    if report['dry_run']:
        lines.append('حالت آزمایشی: هیچ تغییری در دیتابیس ثبت نشد.')
    lines.append(f"انتقال‌یافته: {len(report['moved'])}؛ ردیف دفتر منتقل‌شده: {report['posted']}؛ "
                 f"میزبان ساخته‌شده: {len(report['created_hosts'])}؛ "
                 f"ردیف «فقط مهمان»: {len(report['guest_only_rows'])}؛ "
                 f"حساب مهمان حذف‌شده: {report['removed_players']}")
    for item in report['moved']:
        lines.append(f"{item['guest']} ← {item['host']}؛ سانس #{item['from']} به #{item['to']}")
    return '\n'.join(lines)
