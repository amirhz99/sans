# SPEC-038: Every admin action is refused for a non-admin

Status: verified
Owner: agent
Created: 2026-10-28

## Problem

The admin gate sits in one place: `Bot.message` refuses a non-admin before the
branch that handles `ADMIN_ACTIONS`. That is correct, but the guarantee was
only spot-checked. SPEC-036 notes leave "an exhaustive sweep of `ADMIN_ACTIONS`
for a non-admin" as an open gap: a new action added to the set later could
land above the gate or in a branch of its own and quietly become reachable by
a player. A single missing gate would let a player move money or close a
session.

## Goal

One table-driven test proves, for every member of `ADMIN_ACTIONS` *and* the
neighbouring privileged actions that sit next to them (`batch`, `duplicates`,
`list_links`, `list_players`, `list_sessions`, `report`, `expire`, `pending`,
`config`, `defaults`), that a non-admin request produces the refusal text and
leaves the database unchanged.

The test also fails when a new action is added to `ADMIN_ACTIONS` without the
sweep covering it, so the list and the test move together.

## Scope

- A non-admin profile request naming a privileged action is answered with
  «این عملیات فقط برای ادمین‌های مجاز در گروه مدیریتی است.» (or the
  action-specific equivalent) and creates no `pending` row.
- The database row counts and the balances of every player are identical
  before and after the sweep.
- `ADMIN_ACTIONS` is enumerated from the module, not hardcoded, so the test
  cannot drift from the implementation.
- Each action name is checked for an actual handler branch in
  `Bot.message`, so a typo in the table is a test failure rather than a
  silent no-op.

## Non-goals

- Changing which actions are admin-only. The set is unchanged.
- Covering the agent tool path; `test_set_phone_is_admin_only` and the
  agent contract tests already cover it (SPEC-020).
- Covering the private-chat player actions (`declare_debt`, `identify`,
  `settle_partner`, `request_link`); those are intentionally available to
  players and are asserted to stay available.

## Contracts

- `app.service.ADMIN_ACTIONS` stays the single source of truth for
  privileged actions; `Bot.message` keeps one gate in front of every
  privileged branch.
- A refused request is side-effect free: no `pending` row, no ledger row,
  no conversation state that could be clicked later.
- The refusal is a `UserError`, so it is answered as plain text and never
  as a card with buttons.
- `Bot.is_admin` keeps its documented ordering: the configured admin group is
  the primary boundary and is checked **first**, so every member of that group
  acts as an admin regardless of `admin_access`. `admin_access='allowlist'`
  governs *private* chats only. This ordering was asserted explicitly after a
  first draft of this sweep wrongly assumed the allow-list also applied inside
  the group; nine existing tests stated the real contract and caught it.
- Privileged work is refused at whichever layer is reached first, and the sweep
  asserts the layer rather than a single sentence:
  `ADMIN_ACTIONS` members → the gate («فقط برای ادمین»); `pending` → the
  routing allow-list («مجاز نیست»); `defaults` → Intent validation
  («درخواست معتبر»).

## Acceptance Criteria

- [x] AC-001: for every action in `ADMIN_ACTIONS`, a non-admin request is
  refused and no `pending` row is created.
- [x] AC-002: for every privileged neighbour (`batch`, `duplicates`,
  `list_links`, `list_players`, `list_sessions`, `report`, `expire`,
  `pending`, `config`, `defaults`), a non-admin request is refused.
- [x] AC-003: the database is unchanged after the whole sweep (player and
  session counts, `pending` count, and every player balance).
- [x] AC-004: the same actions are still proposed as cards for an admin, so
  the gate refuses the player without breaking the operator.
- [x] AC-006: the `pending` branch in `Bot.message` carries its own admin
  gate. It lists player names, amounts and phone numbers, and the routed
  action was reachable without one while `/pending` was already admin-only.
  A non-admin receives a refusal and reads nothing.

## Findings

1. **`pending` had no gate of its own.** `/pending` was admin-only, but the
   routed `{'action': 'pending'}` path reached the branch directly, so any
   caller that got past Intent validation could list every open card along with
   the amounts and phone numbers on them. Fixed with an explicit `if not admin:
   raise UserError(...)` in the branch, which is what AC-005 pins.
2. **`is_admin` ordering is intentional, not a hole.** The first draft of this
   sweep treated "a stranger in the admin group can act" as a bug and changed
   the gate to consult the allow-list inside the group. Nine existing tests
   failed, all of them documenting the opposite contract, and the change was
   reverted. The behaviour is now asserted from both sides (group policy and
   allow-list policy) so a future change is a deliberate decision.
3. **Three layers, not one.** `ADMIN_ACTIONS` members hit the gate, `pending`
   hits the routing allow-list, and `defaults` never becomes an `Intent` at all.
   The sweep asserts the layer each one is stopped by, so moving an action
   between layers is visible rather than silent.
- [x] AC-005: a player-only action (`declare_debt`) is still allowed for a
  non-admin, so the gate is an admin boundary and not a blanket refusal.

## Test Mapping

- AC-001 -> `tests/test_sans.py::AdminActionSweepTests::test_every_admin_action_is_refused_for_a_player`
- AC-002 -> `tests/test_sans.py::AdminActionSweepTests::test_privileged_neighbours_are_refused_for_a_player`
- AC-003 -> `tests/test_sans.py::AdminActionSweepTests::test_the_sweep_leaves_the_database_untouched`
- AC-004 -> `tests/test_sans.py::AdminActionSweepTests::test_the_same_actions_still_reach_an_admin`
- AC-005 -> `tests/test_sans.py::AdminActionSweepTests::test_player_actions_stay_available`
- AC-006 -> `tests/test_sans.py::AdminActionSweepTests::test_pending_is_gated_even_when_routed`

## Safety Constraints

- Invalid or unauthorized requests fail closed.
- No unrelated private data is exposed to AI or users.
- Financial and persistence behavior is unchanged.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- The gate is `elif not admin: raise UserError(...)` in `Bot.message`. Every
  privileged branch sits below it, so the sweep is a regression net around
  one line rather than eleven separate checks.
- `player_link` and `player_unlink` are members of `ADMIN_ACTIONS` that the
  model cannot route to; the sweep reaches them the same way any other
  privileged action is reached, through a validated command.
- Adding an action to `ADMIN_ACTIONS` without adding its branch makes
  `test_every_admin_action_is_refused_for_a_player` fail on the absence of a
  handler, which is the intended pressure to keep the two in step.