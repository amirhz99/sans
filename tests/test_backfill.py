import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from app.backfill import (apply_payments, apply_sessions, format_reconcile, marked_sessions,
                          paid_total, read_plan, read_workbook, reconcile)
from app.db import Database
from app.main import build_parser, check_modifiers
from app.service import Service, UserError

# The ten Fridays the operator confirmed for 1405 (SPEC-022).
TEN = ['۸ خرداد', '۱۵ خرداد', '۲۲ خرداد', '۲۹ خرداد',
       '۵ تیر', '۱۲ تیر', '۱۹ تیر', '۲۶ تیر', '۲ مرداد', '۱۶ مرداد']
HEADER = ['نام', 'جلسات آمده', 'جلسات پرداخت شده', 'مجموع  بدهکاری']
SHEET_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def write_xlsx(path, rows):
    """Write a minimal XLSX: text cells are shared strings, numbers are values."""
    shared, body = [], []
    for index, row in enumerate(rows, 1):
        cells = []
        for position, value in enumerate(row):
            if value is None or value == '':
                continue
            column = chr(ord('A') + position)
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{column}{index}"><v>{value}</v></c>')
            else:
                if value not in shared:
                    shared.append(value)
                cells.append(f'<c r="{column}{index}" t="s"><v>{shared.index(value)}</v></c>')
        body.append(f'<row r="{index}">' + ''.join(cells) + '</row>')
    with ZipFile(path, 'w') as archive:
        archive.writestr('xl/sharedStrings.xml',
                         f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{SHEET_NS}">'
                         + ''.join(f'<si><t>{value}</t></si>' for value in shared) + '</sst>')
        archive.writestr('xl/worksheets/sheet1.xml',
                         f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{SHEET_NS}">'
                         '<sheetData>' + ''.join(body) + '</sheetData></worksheet>')
    return path


