# SPEC-020: Admin powers in a private chat, and an explicit phone entry

Status: verified
Owner: agent
Created: 2026-10-22

## Problem

`ADMIN_ACCESS` is parsed by `Config.from_env` and then never used, and
`Bot.is_admin` only compares the chat id with the admin group. Two consequences
that an admin hits immediately:

- **The admin is an ordinary player in private.** `/report`, `/pending`,
  `/players`, `/defaults`, `/expire` and every approval card answer «فقط برای
  ادمین‌های مجاز در گروه مدیریتی است», even for the person who owns the bot.
- **A number can only arrive from the player.** SPEC-016 requires the
  «ارسال شمارهٔ من» button, and SPEC-019 makes the bot ask for it, but an admin
  who already knows a number has no way to record it: `players.phone` is written
  only by `receive_contact`.

## Goal

An allowlisted admin keeps the same powers in a private chat with the bot, and
can record a phone number that the admin typed in that same message. A
non-allowlisted user still has no admin power anywhere except inside the
configured admin group, and a number that is not written in the current message
is refused.

## Scope

- `Bot.is_admin(chat_id, user_id)`:
  - the configured admin group keeps its current meaning: any member of that
    group is an admin (unchanged, and pinned by
    `test_config_does_not_require_user_allowlist_for_group_access`);
  - a private chat (`chat_id == user_id > 0`) is admin only when
    `admin_access == 'allowlist'` and `user_id in config.admins`.
- `ADMIN_ACCESS=group_members` therefore means "group only": no private admin.
- `Bot.proactive_answer` does not relay an admin's own reply back to the admin
  group.
- New `manage_records` operation `set_phone` (admin only):
  `data_json='{"player": "#15", "phone": "09121234567"}'`.
- `AGENT_SYSTEM_PROMPT` and the `manage_records` docstring mention `set_phone`.
- `.env` gains `ADMIN_USER_IDS` and `ADMIN_ACCESS=allowlist` (deployment step,
  not committed).

## Non-goals

- No change to the group boundary: a member of another group never becomes an
  admin.
- No fail-fast for an empty `ADMIN_USER_IDS` under `allowlist`; that combination
  simply grants no private admin, which keeps
  `test_config_does_not_require_user_allowlist_for_group_access` valid.
- No phone number in the AI snapshot, no number typed by a player (SPEC-016
  keeps the button), and no change to financial rules.
- No new dependency and no schema change.

## Contracts

`is_admin(chat_id, user_id) -> bool`

- `False` for a missing, boolean, or non-positive id;
- `True` when `chat_id == config.admin_group`;
- otherwise `True` only when `chat_id == user_id > 0`,
  `config.admin_access == 'allowlist'`, and `user_id in config.admins`.

`set_phone`:

- refuses an unknown key, a missing player, and a non-string phone;
- canonicalises through `Service.normalize_phone`, so an invalid Iranian mobile
  number is refused;
- requires the last ten digits of the number to appear in the current user
  message; a number from an earlier turn or from memory is refused;
- is idempotent through `Service.set_player_phone`: the same value writes
  nothing and records no second audit row.

## Acceptance Criteria

- [x] AC-001: an allowlisted admin is an admin in a private chat and `/report` works there.
- [x] AC-002: a non-allowlisted user is not an admin in a private chat.
- [x] AC-003: `admin_access='group_members'` grants no private admin power.
- [x] AC-004: any member of the configured admin group is still an admin.
- [x] AC-005: `set_phone` refuses a number that is not written in the current message.
- [x] AC-006: `set_phone` stores a canonical number for an admin and is idempotent.
- [x] AC-007: `set_phone` is refused for a non-admin and writes nothing.
- [x] AC-008: an admin's private reply to a proactive message is not relayed to the admin group.

## Test Mapping

- AC-001 -> `tests/test_sans.py::PrivateAdminTests::test_allowlisted_admin_manages_in_private`
- AC-002 -> `tests/test_sans.py::PrivateAdminTests::test_other_users_stay_players_in_private`
- AC-003 -> `tests/test_sans.py::PrivateAdminTests::test_group_members_policy_grants_no_private_admin`
- AC-004 -> `tests/test_sans.py::PrivateAdminTests::test_group_membership_is_still_the_group_boundary`
- AC-005 -> `tests/test_sans.py::PrivateAdminTests::test_set_phone_requires_the_number_in_the_current_message`
- AC-006 -> `tests/test_sans.py::PrivateAdminTests::test_set_phone_stores_and_is_idempotent`
- AC-007 -> `tests/test_sans.py::PrivateAdminTests::test_set_phone_is_admin_only`
- AC-008 -> `tests/test_sans.py::PrivateAdminTests::test_admin_reply_to_outreach_is_not_relayed`

## Safety Constraints

- Only the configured group or an allowlisted user id can act as an admin.
- `set_phone` cannot invent a number: the digits must be in the current message.
- Numbers stay out of the AI snapshot and `agent_logs`.
- Financial rules, the ledger, and the receipt flow are unchanged.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- Decision: the group stays "any member" instead of switching to the allowlist.
  Two existing tests pin that behavior
  (`test_config_does_not_require_user_allowlist_for_group_access` and
  `test_all_group_members_can_manage_only_in_designated_group`), and changing it
  would silently remove access from the other admins in the group.
- Decision: `test_proposal_requires_confirmation_and_callback_is_scoped` now
  clicks from user `77` in chat `77` instead of user `1` in chat `1`. The
  property it proves (a callback outside the admin boundary cannot apply) is
  unchanged; the old fixture only worked because private chat `1` was never an
  admin chat, and after this spec an allowlisted id is.
- Limitation: the allowlist is per user id, so a second admin in the group who
  wants private powers must be added to `ADMIN_USER_IDS`.

## Validation Result

- `python3 -m unittest discover -s tests -t . -v`: 263 tests, OK (1 pre-existing
  skip).
- `python3 -m compileall -q app tests`: OK.
- `git diff --check`: clean.
- Deployment: `.env` now has `ADMIN_USER_IDS=1590533468` and
  `ADMIN_ACCESS=allowlist` (previous file kept at `/tmp/env.backup.*`). With the
  real config, `is_admin(1590533468, 1590533468)` is `True`, a member of the
  admin group is still an admin, and a stranger's private chat is not.
- Not verified live: a private `/report` from the admin's own chat needs a real
  message from him; the bot was restarted with this code at 16:53.
