# SPEC-009: Outbox drops failed callback acknowledgements

Status: verified
Owner: agent
Created: 2026-10-21

## Problem

`answerCallbackQuery` is queued in the transactional `outbox` exactly like a
real message. When Bale rejects it — most often because the callback query id
has already expired — `flush_outbox` treats it as a retryable delivery failure
and applies exponential backoff up to 300 seconds. On the live database four
such rows had reached **344 attempts** each and would have been retried forever,
producing one warning every five minutes and keeping dead work in the queue.
A callback acknowledgement is a transient UI toast: the user-visible response is
delivered separately as a queued `sendMessage`, so retrying it can never improve
the outcome.

## Goal

A failed `answerCallbackQuery` is discarded on its first failure instead of
being retried, while message delivery keeps its existing retry and backoff
behavior. Stale acknowledgement rows already in a database drain on the next
flush without manual SQL.

## Scope

- `app/main.py::flush_outbox` deletes an `answerCallbackQuery` row whose call
  raised `APIError`, logs one warning, and continues with the next row.
- Existing rows with a high `attempts` value follow the same path once their
  `next_attempt` is due, so no migration is required.
- A successful `answerCallbackQuery` keeps being deleted as before.

## Non-goals

- No change to the `outbox` schema, to `sendMessage`/`editMessageText` retry and
  backoff semantics, or to the `editMessageText` fallback that re-queues a
  `sendMessage`.
- No change to how callback handlers produce the acknowledgement text.
- No automatic expiry policy for other outbox methods.

## Contracts

- `flush_outbox(db, client, limit=20, pace=0.15)` keeps its signature and its
  one-row-at-a-time ordering by `id`.
- Only the method name `answerCallbackQuery` selects the drop path. Every other
  method keeps `attempts=attempts+1` and the computed `next_attempt` delay.
- The warning contains the outbox id, the method, and the sanitized `APIError`
  text only; it never logs the callback id or the acknowledgement text.
- Financial state, ledger rows, and pending proposals are untouched: this
  change only removes outbox rows.

## Acceptance Criteria

- [ ] AC-001: A failing `answerCallbackQuery` is removed from `outbox` after one
  flush and is never retried.
- [ ] AC-002: A failing `sendMessage` still increments `attempts` and remains
  queued with a future `next_attempt`; the existing financial idempotency test
  stays green.
- [ ] AC-003: A successful `answerCallbackQuery` is removed from `outbox`.
- [ ] AC-004: Rows left by the previous behavior (high `attempts`, due
  `next_attempt`) are drained by a single flush.
- [ ] AC-005: The drop emits exactly one warning that names the method and the
  outbox id but not the payload.

## Test Mapping

- AC-001 -> `tests/test_sans.py::OperationalTests::test_failed_callback_acknowledgement_is_dropped_not_retried`
- AC-002 -> `tests/test_sans.py::OperationalTests::test_outbox_retries_without_reapplying_financial_operations`
- AC-003 -> `tests/test_sans.py::OperationalTests::test_successful_callback_acknowledgement_is_removed`
- AC-004 -> `tests/test_sans.py::OperationalTests::test_stale_callback_acknowledgements_are_drained`
- AC-005 -> `tests/test_sans.py::OperationalTests::test_failed_callback_acknowledgement_is_dropped_not_retried`

## Safety Constraints

- The change is limited to deleting outbox rows for one non-mutating API method;
  it cannot create a payment, debt, attendance, or identity change.
- A failed toast never suppresses the actual response: the response is queued as
  a separate `sendMessage` row and is still retried.
- Secrets, callback ids, and user text stay out of the log line.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- Live evidence: outbox rows 1-4 were `answerCallbackQuery` with
  `attempts=344`; the retry delay had saturated at the 300 second cap.
- `send_acknowledgement` (the immediate "در حال بررسی" notice) is a different,
  best-effort path and is not affected.
