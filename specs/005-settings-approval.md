# SPEC-005: Settings approval through an admin confirmation button

Status: verified  
Owner: agent  
Created: 2026-10-18

## Problem

Setting changes (`card`, `payment_note`, `approval_required`) did not share one
contract. The legacy intent route already turned a `settings` action into a
pending proposal with «تأیید/رد» buttons, but the LangGraph
`manage_records(update_settings)` tool called `db.set_setting()` immediately and
returned a plain text confirmation. The same admin request therefore behaved
differently depending on which engine handled it, and a value produced by the
model could reach the database without a second, explicit admin action. The
proposal preview was also technical (`key: card`, `value: ...`) and `/pending`
printed raw JSON for command proposals.

## Goal

Every settings change — whether it comes from a model request, `/do`, or the
explicit `/setcard` command — becomes one pending proposal with a readable
Persian preview and inline «تأیید/رد» buttons. Nothing is written to `settings`
until an admin in the configured admin group presses «تأیید».

## Scope

- Add one proposal builder for settings (`Bot.propose_settings`) and reuse it
  from the legacy route and the LangGraph tool.
- Add `/setcard` as the deterministic admin path for the card setting.
- Keep the existing pending kind `commands` and its savepoint-based atomic
  apply; no new table, column, or pending kind.
- Render a Persian preview for each setting and for `/pending`.
- Report approve and reject results to the admin group.
- Preserve the explicit-value rule from `specs/002-routing-persistence.md`
  (AC-012) at both routing engines.

## Non-goals

- No change to financial rules, ledger semantics, or the SQLite schema.
- No automatic application of a setting without a button press.
- No change to receipt approval semantics; `approval_required` only controls
  whether a receipt is reviewed by an admin or auto-approved.
- No new AI provider, agent framework, or dependency.
- No change to identity, binding, or receipt confirmation flows.

## Contracts

- A setting value must be explicit in the *current* user message
  (`setting_value_is_explicit`). Reply text, conversation history, and
  acknowledgements such as «آره انجامش بده» never supply a value.
- The proposal payload is the existing `commands` list produced by
  `Service.prepare('settings', ...)`; applying it re-validates the value through
  `Service.prepare` inside `Service.apply`, then writes it with
  `db.set_setting` and records the audit row.
- `Bot.confirm` applies a `commands` proposal inside `SAVEPOINT
  apply_commands` and marks it `approved` or `rejected` exactly once. A second
  click on the same proposal fails closed.
- A proposal expires after 24 hours or when `db.revision` changed since it was
  created; the expired proposal applies nothing.
- Callbacks are accepted only from a member of `ADMIN_GROUP_ID`; a callback in
  a private chat or another group cannot apply the setting.
- The model never writes `settings` directly; the tool only creates a proposal
  and attaches the approval markup.
- The preview names each setting in Persian and shows `approval_required` as
  «فعال»/«غیرفعال», not `true`/`false`.
- `Service.session_info()`, `/do`, and all existing command names keep their
  current behavior.

## Acceptance Criteria

- [x] AC-001: A `settings` action whose value appears explicitly in the current
  message creates a pending `commands` proposal and leaves `settings`
  unchanged.
- [x] AC-002: The proposal reply contains a Persian preview, a proposal code,
  and inline «تأیید/رد» buttons whose callback data is `yes:<id>` / `no:<id>`.
- [x] AC-003: Approving the proposal writes the value once; a second click on
  the same button reports that the request was already reviewed and does not
  write again.
- [x] AC-004: Rejecting the proposal writes nothing and marks it `rejected`.
- [x] AC-005: An ambiguous acknowledgement (for example «آره انجامش بده») with
  no explicit value creates no proposal and writes nothing, on both the legacy
  route and the LangGraph tool.
- [x] AC-006: The LangGraph `update_settings` operation creates a proposal and
  attaches the approval markup instead of calling `db.set_setting`.
- [x] AC-007: `/setcard` is available to admins only, requires a value in the
  same message, and goes through the same proposal/approval flow.
- [x] AC-008: A proposal whose `db.revision` changed expires and applies
  nothing.
- [x] AC-009: A callback from a private chat or another group cannot approve a
  settings proposal.
- [x] AC-010: `/pending` shows a readable Persian settings summary instead of
  raw JSON, and the preview for disabling receipt approval keeps the safety
  warning.

## Test Mapping

- AC-001, AC-002 -> `tests/test_sans.py::SettingsApprovalTests::test_explicit_card_creates_proposal_without_applying`
- AC-003 -> `tests/test_sans.py::SettingsApprovalTests::test_card_proposal_approval_applies_exactly_once`
- AC-004 -> `tests/test_sans.py::SettingsApprovalTests::test_card_proposal_rejection_keeps_value`
- AC-005 -> `tests/test_sans.py::SettingsApprovalTests::test_ambiguous_reply_creates_no_setting_proposal`
  and `tests/test_agent_contracts.py::AgentContractTests::test_langgraph_rejects_ambiguous_setting_acknowledgement`
- AC-006 -> `tests/test_agent_contracts.py::AgentContractTests::test_langgraph_update_settings_creates_approval_proposal`
  and `tests/test_agent_contracts.py::AgentContractTests::test_langgraph_setting_proposal_applies_only_on_admin_approval`
- AC-007 -> `tests/test_sans.py::SettingsApprovalTests::test_setcard_requires_admin_and_explicit_value`
- AC-008 -> `tests/test_sans.py::SettingsApprovalTests::test_setting_proposal_expires_after_revision_change`
- AC-009 -> `tests/test_sans.py::SettingsApprovalTests::test_private_callback_cannot_approve_setting`
- AC-010 -> `tests/test_sans.py::SettingsApprovalTests::test_pending_summary_shows_readable_setting_preview`

## Safety Constraints

- Invalid or unauthorized requests fail closed and apply no setting.
- A setting value is taken from the current user message only; history and
  reply context cannot supply it.
- The model cannot write settings; only an admin button press applies a
  validated proposal.
- No secrets, `.env` values, or unrelated private data are added to model
  context or to proposal text.
- Financial and receipt behavior is unchanged.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

The approval group is identified only by `ADMIN_GROUP_ID`. Any valid positive
`user_id` inside that chat is treated as an admin, so a positive group id (as
returned by Bale for some groups) works the same as a negative one.

Settings are considered low-risk by the maintainer (a later explicit request can
always replace the card), so the flow deliberately keeps one button press
instead of a typed confirmation code. The proposal code remains in the message
so `/confirm <id>` and `/deny <id>` keep working for callers that prefer typed
commands.

A non-admin `/setcard` is treated as an unknown command and receives the
ordinary help text, so the setting is never proposed. A callback in a private
chat reaches the handler and is rejected there as non-admin; a callback in an
unconfigured group is dropped at the update boundary before any handler runs.
Both cases leave the pending proposal untouched.
