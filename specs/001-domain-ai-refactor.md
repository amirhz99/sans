# SPEC-001: Domain and AI boundary contracts

Status: verified  
Owner: agent  
Created: 2026-10-16

## Problem

AI routing, conversation memory, model context, and user-facing session
rendering need explicit boundaries. Without those boundaries, malformed model
output could reach application actions and follow-up messages could expose
more data than necessary.

## Goal

Provide small, serializable contracts for intent parsing and conversation
state, keep AI context minimal and role-aware, and make session response detail
an explicit presentation choice without changing financial behavior.

## Scope

- Validate AI/deterministic requests through `Intent` and an action allow-list.
- Represent follow-up identity with `EntityRef` and `ConversationState`.
- Coerce invalid response levels to safe `summary` behavior.
- Include only the related session in AI context and omit the player directory.
- Support `summary`, `details`, and admin-only `full` session views.
- Load both legacy and structured prompt content formats.
- Preserve `Service.session_info()` and the current SQLite/ledger behavior.

## Non-goals

- Introducing an agent runtime, orchestration framework, or new dependency.
- Changing financial calculations, approval rules, or database schema.
- Sending receipt media or a complete player directory to the model.
- Migrating old database session text into a new timestamp automatically.

## Contracts

- `Intent.from_dict()` accepts an object with an allow-listed `action`, a
  dictionary `args`, and an optional string `question`; invalid shapes raise a
  validation error.
- `ConversationState` serializes `last_entity`, `response_level`, receipt
  state, and identity phone. Unknown response levels safely become `summary`.
- `AIContextBuilder` emits role, Tehran time, linked self, last entity,
  response level, receipt-awaiting state, and at most the referenced session.
- `SessionView` defaults to `summary`; `full` exposes accounting and
  attendance only for an admin.
- `PromptLoader` accepts root-level legacy prompts and the structured
  `prompts` object, while rejecting a missing defaults object or invalid text
  values.

## Acceptance Criteria

- [x] AC-001: Valid intents and entity/state values round-trip through their
  serializable representations.
- [x] AC-002: Unknown actions, non-object arguments, and malformed entities are
  rejected before routing.
- [x] AC-003: AI context is role-aware and contains no complete player
  directory; an invalid or unrelated entity yields no session context.
- [x] AC-004: Session rendering defaults to summary and expands only for an
  explicit response level, with full accounting restricted to admins.
- [x] AC-005: Legacy and structured prompt formats load successfully, while
  schema errors fail clearly.
- [x] AC-006: The existing `Service.session_info()` compatibility path and
  unknown-AI-action side-effect guard remain intact.

## Test Mapping

- AC-001 -> `tests/test_agent_contracts.py::test_domain_contracts_coerce_and_round_trip`
- AC-002 -> `tests/test_agent_contracts.py::test_invalid_intents_and_entities_fail_closed`
- AC-003 -> `tests/test_agent_contracts.py::test_context_is_role_aware_and_entity_scoped`
- AC-004 -> `tests/test_agent_contracts.py::test_session_view_has_explicit_levels`
- AC-005 -> `tests/test_agent_contracts.py::test_prompt_loader_supports_legacy_and_structured_shapes`
- AC-006 -> `tests/test_sans.py::ArchitectureContractTests::test_bot_persists_last_entity_and_rejects_unknown_ai_action`

## Safety Constraints

- Model output is data, never an authorization decision or SQL command.
- Invalid actions and arguments must not produce a domain side effect.
- Context must not include secrets, receipt contents, or unrelated player data.
- Permission checks remain in the application layer.
- Existing financial and persistence tests remain passing.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

This specification records the completed baseline. Future routing or state
migrations must use a new specification rather than silently widening these
contracts.