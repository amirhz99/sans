# SPEC-034: an unknown command says so

Status: verified  
Owner: agent  
Created: 2026-10-28

## Problem

`command()` ended with `return {'action': 'help', 'args': {}}`. Any text starting
with `/` that did not match a known name therefore answered with the full help
guide — `/bogus`, `/`, `//`, and every admin command typed by a player. The reply
looked like a success, so a typo, an old command name, or a command the speaker
is not allowed to use all read as "done", and the actual mistake was never
visible.

The same shape hid a second problem: a known command sent without its argument
(`/identify`, `/amount`, `/confirm`, `/approve`, `/reject`) built `args: {}` and
only failed because the handler happened to raise. Correctness rested on every
handler remembering to reject an empty value.

## Goal

A command the bot does not have, or a command missing its argument, answers with
one specific line that names the problem, instead of the whole guide or a
handler-specific error.

## Scope

- An unmatched command name raises `UserError(UNKNOWN_COMMAND)` instead of
  returning the help action.
- The commands that need a value refuse an empty one and print their own usage
  line, so the answer names what is missing.
- Command names match without regard to case, and the `@name` suffix that
  addresses the bot in a group keeps working.

## Non-goals

- No privacy change. An admin command typed by a player is still refused, and
  the refusal must not disclose that the command exists for someone else; it
  reads exactly like a genuinely unknown command.
- No change to which command does what, to permissions, or to side effects.
- `/help` still answers with the guide; it is a known command.
- Argument-less commands (`/help`, `/card`, `/pending`, `/expire`) are unchanged;
  `/debt` and `/session` may still omit their optional argument.

## Contracts

- `Bot.command(body, admin)` returns an action dict for a known command, and
  raises `UserError` when the name is unknown or a required value is absent.
- `Bot.local_answer()` already treats `UserError` as an immediately answerable
  route, so a refusal keeps its no-processing-notice behaviour (SPEC-033).
- Group routing is unchanged: a slash command from a non-admin in a group is
  refused earlier, by `message()`, and never reaches this path.
- The public name is derived from the first token only; arguments are untouched.

## Acceptance Criteria

- [x] AC-001: an unknown command answers "این دستور را نمی‌شناسم" and does not
      return the guide.
- [x] AC-002: `/` and `//` are treated as unknown, not as help.
- [x] AC-003: an admin command used by a player is refused, and creates no
      player, proposal, or other side effect.
- [x] AC-004: an unknown command creates no proposal in the admin group.
- [x] AC-005: a command missing its required value names that command's usage.
- [x] AC-006: command names match without regard to case.
- [x] AC-007: `/help@SansBot` still returns the guide.
- [x] AC-008: an unknown command sends no processing notice.

## Test Mapping

- AC-001 -> `tests/test_sans.py::UnknownCommandTests::test_an_unknown_command_is_named_as_unknown`
- AC-002 -> `tests/test_sans.py::UnknownCommandTests::test_a_bare_slash_is_unknown_rather_than_the_guide`
- AC-003 -> `tests/test_sans.py::UnknownCommandTests::test_an_admin_command_used_by_a_player_is_refused_not_guided`
- AC-004 -> `tests/test_sans.py::UnknownCommandTests::test_an_unknown_command_creates_no_proposal_and_no_side_effect`
- AC-005 -> `tests/test_sans.py::UnknownCommandTests::test_a_missing_argument_names_what_is_missing`
- AC-006 -> `tests/test_sans.py::UnknownCommandTests::test_command_names_ignore_case`
- AC-007 -> `tests/test_sans.py::UnknownCommandTests::test_a_known_command_still_works_with_its_mention`
- AC-008 -> `tests/test_sans.py::UnknownCommandTests::test_an_unknown_command_gets_no_processing_notice`

## Safety Constraints

- Invalid or unauthorized requests fail closed.
- A refused command must not reveal that the command exists for another role.
- No model call is involved; a refusal never reaches the AI.
- Financial and persistence behavior is unchanged.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- The refusal is deliberately identical for "no such command" and "that command
  is not available to you". Telling a player that `/setcard` exists but is not
  theirs would advertise the admin surface, and the existing permission boundary
  is already enforced before and inside each admin branch.
- Case-insensitive matching was added because the new refusal made the old
  behaviour worse: `/Help` used to return the guide, which at least looked like
  an answer. Persian keyboards do not produce capital Latin letters, but a phone
  that auto-capitalises the first word, or a copy from a document, does.
- The argument checks cover only the commands whose value is mandatory. Adding
  the check everywhere would have duplicated the parsers that already validate
  numbers and prefixes, so those keep their own messages.
- `SettingsApprovalTests.test_setcard_requires_admin_and_explicit_value` used to
  assert the old behaviour (a player sees the guide). Only that assertion
  changed; the intent — a non-admin never reaches `/setcard` — is unchanged and
  still checked through the absence of a proposal and of a stored card.