# SPEC-022: Real sessions instead of a snapshot, reconciled against the workbook

Status: verified
Owner: agent
Created: 2026-10-24

## Problem

The football account for the period ending 16 Mordad 1405 lives in two places
that disagree:

- **The workbook** (`football-account.xlsx`) holds 51 rows and, per row, the
  number of sessions attended, the number of sessions paid, the total debt, and
  (sometimes) the Bale id. Every row satisfies
  `debt = (attended − paid) × 150,000`: 149 attended slots, 31 paid slots,
  17,700,000 T outstanding.
- **The database** holds 3 sessions (8, 15 and 22 Khordad), 26 players, 51
  attendance rows and 3,600,000 T of payments.

The only importer that reads the workbook, `import_players`, writes one
`opening:` ledger row per player and calls it a day. That has three defects:

1. `_workbook_amount` ignores the «جلسات پرداخت شده» column, so a fully paid row
   («ایلانلو», 1 attended and 1 paid) is recorded as 150,000 T of debt and the
   imported total becomes 17,850,000 instead of 17,700,000.
2. The header string and the note text («بدهی تا آخر شهریور») are hard-coded, so
   the sheet's own period is not recorded.
3. A snapshot cannot answer «which session is this debt for?», so `/debt` cannot
   show the outstanding sessions and `/report` cannot show a per-session total.

The three existing sessions are also incomplete: `session_at` is `NULL` for all
of them, `venue_cost` is `NULL` for the first, and the first is titled
«سانس ۹ خرداد» although 9 Khordad 1405 is a Saturday and every real session was
a Friday at 16:30 in the Teymouri Tarasht hall.

## Goal

Ten real Friday sessions carry the whole period. Attendance, guest counts and
payments are imported from the operator's roster, the workbook's debt column is
the acceptance gate, and after the last session every one of the 51 rows
reconciles to the sheet exactly — with no financial history deleted and every
correction visible as its own ledger row.

## Scope

- `app/backfill.py`:
  - `xlsx_rows`/`read_workbook`: read the sheet, derive
    `debt = (attended − paid) × fee`, and reject a row whose non-empty debt cell
    disagrees with that formula.
  - `read_plan`: read the attendance roster in a wide form (one row per person,
    one column per date, cell = number of guests) or a long form
    (`date,name,guests`).
  - `planned_sessions`: resolve each date, require a Friday, and produce the
    canonical title («سانس ۸ خرداد»), `when_text`, `session_at` and
    `person_fee`/`venue_cost`.
  - `resolve_players`: exact name, then `--alias ROSTER=STORED` (renames the
    stored row and keeps its history), then `--allow-new NAME`, otherwise create.
    A name that strongly resembles an existing player and is neither aliased nor
    explicitly allowed blocks the import.
  - `apply_sessions`: create or repair the sessions, then apply attendance as a
    **delta** — insert the missing people, update the changed guest counts, and
    record the difference in `ledger`; never delete a ledger row.
  - `apply_payments`: bring each person's payments to
    `paid_sessions × fee` with one adjustment row per person.
  - `reconcile`: a read-only table of attended/paid/balance against the sheet.
- `run.py --import-sessions CSV [--dry-run] [--alias A=B] [--allow-new NAME]`,
  `--import-payments XLSX`, `--reconcile XLSX`, and the shared
  `--session-fee`/`--venue-cost`/`--hour` options.
- `_workbook_amount`/`_read_xlsx_players` in `app/main.py` delegate to
  `app/backfill.py`, so the legacy snapshot path stops overstating a fully paid
  row and records the sheet's own period in the note.
- `examples/sessions-template.csv`: the fill-in roster (51 workbook names × 10
  date columns), so the operator marks attendance instead of retyping names.

## Non-goals

- No schema change: `sessions`, `attendance` and `ledger` already express
  everything this specification needs.
- No change to the fee rule, the ledger semantics, or `Service.apply`; the
  backfill writes the same `session:<id>:player:<pid>` charge a finalized
  session writes.
- No deletion of players, sessions, attendance or ledger rows. A correction is
  always a new row with its own `source`.
- No guessing of a person's identity. An unclear name stops the import.
- The bot's chat behaviour, prompts, and response levels are untouched.

## Contracts

- **Roster input** (`--import-sessions`), wide form:

  ```csv
  نام,۸ خرداد,۱۵ خرداد,۲۲ خرداد
  مهیار قهرمان زاده,0,1,
  ```

  An empty cell means absent, `0` means present without a guest, and `n` means
  present with `n` guests. `x`, `✓`, `حاضر`, `بله` are accepted as `0`; `-` and
  `غایب` as absent. Long form: `date,name,guests`.

- **Session identity**: a planned date matches an existing session by its stored
  date (`session_at`, else `when_text`, else `title`), and a stored date within
  three days of the planned Friday is the same session with a wrong date — it is
  repaired in place. Matching never creates a second row for one Friday.

