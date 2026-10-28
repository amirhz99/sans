# SPEC-039: Retire the one-off workbook import

Status: verified  
Owner: agent  
Created: 2026-10-29

## Problem

SPEC-022 added an Excel (`football-account.xlsx`) reader, a payments importer
and a `--reconcile` gate. They existed for exactly one purpose: bring every
player's balance up to the ۱۶ مرداد ۱۴۰۵ cut-off from the operator's
spreadsheet. That migration ran on 2026-10-25 and closed green.

The tool is now spent, and it has become actively misleading:

- The gate can never pass again. It compares a *frozen* sheet against a
  *live* ledger, and the ledger has moved on. Measured before removal:
  «جمع اکسل: 19,050,000 — جمع دفتر: 30,150,000», 26 of 50 rows diffed, exit 1.
  The 24 rows that still match exactly and the 26 that do not are both
  explained by post-cut-off sessions (`session` 251 rows / 38,400,000 T) and
  bot-approved payments (`proposal` 24 rows / −6,900,000 T), plus six players
  who never had a sheet row. A permanently red command is not a regression
  detector; it is noise a future maintainer will try to "fix".
- The binary it needs is not in the repository and never will be. A public
  reader can see `--reconcile` in `--help` and has no way to run it.
- It was the only reason `app/backfill.py` imported `zipfile` and
  `xml.etree`; the reader parsed XLSX by hand rather than through a library.

The ledger is the source of truth. Every ۱۴۰۵ balance lives in `ledger` rows
whose `session_id` points at a real, dated session — no row depends on the
sheet to be interpretable.

## Goal

The workbook reader, the payments importer and the reconcile gate are gone,
and every financial number in the live database is identical before and after
their removal. The two importers that are *general* — roster (`--import-sessions`)
and guest migration (`--migrate-guests`) — keep working unchanged.

## Scope

- Delete `xlsx_rows`, `read_workbook`, `workbook_amount` and `read_overrides`
  from `app/backfill.py`, and drop its Excel-only imports.
- Delete `apply_payments`, `reconcile`, `format_reconcile` and `format_payments`.
- Remove `--import-payments`, `--reconcile` and `--workbook-overrides` from
  `app/main.py`, and the `.xlsx` branch of `--import-players` (which becomes
  CSV-only). Drop `check_modifiers`, now that no modifier is command-specific.
- Remove the hand-rolled XLSX reading (`zipfile`, `xml.etree`) along with the
  reader; no third-party library is involved, so `requirements.txt` does not
  change.
- Move `write_xlsx` out of `tests/test_backfill.py` so `tests/test_sans.py`
  does not import a helper from a module that no longer needs it.
- Rewrite the README's Excel/backfill sections as one short "initial import"
  section covering the two surviving importers.
- Retire SPEC-022 (`Status: retired`-equivalent: `superseded`) and note it in
  SPEC-023/024 where they reference the removed tests.

## Non-goals

- No change to the schema, the ledger semantics, fees, or any bot behaviour.
- No deletion or re-derivation of any ledger, attendance or session row.
- `--import-sessions`, `--migrate-guests`, `--alias`, `--allow-new`,
  `--session-fee`, `--venue-cost`, `--hour` and `--dry-run` keep working.
- The 9 sessions with `session_at = NULL` are a separate finding and are not
  touched here.

## Contracts

- `app.main.build_parser()` exposes no `--reconcile`, `--import-payments` or
  `--workbook-overrides`; passing one is an argparse error (exit 2), not a
  silent fallthrough to starting the bot.
- `--import-players` accepts CSV only; a `.xlsx` path is refused with
  `UserError` before any write.
- `--import-sessions` and `--migrate-guests` produce byte-identical reports
  and database effects to SPEC-022-era behaviour.
- Removing the CLI surface changes no row count and no balance.

## Acceptance Criteria

- [x] AC-001: the parser rejects `--reconcile` and `--import-payments` and
  `--workbook-overrides`, and still accepts `--import-sessions`,
  `--migrate-guests`, `--import-players` and `--init`.
- [x] AC-002: `--import-players` refuses a `.xlsx` path with a clear message
  and writes no ledger row.
- [x] AC-003: `--import-sessions` still creates/repairs sessions and applies
  the roster exactly as before.
- [x] AC-004: `--migrate-guests` still moves explicit guests onto the host
  account exactly as before.
