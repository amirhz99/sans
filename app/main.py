import argparse
import csv
import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .bot import Bot
from .clients import AIClient, APIError, BaleClient
from .config import Config, ROOT, load_env
from .db import Database
from .service import UserError, integer, normalize, text
from .turns import TurnBuffer
# SPEC-022: the roster importer and the guest migration live in their own
# module.  SPEC-039 retired the workbook half of it.
from .backfill import (SESSION_HOUR, VENUE_COST, apply_sessions,
                       format_guest_migration, format_sessions, migrate_guests,
                       read_guest_links, read_plan)

LOG = logging.getLogger('sans')
ACKNOWLEDGEMENT = 'پیامتان دریافت شد؛ در حال بررسی هستم…'
# SPEC-013: how often the polling loop looks for expired proposal cards.
SWEEP_INTERVAL = 300
# SPEC-018: how long two back-to-back messages from one person are merged into a
# single turn.  ``0`` disables coalescing and restores one reply per message.
TURN_COALESCE_SECONDS = 1.2


def coalesce_seconds():
    """Read the coalescing window from the environment, clamped and safe."""
    raw = os.getenv('TURN_COALESCE_SECONDS', '').strip()
    if not raw:
        return TURN_COALESCE_SECONDS
    try:
        return min(10.0, max(0.0, float(raw)))
    except ValueError:
        return TURN_COALESCE_SECONDS


def aliases(values):
    """Parse ``--alias "roster name=stored name"`` into a mapping (SPEC-022)."""
    result = {}
    for value in values or []:
        roster, separator, stored = str(value).partition('=')
        if not separator or not roster.strip() or not stored.strip():
            raise UserError('هر --alias باید به شکل "نام لیست=نام ذخیره‌شده" باشد.')
        result[roster.strip()] = stored.strip()
    return result


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


