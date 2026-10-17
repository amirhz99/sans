import argparse
import csv
import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager
from zipfile import ZipFile
from xml.etree import ElementTree as ET
from pathlib import Path

from .bot import Bot
from .clients import AIClient, APIError, BaleClient
from .config import Config, ROOT, load_env
from .db import Database
from .service import UserError, integer, normalize, text

LOG = logging.getLogger('sans')
ACKNOWLEDGEMENT = 'پیامتان دریافت شد؛ در حال بررسی هستم…'


@contextmanager
def instance_lock(database):
    """Only one poller/importer may change this installation at a time (Linux)."""
    path = Path(str(database) + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UserError('ربات یا ابزار ورود اطلاعات دیگری در حال اجراست؛ ابتدا آن را متوقف کنید.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _xlsx_rows(path):
    """Read the small, plain XLSX workbook used for the football account snapshot."""
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


def _workbook_amount(value, sessions, name):
    """Return تومان; Excel's non-empty debt column is expressed in هزار تومان."""
    if value is None or not str(value).strip():
        count = integer(sessions, 'جلسات آمده', 0, 10000)
        return count * 150000
    try:
        units = Decimal(str(value).replace(',', '').replace('٬', '').strip())
    except InvalidOperation:
        raise UserError(f'مبلغ بدهی {name} در Excel معتبر نیست.') from None
    if units != units.to_integral_value() or units < 0:
        raise UserError(f'مبلغ بدهی {name} در Excel باید عدد صحیح مثبت یا صفر باشد.')
    return integer(int(units) * 1000, 'بدهی اولیه', 0)


def _read_xlsx_players(path):
    rows = _xlsx_rows(path)
    header = rows[0]
    columns = {str(value).replace('\u200c', ' ').strip(): column
               for column, value in header.items() if value is not None}
    required = {'نام   (تا 16 مرداد - 10 جلسه)', 'جلسات آمده', 'مجموع  بدهکاری'}
    if not required <= set(columns):
        raise UserError('Excel باید ستون‌های نام، جلسات آمده و مجموع بدهکاری را داشته باشد.')
    result = []
    for row in rows[1:]:
        raw_name = row.get(columns['نام   (تا 16 مرداد - 10 جلسه)'])
        if raw_name is None or not str(raw_name).strip():
            continue
        name = text(str(raw_name), 'نام')
        sessions = row.get(columns['جلسات آمده'])
        if sessions is None or not str(sessions).strip():
            raise UserError(f'تعداد جلسات {name} در Excel خالی است.')
        amount = _workbook_amount(row.get(columns['مجموع  بدهکاری']), sessions, name)
        result.append({'name': name, 'opening_balance': amount, 'note': 'بدهی تا آخر شهریور'})
    return result


def _read_csv_players(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not {'name', 'opening_balance', 'note'} <= set(reader.fieldnames or []):
            raise UserError('CSV باید ستون‌های name,opening_balance,note داشته باشد.')
        return list(reader)


def _backup_before_snapshot(db, digest):
    destination = db.path.with_name(db.path.name + f'.before-xlsx-{digest[:12]}.sqlite3')
    if destination.exists():
        return destination
    backup = sqlite3.connect(destination)
    try:
        db.conn.backup(backup)
    finally:
        backup.close()
    return destination


def import_players(db, path):
    """Import CSV as before, or replace the opening-balance snapshot from XLSX atomically."""
    path = Path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    is_xlsx = path.suffix.lower() == '.xlsx'
    import_key = ('xlsx-import:' if is_xlsx else 'import:') + digest
    if db.setting(import_key):
        return 0
    rows = _read_xlsx_players(path) if is_xlsx else _read_csv_players(path)
    if is_xlsx:
        _backup_before_snapshot(db, digest)
    with db.conn:
        seen = set()
        prepared = []
        for row in rows:
            name = text(row['name'], 'نام')
            norm = normalize(name)
            if norm in seen:
                raise UserError('نام تکراری در فایل: ' + name)
            seen.add(norm)
            amount = integer(row['opening_balance'], 'بدهی اولیه', -10**12)
            note = text(row.get('note', ''), 'توضیح', False)
            prepared.append((name, norm, amount, note))

        if is_xlsx:
            # This is a new accounting snapshot. Remove only prior imported opening
            # entries; sessions, receipts, and all other financial history survive.
            db.execute("DELETE FROM ledger WHERE source LIKE 'opening:%'")
            db.execute("DELETE FROM settings WHERE key LIKE 'opening:%'")

        for name, norm, amount, note in prepared:
            player = db.one('SELECT * FROM players WHERE normalized_name=?', (norm,))
            if not player:
                ident = db.execute('INSERT INTO players(name,normalized_name,note) VALUES (?,?,?)', (name, norm, note)).lastrowid
            else:
                ident = player['id']
            if not is_xlsx:
                previous = db.setting(f'opening:{ident}', '')
                if previous:
                    if int(previous) != amount:
                        raise UserError(f'ماندهٔ اولیهٔ {name} قبلاً ثبت شده؛ تغییر را با اصلاح حساب انجام دهید.')
                    continue
                if db.one('SELECT 1 FROM ledger WHERE player_id=?', (ident,)):
                    raise UserError(f'حساب {name} گردش دارد؛ برای اضافه‌کردن بدهی از اصلاح حساب استفاده کنید.')
            if amount:
                db.execute('INSERT INTO ledger(player_id,amount,note,source) VALUES (?,?,?,?)',
                           (ident, amount, note if is_xlsx else 'ماندهٔ اولیه؛ ' + note, f'opening:{ident}'))
            db.set_setting(f'opening:{ident}', amount)
            db.audit('cli', 'import_xlsx' if is_xlsx else 'import_player',
                     {'player': ident, 'opening_balance': amount})
        db.set_setting(import_key, 'done')
    return len(rows)


def flush_outbox(db, client, limit=20, pace=0.15):
    for _ in range(limit):
        row = db.one('SELECT * FROM outbox WHERE next_attempt<=? ORDER BY id LIMIT 1', (time.time(),))
        if not row:
            break
        try:
            payload = json.loads(row['payload'])
            client.call(row['method'], payload)
        except APIError as exc:
            if row['method'] == 'editMessageText':
                # Editing is a presentation optimization. If it is rejected
                # (for example because the provider no longer considers the
                # acknowledgement editable), deliver the same response as a
                # normal new message instead of retrying the edit forever.
                fallback = {key: value for key, value in payload.items()
                            if key not in ('message_id',)}
                with db.conn:
                    db.execute('DELETE FROM outbox WHERE id=?', (row['id'],))
                    db.queue('sendMessage', fallback)
                LOG.warning('Outbox %s edit failed; queued send fallback (%s)', row['id'], exc)
                if pace:
                    time.sleep(pace)
                continue
            delay = max(exc.retry_after, min(300, 2 ** min(row['attempts'] + 1, 8)))
            with db.conn:
                db.execute('UPDATE outbox SET attempts=attempts+1,next_attempt=? WHERE id=?',
                           (time.time() + delay, row['id']))
            LOG.warning('Outbox %s %s failed; retry scheduled (%s)', row['id'], row['method'], exc)
        else:
            with db.conn:
                db.execute('DELETE FROM outbox WHERE id=?', (row['id'],))
        if pace:
            time.sleep(pace)


def send_acknowledgement(client, chat_id):
    """Send a best-effort notice and return its provider message id."""
    try:
        result = client.call('sendMessage', {'chat_id': chat_id, 'text': ACKNOWLEDGEMENT})
        message_id = result.get('message_id') if isinstance(result, dict) else None
        return message_id if isinstance(message_id, int) and not isinstance(message_id, bool) else None
    except APIError as exc:
        # The real response remains in the transactional outbox; this notice is only a UX aid.
        LOG.warning('Immediate acknowledgement failed for chat %s (%s)', chat_id, exc)
        return None


def run(db, config):
    client = BaleClient(config.token)
    webhook = client.call('getWebhookInfo', {})
    if webhook and webhook.get('url'):
        raise UserError('این توکن webhook فعال دارد. ابتدا webhook قبلی را از API بله حذف کنید؛ polling هم‌زمان مجاز نیست.')
    me = client.call('getMe', {})
    with db.conn:
        db.set_setting('bot_id', me['id'])
        db.set_setting('bot_username', me.get('username', ''))
    bot = Bot(db, config, AIClient(config))
    LOG.info('Bot started; polling enabled')
    while True:
        try:
            bot.remind()
            flush_outbox(db, client)
            updates = client.call('getUpdates', {'offset': int(db.setting('offset', '0')), 'timeout': 20, 'limit': 50})
            for update in sorted(updates or [], key=lambda u: u['update_id']):
                chat_id = bot.acknowledgement_chat(update)
                acknowledgement = None
                if chat_id is not None:
                    message_id = send_acknowledgement(client, chat_id)
                    if message_id is not None:
                        acknowledgement = {'chat_id': chat_id, 'message_id': message_id}
                bot.process(update, acknowledgement)
                flush_outbox(db, client)
        except APIError as exc:
            LOG.warning('API unavailable: %s', exc)
            time.sleep(max(5, exc.retry_after))
        except Exception as exc:
            # No update is acknowledged on unexpected failures; replay cannot double-charge.
            LOG.error('Processing failed (%s); update will be retried', type(exc).__name__)
            time.sleep(5)


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description='Bale football-session manager')
    parser.add_argument('--database', type=Path, help='SQLite path (overrides DATABASE_PATH)')
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument('--init', action='store_true', help='Initialize database, no credentials needed')
    commands.add_argument('--import-players', type=Path, metavar='CSV_OR_XLSX')
    commands.add_argument('--backup', type=Path, metavar='DESTINATION', help='Create consistent SQLite backup')
    commands.add_argument('--discover-ids', action='store_true', help='Show IDs from recent messages; only BALE_BOT_TOKEN needed')
    commands.add_argument('--logs', type=int, nargs='?', const=30, metavar='N', help='Show last N agent trace logs from database')
    args = parser.parse_args()
    load_env()
    path = args.database or Path(os.getenv('DATABASE_PATH', 'data/sans.sqlite3'))
    if not path.is_absolute():
        path = ROOT / path
    try:
        if args.discover_ids:
            token = os.getenv('BALE_BOT_TOKEN', '').strip()
            if not token:
                raise UserError('ابتدا BALE_BOT_TOKEN را در .env وارد کنید.')
            updates = BaleClient(token).call('getUpdates', {'timeout': 5, 'limit': 100})
            found = set()
            for update in updates or []:
                message = update.get('message', {})
                if message.get('chat') and message.get('from'):
                    found.add((message['chat']['id'], message['from']['id'], message['chat'].get('type', '')))
            for chat_id, user_id, kind in sorted(found):
                print(f'chat_id={chat_id} user_id={user_id} type={kind}')
            if not found:
                print('Send /id to the bot privately and in the admin group, then run this command again.')
            return
        if args.logs is not None:
            db = Database(path)
            try:
                rows = db.recent_agent_logs(args.logs)
                if not rows:
                    print('No agent logs recorded yet.')
                for r in rows:
                    print(
                        f"#{r['id']} [{r['created_at']}] {r['event']} "
                        f"chat={r['chat_id']} user={r['user_id']}({r['sender_name']}) "
                        f"{r['duration_ms']}ms | {r['summary']} | {r['details']}"
                    )
            finally:
                db.close()
            return
        # Backups also acquire the installation lock: stop the bot first.
        with instance_lock(path):
            db = Database(path)
            try:
                if args.init:
                    print('Database initialized:', path)
                elif args.import_players:
                    print('Imported rows:', import_players(db, args.import_players))
                elif args.backup:
                    destination = args.backup.resolve()
                    if destination.exists():
                        raise UserError('فایل مقصد بکاپ نباید از قبل وجود داشته باشد.')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    backup = sqlite3.connect(destination)
                    try:
                        db.conn.backup(backup)
                    finally:
                        backup.close()
                    print('Backup created:', destination)
                else:
                    run(db, Config.from_env())
            finally:
                db.close()
    except KeyboardInterrupt:
        LOG.info('Stopped')
    except (ValueError, OSError, APIError, sqlite3.Error) as exc:
        # HTTP clients only expose sanitized error messages.
        parser.exit(1, f'Error: {exc}\n')


if __name__ == '__main__':
    main()