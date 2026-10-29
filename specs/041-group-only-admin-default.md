# SPEC-041: Make group-only admin the default policy

Status: verified  
Owner: agent  
Created: 2026-10-29

## Problem

`ADMIN_ACCESS` had `allowlist` as its default, both in the `Config` dataclass and
in `Config.from_env`. That is the policy with the *larger* attack surface: under
it, every id in `ADMIN_USER_IDS` keeps full admin powers in a private chat with
the bot, where there is no group membership to check and no second pair of eyes.

An operator who never sets `ADMIN_ACCESS` therefore got private admin powers for
whoever happened to be left in the allow-list, possibly from a much earlier
experiment. The safe policy — admin only inside the configured group — required
opting *in*.

This surfaced while repointing the bot to a new admin group. The operator chose
group-only access and had to express it explicitly, which is the right intent but
the wrong default.

## Goal

An installation that never mentions `ADMIN_ACCESS` grants admin powers only to
members of `ADMIN_GROUP_ID`, and to nobody in a private chat.

## Scope

- Change the `Config.admin_access` dataclass default to `group_members`.
- Change the `Config.from_env` fallback to `group_members`.
- Align `.env.example` with the new default and explain when `allowlist` is
  wanted.
- Align the README table row for `ADMIN_ACCESS`.
- Keep `allowlist` fully supported and unchanged in behaviour.

## Non-goals

- No change to `Bot.is_admin`. The group check stays first; `admin_access` still
  governs *private* chats only. This specification changes a default, not the
  rule.
- No change to `ADMIN_USER_IDS` parsing, validation, or emptiness semantics.
- No schema, ledger, fee, or routing change.
- No automatic migration of an existing `.env`. An installation that already
  sets `ADMIN_ACCESS` keeps that value.

## Contracts

- With `ADMIN_ACCESS` unset, `Config.from_env().admin_access == 'group_members'`.
- With `ADMIN_ACCESS` unset and `ADMIN_USER_IDS` listing an id:
  - that user **is** an admin inside `ADMIN_GROUP_ID`;
  - that user is **not** an admin in a private chat (`is_admin(uid, uid)` is
    false for a positive `uid`).
- `ADMIN_ACCESS=allowlist` still produces `admin_access='allowlist'` and still
  grants private powers to allow-listed ids.
- Any value other than `allowlist` and `group_members` still raises `ValueError`.
- `Config(...)` constructed positionally without `admin_access` now gets
  `group_members`. Tests that relied on the old default and need private admin
  must state `admin_access='allowlist'` explicitly; two existing tests did, and
  one did not and was corrected.

## Acceptance Criteria

- [ ] AC-001: With `ADMIN_ACCESS` unset, the loaded config is group-only.
- [ ] AC-002: Under the default, an allow-listed user is an admin in the admin
  group but not in a private chat.
- [ ] AC-003: A private user whose id is allow-listed sees the player guide, not
  the admin guide, under `group_members`.
- [ ] AC-004: `allowlist` still grants the private admin guide to an allow-listed
  user.
- [ ] AC-005: An unknown `ADMIN_ACCESS` value is still rejected.

## Test Mapping

- AC-001 -> `tests/test_sans.py::OperationalTests::test_config_defaults_to_group_only_admin`
- AC-002 -> `tests/test_sans.py::OperationalTests::test_config_defaults_to_group_only_admin`
- AC-003 -> `tests/test_sans.py::ShortHelpTests::test_group_only_default_denies_private_admin`
- AC-004 -> `tests/test_sans.py::ShortHelpTests::test_private_admin_gets_only_the_admin_guide`
- AC-005 -> `tests/test_sans.py::OperationalTests::test_config_group_members_does_not_require_allowlist`

## Safety Constraints

- This change only *removes* authority from a misconfigured installation; it
  never adds any. No path gains admin where it had none: `allowlist` is still
  reachable, and the group boundary is untouched.
- Every existing allow-list test now states its policy explicitly, so no test
  can pass only because of an implicit default again.
- No database write, no schema migration, and no ledger or fee change.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Validation Result

- `python3 -m unittest discover -s tests -t .`: **472 tests, OK**, exit 0
  (470 before this change; 2 added, none skipped).
- `python3 -m compileall -q app tests`: OK, exit 0.
- `git diff --check`: clean, exit 0.
- Changing the default broke exactly one test,
  `ShortHelpTests::test_private_admin_gets_only_the_admin_guide`, which had
  leaned on the old implicit default. It was fixed by stating
  `admin_access='allowlist'` in the test rather than by weakening the assertion.
- Verified against the live configuration after the change:

  | check | result |
  |---|---|
  | `cfg.admin_access` | `group_members` |
  | member of the admin group | admin |
  | same user, private chat | not admin |
  | old group id | refused by `allowed_chat` |

## Notes

- The operator's stated intent was "everyone admin only inside the group; if I
  ever want private powers I will edit the code myself". `group_members` is
  exactly that, and it needed no code change at all — only the `.env` value.
  Making it the default means a fresh clone behaves that way by accident too.