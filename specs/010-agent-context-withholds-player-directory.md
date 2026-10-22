# SPEC-010: Withhold the complete player directory from agent context

Status: verified
Owner: agent
Created: 2026-10-21

## Problem

`SansAgent._build_live_snapshot` places `players_summary` — every player id,
name, and (for admins) live balance — into the system prompt on every turn, and
sends that prompt to a third-party model endpoint. `AGENTS.md` explicitly says:
"Do not send the complete player directory, secrets, receipt contents, `.env`
values, or unrelated history to the model." The legacy intent path already
withholds it (`app/ai_context.py` documents "never send the complete player
directory"), so the LangGraph path silently violates the project contract and
also inflates every prompt with data most turns never use.

## Goal

The model receives only the caller's own identity plus aggregate counts, and
fetches player names or balances through `query_database` when a turn actually
needs them.

## Scope

- Replace `players_summary` in the live snapshot with `players_count`.
- Tell the agent, in `AGENT_SYSTEM_PROMPT`, that the directory is not preloaded
  and must be read with `query_database` target `players`.
- Keep `caller.linked_player`, sessions, settings, and the bounded current-chat
  history unchanged.

## Non-goals

- No change to `query_database('players')` output, permissions, or tool
  arguments.
- No change to the legacy `AIContextBuilder` path.
- No change to the bounded `recent_chat_messages` window (10 messages of the
  current chat), which is task-related context for the same conversation.
- No change to financial rules, ledger semantics, or schema.

## Contracts

- The JSON snapshot keeps its existing keys except `players_summary`, which is
  replaced by the integer `players_count`.
- A player role still sees `caller.linked_player` for its own record; no other
  player's name, note, id, or balance appears in the prompt.
- An admin can still list players by calling `query_database` with
  `target='players'`; the tool remains the only path to the directory.
- `SansAgent.run` keeps its signature and its system-prompt shape.

## Acceptance Criteria

- [ ] AC-001: An admin snapshot contains `players_count` and no other player's
  name, note, id, or balance.
- [ ] AC-002: A non-admin snapshot likewise contains no player directory.
- [ ] AC-003: The caller's own linked player stays available to the model.
- [ ] AC-004: `query_database('players')` still returns the directory on demand.

## Test Mapping

- AC-001 -> `tests/test_agent_contracts.py::AgentContractTests::test_agent_snapshot_withholds_complete_player_directory`
- AC-002 -> `tests/test_agent_contracts.py::AgentContractTests::test_agent_snapshot_withholds_complete_player_directory`
- AC-003 -> `tests/test_agent_contracts.py::AgentContractTests::test_agent_snapshot_withholds_complete_player_directory`
- AC-004 -> `tests/test_agent_contracts.py::AgentContractTests::test_agent_snapshot_withholds_complete_player_directory`

## Safety Constraints

- Removing data from the prompt cannot grant a permission or cause a write.
- Player notes and balances never leave the host unless an admin explicitly
  queries the directory, and even then only through the existing tool output.
- Secrets, `.env` values, and receipt media are untouched by this change.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- Follow-up (not in scope): the snapshot still includes the club card number and
  payment note because the agent needs them to answer payment questions; moving
  them behind a tool would remove them from the prompt entirely.