- **Charges**: `(1 + guests) × person_fee` per attendance row, posted as
  `session:<id>:player:<pid>` when the pair has no ledger row yet, otherwise as
  `session:<id>:player:<pid>:adjust` (with a numeric suffix if that key is
  taken). `SUM(amount)` over the prefix always equals the current charge.

- **Payments**: a person's paid total is every ledger row that is **not** a
  session charge (`source NOT LIKE 'session:%'`), so a correction stays stable
  and a second run writes nothing. The target is `paid_sessions × fee`; the
  difference to the current total is written as one `backfill:paid:<pid>` row
  (positive when the database recorded too much payment). A name the roster did
  not create is created from the sheet here.

- **Attendance counts slots**: the workbook's «جلسات آمده» counts a player with
  two guests as three attended sessions, so reconcile sums `1 + guests` over the
  marked sessions.

- **Reconcile gate**: `reconcile` returns `ok` only when every workbook row has
  a player, attended/paid/balance equal the sheet, and no player outside the
  sheet carries a non-zero balance. It never writes.

- **Compatibility**: `Service.session_info()`, `/debt`, `/report`,
  `import_players` with a CSV, and the `opening:` ledger key all keep working.
  A session created by this importer is a normal finalized session.

## Acceptance Criteria

- [x] AC-001: The workbook reader derives `debt = (attended − paid) × fee` and refuses a row whose stated debt disagrees, naming the row.
- [x] AC-002: The wide and the long roster forms produce the same sessions and the same attendance.
- [x] AC-003: A roster date that is not a Friday is rejected before anything is written.
- [x] AC-004: Each planned Friday becomes a session with the canonical title, `when_text`, a 16:30 Tehran `session_at`, `venue_cost` and `person_fee`.
- [x] AC-005: An existing session stored one day off is repaired in place; no duplicate session is created and its charges keep their amount.
- [x] AC-006: Attendance and charges that already exist are kept; only the missing people and the missing amount are added.
- [x] AC-007: A changed guest count posts exactly one adjustment row for the difference.
- [x] AC-008: A person dropped from the roster loses the attendance row and gets a reversal ledger row; no ledger row is deleted.
- [x] AC-009: A roster name that strongly resembles an existing player, without an alias or `--allow-new`, aborts the import with nothing written.
- [x] AC-010: An alias renames the stored short name, keeps its id and its ledger history, and is idempotent.
- [x] AC-011: Payments are corrected to the workbook column, and running the import twice changes nothing the second time.
- [x] AC-012: `reconcile` prints a per-person attended/paid/balance diff and writes nothing.
- [x] AC-013: `reconcile` is `ok` and its totals equal the sheet when the ledger matches every row.
- [x] AC-014: `--dry-run` reports the same plan and leaves the database unchanged.
- [x] AC-015: The legacy snapshot importer no longer overstates a fully paid row and records the sheet's period.

## Test Mapping

- AC-001 -> `tests/test_backfill.py::WorkbookTests::test_debt_is_the_formula_and_a_mismatch_names_the_row`
- AC-002 -> `tests/test_backfill.py::PlanTests::test_wide_and_long_rosters_agree`
- AC-003 -> `tests/test_backfill.py::PlanTests::test_a_non_friday_date_is_rejected`
- AC-004 -> `tests/test_backfill.py::SessionImportTests::test_creates_the_ten_fridays_with_their_own_cost_and_fee`
- AC-005 -> `tests/test_backfill.py::SessionImportTests::test_repairs_a_session_stored_one_day_off`
- AC-006 -> `tests/test_backfill.py::SessionImportTests::test_existing_attendance_is_kept_and_only_the_gap_is_added`
- AC-007 -> `tests/test_backfill.py::SessionImportTests::test_a_changed_guest_count_posts_one_adjustment`
- AC-008 -> `tests/test_backfill.py::SessionImportTests::test_removing_a_player_records_a_reversal`
- AC-009 -> `tests/test_backfill.py::NameTests::test_an_ambiguous_name_fails_closed`
- AC-010 -> `tests/test_backfill.py::NameTests::test_an_alias_renames_the_stored_name_and_keeps_history`
- AC-011 -> `tests/test_backfill.py::PaymentTests::test_payments_follow_the_workbook_and_are_idempotent`
- AC-012 -> `tests/test_backfill.py::ReconcileTests::test_reports_differences_without_writing`
- AC-013 -> `tests/test_backfill.py::ReconcileTests::test_is_green_when_every_row_matches`
- AC-014 -> `tests/test_backfill.py::SessionImportTests::test_dry_run_leaves_the_database_unchanged`
- AC-015 -> `tests/test_sans.py::OperationalTests::test_xlsx_snapshot_uses_fixed_note_and_session_fallback`

## Safety Constraints

- The importer runs inside one SQLite transaction; any validation failure rolls
  the whole import back, so a half-filled period is impossible.
- Nothing is deleted: corrections are new ledger rows, and an attendance row is
  only removed together with a reversal row that keeps the balance explainable.
- A name is never guessed. Similarity is advisory and only blocks; it never
  renames, merges or links on its own.
