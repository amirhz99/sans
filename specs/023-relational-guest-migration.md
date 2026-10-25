# SPEC-023: Relational attendee guest migration

Status: superseded  
Superseded-by: SPEC-024  
Owner: agent  
Created: 2026-10-25

## Problem

Some roster names describe a guest and their host rather than independent
players. Treating those rows as players inflates attendance and misattributes
the financial history.

## Scope

- Explicit links in `data/guest-links.csv`, attendance migration, paired ledger adjustments, and the dedicated CLI.

## Non-goals

- No automatic relationship inference and no live database execution in this change.

## Contracts

- `migrate_guests(db, links, dry_run=False)` is transactional and idempotent.

## Goal

Move only explicitly approved links from `data/guest-links.csv` under their
hosts while preserving an auditable ledger and making repeated runs harmless.

## Acceptance Criteria

- [x] AC-001: A linked guest is moved to the nearest host session; an exact tie
  chooses the earlier session.
- [x] AC-002: A missing host is created in the guest's session.
- [x] AC-003: Attendance and ledger history are not destructively deleted; paired
  adjustments preserve the ledger total.
- [x] AC-004: Repeating the migration makes no further changes.
- [x] AC-005: `مهمان نواز` cannot be accepted as a guest link.

## Test Mapping

- AC-001 -> `tests/test_backfill.py::GuestMigrationTests::test_moves_to_nearest_host_and_earlier_tie_preserving_ledger`
- AC-002 -> `tests/test_backfill.py::GuestMigrationTests::test_missing_host_is_created_in_guest_session`
- AC-003 -> `tests/test_backfill.py::GuestMigrationTests::test_moves_to_nearest_host_and_earlier_tie_preserving_ledger`
- AC-004 -> `tests/test_backfill.py::GuestMigrationTests::test_moves_to_nearest_host_and_earlier_tie_preserving_ledger`
- AC-005 -> `tests/test_backfill.py::GuestLinkTests`

## Safety Constraints

No relational name is inferred. Unknown guests and malformed links fail closed.
The live database is not changed by this implementation task.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

`migrate_guests` is a separate CLI operation and supports `--dry-run`.