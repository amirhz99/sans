# SPEC-007: Player Identity Maintenance (rename, merge, delete)

Status: verified  
Owner: agent  
Created: 2026-10-19  
Updated: 2026-10-20 (revision 2: follow-up reference safety)

## Problem

Importing an old session created duplicate and partial player rows. The agent
turned every unrecognized name in `set_attendance` into a new player, so a
short name («موحد») and the later full name («رضا محمدی») became two
players, and the first one stayed behind as an absent row. There was no way to
rename, merge, or remove a player: `players` had no delete or rename path, the
`Action` contract had no such values, and `manage_records` silently ignored
unknown keys, so the model could report success without doing anything.

## Goal

An operator can keep one row per real person: rename a player without losing
history, merge a duplicate into the correct row, and delete a row that has no
records. The bot stops creating duplicates by itself and asks for the full name
instead.

## Scope

- `Service.rename_player(player_id, new_name)`: the id, `bale_id`, attendance,
  ledger, and receipts stay untouched; only `name`/`normalized_name` change.
- `Service.merge_players(source_id, target_id)`: attendance, ledger, and
  receipt rows move to the target, notes are combined, an unbound `bale_id` is
  carried over, and the source row is deleted.
- `Service.delete_player(player_id)`: only for a player with no attendance,
  ledger, receipt, pending reference, or `bale_id`.
- `Service.similar_players`, `Service.duplicate_players`, and
  `Service.duplicates_report` for detecting partial or misspelled names.
- `ensure_player` refuses to create a name that is a strong match for an
  existing player unless `allow_similar=True` is passed explicitly.
- `set_attendance` validates every name before writing, accepts `attendees`
  and `players` as aliases for `present`, and never creates a player.
- `manage_records` rejects unknown keys for players, sessions, and attendance;
  `save_session` accepts `date` as an alias for `when_text`.
- Deterministic admin commands `/duplicates`, `/merge`, `/rename`,
  `/delplayer`, `/newplayer`, plus `duplicate_players` in `query_database`.
- New actions `player_rename`, `player_merge`, `player_delete` in the domain
  `Action` allow-list and in `ADMIN_ACTIONS`.
- Follow-up memory: `AIContextBuilder` sends `last_entity` only while the row
  still exists, and an approved merge/delete repairs the chat's stored
  reference (merge: the surviving player; delete: removed).

## Non-goals

- No change to ledger rules, amounts, `finalized` state, or the SQLite schema.
- No automatic merge or deletion: detection is advisory and every merge or
  delete needs an explicit admin approval.
- No fuzzy matching against Bale profiles; `bale_id` is never inferred.
- `ledger.source` is not rewritten during a merge; it remains the unique audit
  key of the original transaction.

## Contracts

- `rename_player` fails closed when the new name equals the current name or
  already belongs to another player (it points to merge instead).
- `merge_players` fails closed, with no partial write, when:
  - both players have a `bale_id`;
  - a pending proposal references either player (the proposal being applied is
    excluded from this check);
  - the source has more guests than the target in the same session, because
    deleting the source row would silently drop guests.
- `delete_player` fails closed when the player has any history, and suggests a
  merge instead.
- Merge and delete are only reachable through a `pending` proposal of kind
  `commands` with the usual `yes:<id>` / `no:<id>` buttons; the proposal
  expires after 24 hours or a revision change, and a second click changes
  nothing.
- Rename is applied directly for an admin because it is reversible; it writes
  one `player_rename` audit row.
- All three operations bump `db.revision` through `db.audit`.
- A follow-up reference is resolved against the database before it reaches the
  model; an unknown type, a malformed id, or a missing row is sent as `null`
  instead of the stale entry. SQLite reuses freed integer ids, so an
  unvalidated reference could otherwise point at a different person.
- Authorization stays at the transport boundary: only the configured admin
  group, and the LangGraph tool refuses management operations for non-admins.
- The legacy `/do` + `Intent` path and the LangGraph path share the same
  `Service.prepare` / `Service.apply` validation.

## Acceptance Criteria

