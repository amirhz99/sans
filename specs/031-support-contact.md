# SPEC-031: Configurable support account

Status: verified
Owner: agent
Created: 2026-10-28

## Problem

The bot can refuse or fail: the request is outside its topic boundary, the
model cannot resolve it, or the AI service returns an error. In every one of
those cases the person is left with no way forward — the current API-failure
reply (`app/bot.py:939`) says only "the AI service did not answer; try again".
There is no configured human contact, so the operator's handle is either
unknown to the user or hard-coded nowhere at all. Sending a wrong or invented
handle is worse than sending none.

## Goal

An operator can configure one support handle with `SUPPORT_HANDLE`. When a user
cannot be helped by the bot and asks who to contact, the bot names exactly that
handle, verbatim, in the agent reply and through a `/support` command. With no
handle configured the bot points at the admin group instead of inventing a
contact.

## Scope

- `Config.support_handle` is parsed from `SUPPORT_HANDLE`, normalised to the
  single canonical form `@name`, and defaulted to `''` (feature disabled).
- `amirhz99` and `@amirhz99` are both accepted and stored as `@amirhz99`.
- An invalid value (missing name, spaces, punctuation, a URL, `t.me/...`,
  a bare `@`, or a length outside 3–64) raises at startup, like the other
  configuration errors in `Config.from_env`.
- `AGENT_SYSTEM_PROMPT` carries a support rule whose `{support_contact}`
  placeholder is filled with the configured handle, or with a fallback phrase
  when nothing is configured.
- `/support` answers with the configured handle, and with the admin-group
  fallback when it is unset.
- `.env.example`, `.env` and the README table document the new key.

## Non-goals

- The AI-unavailable reply (`app/bot.py:939`) stays byte-identical. The handle
  is given only on an explicit request, never appended to an error message.
- No new tool, action, table, column or migration.
- No change to financial rules, ledger semantics, routing, or response levels.
- No notification to the support account: the bot never messages the handle.

## Contracts

- `parse_support_handle(raw) -> str` returns `''` or `@name`; it raises
  `ValueError` for any other non-empty value. It accepts a non-`str` input by
  raising, so a `None` from the environment cannot leak into the prompt.
- `support_contact(bot) -> str` reads `bot.config.support_handle` through
  `getattr` and returns `''` when the attribute is absent, so lightweight
  config stand-ins used by tests keep working.
- `Config` keeps `support_handle: str = ''` as the last field with a default;
  every existing positional call site stays valid.
- `AGENT_SYSTEM_PROMPT` stays a module-level constant with the `{bot_name}`
  and `{support_contact}` placeholders. The handle is inserted by `str.format`,
  so a value containing `{` would break formatting; the parser forbids it.
- `/support` maps to the `support` action in `command()`. In a group it follows
  the existing rule that slash commands are admin-only.

## Acceptance Criteria

- [x] AC-001: `SUPPORT_HANDLE=@amirhz99` and `SUPPORT_HANDLE=amirhz99` both
  produce `Config.support_handle == '@amirhz99'` through `from_env`.
- [x] AC-002: An invalid `SUPPORT_HANDLE` value raises `ValueError` at startup
  instead of reaching the prompt.
- [x] AC-003: A missing or empty `SUPPORT_HANDLE` leaves support disabled and
  the prompt offers no handle.
- [x] AC-004: `/support` answers with the configured handle verbatim.
- [x] AC-005: `/support` without a configured handle invents no `@` handle and
  refers the user to the admin group.
- [x] AC-006: The rendered agent prompt contains the configured handle and the
  instruction to give it verbatim.
- [x] AC-007: The AI-unavailable reply does not contain the support handle.

## Test Mapping

- AC-001 -> `tests/test_sans.py::SupportHandleTests::test_handle_is_normalized`
- AC-002 -> `tests/test_sans.py::SupportHandleTests::test_invalid_handle_fails_closed`
- AC-003 -> `tests/test_sans.py::SupportHandleTests::test_missing_handle_disables_support`
- AC-004 -> `tests/test_sans.py::SupportHandleTests::test_support_command_shows_the_handle`
- AC-005 -> `tests/test_sans.py::SupportHandleTests::test_support_command_without_a_handle`
- AC-006 -> `tests/test_agent_contracts.py::SupportPromptTests::test_prompt_offers_the_configured_handle`
- AC-007 -> `tests/test_sans.py::SupportHandleTests::test_api_failure_reply_stays_unchanged`

## Safety Constraints

- The handle is configuration, never model output: the model is told to copy
  the exact value and never to rewrite or invent one.
- An invalid configuration fails at startup, so a malformed value cannot be
  injected into the system prompt.
- No player data, receipt content, `.env` value or secret is added to the
  model context by this change.
- Financial behavior, ledger semantics and the SQLite schema are unchanged.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- The error path keeps its existing text by an explicit decision: a failed AI
  turn should not also advertise a contact, and the user can always ask.
- `parse_support_handle` strips a leading `@` before validating, so a copy of a
  handle from the Bale UI (`@name`) works without editing.
- Follow-up, not in scope: a configure-by-command path such as `/setsupport`.
  It needs admin approval-card semantics and was deliberately left out.
