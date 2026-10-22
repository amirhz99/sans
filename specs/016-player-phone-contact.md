# SPEC-016: A player can share a phone number with one button

Status: verified
Owner: agent
Created: 2026-10-22

## Problem

`contacts.phone` already remembers a number that a user shares in a private
chat (SPEC-014), but that row is an address book entry, not player data:

- `players` has no phone column, so `/players`, the debt reply, and the agent's
  `query_database('players')` cannot show a number for a player.
- The number is only captured if the user knows they can send a contact card.
  Nothing in the bot ever offers that action; the only instruction is a line of
  text inside an identity card sent to the admin group.
- The live database has 26 players, 0 with `bale_id`, and an empty `contacts`
  table: the current flow has never been used, so the discovery problem is real.

## Goal

A linked player can share a phone number by pressing one button, the number is
stored on that player's row in one canonical format, admins can see it, and the
model never receives it.

## Scope

- `players.phone TEXT NOT NULL DEFAULT ''` with an additive migration in
  `Database.__init__` (the same pattern as `sessions.session_at` and
  `pending.guard`).
- New `Service.normalize_phone(value)` and `Service.set_player_phone(player_id,
  phone, actor)`:
  - `+989121234567`, `00989121234567`, `989121234567`, `09121234567`, and
    `9121234567` all normalize to `+989121234567`;
  - Persian and Arabic digits are accepted; separators (space, `-`, `.`, `()`)
    are ignored;
  - anything that is not a 10 digit Iranian mobile starting with `9` after the
    country/leading zero prefix is rejected with `UserError`;
  - repeating the stored value writes nothing and records no audit row.
- New `Bot.contact_keyboard()` returning
  `{'keyboard': [[{'text': ..., 'request_contact': True}]]}` and
  `Bot.hide_keyboard()` returning `{'remove_keyboard': True}`. Only `keyboard`
  is sent: `resize_keyboard` and `one_time_keyboard` are not documented for the
  Bale API and are therefore never included.
- `Bot.receive_contact` stores the canonical number in `contacts.phone` for
  every sender and in `players.phone` when the sender is linked; after storing
  on a player it dismisses the button with `remove_keyboard`.
- The share button is offered in the `/debt` reply for a linked player with no
  stored number, in the reply to `/identify` when the sender is already linked
  without a number, and by a new `/phone` slash command in a private chat.
- `/phone` for an unlinked user fails closed and points at `/identify`.
- Admins see the number in `/players` and in the agent's player listing; a
  player never sees another player's number.
- `content/bot-content.json` documents `/phone` in `help` and the system prompt
  explains that a phone is only ever stored from the sender's own contact card.

## Non-goals

- No phone number in `AIContextBuilder` output or in the agent's live snapshot:
  `linked_player` is reduced to `{'id', 'name'}` so adding the column cannot
  leak it.
- No SMS, no phone lookup, no reverse search, no change to the `contacts` schema.
- No change to identity approval: sharing a phone does not verify identity and
  does not bind an account. `/identify` and admin approval remain the only
  binding path.
- No change to `bale_id`, `players.name`, or any financial rule.
- The legacy JSON intent route still cannot reach `set_player_phone`; only the
  contact handler and the `/phone` command can.

## Contracts

`Service.normalize_phone(value) -> str`:

- raises `UserError` for a non-string, empty, non-digit, too short, too long, or
  non-mobile value;
- returns exactly `'+98' + <10 digits starting with 9>`;
- never guesses a missing digit.

`Service.set_player_phone(player_id, phone, actor) -> dict`:

- returns `{'id', 'name', 'phone', 'changed'}`;
- writes `players.phone` and one `audit_logs` row only when the value changes;
- the audit row records the player id, never the number itself.

`Bot.receive_contact(contact, user_id, chat_id, state) -> (reply, keyboard)`:

- keeps the existing ownership guard: `contact['user_id']` must equal the sender
  id, otherwise nothing is stored and `UserError` is raised;
