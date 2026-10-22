# SPEC-011: A proposal expires on its own facts, not on a global counter

Status: verified
Owner: agent
Created: 2026-10-21

## Problem

`Bot.confirm` expired a `commands` proposal when `pending.revision` differed
from the current `db.revision`. `db.revision` is a **global** counter that
`Database.audit` increments on *every* audited change, so approving the first
card of a batch invalidated every other card of that same batch:

```text
12:35  model proposes 9 settle cards, all stored with revision=42
12:36  admin presses «تأیید» on card #1
       -> service.apply -> db.audit -> revision becomes 43
       -> cards #2..#9 now fail with «اطلاعات تغییر کرده» and are marked expired
```

Live evidence (read-only inspection of `data/sans.sqlite3`):

| Batch | Cards | `revision` | Applied |
| --- | --- | --- | --- |
| 12:35 | 9 × `settle` | 42 | 1 (`audit` #43, player 13) |
| 16:28 | 16 × `settle` | 54 | 1 (`audit` #55, player 6) |

37 proposals were left `pending`, none of them approvable, and the admin saw
«اعمال نشد» after every click. The bot even answered «هیچ‌کدام از کارت‌ها
تأیید نشدن» because only one of nine had been applied.

The 24 hour TTL had the same readability problem: it produced one message that
mixed "the data changed" with "the card is old", and `/pending` kept listing
cards that could never be applied.

## Goal

Approving one proposal never invalidates a sibling proposal. A proposal expires
only when its own underlying facts changed (a balance, attendance, a session
price, a setting value) or when it is older than 24 hours, and each case is
reported with a distinct, actionable message. A multi-player request produces
one card and applies with one click.

## Scope

- Replace the global `revision` staleness check with a per-command guard
  fingerprint computed by `Service.guard` at proposal creation and recomputed
  before apply.
- Persist the fingerprint in a new additive `pending.guard` column
  (`TEXT NOT NULL DEFAULT '[]'`), migrated in `Database.__init__` the same way
  `sessions.session_at` is.
- Split the expiry messages: "expired after 24 hours" vs "underlying facts
  changed".
- `/pending` closes proposals older than the TTL and reports how many it closed.
- `propose_financial_change` accepts several players for `settle` and builds one
  card with several commands, applied atomically in the existing savepoint.
- `Bot.propose_commands` becomes the single card builder used by both the
  ordinary route and the multi-player route.

## Non-goals

- No change to ledger semantics, fee rules, receipt approval, or identity flows.
- No change to `Service.prepare`, `Service.apply`, or the `pending` primary key
  and status values.
- No new dependency, table, or pending kind.
- No automatic re-creation of an expired card; the admin asks again.
- `pending.revision` stays written for compatibility but is no longer read.

## Contracts

- `Service.guard(command) -> dict` returns only JSON-serialisable facts that the
  proposal preview depends on, and never raises for a command that
  `Service.prepare` already accepted:
  - `settle` -> `{'balance': int}`
  - `finalize` -> `{'finalized': int, 'person_fee': int, 'attendance': [[player, guests], ...]}`
  - `settings` -> `{'value': str}`
  - `attendance` -> `{'finalized': int, 'guests': int | None}`
  - `update_session` -> the current value of every key being changed
  - `player_note` / `player_rename` -> the current value
  - `adjustment` / `declare_debt` -> `{'player': int}`
  - `player_merge` -> `{'source': int, 'target': int}`
  - `player_delete` -> `{'player': int}`
  - `new_player` / `new_session` -> `{}`
- `Bot.new_pending` stores `[Service.guard(c) for c in payload]` for
  `kind='commands'` and `[]` for every other kind.
- `Bot.confirm` on an approved `commands` proposal fails closed, applying
  nothing, when:
  - `created_at` is older than `Bot.PROPOSAL_TTL` (86400 s) -> `expired`,
    message «این کارت منقضی شده است…»;
  - the stored fingerprint differs from the recomputed one -> `expired`,
    message «…مانده یا اطلاعات مرتبط تغییر کرده است…».
- A proposal whose stored fingerprint is the empty list (every row written
  before this change) skips only the fingerprint check; the TTL still applies.
- `Bot.propose_commands(commands, user_id, chat_id)` accepts 1..30 prepared
  commands, rejects duplicate targets, and returns the existing
  `(preview, buttons)` shape.
- A rejected or expired proposal never writes a ledger row.

## Acceptance Criteria

- [x] AC-001: Approving the second proposal of a batch still applies it, even
  though the first approval advanced `db.revision`.
- [x] AC-002: An unrelated audited change (another player's note) does not
  expire a pending proposal.
- [x] AC-003: A `settle` proposal whose player balance changed since creation is
  expired and writes no ledger row.
- [x] AC-004: A `finalize` proposal whose attendance changed since creation is
  expired and writes no ledger row.
- [x] AC-005: A `settings` proposal whose stored value changed since creation is
  expired, leaves the newer value intact, and reports the changed-data message.
- [x] AC-006: A proposal older than 24 hours is expired with the dedicated
  time-based message even when its fingerprint still matches.
- [x] AC-007: `/pending` marks over-age proposals `expired`, stops listing them,
  and reports the number it closed.
- [x] AC-008: One multi-player settle card applies every command with a single
  «تأیید» press.
- [x] AC-009: A multi-player settle card is atomic: one failing command rolls
  back the whole card and leaves every balance untouched.
- [x] AC-010: A `settle` card that is still current keeps applying normally
  (regression guard against over-expiring).

## Test Mapping

- AC-001 -> `tests/test_sans.py::ProposalStalenessTests::test_second_proposal_of_a_batch_still_applies`
- AC-002 -> `tests/test_sans.py::ProposalStalenessTests::test_unrelated_audit_does_not_expire_proposal`
- AC-003 -> `tests/test_sans.py::ProposalStalenessTests::test_settle_proposal_refuses_when_balance_changed`
- AC-004 -> `tests/test_sans.py::ProposalStalenessTests::test_finalize_proposal_refuses_when_attendance_changed`
- AC-005 -> `tests/test_sans.py::ProposalStalenessTests::test_setting_proposal_expires_when_value_changed`
- AC-006 -> `tests/test_sans.py::ProposalStalenessTests::test_proposal_older_than_ttl_is_expired`
- AC-007 -> `tests/test_sans.py::ProposalStalenessTests::test_pending_list_closes_old_proposals`
- AC-008 -> `tests/test_sans.py::ProposalStalenessTests::test_multi_player_settle_card_applies_in_one_click`
  and `tests/test_agent_contracts.py::AgentContractTests::test_langgraph_multi_player_settle_creates_one_card`
- AC-009 -> `tests/test_sans.py::ProposalStalenessTests::test_multi_player_settle_card_is_atomic`
- AC-010 -> `tests/test_sans.py::ProposalStalenessTests::test_current_settle_card_still_applies`

## Safety Constraints

- Expiry is fail-closed: it marks the proposal `expired` and returns a
  `UserError`; no ledger, attendance, session, or setting write happens.
- The fingerprint contains identifiers, amounts, and counts only — never message
  text, player notes shown to third parties, receipt contents, or secrets.
- The AI still cannot approve anything: `propose_financial_change` only creates
  a pending card, and the apply path stays behind `Bot.confirm`.
- `guard` values are derived from the same validated command that
  `Service.prepare` produced; raw model arguments are never compared directly.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- Supersedes the staleness rule in `specs/005-settings-approval.md` (Contracts,
  bullet "A proposal expires after 24 hours or when `db.revision` changed"). The
  rest of SPEC-005 stays valid.
- Migration is additive and idempotent, mirroring `sessions.session_at` in
  `app/db.py`. Existing `pending` rows receive `'[]'` and therefore keep only the
  TTL rule.
- The 37 stale proposals on the live database become closable through
  `/pending`; they are intentionally not applied because their balances moved
  since they were created.
- 2026-10-22: `AC-005` was ticked but its mapped test
  (`test_setting_proposal_expires_when_value_changed`) did not exist. The test is
  written now, together with
  `test_setting_proposal_still_applies_when_its_value_is_unchanged` for the
  over-expiry direction, and `SpecContractTests` now fails when a mapping points
  at a missing test.
