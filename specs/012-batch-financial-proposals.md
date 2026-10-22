# SPEC-012: One approval card for multi-player balance corrections

Status: verified
Owner: agent
Created: 2026-10-22

## Problem

`propose_financial_change` could only correct one player at a time. The tool
already accepted a `players` argument for `settle`, but `adjustment` ignored it
and passed the whole comma-separated string to `Service.player`, so a request
such as «بدهی مهدی و رضا و سعید را هر کدام ۵۰ هزار کم کن» failed with «بازیکن
پیدا نشد» (`agent_logs` #581). The model then retried per player and produced a
burst of single-player cards.

`settle` had a second defect: `Service.prepare('settle')` raises when the
current balance is `<= 0`, and the agent prepared every requested player before
building the card. One already-settled member aborted the whole batch with
«این حساب بدهی مثبت ندارد و قبلاً تسویه است», so a group settle never produced
a card.

Per-person amounts were also impossible: `integer()` rejects thousands
separators, so a comma-separated `players` list cannot carry an amount per
name, and a shared `amount` cannot express «مهدی ۱۵۰ هزار بدهکار، رضا ۵۰ هزار
بستانکار».

## Goal

One financial request for several players produces exactly one approval card
with one «تأیید» button. The card supports both a shared amount for every
player and an explicit amount per player, and it fails closed — no card, no
ledger row — when any part of the batch is invalid.

## Scope

- `propose_financial_change` accepts `players` (names or `#id`, separated by
  `,`, `،` or newline) for `adjustment` and `settle`, capped at 30 entries.
- New `entries` argument (JSON array of `{"player": <name|id>, "amount": <int>}`)
  for per-person `adjustment` amounts. `entries` is mutually exclusive with
  `players` and with a non-zero `amount`.
- `settle` skips members whose balance is `<= 0`, reports them, and still
  builds one card for the remaining members. If nobody has a positive balance,
  no card is created.
- Every batch is prepared before anything is proposed, so an unknown or
  ambiguous name, an invalid amount, or a duplicate target produces one clear
  error and no `pending` row.
- The agent system prompt documents the two shapes and forbids per-player
  cards.

## Non-goals

- No change to ledger semantics, `Service.prepare`, `Service.apply`, or the
  approval savepoint: a batch is still applied atomically by `Bot.confirm`.
- No change to `declare_debt` (a player still declares only their own debt) and
  no change to `finalize`.
- No new dependency and no new table.

## Contracts

`propose_financial_change(action, player='', session='', amount=0, note='',
players='', entries='')`

| Input | Result |
| --- | --- |
| `adjustment`, `player`, `amount` | one command, unchanged behavior |
| `adjustment`, `players`, `amount` | one card, one command per player, same amount |
| `adjustment`, `entries` | one card, one command per entry, per-person amount |
| `adjustment`, both `entries` and (`players` or `amount`) | error, no card |
| `settle`, `players` | one card, one command per member with balance `> 0` |
| `settle`, nobody with a positive balance | error text, no card |
| `entries` not valid JSON / not a list / empty / more than 30 | error, no card |
| any referenced player unknown or ambiguous | error, no card |
| duplicate player inside one batch | error from `Bot.propose_commands`, no card |

`Service.prepare` keeps rejecting a single `settle` with `balance <= 0`; only
the batch tool filters those members out. The guard fingerprint from SPEC-011
(`{'player': id}` for `adjustment`, `{'balance': n}` for `settle`) is unchanged,
so a batch card still expires if any member's balance moves before approval.

## Acceptance Criteria

- [x] AC-001: `adjustment` with `players` and one shared `amount` creates a
  single `commands` proposal containing one command per player.
- [x] AC-002: `adjustment` with `entries` applies each entry's own amount.
- [x] AC-003: `entries` together with `players` or a non-zero `amount` is
  rejected and creates no proposal.
- [x] AC-004: malformed `entries` (bad JSON, empty list, unknown key, zero or
  non-integer amount, more than 30 items) is rejected and creates no proposal.
- [x] AC-005: `settle` for a mixed list skips members with `balance <= 0`,
  names them in the reply, and builds one card for the rest.
- [x] AC-006: `settle` where every member is already settled creates no card
  and explains why.
- [x] AC-007: creating a batch card moves no balance; approving it once applies
  every command.
- [x] AC-008: an unknown or ambiguous player name in a batch creates no card.

## Test Mapping

- AC-001 -> `tests/test_agent_contracts.py::BatchFinancialToolTests.test_adjustment_with_shared_amount_creates_one_card`
- AC-002 -> `tests/test_agent_contracts.py::BatchFinancialToolTests.test_adjustment_with_entries_uses_per_person_amounts`
- AC-003 -> `tests/test_agent_contracts.py::BatchFinancialToolTests.test_entries_are_mutually_exclusive_with_players_and_amount`
- AC-004 -> `tests/test_agent_contracts.py::BatchFinancialToolTests.test_invalid_entries_fail_closed` and `tests/test_agent_contracts.py::BatchFinancialToolTests.test_settle_rejects_entries_because_amount_is_derived`
- AC-005 -> `tests/test_agent_contracts.py::BatchFinancialToolTests.test_settle_skips_settled_players_and_still_builds_one_card`
- AC-006 -> `tests/test_agent_contracts.py::BatchFinancialToolTests.test_settle_without_positive_balance_creates_no_card`
- AC-007 -> `tests/test_agent_contracts.py::BatchFinancialToolTests.test_batch_card_moves_nothing_until_one_approval`
- AC-008 -> `tests/test_agent_contracts.py::BatchFinancialToolTests.test_unknown_player_fails_closed_without_a_card`

## Safety Constraints

- Invalid or unauthorized requests fail closed: validation happens before
  `new_pending`, so a rejected batch leaves no `pending` row and no outbox row.
- No player is created as a side effect of a financial correction.
- Financial rules and ledger sources are unchanged; every command in a batch
  keeps its own `proposal:<id>:<index>` source and the batch stays atomic.
- Only names and amounts of the requested players are echoed in the admin card.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- `entries` is JSON because `integer()` rejects `,`/`٬` separators; a comma list
  cannot carry an amount per name unambiguously.
- `settle` amounts are always derived from the live balance, so `entries` is
  rejected for it rather than silently ignored.
- Skipped members are reported in the tool result so the model can tell the
  admin who was already settled; they are not added to the card.

## Validation Result

- `python3 -m unittest discover -s tests -t . -v`: 177 tests, OK (1 skipped).
- `python3 -m compileall -q app tests`: OK.
- `git diff --check`: clean.
- Live `agent_logs` #581 and the bot log confirm the old failure («بازیکن پیدا
  نشد» for `players="مهدی روستایی, امیرحسین زارعی, ..."`); the same call now
  builds one card.

- No change to ledger semantics, `Service.prepare`, `Service.apply`, or the
  approval savepoint: a batch is still applied atomically by `Bot.confirm`.
- No change to `declare_debt` (a player still declares only their own debt) and
  no change to `finalize`.
- No new dependency and no new table.
