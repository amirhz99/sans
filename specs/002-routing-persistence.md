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
- [x] AC-009: Only private chats and the configured admin group are active;
  messages, media, callbacks, acknowledgements, AI calls, and side effects from
  every other group or supergroup are ignored before routing.
- [x] AC-010: Every valid sender in the configured admin group has admin
  capabilities, while private-chat users retain ordinary user capabilities and
  never receive group-admin access.
- [x] AC-011: Private `/start` guidance contains only private-user capabilities;
  a successful immediate acknowledgement is edited into the final response,
  while failed or unsuitable edits fall back to the normal outbound message.
- [x] AC-012: An admin setting change must contain its exact value in the
  current message. A reply to an earlier question, chat history, or an
  ambiguous acknowledgement such as «آره انجامش بده» must not supply or
  confirm a missing card number, payment note, or approval setting.

## Test Mapping

- AC-001 -> `tests/test_agent_contracts.py` and targeted routing tests in
  `tests/test_sans.py`
- AC-002 -> `tests/test_agent_contracts.py::test_domain_contracts_coerce_and_round_trip`
- AC-003 -> `tests/test_sans.py::ArchitectureContractTests::test_bot_persists_last_entity_and_rejects_unknown_ai_action`
- AC-004 -> existing integration and financial tests in `tests/test_sans.py`
- AC-005 -> `tests/test_sans.py::PermissionAndConversationTests::test_group_reply_to_bot_is_addressed_without_bot_name` and
  `test_group_reply_to_another_user_is_not_addressed` and
  `test_every_group_member_gets_admin_access_in_designated_group` and
  `test_bot_username_mention_is_a_valid_group_trigger`
- AC-006 -> `tests/test_sans.py::PermissionAndConversationTests::test_group_only_responds_when_addressed` and
  `test_bare_private_greeting_reaches_ai_without_side_effect` and
  `test_greeting_keeps_conversation_context_for_the_next_request` and
  `test_bot_username_mention_is_a_valid_group_trigger`
- AC-007 -> `tests/test_sans.py::PermissionAndConversationTests::test_user_help_is_short_and_does_not_show_admin_commands` and
  `test_admin_help_replaces_the_player_guide`
- AC-008 -> `tests/test_sans.py::PermissionAndConversationTests::test_bare_greeting_never_runs_a_model_action` and
  `test_bare_greeting_falls_back_when_the_model_fails` and
  `test_greeting_caption_on_a_receipt_keeps_the_receipt_flow`
- AC-009 -> `tests/test_sans.py::PermissionAndConversationTests::test_unauthorized_group_is_ignored_before_any_side_effect` and
  `test_unauthorized_group_callback_is_ignored_without_callback_answer` and
  `test_acknowledgement_targets_only_messages_the_bot_will_process`
- AC-010 -> `tests/test_sans.py::PermissionAndConversationTests::test_all_group_members_can_manage_only_in_designated_group` and
  `test_every_group_member_gets_admin_access_in_designated_group` and
  `test_all_members_policy_does_not_grant_private_management`
- AC-011 -> `tests/test_sans.py::PermissionAndConversationTests::test_private_start_gives_private_user_guidance_only`,
  `test_final_response_uses_a_new_message_after_acknowledgement` (SPEC-028 replaced
  the in-place edit), and
  `test_failed_acknowledgement_edit_falls_back_to_new_message`, and
  `test_media_response_keeps_normal_send_path_after_acknowledgement`
- AC-012 -> `tests/test_sans.py::PermissionAndConversationTests::test_positive_group_id_grants_group_admin_access`,
  `test_ambiguous_group_acknowledgement_cannot_set_card`, and
  `tests/test_agent_contracts.py::AgentContractTests::test_langgraph_rejects_ambiguous_setting_acknowledgement`

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

Revision (2026-10-17): AC-009 and AC-010 are now implemented at the update
boundary. The configured `ADMIN_GROUP_ID` is the sole active group boundary;
all members of that group have admin capabilities, while legacy user allowlist
configuration is retained only for compatibility and is no longer required.
Unauthorized group updates are consumed for polling-offset bookkeeping but do
not create message/agent logs, conversation state, receipts, proposals, or
outbound messages.

Revision (2026-10-17): AC-012 is implemented at both the legacy intent route
and the LangGraph settings tool. Setting values are validated against the
current user message only; reply text and persisted conversation history are
not accepted as the value of a new setting.

Revision (2026-10-16): AC-006 previously required a fixed reply for a bare
greeting without an AI call. Real usage showed that the fixed line read as a
canned answer and hid the assistant, so a bare greeting now goes through the
model like any free message and AC-008 keeps that path side-effect free. The
greeting reply is model-generated, so its wording is not deterministic; only the
fallback line (`GREETING_REPLY` in `app/bot.py`) is fixed. A bare greeting is
answered with a single message, so it receives no immediate "processing" notice
from `Bot.acknowledgement_chat()`. `content/bot-content.json` carries the
matching prompt rule; restart the service after editing it.

Revision (2026-10-17): AC-011 is implemented without a schema change. The
polling boundary retains the message id returned by `sendMessage`, queues
`editMessageText` for short final responses, and converts an edit failure to a
normal `sendMessage` fallback. Long responses continue to use the existing
chunking behavior, and media/multistep responses keep the normal send path.