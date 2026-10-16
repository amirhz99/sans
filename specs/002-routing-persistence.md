# SPEC-002: Routing and conversation persistence hardening

Status: verified  
Owner: agent  
Created: 2026-10-16

## Problem

The current boundary contracts are in place, but routing still has legacy
branches and conversation persistence still stores a dictionary beside a short
history. Further changes need a focused contract so that migration work does
not accidentally broaden permissions or alter financial behavior.

## Goal

Make action routing and conversation-state persistence consistently use the
validated domain contracts while preserving all existing callers and stored
state formats.

## Scope

- Identify remaining routing branches that bypass `Intent` validation.
- Define one compatibility boundary for converting persisted state to and from
  `ConversationState`.
- Resolve follow-up entities through validated `last_entity` references.
- Add regression tests for malformed state, invalid actions, permission
  boundaries, and no-side-effect failure paths.
- Preserve deterministic commands and the existing confirmation workflow.

## Non-goals

- No rewrite of `Bot`, `Service`, or the SQLite schema.
- No new AI provider, agent framework, ORM, or asynchronous runtime.
- No automatic approval of financial actions.
- No change to session fees, attendance accounting, debts, or receipts.

## Contracts

- Every model-produced action crosses `Intent.from_dict()` before application
  routing.
- Persisted state remains JSON-compatible and old state without new fields
  continues to load safely.
- `last_entity` is a hint for the current conversation only; it cannot grant
  access to another player's data or bypass admin checks.
- Unknown actions, malformed state, and unauthorized actions produce a safe
  response and no domain mutation.
- A bare greeting reaches the model but is restricted to a clarifying question;
  it never becomes a domain action and never creates a pending proposal.

## Acceptance Criteria

- [x] AC-001: Every AI action path validates through the domain intent contract.
- [x] AC-002: Legacy and current conversation state both round-trip without
  losing a valid follow-up entity.
- [x] AC-003: Invalid or unauthorized follow-up requests produce no financial
  or persistence side effect.
- [x] AC-004: Existing deterministic commands, confirmation callbacks, and
  public compatibility methods keep their current behavior.
- [x] AC-005: A group message is processed only when it is a command, addresses
  the configured bot, or replies to a message sent by this bot; replies to
  another user's message remain ignored. Addressing triggers processing for
  non-admin users too, without changing their permissions.
- [x] AC-006: A bare greeting is routed to the AI like any ordinary free
  message, so the user gets a conversational reply, while an unaddressed group
  greeting is still ignored.
- [x] AC-007: User help contains only public commands, and admin help clearly
  separates public commands from admin commands without exposing admin help to
  ordinary users.
- [x] AC-008: A bare greeting can never trigger a domain action or create a
  pending proposal; when the AI is unavailable or returns an invalid command the
  user still receives one short fixed greeting line instead of a technical error.

## Test Mapping

- AC-001 -> `tests/test_agent_contracts.py` and targeted routing tests in
  `tests/test_sans.py`
- AC-002 -> `tests/test_agent_contracts.py::test_domain_contracts_coerce_and_round_trip`
- AC-003 -> `tests/test_sans.py::ArchitectureContractTests::test_bot_persists_last_entity_and_rejects_unknown_ai_action`
- AC-004 -> existing integration and financial tests in `tests/test_sans.py`
- AC-005 -> `tests/test_sans.py::PermissionAndConversationTests::test_group_reply_to_bot_is_addressed_without_bot_name` and
  `test_group_reply_to_another_user_is_not_addressed` and
  `test_non_admin_group_mention_gets_public_response_without_admin_access` and
  `test_bot_username_mention_is_a_valid_group_trigger`
- AC-006 -> `tests/test_sans.py::PermissionAndConversationTests::test_group_only_responds_when_addressed` and
  `test_bare_private_greeting_reaches_ai_without_side_effect` and
  `test_greeting_keeps_conversation_context_for_the_next_request` and
  `test_bot_username_mention_is_a_valid_group_trigger`
- AC-007 -> `tests/test_sans.py::PermissionAndConversationTests::test_user_help_is_short_and_does_not_show_admin_commands` and
  `test_admin_help_separates_public_and_admin_commands`
- AC-008 -> `tests/test_sans.py::PermissionAndConversationTests::test_bare_greeting_never_runs_a_model_action` and
  `test_bare_greeting_falls_back_when_the_model_fails` and
  `test_greeting_caption_on_a_receipt_keeps_the_receipt_flow`

## Safety Constraints

- Fail closed on unknown actions, invalid arguments, invalid entity references,
  and invalid permissions.
- Do not infer identity, approval, amount, or entity IDs from untrusted text.
- Do not modify ledger rows while parsing, validating, or rendering a request.
- Keep history and context bounded.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

This is the next implementation boundary, not a claim that the migration is
complete. It should be split into small patches if routing and persistence do
not have the same risk profile.

Revision (2026-10-16): AC-006 previously required a fixed reply for a bare
greeting without an AI call. Real usage showed that the fixed line read as a
canned answer and hid the assistant, so a bare greeting now goes through the
model like any free message and AC-008 keeps that path side-effect free. The
greeting reply is model-generated, so its wording is not deterministic; only the
fallback line (`GREETING_REPLY` in `app/bot.py`) is fixed. A bare greeting is
answered with a single message, so it receives no immediate "processing" notice
from `Bot.acknowledgement_chat()`. `content/bot-content.json` carries the
matching prompt rule; restart the service after editing it.