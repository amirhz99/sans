# SPEC-021: A deterministic first contact, and a phone number that survives approval

Status: verified
Owner: agent
Created: 2026-10-22

## Problem

A brand-new user who opens the bot gets no onboarding at all. The first reply
comes from the model, so it depends on an external API call and on the model's
choice of words: sometimes a long introduction, sometimes internal vocabulary
(«وصل شدن حساب به بازیکن‌ها», «تطبیق هویت»), and if the provider fails the very
first message a stranger sees is
«سرویس هوش مصنوعی فعلاً پاسخ معتبر نداد». The bot also never tells the user
what to do next, and nobody asks for the phone number that SPEC-016 introduced
except after an approval card.

Two data bugs make the gap worse:

- **The number is lost at approval.** `receive_contact` stores the number in
  `contacts` and in the pending payload, but `Bot.confirm` only calls
  `service.bind()` and discards `payload['phone_number']`. Live proof:
  pending `9b7898198d6e47ac` (kind `identity`, approved) carries
  `phone_number=+989366371562` for player #16, while `players.phone` is still
  `''` and no `set_player_phone` audit row exists.
- **The agent path drops the number too.** `bot.identify(name, user_id,
  chat_id, sender)` is called from `manage_records` without a phone, so a card
  the model builds shows «شماره: ارسال نشده» even when `contacts.phone` is
  known.

## Goal

A first private message from an unknown user is answered by the bot itself,
deterministically, with exactly the two things it needs: the full name and the
phone number. The model is not involved in that message. From the second
message on, the model runs the conversation as before. Any number the user
shared is attached to the player when the admin approves the identity, and the
address book keeps the latest name and username without ever losing a stored
value to an empty profile field.

## Scope

- `Bot.welcome(user_id) -> (text, keyboard)`: four deterministic branches from
  what the database already knows (linked + number, linked without number,
  number but no account, nothing).
- `Bot.first_contact(...)`: decides whether the welcome applies. It applies to
  a private chat, a non-admin sender, no contact/photo/document, no open
  receipt step, no open identity request, no `welcomed:<user_id>` marker, and a
  body that is either `/start` or not a slash command.
- `/start` and a bare greeting are answered with the welcome **only**; any
  other first question gets the welcome followed by the normal model answer.
- The marker `welcomed:<user_id>` in `settings` makes the welcome once per
  account. No schema change.
- `Bot.identify` resolves the number itself: an explicit `phone_number` first,
  otherwise `contacts.phone` for the same user id. Both the slash command and
  the `manage_records` `identify` operation share this path.
- `Bot.identity_note` lists up to three `Service.similar_players` candidates
  with their ids so the admin can `/link` without opening `/players`. The
  suggestion is advisory: no card is created from a guess, and the user is
  never asked to confirm a name the bot inferred.
- `Bot.confirm` stores `payload['phone_number']` on the newly linked player
  through `Service.set_player_phone`, and never overwrites an existing number.
- `receive_contact` for a user with no open identity request asks for the full
  name, dismisses the share keyboard, and records the marker.
- `Service.touch_contact` keeps a stored `display_name`/`username` when the
  incoming profile field is empty.
- `acknowledgement_chat` returns `None` for `/start`, because the welcome is
  immediate.
- The agent snapshot gains `sender_name` (the full profile name),
  `needs_introduction` and `has_pending_identity`, plus two prompt lines that
  tell the model not to repeat the introduction and to look the name up
  before calling `identify`.

## Non-goals

- No card is built from a name guess. A player is only proposed after the user
  typed a name (or the model asked and the user agreed).
- The bot never asks for `username`; it reads it from the profile.
- No change to financial rules, the ledger, the schema, the group boundary, or
  the receipt flow.
- No phone number, `contacts` row, or player directory goes to the model.
- No new dependency.

## Contracts

`Bot.welcome(user_id) -> (str, dict | None)`

- linked player with a stored number → plain welcome, no keyboard;
- linked player without a number → welcome that asks for the number only, with
  the contact keyboard;
- no linked player but `contacts.phone` exists → asks for the full name only,
  no keyboard;
- otherwise → asks for the full name and the number, with the contact
  keyboard.

`Bot.first_contact(msg, chat_id, user_id, admin, private, body, state)`

- returns `None` when any guard fails, so no message and no marker;
- otherwise returns `(reply, keyboard, terminal)` and the caller writes the
  marker;
- `terminal` is `True` for `/start`, an empty body, or a bare greeting.

`Bot.identify(name, user_id, chat_id, sender=None, phone_number=None)`

- unchanged refusals: an already linked account, an empty name, an open
  identity request;
- an explicit number is canonicalised with `normalize_phone`; an invalid one is
  dropped instead of attached;
- when no number is passed, `contacts.phone` for `user_id` is used;
- the card also lists up to three similar names as `/link <id> <player>`.

`Bot.confirm` for `kind in ('identity', 'binding')`

- binds first, then stores `payload['phone_number']` when present, valid, and
  the player has no number yet;
- a failure to store the number never rolls back the binding.

`Service.touch_contact`

- `display_name` and `username` use `COALESCE(NULLIF(excluded.x, ''), contacts.x)`,
  matching the existing `phone` rule.

## Acceptance Criteria