- The workbook is the reference for money. If the sheet's own formula and its
  debt column disagree, the import stops instead of choosing one.
- `--reconcile` and `--dry-run` open no write path; the installation lock is
  still taken so they cannot observe a half-written state.
- Numbers stay out of the model context; this tool never calls the model.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- Decision: real sessions, not an `opening:` snapshot. The sheet's own formula
  reproduces all 51 debts from attended and paid slots, and 19 of the 23 players
  already in the database match their sheet value, so a snapshot would duplicate
  what the ledger can derive.
- Decision: the ten sessions are 8, 15, 22 and 29 Khordad, 5, 12, 19 and 26 Tir,
  and 2 and 16 Mordad 1405 — ten consecutive Fridays except 9 Mordad, which was
  cancelled. The operator confirmed every session was a Friday at 16:30.
- Decision: the stored «۹ خرداد» session is repaired rather than replaced,
  because 9 Khordad 1405 is a Saturday and the operator confirmed the Friday
  list. Its 14 attendance rows and 2,400,000 T of charges stay untouched.
- Limitation: the workbook has no per-session column, so the per-person
  attended/paid totals are what the gate can prove. A roster that distributes
  the same total over the wrong sessions still reconciles; the roster is the
  operator's record and is imported as given.
- Limitation: a player with activity that the sheet does not list (the live
  database has one, «علی صدری») is reported as `extra` and keeps the gate red
  until the sheet is extended or the row is settled by hand.

## Validation Result

- `python3 -m unittest discover -s tests -t . -v`: 304 tests, OK, no skips. The
  23 new `tests/test_backfill.py` tests replace the one test that used to skip
  because it pointed at a workbook outside the repository.
- `python3 -m compileall -q app tests`: OK.
- `git diff --check`: clean.
- End-to-end rehearsal on a temporary database
  (`run.py --database /tmp/cli-022.sqlite3`): `--import-sessions
  examples/sessions-template.csv --dry-run` wrote nothing and reported the ten
  Fridays; the real run created ten finalized sessions with
  `session_at=…T16:30+03:30`; `--import-payments football-account.xlsx` stopped
  on three names that resemble stored players, and with
  `--allow-new رهام/رفیق علی بختیاری/امین` it created the 51 sheet rows and
  recorded exactly 4,650,000 T of payments (31 slots); `--reconcile` exited
  non-zero with `جمع اکسل: 17,700,000 — جمع دفتر: -4,650,000`, which is the
  correct red state before the per-session roster is entered.
- Green rehearsal on a clean database with a roster that distributes every sheet
  slot over the ten Fridays: 51 players, 10 sessions, 149 attendance rows, 166
  ledger rows, `reconcile` `ok`, «جمع اکسل: 17,700,000 — جمع دفتر:
  17,700,000», every per-person diff zero.
- Production rehearsal on a copy of the live database with
  `--alias "عباس رسولی=رسولی" --alias "محمد مهدی سبحانی=سبحانی"` and
  `--allow-new` for the three look-alikes: sessions 1–3 repaired in place
  («۹ خرداد» → «۸ خرداد», `session_at` and `venue_cost` filled, 14 ledger notes
  re-labelled with their amounts unchanged), seven sessions created, 107
  attendance rows added, 1 guest count corrected, 9 removed with reversal rows,
  the two aliases renamed (#13 and #27) keeping their history, 26 new players
  created, and all 16 pre-existing `proposal:` rows still present. The gate is
  red on exactly one line — «علی صدری» with −300,000 T, the player the sheet
  does not list — so `ledger total 17,400,000` differs from the sheet by his
  300,000 T settlement.
- The legacy snapshot path was re-run on the real workbook: 51 rows,
  `SUM(amount) = 17,700,000` (was 17,850,000), every note
  «بدهی تا ۱۶ مرداد ۱۴۰۵», and the fully paid «ایلانلو» now has no ledger row.
- Not yet done (waiting for the operator's per-session roster): running
  `--import-sessions` against the live database. `reconcile` is the gate and it
  is still red, as expected.

## Live deployment plan

- `examples/sessions-template.csv` holds the 51 sheet names and the ten date
  columns. Filling a cell marks attendance; the number in it is the guest count.
- The rehearsal surfaced three names that must be decided by the operator, not
  by the tool: «رهام» ≈ «پرهام», «رفیق علی بختیاری» ≈ «علی بختیاری» and
  «امین» ≈ «سید امین طاهری». They are a deliberate stop, not a defect.
- «رسولی» and «سبحانی» in the live database are the sheet's «عباس رسولی» and
  «محمد مهدی سبحانی»; they are renamed through `--alias "عباس رسولی=رسولی"`
  and `--alias "محمد مهدی سبحانی=سبحانی"`, which keeps their ids and ledger
  rows.
- «علی صدری» has three attendances and one 300,000 T settlement in the live
  database and no sheet row. He is reported as `extra`; the sheet has to gain a
  row for him or his record has to be settled by hand.