- [x] AC-005: `app.backfill` no longer exposes the workbook symbols, and
  importing it does not pull in `zipfile` or `xml.etree`.
- [x] AC-006: the live database's financial fingerprint (ledger row count,
  ledger sum, per-player balances, session/attendance counts) is unchanged by
  this specification.

## Test Mapping

- AC-001 -> `tests/test_backfill.py::CliTests::test_the_workbook_flags_are_gone`
- AC-002 -> `tests/test_sans.py::OperationalTests::test_xlsx_player_import_is_refused`
- AC-003 -> `tests/test_backfill.py::SessionImportTests::test_creates_the_ten_fridays_with_their_own_cost_and_fee`
  `test_import_is_idempotent`
- AC-004 -> `tests/test_backfill.py::GuestMigrationTests`
  `GuestLinkTests::test_links_are_explicit_and_mostly_natural`
- AC-005 -> `tests/test_backfill.py::CliTests::test_backfill_module_has_no_workbook_symbols`
- AC-006 -> `tests/test_backfill.py::CliTests::test_financial_surface_is_untouched`

## Safety Constraints

- The bot is stopped and a fresh backup exists before any file is removed.
- No `DELETE`/`UPDATE` is run against the live database by this change.
- Financial and persistence behavior is unchanged unless explicitly scoped.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Validation Result

- `python3 -m unittest discover -s tests -t . -v`: **462 tests, OK**, exit 0.
  The count went from 471 to 462: `tests/test_backfill.py` dropped from 35 to
  26 tests (the workbook, payments and reconcile classes plus
  `GuestReconcileTests`), and 4 were added (`CliTests` gained three, and
  `test_xlsx_snapshot_uses_fixed_note_and_session_fallback` was replaced by
  `test_xlsx_player_import_is_refused`). No test was skipped.
- `python3 -m compileall -q app tests`: OK, exit 0.
- `git diff --check`: clean, exit 0.
- **Live database fingerprint, before and after, identical:**

  | measure | before | after |
  |---|---|---|
  | players | 49 | 49 |
  | sessions | 19 | 19 |
  | attendance rows | 240 | 240 |
  | ledger rows | 283 | 283 |
  | ledger sum (T) | 30,150,000 | 30,150,000 |
  | finalized / voided | 19 / 1 | 19 / 1 |
  | per-player balance SHA-256 | `02f0983…cfde85` | `02f0983…cfde85` |

  The ledger split was also unchanged: `session` 251 rows / 38,400,000,
  `proposal` 24 / −6,900,000, `backfill` 8 / −1,350,000.
- Command-line behaviour checked directly:
  `--reconcile`, `--import-payments` and `--workbook-overrides` each exit **2**
  (argparse error, no silent fallthrough to starting the bot); a `.xlsx`
  argument to `--import-players` exits **1** with «ورود فایل اکسل حذف شده
  است؛ فایل را به CSV … تبدیل کنید.» and writes nothing.
- A pre-change backup was taken with the bot stopped:
  `data/backups/sans-backup-20261029-003935-pre039.sqlite3`.
- The four one-off data files were **archived, not destroyed**, in
  `data/backups/retired-import-20261029-010940/` (mode 600, gitignored):
  `football-account.xlsx`, `sessions.csv`, `workbook-overrides.csv`,
  `guest-links.csv`. The working copies were then removed.
- `requirements.txt` is unchanged: the reader parsed XLSX with `zipfile` and
  `xml.etree` from the standard library, so no dependency was involved.

## Notes

- The pre-removal fingerprint was captured from the live database; it is the
  evidence that this specification removed code and nothing else.
- The reconcile gate was **red** before removal, and that is expected, not a
  failure: «جمع اکسل: 19,050,000 — جمع دفتر: 30,150,000», 26 of 50 rows
  diffed, exit 1. An earlier draft of this specification required a green gate
  first; that criterion was impossible and was replaced by the before/after
  fingerprint above.
- One regression was introduced and caught by the suite while doing this:
  `_session_distance` looked like workbook-only code but `_guest_host_session`
  (the SPEC-024 migration) depends on it. It was restored, and
  `GuestMigrationTests` passes again.
- **Follow-up, not fixed here:** 9 sessions (ids 11–19) carry
  `session_at = NULL`. They predate this change and are unrelated to the
  workbook, but any report that filters «past sessions» by date will not see
  them. Tracked separately.