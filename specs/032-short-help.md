# SPEC-032: One short, process-first guide per audience

Status: verified
Owner: agent
Created: 2026-10-28

## Problem

`/help` answered everybody with the same message, assembled by appending:

```python
reply = HELP + ('\n\n' + ADMIN_HELP if admin else '')
```

That had four consequences:

1. An admin received the player guide *and* the admin guide, so the part he
   needs started below eight lines he never uses ("send a receipt from your
   private chat", "do I owe anything").
2. In the management group the player guide is noise. No player reads it there,
   and it invites player behaviour in the admin chat.
3. The player guide appended the full `Service.debt()` block: up to 30 ledger
   rows, an outstanding breakdown, pending receipts. Asking for help could
   produce a 50-line message.
4. Both texts were mostly command syntax (`/session | /debt | /card`,
   `/merge 12 18`, `/setdefault person_fee 200000`). The bot understands plain
   Persian, so advertising command grammar makes the product look harder than
   it is. `GREETING_REPLY`, `WELCOME_COMPLETE`, the agent's `help` fallback and
   `identify()` advertised the same commands in four more places.

Separately, a person who introduced himself by name but shared no number got a
card with no number and no hint that anything was missing.

## Goal

One audience, one guide, no commands. An admin opening help in the management
group or in his own private chat reads only what an admin does; a player reads
only what a player does. Both guides describe processes and outcomes. The
balance arrives as its own short message so the guide stays constant. A person
who gives a name without a number is told so in the same reply and handed the
share-contact button.

## Scope

- `Bot.help_reply(user_id, admin, private)` returns `(text, keyboard, followup)`.
  `admin` selects `ADMIN_HELP` alone, for the management group and for an
  allowlisted admin's private chat. Everyone else gets `HELP` alone.
- The balance is one line queued *after* the guide, never inside it:
  `بدهی فعلی: N تومان`, `بستانکار: N تومان`, or `تسویه شده‌ای ✅`.
- The balance appears only for a linked player in a private chat. An admin guide
  never carries a balance.
- A linked player with no number on file gets the player guide with the
  share-contact keyboard.
- `identify()` appends one reminder line when the identity card carries no
  number; the caller attaches `contact_keyboard()` for that case.
- `identify()` and its early returns stop advertising commands.
- `content/bot-content.json` `help` and `admin_help` are rewritten as process
  lists; neither contains a command.
- `GREETING_REPLY`, `WELCOME_COMPLETE` and the agent's JSON `help` fallback stop
  advertising commands.

## Non-goals

- Commands keep working exactly as before. Only their *advertisement* is
  removed; `Bot.command()` is untouched.
- `Service.debt()` is unchanged, so `/debt` still shows the full ledger.
- No new keyboard, callback or outbox mechanism is introduced.

## Contracts

- `help_reply` is a pure read. It writes nothing.
- The `followup` line is queued through `db.say` after the main reply, so
  message order is guide first, balance second.
- `identify(name, user_id, chat_id, sender=None, phone_number=None)` keeps its
  signature and still returns one string, because `app/agent.py` calls it.
- `phone_missing(user_id)` reads the pending payload. A missing or unparseable
  payload fails closed to `False`, so no spurious reminder is sent.
- An admin never sees the player guide; a player never sees the admin guide.

## Acceptance Criteria

- [x] AC-001: The player guide contains no `/` command.
- [x] AC-002: The admin guide contains no `/` command.
- [x] AC-003: An admin in the management group sees the admin guide and not the
  player guide.
- [x] AC-004: A player in a private chat never sees the admin guide.
- [x] AC-005: An allowlisted admin in his own private chat sees the admin guide
  and not the player guide.
- [x] AC-006: The balance is a separate one-line message delivered after the
  guide.
- [x] AC-007: `/debt` still returns the full ledger.
- [x] AC-008: `GREETING_REPLY` and `WELCOME_COMPLETE` advertise no command.
- [x] AC-009: A name without a number gets the reminder line and the
  share-contact keyboard.
- [x] AC-010: A name that already carries a number gets no reminder.
- [x] AC-011: An introduction reply names no command, while its admin card still
  carries the request id and the wanted name.
- [x] AC-012: A repeated introduction is answered with no command either.

## Test Mapping

- AC-001 -> `tests/test_sans.py::ShortHelpTests::test_player_help_advertises_no_command`
- AC-002 -> `tests/test_sans.py::ShortHelpTests::test_admin_group_gets_only_the_admin_guide`
- AC-003 -> `tests/test_sans.py::ShortHelpTests::test_admin_group_gets_only_the_admin_guide`
- AC-004 -> `tests/test_sans.py::ShortHelpTests::test_private_player_never_sees_the_admin_guide`
- AC-005 -> `tests/test_sans.py::ShortHelpTests::test_private_admin_gets_only_the_admin_guide`
- AC-006 -> `tests/test_sans.py::ShortHelpTests::test_help_balance_is_a_separate_short_message`
- AC-007 -> `tests/test_sans.py::ShortHelpTests::test_debt_command_still_shows_the_full_ledger`
- AC-008 -> `tests/test_sans.py::ShortHelpTests::test_welcome_and_greeting_advertise_no_command`
- AC-009 -> `tests/test_sans.py::ShortHelpTests::test_name_without_a_number_is_reminded_with_the_button`
- AC-010 -> `tests/test_sans.py::ShortHelpTests::test_a_name_that_carries_a_number_is_not_reminded`
- AC-011 -> `tests/test_sans.py::ShortHelpTests::test_introduction_never_answers_with_a_command`
- AC-012 -> `tests/test_sans.py::ShortHelpTests::test_a_second_introduction_never_names_a_command_either`

## Safety Constraints

- The admin guide cannot reach a non-admin: `admin` comes from `Bot.is_admin`.
- A malformed pending payload yields no reminder instead of an exception.
- The balance reads `Service.balance`, the same source `/debt` uses.
- The help path writes nothing, so repeated `/help` cannot compound state.

## Decisions

- Decision: the balance is a separate message rather than a suffix, so the guide
  text stays constant and a financial query cannot delay it.
- Decision: attendance is presented last and marked optional, because recording
  attendance is the admin's job.
- Decision: identity guidance is presented first, because a newcomer cannot use
  any other feature until the account is linked.
- Decision: the support line was removed from the admin guide. The handle exists
  so that a *player* can escalate; an admin is the one who resolves.
- Decision: the old admin-help test was renamed, because the property it
  asserted inverted: the guides are now exclusive instead of cumulative.  Its
  name promised "separates public and admin commands"; the replacement asserts
  that the admin guide *replaces* the player guide.

## Limitations

- Both guides are static text plus one balance line. A new capability requires
  editing `content/bot-content.json`.
- Amounts render with ASCII digits (`350,000`), matching the rest of the bot,
  while the surrounding prose is Persian.
- After a name-without-number introduction the admin receives two cards for one
  person: the bare one, then the one carrying the number. Editing the first card
  instead of sending a second is deferred.
- A person who introduces himself while already linked still gets the old
  "already identified" sentence, which now names no command but also offers no
  next step beyond asking for the balance in words.

## Validation

- `python3 -m unittest discover -s tests -t .`: 403 tests, OK.
- `python3 -m compileall -q app tests`: OK.
- `git diff --check`: clean.