def _read_csv_players(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not {'name', 'opening_balance', 'note'} <= set(reader.fieldnames or []):
            raise UserError('CSV باید ستون‌های name,opening_balance,note داشته باشد.')
        return list(reader)


def import_players(db, path):
    """Import CSV opening balances, once per file digest.

    SPEC-039: the ``.xlsx`` snapshot branch is gone.  It existed to load one
    operator's spreadsheet and refused nothing a public user could provide.
    """
    path = Path(path)
    if path.suffix.lower() == '.xlsx':
        raise UserError('ورود فایل اکسل حذف شده است؛ فایل را به CSV با ستون‌های '
                        'name,opening_balance,note تبدیل کنید.')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    import_key = 'import:' + digest
    if db.setting(import_key):
        return 0
    rows = _read_csv_players(path)
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

        for name, norm, amount, note in prepared:
            player = db.one('SELECT * FROM players WHERE normalized_name=?', (norm,))
            if not player:
                ident = db.execute('INSERT INTO players(name,normalized_name,note) VALUES (?,?,?)', (name, norm, note)).lastrowid
            else:
                ident = player['id']
            previous = db.setting(f'opening:{ident}', '')
            if previous:
                if int(previous) != amount:
                    raise UserError(f'ماندهٔ اولیهٔ {name} قبلاً ثبت شده؛ تغییر را با اصلاح حساب انجام دهید.')
                continue
            if db.one('SELECT 1 FROM ledger WHERE player_id=?', (ident,)):
                raise UserError(f'حساب {name} گردش دارد؛ برای اضافه‌کردن بدهی از اصلاح حساب استفاده کنید.')
            if amount:
                db.execute('INSERT INTO ledger(player_id,amount,note,source) VALUES (?,?,?,?)',
                           (ident, amount, 'ماندهٔ اولیه؛ ' + note, f'opening:{ident}'))
            db.set_setting(f'opening:{ident}', amount)
            db.audit('cli', 'import_player',
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
            result = client.call(row['method'], payload)
        except APIError as exc:
            chat_id = payload.get('chat_id') if isinstance(payload, dict) else None
            if (row['method'] == 'sendMessage' and isinstance(chat_id, int)
                    and not isinstance(chat_id, bool) and chat_id > 0
                    and exc.code in (400, 403)):
                # SPEC-014: the recipient blocked the bot or the chat no longer
                # exists. Retrying can never succeed and would stall the queue
                # for every other chat, so drop it and record the failure.
                with db.conn:
                    db.execute('DELETE FROM outbox WHERE id=?', (row['id'],))
                    db.mark_delivery_failed(row['ref'])
                LOG.warning('Outbox %s private send dropped (%s)', row['id'], exc)
                if pace:
                    time.sleep(pace)
                continue
            if row['method'] == 'answerCallbackQuery':
                # A callback acknowledgement is a best-effort UI toast. The
                # provider expires callback query ids quickly, so a rejected
                # acknowledgement can never succeed on retry; the real response
                # is a separate outbox row. Drop it instead of retrying forever.
                with db.conn:
                    db.execute('DELETE FROM outbox WHERE id=?', (row['id'],))
                LOG.warning('Outbox %s answerCallbackQuery dropped (%s)', row['id'], exc)
                if pace:
                    time.sleep(pace)
                continue
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
                # SPEC-013/014: remember the provider message id of a proposal
                # card or a proactive player message.
                message_id = payload.get('message_id') if isinstance(payload, dict) else None
                if isinstance(message_id, bool) or not isinstance(message_id, int):
                    message_id = result.get('message_id') if isinstance(result, dict) else None
                db.record_delivery(row['ref'], message_id)
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
    # SPEC-018: merge rapid fragments into one turn instead of answering each one.
    turns = TurnBuffer(coalesce_seconds())
    # SPEC-013: close proposals that expired while the bot was not running, then
    # keep doing it on a bounded interval. No API request is involved.
    with db.conn:
        closed = bot.sweep_proposals()
    if closed:
        LOG.info('Closed %s expired proposals at startup', closed)
    last_sweep = time.time()
    LOG.info('Bot started; polling enabled')
    while True:
        try:
            if time.time() - last_sweep >= SWEEP_INTERVAL:
                last_sweep = time.time()
                with db.conn:
                    closed = bot.sweep_proposals()
                if closed:
                    LOG.info('Closed %s expired proposals', closed)
            bot.remind()
            flush_outbox(db, client)
            # SPEC-018: deliver the turns whose short window has closed before
            # asking for new updates, so a reply never waits on the long poll.
            for turn in turns.ready(time.time()):
                bot.process(turn, turn.get('_ack'), client=client)
                flush_outbox(db, client)
            # A pending turn must not sit behind the 20 second long poll.
            poll_timeout = 20 if not len(turns) else 1
            updates = client.call('getUpdates', {'offset': int(db.setting('offset', '0')),
                                                 'timeout': poll_timeout, 'limit': 50})
            for update in sorted(updates or [], key=lambda u: u['update_id']):
                state = turns.add(update, time.time())
                if state in ('joined', 'duplicate'):
                    # SPEC-042: a redelivered update is already part of the open
                    # turn; it must not add text or raise a second ack.
                    continue
                if state == 'new':
                    # Acknowledge the first fragment immediately; the reply comes
                    # once the person stops typing for the window.
                    chat_id = bot.acknowledgement_chat(update)
                    if chat_id is not None:
                        message_id = send_acknowledgement(client, chat_id)
                        if message_id is not None:
                            update['_ack'] = {'chat_id': chat_id, 'message_id': message_id}
                    continue
                # Not coalesced (command, receipt, contact): keep this person's
                # earlier fragments ahead of it so order is preserved.
                for pending in turns.flush_key(update):
                    bot.process(pending, pending.get('_ack'), client=client)
                    flush_outbox(db, client)
                chat_id = bot.acknowledgement_chat(update)
                acknowledgement = None
                if chat_id is not None:
                    message_id = send_acknowledgement(client, chat_id)
                    if message_id is not None:
                        acknowledgement = {'chat_id': chat_id, 'message_id': message_id}
                bot.process(update, acknowledgement, client=client)
                flush_outbox(db, client)
        except APIError as exc:
            LOG.warning('API unavailable: %s', exc)
            time.sleep(max(5, exc.retry_after))
        except Exception as exc:
            # No update is acknowledged on unexpected failures; replay cannot double-charge.
            LOG.error('Processing failed (%s); update will be retried', type(exc).__name__)
            time.sleep(5)


def build_parser():
    """Build the argument parser used by every command-line entry point.

    SPEC-039: the workbook flags (``--reconcile``, ``--import-payments``,
    ``--workbook-overrides``) are gone with the importer they served, so no
    option is command-specific any more.
    """
    parser = argparse.ArgumentParser(description='Bale football-session manager')
    parser.add_argument('--database', type=Path, help='SQLite path (overrides DATABASE_PATH)')
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument('--init', action='store_true', help='Initialize database, no credentials needed')
    commands.add_argument('--import-players', type=Path, metavar='CSV',
                          help='Import opening balances from a CSV (name,opening_balance,note)')
    commands.add_argument('--import-sessions', type=Path, metavar='ROSTER_CSV',
                          help='SPEC-022: create/repair the real sessions and apply the roster')
    commands.add_argument('--migrate-guests', type=Path, metavar='LINKS_CSV',
                          help='SPEC-024: move explicit guests onto their host account')
    commands.add_argument('--backup', type=Path, metavar='DESTINATION', help='Create consistent SQLite backup')
    commands.add_argument('--discover-ids', action='store_true', help='Show IDs from recent messages; only BALE_BOT_TOKEN needed')
    commands.add_argument('--logs', type=int, nargs='?', const=30, metavar='N', help='Show last N agent trace logs from database')
    # SPEC-022 modifiers: they describe the roster, not the database.
    parser.add_argument('--dry-run', action='store_true',
                        help='SPEC-022: report what would change and roll it back')
    parser.add_argument('--alias', action='append', default=[], metavar='ROSTER=STORED',
                        help='SPEC-022: a roster name that belongs to an existing player')
    parser.add_argument('--allow-new', action='append', default=[], metavar='NAME',
                        help='SPEC-022: create this name even though it resembles another player')
    parser.add_argument('--session-fee', type=int, default=150000, metavar='TOMAN',
                        help='SPEC-022: fee per person (default 150000)')
    parser.add_argument('--venue-cost', type=int, default=VENUE_COST, metavar='TOMAN',
                        help='SPEC-022: hall cost per session (default 2500000)')
    parser.add_argument('--hour', default=SESSION_HOUR, metavar='HH:MM',
                        help='SPEC-022: session start time in Tehran (default 16:30)')
    return parser


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = build_parser()
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
                elif args.import_sessions:
                    plan = read_plan(args.import_sessions, args.hour)
                    report = apply_sessions(db, plan, aliases=aliases(args.alias),
                                            allow_new=args.allow_new, dry_run=args.dry_run,
                                            fee=args.session_fee, venue_cost=args.venue_cost)
                    print(format_sessions(report))
                elif args.migrate_guests:
                    report = migrate_guests(db, read_guest_links(args.migrate_guests),
                                            dry_run=args.dry_run)
                    print(format_guest_migration(report))
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