# SPEC-036: Missing-session advice and untested read commands

Status: verified  
Owner: agent  
Created: 2026-10-28

## Problem

Two problems surfaced in the production-readiness audit.

1. `Service.session()` raises one message for every miss:

   ```text
   سانسی پیدا نشد؛ عنوان و زمان سانس جدید را اعلام کنید.
   ```

   That sentence is advice to *create* a session.  It is correct when the
   caller asked «what is the current session?» and nothing is open.  It is
   wrong for every other caller, because all of them were given an explicit
   reference they wanted to *use*:

   ```text
   /void 999       -> «... عنوان و زمان سانس جدید را اعلام کنید.»
   /session 999    -> same
   finalize 999    -> same
   attendance 999  -> same
   ```

   An admin asking to close session 999 is told to announce a new session.
   `Bot` then makes it worse: `session_info` catches `UserError`, calls
   `ask_admin('session', ...)` — which posts *another* message to the admin
   group asking for the next session's time and fee — and re-raises.

2. `/links`, `/session` and `/unlink2` were verified by hand to work, but
   have no automated test at all.  A regression in any of them would be
   invisible to the suite.

## Goal

A miss on an explicit session reference says the session was not found and
points at `/sessions`, and creates no side effect in the admin group.  The
three read commands get regression tests.

## Scope

- `Service.session()` distinguishes a *missing explicit reference* from
  *nothing is currently open*.
- `Bot` only asks the admin for new-session details when the miss really was
  «no current session».
- Tests for `/links`, `/session`, `/unlink2` and for the corrected message
  through `/void`, `/session`, `finalize` and `attendance`.

## Non-goals

- No change to how a session is looked up, picked, or defaulted.
- No change to financial rules, ledger semantics, or the SQLite schema.
- No change to `save_session_flexible`, which intentionally treats an
  unresolvable reference as «create a new session».
- No new commands.

## Contracts

- `Service.session(ref)` keeps raising `UserError` for a miss.  The message
  now depends on the argument only:
  - `ref` in `(None, '', 'current', 'جاری')` and no open session: the
    existing create-oriented message (unchanged, compatibility path).
  - any other `ref`: a not-found message naming the reference and pointing
    at `/sessions`.
- The exception type, the call signature, and every return value stay the
  same, so callers that catch `UserError` keep working.  In particular
  `save_session_flexible(session_ref=...)` still falls back to
  create-by-title.
- `ask_admin('session', ...)` is reached only when the reference was
  `current`/empty, so a mistyped id never triggers a new-session question.
- `/links`, `/session`, `/unlink2` keep their current output.

## Acceptance Criteria

- [ ] AC-001: `Service.session(999)` on a database with no such session
  raises `UserError` that does not tell the caller to announce a new session
  and does mention `/sessions`.
- [ ] AC-002: `Service.session()` / `session('current')` with no open
  session keeps the create-oriented message unchanged.
- [ ] AC-003: `/session 999` replies with the not-found message and does not
  queue a new-session request to the admin group.
- [ ] AC-004: `/void 999` fails closed: no approval card, no pending row, and
  no session is created.
- [ ] AC-005: `finalize` and `attendance` with an unknown session reference
  both report not-found instead of the create advice.
- [ ] AC-006: `/links` reports «هیچ پیوندی ثبت نشده.» when empty and lists
  the pair after a real link.
- [ ] AC-007: `/session` with no argument shows the open session; with its id
  shows the same session.
- [ ] AC-008: `/unlink2` rejects a one-argument call with usage, and says the
  pair is not linked when it is not.

## Test Mapping

- AC-001 -> `tests/test_sans.py::MissingSessionAdviceCase::test_explicit_miss_does_not_advise_creating`
- AC-002 -> `tests/test_sans.py::MissingSessionAdviceCase::test_current_miss_keeps_create_advice`
- AC-003 -> `tests/test_sans.py::MissingSessionAdviceCase::test_session_command_does_not_ask_for_a_new_session`
- AC-004 -> `tests/test_sans.py::MissingSessionAdviceCase::test_void_with_unknown_reference_fails_closed`
- AC-005 -> `tests/test_sans.py::MissingSessionAdviceCase::test_finalize_and_attendance_reference_miss`
- AC-006 -> `tests/test_sans.py::UntestedReadCommandsCase::test_links_empty_then_listed`
- AC-007 -> `tests/test_sans.py::UntestedReadCommandsCase::test_session_command_current_and_by_id`
- AC-008 -> `tests/test_sans.py::UntestedReadCommandsCase::test_unlink2_usage_and_not_linked`

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

- The wrong advice was pre-existing, not introduced by SPEC-035; `/void`
  merely made it easy to reach.
- `Bot.session_info` runs before the admin gate, so a *player* sending
  `/session 999` also hit `ask_admin` and caused an admin-group message.  The
  narrowed condition fixes that path too.
- Verified by reverting `Service.session()`: the eight new assertions fail
  with the old message and pass with the new one, while AC-002 keeps passing
  either way, which is what makes it a compatibility guard.
- `/links`, `/session` and `/unlink2` are covered by
  `UntestedReadCommandsCase`; they had no test before this specification.
- `BaseCase.last_reply()` was added because `replies()` concatenates the whole
  outbox, so a second turn cannot be asserted with `assertNotIn`.
- Server move needs no code change: `ADMIN_GROUP_ID` is the only group
  boundary (`Bot.allowed_chat`), so pointing it at the main admin group moves
  the bot there.
- Remaining known gap, deliberately out of scope: there is still no test that
  a *player* running an admin-only command is refused for every command in
  `ADMIN_ACTIONS`; the sampling done in the audit passed, but it is not
  exhaustive.