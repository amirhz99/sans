import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from app import backfill
from app.backfill import (apply_sessions, marked_sessions, migrate_guests, paid_total,
                          read_guest_links, read_plan)
from app.db import Database
from app.main import build_parser
from app.service import Service, UserError
from tests.helpers import write_xlsx

# The ten Fridays the operator confirmed for 1405 (SPEC-022).
TEN = ['۸ خرداد', '۱۵ خرداد', '۲۲ خرداد', '۲۹ خرداد',
       '۵ تیر', '۱۲ تیر', '۱۹ تیر', '۲۶ تیر', '۲ مرداد', '۱۶ مرداد']
HEADER = ['نام', 'جلسات آمده', 'جلسات پرداخت شده', 'مجموع  بدهکاری']


class BackfillCase(unittest.TestCase):
    """Shared temporary database, roster writer and session builder."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite3'
        self.db = Database(self.path)
        self.service = Service(self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def file(self, name, contents):
        path = Path(self.temp.name) / name
        path.write_text(contents, encoding='utf-8')
        return path

    def roster(self, marks, name='roster.csv', labels=None):
        """``marks`` maps a name to ``{label: guests}``; a missing label is absent."""
        labels = labels or TEN
        lines = [','.join(['نام'] + labels)]
        for person, cells in marks.items():
            values = ['' if cells.get(label) is None else str(cells[label]) for label in labels]
            lines.append(','.join([person] + values))
        return self.file(name, '\n'.join(lines) + '\n')

    def player(self, name):
        """Create a player and commit it, so a failed import cannot take it away."""
        with self.db.conn:
            return self.service.ensure_player(name)

    def session(self, title, when_text, players=None, finalized=True, session_at=None):
        """Create one stored session with attendance and charges for ``players``.

        ``players`` is a list of ``(name, guests)``; each person is created when
        needed, so a test can predict the ids it asserts on.
        """
        with self.db.conn:
            row = self.service.save_session_flexible(title=title, when_text=when_text)
            if session_at:
                self.db.execute('UPDATE sessions SET session_at=? WHERE id=?', (session_at, row['id']))
            for name, count in players or []:
                player = self.service.ensure_player(name)
                # SPEC-024: the roster path always means «the player came».
                self.db.execute('INSERT INTO attendance(session_id,player_id,guests,own) '
                                'VALUES (?,?,?,1)',
                                (row['id'], player['id'], count))
                self.db.execute(
                    'INSERT INTO ledger(player_id,session_id,amount,note,source) VALUES (?,?,?,?,?)',
                    (player['id'], row['id'], (1 + count) * 150000,
                     f"{title}؛ خود فرد + {count} مهمان. ",
                     f"session:{row['id']}:player:{player['id']}"))
            if finalized:
                self.db.execute('UPDATE sessions SET finalized=1 WHERE id=?', (row['id'],))
        return self.db.one('SELECT * FROM sessions WHERE id=?', (row['id'],))

    def counts(self):
        """A cheap fingerprint of everything the importer could change."""
        return (
            self.db.one('SELECT COUNT(*) n FROM players')['n'],
            self.db.one('SELECT COUNT(*) n FROM sessions')['n'],
            self.db.one('SELECT COUNT(*) n FROM attendance')['n'],
            self.db.one('SELECT COUNT(*) n FROM ledger')['n'],
            self.db.one('SELECT COALESCE(SUM(amount),0) n FROM ledger')['n'],
        )


class PlanTests(BackfillCase):
    def test_wide_and_long_rosters_agree(self):
        labels = ['۸ خرداد', '۱۵ خرداد', '۲۲ خرداد']
        wide = self.roster({'علی': {'۸ خرداد': 0, '۱۵ خرداد': 1, '۲۲ خرداد': 'x'}}, labels=labels)
        long = self.file('long.csv', 'date,name,guests\n'
                                     '۸ خرداد,علی,0\n۱۵ خرداد,علی,1\n۲۲ خرداد,علی,حاضر\n')
        first, second = read_plan(wide), read_plan(long)
        self.assertEqual([item['title'] for item in first['sessions']],
                         [item['title'] for item in second['sessions']])
        self.assertEqual(first['sessions'][0]['session_at'], '2026-05-29T16:30+03:30')
        self.assertEqual(first['entries'], second['entries'])
        self.assertEqual([item['guests'] for item in first['entries']], [0, 1, 0])

    def test_a_non_friday_date_is_rejected(self):
        path = self.roster({'علی': {'۹ خرداد': 0}}, labels=['۹ خرداد'])
        with self.assertRaises(UserError) as caught:
            read_plan(path)
        self.assertIn('نه جمعه', str(caught.exception))
        self.assertFalse(self.db.all('SELECT * FROM sessions'))

    def test_a_duplicate_date_and_a_bad_guest_count_are_rejected(self):
        path = self.file('plan.csv', 'نام,۸ خرداد,۸ خرداد\nعلی,0,0\n')
        with self.assertRaises(UserError) as caught:
            read_plan(path)
        self.assertIn('دو بار', str(caught.exception))
        bad = self.roster({'علی': {'۸ خرداد': 'شاید'}})
        with self.assertRaises(UserError) as caught:
            read_plan(bad)
        self.assertIn('شاید', str(caught.exception))

    def test_an_empty_plan_is_still_a_plan(self):
        path = self.file('empty.csv', ','.join(['نام'] + TEN) + '\n')
        plan = read_plan(path)
        self.assertEqual(len(plan['sessions']), 10)
        self.assertEqual(plan['entries'], [])


class SessionImportTests(BackfillCase):
    def test_creates_the_ten_fridays_with_their_own_cost_and_fee(self):
        path = self.roster({'علی': {label: 0 for label in TEN}})
        report = apply_sessions(self.db, read_plan(path))
        self.assertEqual((report['created'], report['repaired']), (10, 0))
        rows = self.db.all('SELECT * FROM sessions ORDER BY id')
        self.assertEqual([row['title'] for row in rows], ['سانس ' + label for label in TEN])
        self.assertTrue(all(row['session_at'].endswith('16:30+03:30') for row in rows))
        self.assertTrue(all(row['venue_cost'] == 2500000 for row in rows))
        self.assertTrue(all(row['person_fee'] == 150000 for row in rows))
        self.assertTrue(all(row['finalized'] == 1 for row in rows))
        self.assertIn('۸ خرداد ۱۴۰۵', [row['when_text'] for row in rows])
        self.assertEqual(self.service.balance(self.player('علی')['id']), 1500000)
        # SPEC-022: the import marks its sessions so reconcile counts the same set.
        self.assertEqual(len(marked_sessions(self.db)), 10)

    def test_repairs_a_session_stored_one_day_off(self):
        original = self.session('سانس ۹ خرداد', '۹ خرداد', players=[('علی', 0)], finalized=True)
        path = self.roster({'علی': {'۸ خرداد': 0}})
        report = apply_sessions(self.db, read_plan(path))
        self.assertEqual((report['created'], report['repaired']), (9, 1))
        row = self.db.one('SELECT * FROM sessions WHERE id=?', (original['id'],))
        self.assertEqual(row['title'], 'سانس ۸ خرداد')
        self.assertEqual(row['when_text'], '۸ خرداد ۱۴۰۵')
        self.assertEqual(row['session_at'], '2026-05-29T16:30+03:30')
        self.assertEqual(row['venue_cost'], 2500000)
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM sessions')['n'], 10)
        charge = self.db.one('SELECT * FROM ledger WHERE source=?',
                             (f"session:{original['id']}:player:1",))
        self.assertEqual(charge['amount'], 150000)
        # The note follows the corrected title; the money does not move.
        self.assertEqual(charge['note'], 'سانس ۸ خرداد؛ خود فرد + 0 مهمان. ')
        self.assertEqual(report['notes'], 1)

    def test_existing_attendance_is_kept_and_only_the_gap_is_added(self):
        original = self.session('سانس ۸ خرداد', '۸ خرداد ۱۴۰۵', players=[('علی', 0)], finalized=True)
        path = self.roster({'علی': {'۸ خرداد': 0}, 'رضا': {'۸ خرداد': 2},
                            'سارا': {'۸ خرداد': 0}})
        before = self.db.one('SELECT COUNT(*) n FROM ledger')['n']
        report = apply_sessions(self.db, read_plan(path))
        self.assertEqual((report['added'], report['removed'], report['posted']), (2, 0, 2))
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM ledger')['n'], before + 2)
        kept = self.db.one('SELECT * FROM ledger WHERE source=?',
                           (f"session:{original['id']}:player:1",))
        self.assertEqual(kept['amount'], 150000)
        self.assertEqual(self.service.balance(2), 450000)

    def test_a_changed_guest_count_posts_one_adjustment(self):
        original = self.session('سانس ۸ خرداد', '۸ خرداد ۱۴۰۵', players=[('علی', 0)], finalized=True)
        path = self.roster({'علی': {'۸ خرداد': 2}})
        report = apply_sessions(self.db, read_plan(path))
        attendance = self.db.one('SELECT * FROM attendance WHERE session_id=? AND player_id=?',
                                 (original['id'], 1))
        self.assertEqual(attendance['guests'], 2)
        self.assertEqual((report['added'], report['updated'], report['posted']), (0, 1, 1))
        rows = self.db.all('SELECT * FROM ledger WHERE player_id=1 ORDER BY id')
        self.assertEqual([row['amount'] for row in rows], [150000, 300000])
        self.assertEqual(rows[1]['source'], f"session:{original['id']}:player:1:adjust")
        self.assertEqual(self.service.balance(1), 450000)

    def test_removing_a_player_records_a_reversal(self):
        self.session('سانس ۸ خرداد', '۸ خرداد ۱۴۰۵', players=[('علی', 1), ('رضا', 0)], finalized=True)
        path = self.roster({'رضا': {'۸ خرداد': 0}})
        report = apply_sessions(self.db, read_plan(path))
        self.assertEqual((report['removed'], report['posted']), (1, 1))
        self.assertFalse(self.db.all('SELECT * FROM attendance WHERE player_id=?', (1,)))
        rows = self.db.all('SELECT * FROM ledger WHERE player_id=1 ORDER BY id')
        self.assertEqual([row['amount'] for row in rows], [300000, -300000])
        self.assertEqual(self.service.balance(1), 0)
        self.assertEqual(self.service.balance(2), 150000)
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM ledger')['n'], 3)

    def test_import_is_idempotent(self):
        path = self.roster({'علی': {'۸ خرداد': 0, '۱۵ خرداد': 1}})
        apply_sessions(self.db, read_plan(path))
        fingerprint = self.counts()
        second = apply_sessions(self.db, read_plan(path))
        self.assertEqual((second['posted'], second['added'], second['updated']), (0, 0, 0))
        self.assertEqual(self.counts(), fingerprint)

    def test_dry_run_leaves_the_database_unchanged(self):
        self.session('سانس ۹ خرداد', '۹ خرداد', players=[('علی', 0)], finalized=True)
        path = self.roster({'علی': {'۸ خرداد': 0, '۱۵ خرداد': 0}})
        before = self.counts()
        titles = [row['title'] for row in self.db.all('SELECT * FROM sessions')]
        report = apply_sessions(self.db, read_plan(path), dry_run=True)
        self.assertTrue(report['dry_run'])
        self.assertEqual((report['created'], report['repaired']), (9, 1))
        self.assertEqual(self.counts(), before)
        self.assertEqual(titles, [row['title'] for row in self.db.all('SELECT * FROM sessions')])
class NameTests(BackfillCase):
    def test_an_ambiguous_name_fails_closed(self):
        self.player('علی رضایی')
        path = self.roster({'علی': {'۸ خرداد': 0}})
        with self.assertRaises(UserError) as caught:
            apply_sessions(self.db, read_plan(path))
        self.assertIn('علی رضایی', str(caught.exception))
        self.assertIn('--alias', str(caught.exception))
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM players')['n'], 1)
        self.assertFalse(self.db.all('SELECT * FROM sessions'))

    def test_allow_new_accepts_a_similar_name_as_a_new_player(self):
        original = self.player('علی رضایی')
        path = self.roster({'علی': {'۸ خرداد': 0}})
        report = apply_sessions(self.db, read_plan(path), allow_new=['علی'])
        self.assertEqual([item['name'] for item in report['players_created']], ['علی'])
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM players')['n'], 2)
        self.assertEqual(self.service.player(original['id'])['name'], 'علی رضایی')

    def test_an_alias_renames_the_stored_name_and_keeps_history(self):
        original = self.player('رسولی')
        with self.db.conn:
            self.db.execute('INSERT INTO ledger(player_id,amount,note,source) VALUES (?,?,?,?)',
                            (original['id'], -150000, 'پرداخت قبلی', 'proposal:old'))
        path = self.roster({'عباس رسولی': {'۸ خرداد': 0}})
        report = apply_sessions(self.db, read_plan(path), aliases={'عباس رسولی': 'رسولی'})
        self.assertEqual(report['renamed'][0]['new'], 'عباس رسولی')
        player = self.db.one('SELECT * FROM players WHERE id=?', (original['id'],))
        self.assertEqual(player['name'], 'عباس رسولی')
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM players')['n'], 1)
        self.assertEqual(self.service.balance(original['id']), -150000 + 150000)
        again = apply_sessions(self.db, read_plan(path), aliases={'عباس رسولی': 'رسولی'})
        self.assertEqual(again['renamed'], [])
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM players')['n'], 1)


class CliTests(BackfillCase):
    """SPEC-039: the workbook flags are gone, the roster flags are not."""

    def test_the_workbook_flags_are_gone(self):
        parser = build_parser()
        for flag in ('--reconcile', '--import-payments', '--workbook-overrides'):
            with self.assertRaises(SystemExit) as caught, \
                    patch('sys.stderr', new_callable=StringIO):
                parser.parse_args([flag, 'account.xlsx'])
            self.assertEqual(caught.exception.code, 2, flag)

    def test_the_roster_and_guest_flags_survive(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args(['--import-sessions', 'roster.csv']).import_sessions,
                         Path('roster.csv'))
        self.assertEqual(parser.parse_args(['--migrate-guests', 'links.csv']).migrate_guests,
                         Path('links.csv'))
        self.assertEqual(parser.parse_args(['--import-players', 'p.csv']).import_players,
                         Path('p.csv'))
        self.assertTrue(parser.parse_args(['--init']).init)
        # The roster modifiers still travel with the roster command.
        parsed = parser.parse_args(['--import-sessions', 'roster.csv', '--dry-run',
                                    '--venue-cost', '3000000', '--alias', 'a=b'])
        self.assertTrue(parsed.dry_run)
        self.assertEqual(parsed.venue_cost, 3000000)
        self.assertEqual(parsed.alias, ['a=b'])

    def test_backfill_module_has_no_workbook_symbols(self):
        for name in ('xlsx_rows', 'read_workbook', 'workbook_amount', 'read_overrides',
                     'apply_payments', 'reconcile', 'format_reconcile', 'format_payments'):
            self.assertFalse(hasattr(backfill, name), name)
        # The reusable half is still exported.
        for name in ('read_plan', 'apply_sessions', 'format_sessions',
                     'read_guest_links', 'migrate_guests', 'format_guest_migration'):
            self.assertTrue(hasattr(backfill, name), name)
        # SPEC-039: the hand-rolled XLSX reader took these with it.
        source = Path(backfill.__file__).read_text(encoding='utf-8')
        self.assertNotIn('zipfile', source)
        self.assertNotIn('xml.etree', source)

    def test_financial_surface_is_untouched(self):
        """A dry-run roster import still reports the same thing and writes nothing."""
        self.session('سانس ۸ خرداد', '۸ خرداد ۱۴۰۵', players=[('علی رضایی', 0)])
        before = self.counts()
        plan = read_plan(self.roster({'علی رضایی': {'۸ خرداد': 0}}), '16:30')
        report = apply_sessions(self.db, plan, dry_run=True)
        self.assertTrue(report['dry_run'])
        self.assertEqual(self.counts(), before)

class GuestLinkTests(BackfillCase):
    def test_links_are_explicit_and_mostly_natural(self):
        path = self.file('links.csv', 'guest,host,rule\nایمان,محمد,nearest\n')
        self.assertEqual(read_guest_links(path)[0]['host'], 'محمد')
        bad = self.file('bad-links.csv', 'guest,host\nمهمان نواز,نواز\n')
        with self.assertRaises(UserError):
            read_guest_links(bad)


class GuestMigrationTests(BackfillCase):
    """SPEC-024: a guest lives on the host's account, never on its own."""

    def dates(self, **titles):
        with self.db.conn:
            for title, stamp in titles.items():
                self.db.execute('UPDATE sessions SET session_at=? WHERE title=?', (stamp, title))

    def test_guest_lands_on_host_and_player_row_disappears(self):
        session = self.session('سانس مشترک', 'مشترک', players=[('میزبان', 0), ('مهمان', 0)])
        guest_id = self.service.player('مهمان')['id']
        before = self.counts()
        report = migrate_guests(self.db, [{'guest': 'مهمان', 'host': 'میزبان'}])
        host = self.service.player('میزبان')
        self.assertEqual([(item['guest'], item['to']) for item in report['moved']],
                         [('مهمان', session['id'])])
        self.assertEqual(report['removed_players'], 1)
        # The guest has no account, no attendance and no ledger row of its own.
        self.assertIsNone(self.db.one('SELECT 1 FROM players WHERE id=?', (guest_id,)))
        self.assertIsNone(self.db.one('SELECT 1 FROM ledger WHERE player_id=?', (guest_id,)))
        row = self.db.one('SELECT * FROM attendance WHERE session_id=? AND player_id=?',
                          (session['id'], host['id']))
        self.assertEqual((row['own'], row['guests']), (1, 1))
        self.assertEqual(self.service.guest_names(session['id'], host['id']), ['مهمان'])
        self.assertEqual(self.service.balance(host['id']), 300000)
        self.assertEqual(self.counts()[3], before[3])
        self.assertEqual(self.counts()[4], before[4])

    def test_moves_to_nearest_host_and_earlier_tie_preserving_ledger(self):
        self.session('سانس اول', 'اول', players=[('میزبان', 0)])
        self.session('سانس مهمان', 'مهمان', players=[('مهمان', 0)])
        self.session('سانس سوم', 'سوم', players=[('میزبان', 0)])
        self.dates(**{'سانس اول': '2026-01-02T16:30+03:30',
                      'سانس مهمان': '2026-01-03T16:30+03:30',
                      'سانس سوم': '2026-01-04T16:30+03:30'})
        before = self.counts()
        report = migrate_guests(self.db, [{'guest': 'مهمان', 'host': 'میزبان'}])
        first = self.db.one("SELECT id FROM sessions WHERE title='سانس اول'")['id']
        third = self.db.one("SELECT id FROM sessions WHERE title='سانس سوم'")['id']
        host = self.service.player('میزبان')
        # Both neighbours are one day away, so the earlier session wins.
        self.assertEqual(report['moved'][0]['to'], first)
        self.assertEqual(self.db.one('SELECT guests FROM attendance WHERE session_id=? AND player_id=?',
                                     (first, host['id']))['guests'], 1)
        self.assertEqual(self.db.one('SELECT guests FROM attendance WHERE session_id=? AND player_id=?',
                                     (third, host['id']))['guests'], 0)
        # Money followed the person instead of a new charge being posted: not a
        # single ledger row was added or removed.
        self.assertEqual(self.counts()[4], before[4])
        self.assertEqual(self.counts()[3], before[3])

    def test_missing_host_is_created_in_guest_session(self):
        session = self.session('سانس مهمان', 'مهمان', players=[('مهمان', 0)])
        before = self.counts()
        report = migrate_guests(self.db, [{'guest': 'مهمان', 'host': 'خلیل'}])
        host = self.service.player('خلیل')
        self.assertIn('خلیل', report['created_hosts'])
        row = self.db.one('SELECT * FROM attendance WHERE session_id=? AND player_id=?',
                          (session['id'], host['id']))
        # The host never played, so only the guest share is charged.
        self.assertEqual((row['own'], row['guests']), (0, 1))
        self.assertEqual(self.service.balance(host['id']), 150000)
        self.assertEqual(self.counts()[3:5], before[3:5])

    def test_two_guests_of_an_absent_host_share_one_guest_only_row(self):
        self.session('سانس مهمان‌ها', 'مهمان‌ها', players=[('مهمان یک', 0), ('مهمان دو', 0)])
        report = migrate_guests(self.db, [{'guest': 'مهمان یک', 'host': 'خلیل'},
                                          {'guest': 'مهمان دو', 'host': 'خلیل'}])
        host = self.service.player('خلیل')
        row = self.db.one('SELECT * FROM attendance WHERE player_id=?', (host['id'],))
        self.assertEqual((row['own'], row['guests']), (0, 2))
        self.assertEqual(self.service.balance(host['id']), 300000)
        self.assertEqual(len(report['guest_only_rows']), 2)
        self.assertEqual(len(self.service.guest_names(row['session_id'], host['id'])), 2)

    def test_repeating_the_migration_changes_nothing(self):
        self.session('سانس مشترک', 'مشترک', players=[('میزبان', 0), ('مهمان', 0)])
        migrate_guests(self.db, [{'guest': 'مهمان', 'host': 'میزبان'}])
        after = self.counts()
        again = migrate_guests(self.db, [{'guest': 'مهمان', 'host': 'میزبان'}])
        self.assertEqual((again['moved'], again['posted'], again['removed_players']), ([], 0, 0))
        self.assertEqual(again['already_done'], 1)
        self.assertEqual(self.counts(), after)

    def test_a_dry_run_writes_nothing(self):
        self.session('سانس مشترک', 'مشترک', players=[('میزبان', 0), ('مهمان', 0)])
        before = self.counts()
        report = migrate_guests(self.db, [{'guest': 'مهمان', 'host': 'میزبان'}], dry_run=True)
        self.assertTrue(report['dry_run'])
        self.assertEqual(self.counts(), before)
        self.assertFalse(self.db.all('SELECT * FROM guest_migrations'))
        self.assertFalse(self.db.all('SELECT * FROM attendance_guests'))

    def test_an_unknown_guest_fails_closed(self):
        self.session('سانس مشترک', 'مشترک', players=[('میزبان', 0)])
        before = self.counts()
        with self.assertRaises(UserError):
            migrate_guests(self.db, [{'guest': 'ناشناس', 'host': 'میزبان'}])
        self.assertEqual(self.counts(), before)
