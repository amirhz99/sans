# SPEC-024: Optional guest names billed to the host

Status: verified  
Owner: agent  
Created: 2026-10-25

## Problem

A guest is only a counter (`attendance.guests`), so the operator cannot record
*who* came, and the imported roster created one independent player per guest
name.  Those rows inflate the player directory, get their own debt, and cannot
be reached as real accounts.  SPEC-023 tried to fold them into the host but paid
for a host that never attended and broke the workbook reconcile gate.

## Scope

- An optional guest-name list on the attendance action, stored in a new
  `attendance_guests` table.
- An `own` flag on `attendance` so a host who was absent can still be charged for
  his guests only.
- A rewrite of the guest migration: a guest always lands on the host's account,
  at the closest session the host actually attended, preferring the earlier one
  on a tie.
- `--reconcile` folds a migrated guest row into its host instead of reporting a
  difference.

## Non-goals

- No automatic relationship inference: only `data/guest-links.csv` is used.
- No change to the billing rule: a share is still `(own + guests) × person_fee`.
- No live database execution inside the test suite.

## Contracts

- `Service.prepare('attendance', args)` accepts `guest_names: [str]`.
- `migrate_guests(db, links, dry_run=False, fee=150000)` is transactional and
  idempotent.
- `reconcile(db, rows, fee, session_ids=None)` stays read-only.

## Goal

Record who a guest was when the operator knows it, and keep the money on the
host's account exactly once — so the ledger total and the workbook reconcile are
unchanged by the migration.

## Acceptance Criteria

- [x] AC-001: A guest is billed to the host, never to an account of its own.
- [x] AC-002: A guest moves to the closest session the host attended; on an equal
  distance the earlier session wins.  A host with no attendance at all keeps the
  guest's own session with `own=0`, so only the guest share is charged.
- [x] AC-003: The ledger total does not change and no extra share is invented.
- [x] AC-004: Repeating the migration makes no further changes; `--dry-run`
  writes nothing.
- [x] AC-005: `--reconcile` still passes after the migration because a workbook
  row of a migrated guest is counted on its host.
- [x] AC-006: `guest_names` is optional; `len(names) > guests`, an empty name, or
  a duplicate name fails closed with no side effect.
- [x] AC-007: Removing attendance removes its guest names; `own=1` stays the
  default so every existing row keeps today's behaviour.
- [x] AC-008: `مهمان نواز` is never accepted as a guest, and an unknown guest
  fails closed.

## Test Mapping

- AC-001 -> `tests/test_backfill.py::GuestMigrationTests::test_guest_lands_on_host_and_player_row_disappears`
- AC-002 -> `tests/test_backfill.py::GuestMigrationTests::test_moves_to_nearest_host_and_earlier_tie_preserving_ledger`
  `test_missing_host_is_created_in_guest_session`
- AC-003 -> `tests/test_backfill.py::GuestMigrationTests::test_missing_host_is_created_in_guest_session`
- AC-004 -> `tests/test_backfill.py::GuestMigrationTests::test_repeating_the_migration_changes_nothing`
  `test_a_dry_run_writes_nothing`
- AC-005 -> `tests/test_backfill.py::GuestReconcileTests::test_reconcile_folds_a_migrated_guest_into_its_host`
- AC-006 -> `tests/test_sans.py::GuestNameTests`
- AC-007 -> `tests/test_sans.py::GuestNameTests::test_removing_attendance_removes_guest_names`
  `tests/test_sans.py::GuestOwnShareTests`
- AC-008 -> `tests/test_backfill.py::GuestLinkTests`
  `GuestMigrationTests::test_an_unknown_guest_fails_closed`

## Safety Constraints

The billing rule, the ledger semantics and the existing rows are untouched:
`own` defaults to 1 and guest names never affect money.  The live database is
not changed by this implementation task; the operator runs the migration after
stopping the bot and taking a fresh backup.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

`attendance_guests` is descriptive: `guests` stays authoritative for billing, so
a name is never required.  SPEC-023 is superseded by this specification.

The migration was exercised on a copy of the live database in `/tmp` (the live
file was not opened for writing): 11 moves, ledger total unchanged at
19,050,000 تومان, `--reconcile` green, and a second run changed nothing.
