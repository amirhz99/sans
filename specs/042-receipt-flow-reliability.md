# SPEC-042: Make the receipt flow reliable against redelivered updates

Status: verified
Owner: agent
Created: 2026-10-29

## Problem

A player sent a receipt photo and then the amount `1050000` as a separate
message. The bot answered «مبلغ ۱٬۰۵۰٬۰۰۰ تومان رو با رسیدت ثبت کردم» but no
receipt was ever created: `receipts` and `outbox` both stayed empty, and
`conversations.state` still held the un-consumed photo 13 hours later. The
admin group received a `declare_debt` debt card instead of a receipt.

Investigation of the production database found the message body was ten copies
of the same text:

```text
summary: '1050000\n1050000\n1050000\n1050000\n1050000\n...'
```

`Bot.parse_amount` is `re.fullmatch(r'(\d+)\s*(?:تومان)?')`, which returns
`None` for that body, so the receipt branch
(`amount = self.parse_amount(body) if state.get('receipt')`) fell through to
the model. The model called `declare_debt` and invented an affirmative
confirmation.

The duplication is not specific to receipts. Of 94 coalesced turns sampled from
`agent_logs`, 35 were exactly ten copies of one text — the same update_id
appended repeatedly. Reproduced directly against `TurnBuffer`:

```text
coalesced ids = [1051, 1051, 1051, 1051, 1051, 1051, 1051, 1051, 1051, 1051]
```

`TurnBuffer.add` appends to `entry['texts']` for every call, without checking
that the update is the one it already holds. `processed_updates`
deduplication runs later, inside `Bot.process`, and cannot prevent this: the
buffer fills and emits `max_messages` copies before `process` is ever called.
The same defect corrupts any numeric amount, any repeated `/cancel`, and any
repeated player name.

Two further defects make the failure silent instead of loud:

1. When a receipt is pending, an unparseable amount falls through to the model
   instead of asking for a number. This is a routing violation: a step the bot
   itself opened must not be handed to an untrusted component.
2. `state['receipt']` never expires, so a stale photo is treated as a pending
   receipt on every later message from that account.

## Goal

- A repeated or redelivered update contributes one fragment to a turn, never
  ten.
- While a receipt photo is pending, only a parseable amount completes the
  submission; anything else gets a deterministic correction prompt and never
  reaches the model.
- A pending receipt photo expires instead of lingering indefinitely.
- The product never tells a player a receipt was recorded unless a receipt row
  exists.

## Scope

- `TurnBuffer.add`: ignore an update whose `update_id` is already held in the
  current turn, return `'duplicate'`, and leave the window untouched.
- `TurnBuffer._pop`: de-duplicate repeated identical fragments so a genuinely
  distinct repeat does not inflate the amount of text.
- `Bot.parse_amount`: tolerate newlines and surrounding whitespace by parsing
  the last non-empty line, still requiring one numeric token.
- `Bot.message`: when `state['receipt']` is set and the body is not a
  parseable amount, raise `UserError` with a correction prompt instead of
  continuing to the model.
- `Bot.message`: expire `state['receipt']` after `RECEIPT_TTL_SECONDS` (24
  hours) and tell the player the receipt was discarded.
- Model prompt: state that `declare_debt` / `payment_amount` never records a
  receipt, so no affirmative receipt confirmation can be invented.

## Non-goals

- No change to `receipts`, `payments`, `ledger`, or any financial rule.
- No change to SQLite schema or migrations.
- No change to proposal guards, approval codes, or the admin permission model.
- No change to `Service.session_info()` or the legacy prompt format.
- Removing the ten real player names from git history is tracked separately and
  is **not** part of this specification.

## Contracts

- `TurnBuffer.add(update, now)` returns `'duplicate'` for an update_id already
  buffered. Callers must treat `'duplicate'` exactly like `'joined'`: no
  acknowledgement, no processing, and no offset advance there (the
  acknowledging `process` call already covers every id in `_coalesced`).
- A duplicate never refreshes `deadline`, so a redelivered update cannot keep
  postponing the reply forever.
- `parse_amount('1050000\n1050000')` returns `1050000`. `parse_amount('سلام')`
  still returns `None`.
- `state['receipt']` keeps its existing shape
  (`file_id`, `file_key`, `media_type`, `chat_id`, `message_id`) and gains one
  key, `at`, holding the epoch seconds it was stored. Older states without
  `at` are treated as fresh, so an upgrade cannot discard a receipt the player
  is in the middle of sending.

## Acceptance Criteria

- [x] AC-001: `TurnBuffer.add` called ten times with one update yields one
      fragment and coalesces to exactly one copy of the text.
- [x] AC-002: A duplicate does not extend the coalescing deadline.
- [x] AC-003: Distinct updates are still coalesced in order, one fragment each.
- [x] AC-004: `parse_amount` accepts a multi-line numeric body.
- [x] AC-005: `parse_amount` still rejects non-numeric text.
- [x] AC-006: A pending receipt plus a non-numeric message raises `UserError`
      and creates no receipt, no debt card, and no model call.
- [x] AC-007: A pending receipt plus a numeric message submits the receipt with
      the stored photo attached.
- [x] AC-008: A pending receipt older than the TTL is discarded with an explicit
      notice and does not attach to the new message.
- [x] AC-009: A receipt state without `at` is still usable.
- [x] AC-010: The confirmation handed to the player is produced by
      `submit_receipt`, which runs only after the receipt row exists.

## Test Mapping

- AC-001 -> `tests/test_spec_042.py::test_duplicate_update_counts_once`
- AC-002 -> `tests/test_spec_042.py::test_duplicate_does_not_extend_deadline`
- AC-003 -> `tests/test_spec_042.py::test_distinct_updates_still_coalesce`
- AC-004 -> `tests/test_spec_042.py::test_parse_amount_accepts_repeated_body`
- AC-005 -> `tests/test_spec_042.py::test_parse_amount_still_rejects_text`
- AC-006 -> `tests/test_spec_042.py::test_pending_receipt_rejects_unparseable_amount`
- AC-007 -> `tests/test_spec_042.py::test_pending_receipt_submits_with_photo`
- AC-008 -> `tests/test_spec_042.py::test_expired_receipt_is_discarded`
- AC-009 -> `tests/test_spec_042.py::test_receipt_without_timestamp_is_kept`
- AC-010 -> `tests/test_spec_042.py::test_confirmation_requires_receipt_row`

## Safety Constraints

- Invalid or unauthorized requests fail closed.
- No unrelated private data is exposed to AI or users.
- Financial and persistence behavior is unchanged unless explicitly scoped.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

The offset and `processed_updates` bookkeeping were verified consistent
(`offset=510`, `MAX(update_id)=509`, 394 rows), so the duplicate was created
before `process()` and not by a replay of an acknowledged update.