- returns `hide_keyboard()` only after a linked player's number was stored, and
  `None` otherwise.

`Database.say(chat_id, text, buttons=None, ref=None, keyboard=None)`:

- `keyboard` is sent verbatim as `reply_markup` on the last chunk and takes
  precedence over `buttons`; existing positional callers are unchanged.

## Acceptance Criteria

- [x] AC-001: The five common Iranian formats normalize to `+989121234567`.
- [x] AC-002: An invalid or non-mobile number is rejected and stores nothing.
- [x] AC-003: A linked player's contact card is stored on `players.phone` and in `contacts.phone`.
- [x] AC-004: An unlinked sender's contact card is stored only in `contacts`.
- [x] AC-005: A contact card for another user is rejected and no player row changes.
- [x] AC-006: The share button payload contains `request_contact: true` and no undocumented keyboard keys.
- [x] AC-007: Storing a number on a linked player dismisses the button with `remove_keyboard`.
- [x] AC-008: `/phone` reports the stored number and fails closed for an unlinked user.
- [x] AC-009: Sending the same number twice writes one audit row.
- [x] AC-010: The phone number never appears in the model context or agent snapshot.
- [x] AC-011: Admins see the number in `/players`; players never do.

## Test Mapping

- AC-001 -> `tests/test_sans.py::PlayerPhoneTests::test_phone_normalization_accepts_iranian_formats`
- AC-002 -> `tests/test_sans.py::PlayerPhoneTests::test_phone_normalization_rejects_invalid_numbers`
- AC-003 -> `tests/test_sans.py::PlayerPhoneTests::test_linked_player_contact_is_stored_on_the_player`
- AC-004 -> `tests/test_sans.py::PlayerPhoneTests::test_unlinked_contact_stays_in_the_address_book_only`
- AC-005 -> `tests/test_sans.py::PlayerPhoneTests::test_contact_for_another_user_never_reaches_a_player`
- AC-006 -> `tests/test_sans.py::PlayerPhoneTests::test_share_button_uses_documented_keyboard_only`
- AC-007 -> `tests/test_sans.py::PlayerPhoneTests::test_storing_a_number_dismisses_the_button`
- AC-008 -> `tests/test_sans.py::PlayerPhoneTests::test_phone_command_reports_and_fails_closed`
- AC-009 -> `tests/test_sans.py::PlayerPhoneTests::test_setting_the_same_phone_again_is_idempotent`
- AC-010 -> `tests/test_agent_contracts.py::PlayerPhoneContextTests::test_phone_is_not_part_of_the_model_context`
- AC-011 -> `tests/test_sans.py::PlayerPhoneTests::test_admin_player_list_shows_the_number`

## Safety Constraints

- Invalid or unauthorized requests fail closed; a rejected contact writes
  nothing.
- The number is only accepted from the sender's own contact card; a forwarded
  card is refused.
- The model and `AIContextBuilder` never receive a phone number.
- Financial and persistence behavior is unchanged; `players.phone` is the only
  new column.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- Decision: numbers are stored in E.164 (`+98…`) so `/players`, the agent tool,
  and any future export agree on one format. Foreign numbers are refused rather
  than stored in an inconsistent shape; that is a deliberate limitation for an
  Iran-only bot.
- Decision: the button is a reply keyboard, because a contact can only be
  requested by `KeyboardButton.request_contact`. It is therefore sent as a new
  message, never by `editMessageText`, which cannot attach a reply keyboard.
- Limitation: Bale offers no way for the bot to read a number without the button
  press, so an admin cannot backfill `players.phone` for an unlinked player.
  `/bind` still links the account first.
- Validation (2026-10-22): 205 tests pass (1 pre-existing skip). The migration
  was dry-run on a copy of `data/sans.sqlite3`: only `players.phone` was added,
  all row counts and the 26 existing player rows were unchanged, and no number
  was backfilled.
