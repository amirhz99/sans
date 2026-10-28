# SPEC-035: A session that never happened

Status: verified
Owner: agent
Created: 2026-10-28

## Problem

A session can be created and then not take place. Nobody comes, or the hall is
closed, or the evening is cancelled. The row stays open, and an open row is what
`Service.session(ref=None)` resolves as «current»: every card that says
«سانس جاری» keeps pointing at an evening that is already in the past, and
`configuration()` keeps listing it as an open session.

There is no operation today that closes such a session:

- `finalize` refuses before it records anything, because it needs money to
  charge: `prepare()` raises «ابتدا حاضران سانس را ثبت کنید.» when the session
  has no attendance rows. That guard is correct: finalizing is a financial
  action and must never invent a charge.
- `attendance` cannot express "nobody came". Deleting attendance rows leaves the
  session open, and creating a zero-guest row for a player who was not there
  would be a lie that also masks a real absence.
- `update_session` can change the title or the fee, but never closes the row.

So a no-show session had to be closed by hand in SQLite, outside every
validation, every audit row, and every approval card.

Note on the existing `finalized` column: `prepare()` resolves a session with
`draft=True` for `attendance` and `finalize`, so a finalized session already
refuses both. Closing a session is therefore "closed means closed" by an
existing rule, not a new one.

## Goal

An admin can close a no-show session with one approval card, and the session
leaves the open list without charging anybody and without touching the ledger.

## Scope

- **Schema.** `sessions` gains `voided INTEGER NOT NULL DEFAULT 0 CHECK(voided
  IN (0, 1))`. `sessions.finalized` keeps its meaning: the session is closed.
  `voided` only records *why* it closed.
- **Domain.** `Action.VOID_SESSION = 'void_session'` joins the action
  allow-list in `app/domain/models.py`.
- **Service** (`app/service.py`):
  - `void_session` joins `ADMIN_ACTIONS`.
  - `prepare('void_session', {'session': ref, 'reason': text})` resolves the
    session with `draft=True` (so an already closed session is refused by the
    existing rule), refuses when the session has attendance rows, and refuses an
    already voided session.
  - `apply()` writes `finalized=1, voided=1` and appends the reason to the note,
    and **inserts no ledger row**.
  - `guard()` fingerprints the session's closed/voided flags and its attendance
    count, so a card that was shown before attendance changed expires instead of
    applying.
  - `preview()` renders a void card: what is being closed, that no money moves,
    that nobody is notified, and that it cannot be undone.
  - `session_info()` / `SessionView` and `report()` show «برگزار نشد».
- **Bot** (`app/bot.py`): `/void [session] [دلیل]` builds the card. With no
  argument the current open session is used; the reply names it.
- **Agent** (`app/agent.py`, `content/bot-content.json`): the routing table
  offers `void_session` when the admin says the session did not happen
  («برگزار نشد»، «کسی نیومد»، «لغو شد»).
- **README**: the admin command list mentions `/void`.

## Non-goals

- **No change to any financial rule.** `venue_cost * attendees` stays; the venue
  cost is never split onto players, and a voided session moves no money at all.
  A session whose hall must still be paid for is *not* this specification: that
  is a real charge and belongs in `finalize` or `adjustment`.
- No automatic detection of a "past and empty" session. A session is voided only
  when an admin explicitly asks for it.
- No deletion. The session row, its title, its fee, and its note stay in the
  database so `report()` and the history stay complete.
- No player notification. Nobody attended, so nobody is told anything.
- No reopening. A voided session stays voided; a real session that must be
  re-created is a new row, exactly as a finalized session would be.
- `finalize` keeps its refusal for an empty session. This specification adds a
  separate action rather than relaxing that guard.
## Contracts

- **Persistence.** `sessions.voided` is additive with a default of `0`, so every
  existing row is a normal session. `app/db.py` adds the column with the
  `ALTER TABLE ... DEFAULT 0` pattern already used for `session_at`, and
  `app/schema.sql` creates it for a fresh database. No other table changes.
- **State.** A voided session satisfies `finalized=1 AND voided=1`. Since
  `session(ref=None)` selects `finalized=0`, a voided session is no longer the
  current session and no longer appears among the open sessions.
- **Action.** `void_session` takes `session` (id, title or `current`) and an
  optional `reason`. It is in `ADMIN_ACTIONS`, so an admin states it with
  `/void`, `/do`, or plain language through the agent, and it reaches `apply()`
  only after an approval click.
- **Refusals, all failing closed:**
  - the session has attendance rows: the message states how many are registered
    and says to remove them first; nothing is changed.
  - the session is already closed: the existing «حساب این سانس بسته شده…» from
    `session(..., draft=True)`.
  - the session is already voided: «این سانس قبلاً برگزار نشده ثبت شده است.»
  - the session reference resolves to nothing or to more than one row: the
    existing `session()` errors.
  - a non-admin: the existing `ADMIN_ACTIONS` boundary and the `/void` admin
    guard.
