# SPEC-017: Session defaults are configurable and confirmed by the admin

Status: verified
Owner: agent
Created: 2026-10-22

## Problem

`Service.prepare('new_session')` silently injects three values from
`content/bot-content.json`: the location, the venue cost, and the per-person
fee. Two problems follow from that:

- The values are code, not settings. An admin who moves to a different hall has
  to edit a JSON file and restart the bot; `/config` prints the content values
  as if they were configuration, and the assistant's snapshot repeats the same
  hardcoded numbers.
- The admin never sees that a default was used. The approval card shows the
  numbers, but it does not say which of them came from a default and which the
  admin actually stated. A wrong hall or a stale fee therefore gets approved by
  a click, and the mistake is only noticed after finalisation.

The session time is the opposite case: it has no default at all, and that is
correct, because guessing an hour is exactly what SPEC-008 forbids.

## Goal

An admin can read and change the defaults for a new session, the assistant and
`/config` report the effective values instead of the file contents, and a card
that used a default says so and asks the admin to confirm or correct it.

## Scope

- New settings keys, all written through the existing approval-card flow:
  - `default_location` — non-empty text, at most 200 characters;
  - `default_venue_cost` — integer `>= 0`;
  - `default_person_fee` — integer `>= 1`.
- New `Service.session_defaults()` returning the effective
  `{'location', 'venue_cost', 'person_fee'}`: the stored setting when present,
  otherwise the value from `content/bot-content.json`. A stored value that
  cannot be parsed falls back instead of breaking the card.
- `Service.prepare('new_session')` uses `session_defaults()` and records which
  keys came from a default in `args['defaults_used']`.
- `Service.preview` replaces that marker with one readable line, and
  `Bot.propose_commands` appends a confirmation sentence naming the defaults and
  telling the admin to reject the card and write the exact value if they are
  wrong.
- New admin commands `/defaults` (read) and `/setdefault <key> <value>`
  (proposes a settings card, exactly like `/setcard`).
- `Service.configuration()` and the agent snapshot report the effective
  defaults; `AIContextBuilder` exposes them as `session_defaults` so the legacy
  JSON route no longer depends on hardcoded prompt numbers.
- `content/bot-content.json` stops hardcoding the three numbers in the system
  prompt and documents `/defaults` and `/setdefault` in `admin_help`.

## Non-goals

- No default for the session time. `when_text` stays mandatory and
  `parse_session_datetime` keeps rejecting vague hours.
- No schema change: defaults live in the existing `settings` table.
- No change to the finalisation formula, the ledger, or the guard fingerprint.
- `/setdefault` never writes directly; a settings change still requires the
  «تأیید» click (SPEC-005).
- Existing sessions are never rewritten when a default changes.

## Contracts

`Service.session_defaults() -> dict`:

- always returns all three keys with valid types;
- reads `default_location`, `default_venue_cost`, `default_person_fee` from
  `settings`, falling back per key to `content/bot-content.json`;
- never raises for a corrupt stored value.

`Service.prepare('new_session', args)`:

- `args['defaults_used']` is a sorted list containing only the keys that were
  not stated explicitly;
- an explicit `location`, `venue_cost`, or `person_fee` always wins and is
  validated with the existing `text`/`integer` rules;
- `when_text` remains required and is parsed by `parse_session_datetime`.

`/setdefault <key> <value>`:

- only for an admin in the admin group, like `/setcard`;
- maps `location`, `venue_cost`, `person_fee` to the `default_*` settings key;
- an unknown key, a negative amount, or a zero fee is refused before a card is
  created.

## Acceptance Criteria

- [x] AC-001: A new session uses the stored settings defaults and reports which keys were defaulted.
- [x] AC-002: An explicit value from the admin wins over the default.
- [x] AC-003: Without settings the content-file defaults are used.
- [x] AC-004: A corrupt stored default falls back to the content file.
- [x] AC-005: `/setdefault` creates an approval card and writes nothing before the click.
- [x] AC-006: Approving the card writes the setting, and the next session uses it.
- [x] AC-007: `/setdefault` refuses an unknown key, a negative cost, and a zero fee.
- [x] AC-008: The approval card names the defaults and asks the admin to confirm or correct them.
- [x] AC-009: `/defaults` lists the effective values and states that time is never defaulted.
- [x] AC-010: `/defaults` is admin-only and leaks no value to a player.
- [x] AC-011: A new session without an exact time is still refused.
- [x] AC-012: The model context reports the effective defaults, not the file contents.

## Test Mapping

- AC-001 -> `tests/test_sans.py::SessionDefaultsTests::test_new_session_uses_the_stored_defaults`
- AC-002 -> `tests/test_sans.py::SessionDefaultsTests::test_explicit_values_win_over_defaults`
- AC-003 -> `tests/test_sans.py::SessionDefaultsTests::test_content_defaults_apply_without_settings`
- AC-004 -> `tests/test_sans.py::SessionDefaultsTests::test_corrupt_default_falls_back`
- AC-005 -> `tests/test_sans.py::SessionDefaultsTests::test_setdefault_only_builds_a_card`
- AC-006 -> `tests/test_sans.py::SessionDefaultsTests::test_approving_the_card_changes_the_next_session`
- AC-007 -> `tests/test_sans.py::SessionDefaultsTests::test_setdefault_refuses_invalid_input`
- AC-008 -> `tests/test_sans.py::SessionDefaultsTests::test_card_names_the_defaults_and_asks_to_confirm`
- AC-009 -> `tests/test_sans.py::SessionDefaultsTests::test_defaults_command_lists_effective_values`
- AC-010 -> `tests/test_sans.py::SessionDefaultsTests::test_defaults_command_is_admin_only`
- AC-011 -> `tests/test_sans.py::SessionDefaultsTests::test_session_time_is_never_defaulted`
- AC-012 -> `tests/test_agent_contracts.py::SessionDefaultsContextTests::test_context_reports_effective_defaults`

## Safety Constraints

- A defaults change is a settings change: it needs an explicit value in the
  same message and an admin click.
- Invalid input fails closed and creates no card.
- The confirmation sentence is produced by the bot, never by the model.
- Financial rules and the ledger are untouched; only `settings` gains keys.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- Decision: defaults are stored as `settings` rows rather than new columns, so
  no migration is needed and the existing audit/approval path applies.
- Decision: `defaults_used` travels inside the prepared command args because
  that is the only object both the preview and the card builder already share.
  It is never written to the database.
- Limitation: changing a default does not rewrite an already built card. The
  card keeps the numbers the admin saw, and the guard fingerprint still only
  covers the facts that a preview depends on.
- Validation (2026-10-22): 218 tests pass (1 pre-existing skip). The legacy
  prompt assertion in `OperationalTests` was updated on purpose: it used to
  require the hardcoded `۲٬۵۰۰٬۰۰۰` in the system prompt, and this spec removes
  that number in favour of `context.session_defaults`.
- Dry run on a copy of `data/sans.sqlite3`: `session_defaults()` returned the
  content values, no `settings` row was written, and the 62 ledger rows
  (`5,100,000` total) were unchanged.