class BackfillCase(unittest.TestCase):
    """Shared temporary database, workbook writer and roster writer."""

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

    def workbook(self, rows, name='account.xlsx'):
        return write_xlsx(Path(self.temp.name) / name, rows)

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
                self.db.execute('INSERT INTO attendance VALUES (?,?,?)',
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
class WorkbookTests(BackfillCase):
    def test_explicit_overrides_replace_rows_and_can_add_approved_player(self):
        workbook = self.workbook([HEADER, ['علی', 3, 1, 300], ['رضا', 2, 0, 300]])
        overrides = self.file('overrides.csv',
                             'name,attended,paid,debt\nعلی,4,1,450\nسارا,7,2,750\n')
        rows = read_workbook(workbook, 150000, overrides)
        self.assertEqual([(row['name'], row['attended'], row['paid'], row['debt']) for row in rows],
                         [('علی', 4, 1, 450000), ('رضا', 2, 0, 300000),
                          ('سارا', 7, 2, 750000)])

    def test_an_override_with_inconsistent_debt_fails_closed(self):
        workbook = self.workbook([HEADER, ['علی', 3, 1, 300]])
        overrides = self.file('bad-overrides.csv', 'name,attended,paid,debt\nعلی,4,1,300\n')
        with self.assertRaises(UserError):
            read_workbook(workbook, 150000, overrides)

    def test_debt_is_the_formula_and_a_mismatch_names_the_row(self):
        path = self.workbook([HEADER, ['مهیار', 10, 2, 1200], ['ایلانلو', 1, 1, ''],
                              ['قامتی', 1, 0, '']])
        rows = read_workbook(path, 150000)
        self.assertEqual([row['debt'] for row in rows], [1200000, 0, 150000])
        self.assertEqual(rows[1]['paid'], 1)
        bad = self.workbook([HEADER, ['مهیار', 10, 2, 1300]], 'bad.xlsx')
        with self.assertRaises(UserError) as caught:
            read_workbook(bad, 150000)
        self.assertIn('مهیار', str(caught.exception))
        self.assertIn('1,200,000', str(caught.exception))


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
        self.assertIn('جمعه نیست', str(caught.exception))
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


class PaymentTests(BackfillCase):
    def test_payments_follow_the_workbook_and_are_idempotent(self):
        ali = self.player('علی')
        reza = self.player('رضا')
        with self.db.conn:
            # The database recorded more payment than the sheet knows about.
            self.db.execute('INSERT INTO ledger(player_id,amount,note,source) VALUES (?,?,?,?)',
                            (ali['id'], -450000, 'پرداخت قبلی', 'proposal:old'))
        path = self.workbook([HEADER, ['علی', 3, 1, 300], ['رضا', 1, 0, 150]])
        report = apply_payments(self.db, read_workbook(path, 150000), 150000)
        self.assertEqual(report['adjusted'], [{'name': 'علی', 'amount': 300000}])
        self.assertEqual(report['unchanged'], 1)
        row = self.db.one('SELECT * FROM ledger WHERE source=?', (f'backfill:paid:{ali["id"]}',))
        self.assertEqual(row['amount'], 300000)
        self.assertIn('اصلاح پرداخت', row['note'])
        self.assertEqual(self.service.balance(ali['id']), -150000)
        self.assertEqual(self.service.balance(reza['id']), 0)
        fingerprint = self.counts()
        second = apply_payments(self.db, read_workbook(path, 150000), 150000)
        self.assertEqual((second['adjusted'], second['unchanged']), ([], 2))
        self.assertEqual(self.counts(), fingerprint)

    def test_an_opening_snapshot_row_is_not_read_as_a_payment(self):
        ali = self.player('علی')
        with self.db.conn:
            # A legacy ``--import-players`` snapshot: a debt, never a payment.
            self.db.execute('INSERT INTO ledger(player_id,amount,note,source) VALUES (?,?,?,?)',
                            (ali['id'], 1200000, 'بدهی تا ۱۶ مرداد ۱۴۰۵', f'opening:{ali["id"]}'))
        path = self.workbook([HEADER, ['علی', 2, 1, 150]])
        report = apply_payments(self.db, read_workbook(path, 150000), 150000)
        self.assertEqual([item['amount'] for item in report['adjusted']], [-150000])
        self.assertEqual(self.service.balance(ali['id']), 1200000 - 150000)
        self.assertEqual(paid_total(self.db, ali['id']), 150000)

    def test_a_missing_player_is_created_from_the_sheet(self):
        path = self.workbook([HEADER, ['ناشناس', 1, 0, '']])
        report = apply_payments(self.db, read_workbook(path, 150000), 150000)
        self.assertEqual([item['name'] for item in report['created']], ['ناشناس'])
        player = self.service.player('ناشناس')
        self.assertEqual(self.service.balance(player['id']), 0)

    def test_an_ambiguous_name_stops_the_payment_import(self):
        self.player('علی رضایی')
        path = self.workbook([HEADER, ['علی', 1, 0, '']])
        with self.assertRaises(UserError) as caught:
            apply_payments(self.db, read_workbook(path, 150000), 150000)
        self.assertIn('علی رضایی', str(caught.exception))
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM players')['n'], 1)
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM ledger')['n'], 0)


class ReconcileTests(BackfillCase):
    def seeded(self, marks):
        """One finalized session with charges; returns its id."""
        return self.session('سانس ۸ خرداد', '۸ خرداد ۱۴۰۵', players=marks)['id']

    def pay(self, player_id, amount, source):
        with self.db.conn:
            self.db.execute('INSERT INTO ledger(player_id,amount,note,source) VALUES (?,?,?,?)',
                            (player_id, amount, 'پرداخت', source))

    def test_is_green_when_every_row_matches(self):
        session_id = self.seeded([('علی رضایی', 1), ('رضا محمدی', 0)])
        ali, reza = self.player('علی رضایی')['id'], self.player('رضا محمدی')['id']
        self.pay(ali, -150000, f'test:pay:{ali}')
        self.pay(reza, -150000, f'test:pay:{reza}')
        path = self.workbook([HEADER, ['علی رضایی', 2, 1, 150], ['رضا محمدی', 1, 1, 0]])
        result = reconcile(self.db, read_workbook(path, 150000), 150000, session_ids=[session_id])
        self.assertEqual([row['attended'] for row in result['rows']], [2, 1])
        self.assertEqual([row['diff'] for row in result['rows']], [0, 0])
        self.assertEqual(result['sheet_total'], 150000)
        self.assertEqual(result['ledger_total'], 150000)
        self.assertTrue(result['ok'])
        self.assertIn('می‌خوانند', format_reconcile(result))

    def test_reports_differences_without_writing(self):
        session_id = self.seeded([('علی رضایی', 0)])
        before = self.counts()
        path = self.workbook([HEADER, ['علی رضایی', 3, 2, 150], ['رضا محمدی', 1, 0, '']])
        result = reconcile(self.db, read_workbook(path, 150000), 150000, session_ids=[session_id])
        self.assertFalse(result['ok'])
        self.assertEqual(result['missing'], ['رضا محمدی'])
        self.assertEqual(result['rows'][0]['attended'], 1)
        self.assertEqual(result['rows'][0]['sheet_attended'], 3)
        self.assertEqual(result['rows'][0]['paid'], 0)
        self.assertEqual(result['rows'][0]['sheet_paid'], 300000)
        text = format_reconcile(result)
        self.assertIn('علی رضایی', text)
        self.assertIn('رضا محمدی', text)
        self.assertIn('اختلاف هست', text)
        self.assertEqual(self.counts(), before)

    def test_a_player_outside_the_sheet_keeps_the_gate_red(self):
        session_id = self.seeded([('علی رضایی', 0), ('سارا کریمی', 0)])
        path = self.workbook([HEADER, ['علی رضایی', 1, 0, 150]])
        result = reconcile(self.db, read_workbook(path, 150000), 150000, session_ids=[session_id])
        self.assertFalse(result['ok'])
        self.assertEqual(result['extra'], [{'name': 'سارا کریمی', 'balance': 150000}])
        self.assertIn('خارج از اکسل', format_reconcile(result))
        self.assertEqual(result['ledger_total'], 300000)

    def test_marked_sessions_narrow_the_count(self):
        first = self.seeded([('علی رضایی', 0)])
        self.session('سانس ۱۵ خرداد', '۱۵ خرداد ۱۴۰۵', players=[('علی رضایی', 0)])
        rows = read_workbook(self.workbook([HEADER, ['علی رضایی', 1, 0, 150]]), 150000)
        marked = reconcile(self.db, rows, 150000, session_ids=[first])
        self.assertEqual(marked['rows'][0]['attended'], 1)
        self.assertEqual(marked['ledger_total'], 300000)
        unmarked = reconcile(self.db, rows, 150000)
        self.assertEqual(unmarked['rows'][0]['attended'], 2)
        self.assertFalse(unmarked['ok'])
        # No import has run in this database, so nothing is marked yet.
        self.assertEqual(marked_sessions(self.db), [])


class CliTests(unittest.TestCase):
    def test_the_override_file_is_a_modifier_not_a_command(self):
        """``--workbook-overrides`` must travel with the command it corrects."""
        parser = build_parser()
        for command in ('--reconcile', '--import-payments'):
            parsed = parser.parse_args([command, 'account.xlsx',
                                        '--workbook-overrides', 'overrides.csv'])
            self.assertEqual(parsed.workbook_overrides, Path('overrides.csv'))
            check_modifiers(parsed)
        parsed = parser.parse_args(['--import-sessions', 'roster.csv', '--dry-run'])
        self.assertIsNone(parsed.workbook_overrides)
        self.assertTrue(parsed.dry_run)
        with self.assertRaises(UserError):
            check_modifiers(parser.parse_args(['--workbook-overrides', 'overrides.csv']))