- **Ledger.** `apply('void_session', ...)` performs no `INSERT INTO ledger`. The
  balance of every player is byte-for-byte unchanged by the operation.
- **Guard.** `guard({'action': 'void_session', ...})` returns
  `{'finalized': int, 'voided': int, 'attendance': <count>}`. A card expires
  when the session was closed elsewhere or attendance was added after the card
  was shown.
- **Reason.** The reason is appended to `sessions.note` with a
  «برگزار نشد:» prefix, so a later reader of `report()` or the admin full view
  sees why. An empty reason writes the prefix alone.
- **Compatibility.** `Service.session_info()` keeps its signature and stays the
  summary by default. A session that is not voided renders exactly as before.
  The legacy content format and the `bot-content.json` keys are untouched.
## Acceptance Criteria

- [x] AC-001: an open session with no attendance is voided by an approved card;
      afterwards `finalized=1` and `voided=1`.
- [x] AC-002: voiding creates no ledger row and leaves every player balance at
      zero.
- [x] AC-003: after voiding, `session(ref=None)` no longer returns that session
      and `configuration()` no longer lists it as open.
- [x] AC-004: a session with attendance is refused, the message says how many
      are registered, and neither the session nor the ledger changed.
- [x] AC-005: an already closed (finalized) session is refused.
- [x] AC-006: an already voided session is refused the second time.
- [x] AC-007: the proposal expires when attendance is added after the card was
      shown, and the session stays open.
- [x] AC-008: `/void` from a non-admin is refused as an unknown command, and
      `/void` with no open session answers clearly instead of voiding something.
- [x] AC-009: the card names the session, states that no money moves, that
      nobody is notified, and that it cannot be undone.
- [x] AC-010: a voided session reads «برگزار نشد» in the session list, in the
      admin full view and in `report()`.
- [x] AC-011: `sessions.voided` is added to an existing database by the
      migration and every pre-existing row reads `0`.
- [x] AC-012: `void_session` is a domain action and is in `ADMIN_ACTIONS`, so it
      is never reachable as a read-only action.

## Test Mapping

- AC-001 -> `tests/test_sans.py::VoidSessionTests::test_an_empty_session_is_voided_and_closed`
- AC-002 -> `tests/test_sans.py::VoidSessionTests::test_voiding_writes_no_ledger_row`
- AC-003 -> `tests/test_sans.py::VoidSessionTests::test_a_voided_session_is_no_longer_current`
- AC-004 -> `tests/test_sans.py::VoidSessionTests::test_a_session_with_attendance_is_refused`
- AC-005 -> `tests/test_sans.py::VoidSessionTests::test_a_finalized_session_cannot_be_voided`
- AC-006 -> `tests/test_sans.py::VoidSessionTests::test_voiding_twice_is_refused`
- AC-007 -> `tests/test_sans.py::VoidSessionTests::test_the_card_expires_when_attendance_is_added`
- AC-008 -> `tests/test_sans.py::VoidSessionTests::test_a_player_cannot_void_a_session`
  `tests/test_sans.py::VoidSessionTests::test_void_without_an_open_session_answers`
- AC-009 -> `tests/test_sans.py::VoidSessionTests::test_the_card_states_every_consequence`
- AC-010 -> `tests/test_sans.py::VoidSessionTests::test_a_voided_session_reads_as_not_held`
- AC-011 -> `tests/test_sans.py::VoidSessionTests::test_a_legacy_database_gets_the_voided_column`
- AC-012 -> `tests/test_agent_contracts.py::ActionContractTests::test_void_session_is_an_admin_action`

## Safety Constraints

- Invalid or unauthorized requests fail closed; a refused void changes nothing.
- A void is a side effect that needs an explicit admin approval card.
- No unrelated private data is exposed to AI or users.
## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- The action is named `void_session` rather than `cancel_session` because
  `cancel` already means "drop the pending proposal", and two meanings for one
  word in the same chat would be a trap on the day it matters.
- The venue cost is deliberately *not* recorded as a charge here. The system
  never splits it onto players, so writing it into the ledger would create a
  charge with no payer. A hall that was still paid for is `adjustment` work on a
  named player, or belongs to a future specification about hall accounting.
- With attendance present the operation is refused rather than destructive: a
  one-click "void anyway" would silently discard a list the admin had just
  entered. Removing attendance first is the same number of clicks and shows the
  admin what is being discarded.
- Deploying this change needs one backup of `data/sans.sqlite3` before the
  restart, because the restart runs the `ALTER TABLE`.
- Financial rules are unchanged: a void moves no money, and `finalize` keeps its
  refusal for an empty session.
- The schema change is additive, defaulted, and covered by AC-011 so an existing
  production database keeps every row.