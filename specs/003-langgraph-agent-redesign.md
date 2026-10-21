# SPEC-003: Conversational LangGraph Agent and Minimal Flexible Schema

Status: verified
Owner: agent
Created: 2026-10-16

## Problem

The previous architecture forced every conversational turn through a rigid
single-action intent extractor, withheld the player directory and group chat
history from the model, enforced strict timestamp triggers on `sessions` that
blocked historical/incomplete session imports, and replied with hardcoded
Python templates instead of natural assistant responses.

## Goal

Introduce a minimal, standard LangGraph-based conversational agent (`StateGraph`
with tool calling) backed by a clean, flexible SQLite schema that supports
incomplete/historical sessions, full group/private chat memory, direct
autonomous management of non-financial records, and human-in-the-loop admin
confirmation for sensitive financial mutations.

## Scope

- Remove the `sessions_require_session_at` database trigger and allow sessions
  with optional/historical dates and zero or unset fees.
- Add `chat_messages` table to record group and private chat history so the
  agent understands multi-user context.
- Implement a LangGraph `StateGraph` agent (`app/agent.py`) with structured
  tools for querying data, managing players/sessions/attendance (including bulk
  historical session entry), proposing financial ledger changes for admin
  approval, and asking admins clarifying questions.
- Allow the AI agent to generate natural Persian responses directly after
  executing tools while preserving deterministic slash commands and approval
  callbacks.

## Non-goals

- Removing double-entry/integer ledger safety or allowing unapproved financial
  mutations from non-admin users.
- Breaking existing deterministic CLI tools (`--init`, `--backup`).

## Contracts

- `Database` initializes the minimal schema without the `sessions_require_session_at`
  trigger and provides `log_message()` and `recent_messages()` helpers for chat
  context.
- `SansAgent` builds a LangGraph `StateGraph` (`agent` node and `tools` node)
  that can execute multiple tool calls per turn and return a natural Persian
  response.
- Sensitive financial mutations (`finalize`, `adjustment`, `settle`,
  `declare_debt`) requested conversationally create a `pending` approval card
  with inline Yes/No buttons in the admin group rather than mutating the ledger
  silently without confirmation.
- Historical or incomplete sessions (missing exact time, location, or fee) can
  be recorded directly via the agent or service layer.

## Acceptance Criteria

- [x] AC-001: Sessions can be created without `session_at` or `when_text` and
  with `person_fee >= 0` to support historical and incomplete session data.
- [x] AC-002: Group and private messages are logged in `chat_messages` and
  exposed to the agent context for situational awareness.
- [x] AC-003: The LangGraph agent can call read and write tools across multiple
  steps and return a natural conversational response.
- [x] AC-004: Sensitive financial changes create pending admin confirmation
  proposals and only mutate `ledger` after approval.
- [x] AC-005: Every incoming message, skipped message reason, LLM tool call, and
  agent reply is recorded in `agent_logs` (and `data/logs/agent.log`) and the
  legacy JSON-only prompt is excluded from `SansAgent`.
- [x] AC-006: The LangGraph and legacy AI prompts constrain unrelated requests to
  the football-session scope; LangGraph must not call a tool, and the legacy
  extractor must return `action=clarify` with the fixed Persian scope response.

## Test Mapping

- AC-001 -> `tests/test_sans.py`
- AC-002 -> `tests/test_sans.py`
- AC-003 -> `tests/test_agent_contracts.py`
- AC-004 -> `tests/test_sans.py`
- AC-006 -> `tests/test_agent_contracts.py::AgentContractTests::test_ai_prompts_reject_unrelated_requests_without_tools`

## Safety Constraints

- Non-admin users cannot execute admin write tools or view private admin notes.
- Financial mutations remain integer-only in `ledger` and require admin
  confirmation when triggered via pending proposals.
- Secrets and `.env` values are never included in model context.
- This prompt-level boundary does not pre-filter messages before the model call;
  unrelated messages may still consume model tokens, but they must not receive
  an out-of-scope answer or trigger an agent tool through the defined prompts.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- SPEC-007 supersedes the implicit player creation in `set_attendance`; the
  agent now asks for a full name instead of adding a new player row.
