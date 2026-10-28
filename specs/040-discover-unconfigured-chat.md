# SPEC-040: Reveal the id of an unconfigured chat

Status: verified  
Owner: agent  
Created: 2026-10-29

## Problem

Switching the bot from the test group to the real admin group is currently
impossible from inside the bot, and the failure is silent.

`Bot.allowed_chat` refuses every group whose `chat_id` is not
`config.admin_group` *before* any handler runs. That ordering is correct — it is
the fail-closed outer gate — but it makes `/id` unusable in exactly the
situation where it is needed. Measured on the live bot while repointing it:

| what | value |
|---|---|
| polling `offset` | 479 → **480** (one update really arrived) |
| `chat_messages.max(id)` | 434 → **434** (nothing recorded) |
| `agent_logs.max(id)` | 1032 → **1032** (nothing recorded) |
| outbound messages | **0** (no reply) |

The operator sent `/id` in «مالی فوتبال رضوان». The update was fetched,
refused, and dropped without a single trace. The bot cannot tell the operator
the id of a group precisely because the group is not yet configured.

`--discover-ids` does not solve this. It calls `getUpdates` itself while the
service is already polling, so the two compete for the same updates, and the
README already warns not to run it alongside the service.

## Goal

An operator can learn the `chat_id` of a group the bot was just added to,
without stopping the bot, and without the outer gate ever granting access,
replying, or executing anything in that group.

## Scope

- Add a `discover_chat` key in the `settings` table. Only the exact value `1`
  enables it; every other value, and the absence of the key, means off.
- When it is on, the refusal branch of `Bot.process` records one `REFUSED_CHAT`
  row in `agent_logs` containing the numeric `chat_id`, the chat type, and the
  chat title. Nothing else changes in that branch.
- Keep the existing bookkeeping: the update is still marked processed and the
  polling offset still advances.
- Document the option in the README as an operator procedure, including the
  step that removes the key again.

## Non-goals

- No new CLI flag, no new environment variable, and no change to `--discover-ids`.
- `allowed_chat`, `allowed_update`, `is_admin` and the routing order are
  untouched; this specification adds observation, not permission.
- Message text from a refused chat is **not** stored. Only the numeric id, the
  chat type and the title reach the diagnostic log.
- No change to the schema, the ledger, fees, or any financial rule.
- The key is not consumed automatically. Turning it off is an operator step, and
  leaving it on grants nothing because it only writes a log line.

## Contracts

- `Bot.process` refuses an unconfigured chat exactly as before. With
  `discover_chat` enabled or disabled, an update from such a chat:
  - produces no outbound message (`outbox` row count unchanged),
  - produces no `chat_messages` row,
  - produces no `audit_logs` row and no `revision` bump,
  - does not create a `pending` row,
  - leaves `is_admin(chat_id)` false for every member of that chat.
- `discover_chat` treats only the string `1` as enabled. `true`, `on`, `yes`,
  `0`, an empty string, and a missing key all mean off.
- A refused update whose payload has no usable integer `chat_id` (missing
  `message`, missing `chat`, `chat_id` a string or a bool) is skipped silently;
  the refusal itself still happens.
- The recorded summary is one line: `<chat_id> <chat_type> <title>`, with the
  title truncated to 60 characters. The title is the only operator-supplied
  text that reaches the log, and it is never interpreted.

## Acceptance Criteria

- [ ] AC-001: With `discover_chat` absent, an update from an unconfigured group
  leaves `agent_logs` unchanged and is still refused.
- [ ] AC-002: With `discover_chat = 1`, an update from an unconfigured group
  writes one `REFUSED_CHAT` row whose summary starts with the group id.
- [ ] AC-003: With `discover_chat = 1`, the discovery grants nothing: no
  outbound message is queued, no `chat_messages` row is written, `is_admin`
  stays false for every member, and no `pending` row appears.
- [ ] AC-004: Only the exact value `1` enables discovery.
- [ ] AC-005: A malformed payload is refused without a traceback and without a
  log row, even when discovery is enabled.
- [ ] AC-006: The authorised admin group is unaffected: with discovery enabled,
  an admin-group message is still handled normally and records no
  `REFUSED_CHAT`.

## Test Mapping

- AC-001 -> `tests/test_sans.py::DiscoverChatTests::test_refused_chat_is_silent_by_default`
- AC-002 -> `tests/test_sans.py::DiscoverChatTests::test_discover_chat_records_refused_group`
  and `test_discover_chat_records_the_title_when_the_update_carries_one`
- AC-003 -> `tests/test_sans.py::DiscoverChatTests::test_discover_chat_grants_no_access`
  and `test_discovery_survives_a_callback_from_an_unknown_chat`
- AC-004 -> `tests/test_sans.py::DiscoverChatTests::test_only_exact_value_one_enables_discovery`
- AC-005 -> `tests/test_sans.py::DiscoverChatTests::test_malformed_payload_is_ignored`
- AC-006 -> `tests/test_sans.py::DiscoverChatTests::test_admin_group_is_unaffected`

## Safety Constraints

- Discovery only writes to `agent_logs`, a diagnostic table. It cannot change
  the ledger, the roster, the settings revision, or any pending proposal.
- The refusal branch keeps its `BEGIN IMMEDIATE` transaction; the log row and
  the offset advance commit together with the processed-update marker.
- No `DELETE` or `UPDATE` is run against game data by this change.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Validation Result

- `python3 -m unittest discover -s tests -t .`: **470 tests, OK**, exit 0.
  The suite was 462 before this change; `tests/test_sans.py::DiscoverChatTests`
  adds 8. No test was skipped.
- `python3 -m compileall -q app tests`: OK, exit 0.
- `git diff --check`: clean, exit 0.
- Live evidence that motivated the change, captured on the running service:

  | what | before sending `/id` | after sending `/id` |
  |---|---|---|
  | polling `offset` | 479 | **480** |
  | `chat_messages.max(id)` | 434 | 434 |
  | `agent_logs.max(id)` | 1032 | 1032 |
  | outbound messages | 0 | 0 |

  The update really arrived and was really dropped without a trace, which is
  exactly the failure this specification names.
- The stage was also checked for blockers before the cutover: `pending` holds
  **0 open** rows (62 `expired`, 23 `approved`, 2 `rejected`), so no stale
  approval card can leak into the new group.

## Notes

- `_valid_int` was added next to `_valid_id` rather than reusing it: `_valid_id`
  is positive-only because it guards user ids, and a Bale group id is normally
  negative. Reusing it would have made discovery silently ignore every real
  group.
- The title is truncated to 60 characters and is the only operator-supplied
  text that reaches the log. Message text is never stored for a refused chat.
- `test_spec_contracts` requires the literal heading `## Test Mapping` and
  `- [ ] AC-00X:` bullets; the first draft used `## Test mapping` and bare
  AC bullets, and the contract suite caught it.