- [x] AC-001: the first private message of an unknown user asks for the full name and the number with the contact button.
- [x] AC-002: the welcome is sent once per account and not again on later messages.
- [x] AC-003: a linked player without a number is asked for the number only.
- [x] AC-004: a linked player with a number gets a plain welcome and no contact button.
- [x] AC-005: a known number without a linked account asks for the name only.
- [x] AC-006: `/start` from a new user returns the welcome instead of the help text.
- [x] AC-007: an identity request as the first message gets no welcome.
- [x] AC-008: an admin in private, and any group message, never gets the welcome.
- [x] AC-009: a command after the welcome is answered normally and does not resend the welcome.
- [x] AC-010: sharing a number before the name asks for the full name and dismisses the keyboard.
- [x] AC-011: an identity card built through the agent tool carries the saved contact number.
- [x] AC-012: approving an identity card stores its number on the player and records an audit row.
- [x] AC-013: approving a card never overwrites a number the player already has.
- [x] AC-014: a later message with no username does not erase the stored username.
- [x] AC-015: the welcome is deterministic and makes no model call.
- [x] AC-016: the identity card lists similar player names with ids for `/link`.

## Test Mapping

- AC-001 -> `tests/test_sans.py::FirstContactTests::test_first_private_message_asks_for_a_name_and_a_number`
- AC-002 -> `tests/test_sans.py::FirstContactTests::test_welcome_is_sent_once_per_account`
- AC-003 -> `tests/test_sans.py::FirstContactTests::test_linked_player_without_a_number_is_asked_for_the_number_only`
- AC-004 -> `tests/test_sans.py::FirstContactTests::test_complete_account_gets_a_plain_welcome_without_the_button`
- AC-005 -> `tests/test_sans.py::FirstContactTests::test_known_number_asks_for_the_name_only`
- AC-006 -> `tests/test_sans.py::FirstContactTests::test_start_of_a_new_user_is_the_welcome_not_the_help`
- AC-007 -> `tests/test_sans.py::FirstContactTests::test_identity_request_as_a_first_message_gets_no_welcome`
- AC-008 -> `tests/test_sans.py::FirstContactTests::test_admin_and_group_messages_never_get_the_welcome`
- AC-009 -> `tests/test_sans.py::FirstContactTests::test_a_command_after_the_welcome_is_answered_normally`
- AC-010 -> `tests/test_sans.py::FirstContactTests::test_contact_before_the_name_asks_for_the_full_name`
- AC-011 -> `tests/test_sans.py::FirstContactTests::test_agent_identify_card_carries_the_saved_number`
- AC-012 -> `tests/test_sans.py::FirstContactTests::test_approval_stores_the_number_the_card_carried`
- AC-013 -> `tests/test_sans.py::FirstContactTests::test_approval_does_not_overwrite_an_existing_number`
- AC-014 -> `tests/test_sans.py::FirstContactTests::test_a_message_without_a_username_keeps_the_stored_one`
- AC-015 -> `tests/test_sans.py::FirstContactTests::test_welcome_makes_no_model_call`
- AC-016 -> `tests/test_sans.py::FirstContactTests::test_identity_card_lists_similar_names_for_the_admin`

## Safety Constraints

- The welcome contains no player data, no balance, and no internal vocabulary;
  it asks only for a name and a number.
- A guess never links, renames, or creates a player; only an admin approval or
  `/link` does.
- The number is read from `contacts` for the same `user_id` only; a contact
  card for another user is still refused (SPEC-016).
- An invalid or missing number is skipped, so approval still succeeds.
- Numbers stay out of the model snapshot, `agent_logs`, and conversation
  history.
- Financial rules, the ledger, and the schema are untouched.


## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- Decision: the welcome is code, not the model. A first impression must not
  depend on an external API or on the model's wording, and it must be testable
  with an exact assertion.
- Decision: the guess is advisory and lives on the admin card. Live data shows
  why: with 26 players, «امیرحسین» matches two players, «علی» three, «محمد»
  two, «حسین» two. A deterministic guess would frequently pick a person the
  user is not, and the user would then see somebody else's debt.
- Decision: `BaseCase.send` sets the `welcomed:<user>` marker by default and
  takes `first_contact=True` to leave it unset. Every existing test keeps its
  original path; only the new class exercises onboarding.
- Decision: two existing assertions change because the behaviour changed:
  `test_private_start_gives_private_user_guidance_only` now sends `/start`
  twice (welcome, then help) and
  `test_unlinked_contact_stays_in_the_address_book_only` expects the new
  «شماره‌ات ثبت شد … نام کاملت را بنویس» wording instead of `/identify`.
- Limitation: most Bale profiles carry only a given name, so the model will
  usually ask for the full name; that is expected, not a defect.
- Limitation: the model-driven path depends on the model choosing to call
  `identify`; the `/identify` command remains the deterministic fallback.
- Deployment: the live row for player #16 is corrected with
  `Service.set_player_phone(16, '+989366371562', 'migration:spec-021')` after a
  database backup, because the approved card already carried the number.
- Decision: the marker is per account, not per "unknown account", so existing
  players also see the welcome once on their next private message. For a linked
  player without a number that is exactly the SPEC-019 reminder; for a complete
  account it is a single line with the command shortcuts. Admins are excluded.

## Validation Result

- `python3 -m unittest discover -s tests -t . -v`: 282 tests, OK (1
  pre-existing skip). The 16 `FirstContactTests` and 3
  `OnboardingAgentContextTests` are new.
- `python3 -m compileall -q app tests`: OK.
- `git diff --check`: clean.
- Live data: backup at `data/sans-backup-20261022-215515.sqlite3`; player #16
  now has `+989366371562` and one `set_player_phone` audit row by
  `migration:spec-021`. The bot was stopped, patched, and restarted under
  `systemctl --user` (`active`, «Bot started; polling enabled»).
- Behaviour smoke test on a temporary database: a first question gets the
  welcome and then the model answer; the second message gets no second
  welcome; `/start@<bot>` is the welcome only; `/help` as the very first
  message is answered normally and leaves the marker unset.