- [x] AC-001: A rename keeps the player id, binding, and all history, and is recorded in `audit_logs`.
- [x] AC-002: A rename to an existing or unchanged name is refused with no write.
- [x] AC-003: A merge moves attendance, ledger, and receipts to the target, deletes the source, and preserves the total balance.
- [x] AC-004: A merge is refused without any write when guests would be lost, when both players are bound, or when a pending proposal references them.
- [x] AC-005: A delete is allowed only for a player without history; otherwise it is refused and merge is suggested.
- [x] AC-006: Merge and delete write nothing until an admin presses «تأیید»; rejection and a second click change nothing.
- [x] AC-007: A partial or misspelled name never creates a second player; the candidates are returned with their `#id`.
- [x] AC-008: `set_attendance` validates all names before writing, rejects unknown keys, and accepts the documented aliases.
- [x] AC-009: Duplicate detection is admin-only in both the `/duplicates` command and the `duplicate_players` query target.
- [x] AC-010: Non-admin callers cannot rename, merge, or delete through either the command path or the agent tool.
- [x] AC-011: `last_entity` reaches the model only when the referenced row still exists; deleted, malformed, and unknown-type references are sent as `null`.
- [x] AC-012: After an approved merge the chat's follow-up reference points at the surviving player, and after an approved delete it is cleared.

## Test Mapping

- AC-001 -> `tests/test_sans.py::PlayerIdentityTests::test_rename_keeps_id_history_and_binding`
- AC-002 -> `tests/test_sans.py::PlayerIdentityTests::test_rename_rejects_existing_or_unchanged_name`
- AC-003 -> `tests/test_sans.py::PlayerIdentityTests::test_merge_moves_attendance_ledger_and_receipts`
- AC-004 -> `tests/test_sans.py::PlayerIdentityTests::test_merge_refuses_to_lose_guests`
- AC-004 -> `tests/test_sans.py::PlayerIdentityTests::test_merge_refuses_two_bound_players_and_pending_references`
- AC-005 -> `tests/test_sans.py::PlayerIdentityTests::test_delete_only_removes_unused_players`
- AC-006 -> `tests/test_sans.py::PlayerIdentityTests::test_merge_and_delete_need_an_admin_confirmation`
- AC-006 -> `tests/test_sans.py::PlayerIdentityTests::test_rejected_merge_changes_nothing`
- AC-007 -> `tests/test_sans.py::PlayerIdentityTests::test_ensure_player_asks_before_creating_a_similar_name`
- AC-007 -> `tests/test_agent_contracts.py::AgentContractTests::test_manage_records_is_strict_about_players_and_keys`
- AC-008 -> `tests/test_agent_contracts.py::AgentContractTests::test_manage_records_is_strict_about_players_and_keys`
- AC-009 -> `tests/test_sans.py::PlayerIdentityTests::test_duplicates_report_and_players_hint`
- AC-009 -> `tests/test_agent_contracts.py::AgentContractTests::test_merge_tool_only_proposes_and_duplicates_are_admin_only`
- AC-010 -> `tests/test_sans.py::PlayerIdentityTests::test_non_admin_cannot_rename_merge_or_delete`
- AC-010 -> `tests/test_agent_contracts.py::AgentContractTests::test_merge_tool_only_proposes_and_duplicates_are_admin_only`
- AC-011 -> `tests/test_sans.py::PlayerIdentityTests::test_stale_follow_up_reference_never_reaches_the_model`
- AC-012 -> `tests/test_sans.py::PlayerIdentityTests::test_merge_repairs_and_delete_clears_follow_up_memory`

## Safety Constraints

- Unknown actions, unknown keys, and unresolved names fail closed; no partial
  attendance list is written.
- The merge/delete check excludes only the proposal being applied, never an
  unrelated pending request.
- No player directory beyond the admin group is exposed; non-admins get a
  short refusal instead of the list.
- Financial values, ledger rows, and the SQLite schema are unchanged.
- Every merge/delete/rename is auditable and bumps `db.revision`.
- A stale follow-up reference cannot be replayed onto a different player after
  an id is freed and reused.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- Detection is heuristic: token containment, or a `difflib` ratio >= 0.85
  where multi-token names must also share a similar given name (so two people
  with the same family name are not paired). It only produces suggestions, and
  the admin can always force a genuinely new player.
- `set_attendance` no longer creates players; the operator must register the
  full name first (`upsert_players`, `/newplayer`, or the `new_player` action).
- This spec supersedes the implicit player creation described in SPEC-003.
- Revision 2 added AC-011/AC-012 after review: deleting a row exposed a
  follow-up hazard, because `players.id` has no `AUTOINCREMENT` and SQLite can
  reuse a freed id. Both the context builder and `Bot.confirm` now treat
  `last_entity` as untrusted memory that must be re-checked against the
  database.
