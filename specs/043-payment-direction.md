# SPEC-043: Make a declared payment decrease debt, never increase it

Status: verified
Owner: agent
Created: 2026-10-30

## Problem

A player wrote «۱۵۰٬۰۰۰ تومان پرداخت کردم» in a private chat without a receipt
photo. The bot raised an approval card in the admin group and the admin rejected
it, yet the player was still told the number had gone down. A second occurrence
is already in the production ledger:

```text
280  p=15  +1,050,000  اعلام پرداخت ۱٬۰۵۰٬۰۰۰ تومانی … همراه رسید  proposal:bb70723f5e0c4f48:0
```

The player had paid. The ledger added the amount to their debt instead of
subtracting it, so the balance went from `1,050,000` to `2,100,000`.

The receipt path is correct: `Service.review_receipt` writes
`-row['amount']` with `source='receipt:<id>'`. The failure is entirely in the
*directionless* declaration path, and it is not a prompt problem:

1. `Agent.propose_financial_change` (app/agent.py) exposes `declare_debt` as a
   free-standing action. The model chooses it for "I paid".
2. `Service.prepare` enforces a **lower bound of 1** for `declare_debt`:

   ```python
   a['amount'] = integer(args.get('amount'), 'اصلاح حساب',
                         1 if action == 'declare_debt' else -10**12)
   ```

   A negative amount is refused, so the only representable outcome is an
   increase in debt. "I paid 150,000" and "I owe 150,000" produce the same row.
3. `Service.apply` inserts `a['amount']` verbatim, so the sign is whatever the
   caller supplied.
4. `Bot.confirm` reports the rejection only to the admin chat, using a fixed
   sentence. The player is never told the request was refused, and the model is
   free to state any balance in the private chat.

Nothing in the domain records *what the caller meant*, so the ledger cannot tell
a payment from a charge.
## Goal

A player who says «پرداخت کردم» gets a card that, once approved, **decreases**
their balance, and one who says «بدهکارم» gets a card that increases it. No
surface — service, agent, or bot — can create a declaration without saying which
of the two it is.

## Scope

- A closed `DeclaredDirection` vocabulary in `app/domain/`.
- `declare_debt` requires `direction` (`owed` | `paid`) in `prepare`, `apply`,
  `Agent.propose_financial_change`, and `Bot.request_self_debt`.
- The ledger sign is computed from `direction`, never from the sign of `amount`.
- `Service.preview` renders the direction in Persian.
- `Bot.confirm` notifies the requesting player of the outcome with the
  service-rendered balance.

## Non-goals

- Changing the receipt flow, its TTL, or `review_receipt`.
- Adding any admin path that writes a credit without a receipt: `adjustment`
  keeps its signed-amount semantics and its `ADMIN_ACTIONS` gate.
- Migrating, rewriting, or deleting existing ledger rows.
- Changing the SQLite schema.

## Contracts

- `DeclaredDirection.parse` accepts exactly `'owed'` and `'paid'` and raises
  `ValueError` for everything else, including `''`, `'PAID'`, and `None`.
- `Service.prepare('declare_debt', …)` returns args with `direction` set to the
  normalized value and `amount` a **positive** integer; a negative amount is a
  `UserError` for both directions.
- `Service.apply` writes `direction.sign * abs(amount)`, so `paid` always credits.
- `Bot.request_self_debt` refuses an unrecognised direction with a `UserError`
  and creates no `pending` row. Its open-card deduplication matches on
  `(player, direction)`, so an open `owed` card does not block a `paid` one.
- The proposal fingerprint for `declare_debt` includes `amount` and `direction`,
  so a changed direction expires the old card.
- `Service.session_info()`, the legacy JSON `Intent` route, the `pending` table
  shape, and every `ADMIN_ACTIONS` gate stay unchanged.

## Acceptance Criteria

- [ ] AC-001: `prepare('declare_debt', …)` without `direction` raises `UserError`.
- [ ] AC-002: applying `direction='paid'` writes a negative ledger amount.
- [ ] AC-003: applying `direction='paid'` decreases the balance.
- [ ] AC-004: applying `direction='owed'` increases the balance.
- [ ] AC-005: a negative amount is refused for both directions.
- [ ] AC-006: `'PAID'`, `''`, and a missing direction are all refused.
- [ ] AC-007: the card preview states the direction in Persian and no ledger row
  exists before approval.
- [ ] AC-008: rejecting a player declaration notifies the player that nothing
  changed and renders the unchanged balance from the service.
- [ ] AC-009: an open `owed` card does not block a new `paid` card.
- [ ] AC-010: the full suite, compileall, and `git diff --check` pass.

## Test Mapping

- AC-001 -> `tests/test_spec_043.py::PaymentDirectionTests::test_prepare_refuses_a_declaration_without_direction`
- AC-002, AC-003 -> `tests/test_spec_043.py::PaymentDirectionTests::test_a_declared_payment_decreases_the_balance`
- AC-004 -> `tests/test_spec_043.py::PaymentDirectionTests::test_a_declared_debt_increases_the_balance`
- AC-005, AC-006 -> `tests/test_spec_043.py::PaymentDirectionTests::test_an_invalid_direction_or_amount_is_refused`
- AC-007 -> `tests/test_spec_043.py::PaymentDirectionTests::test_the_card_names_the_direction_and_waits_for_approval`
- AC-008 -> `tests/test_spec_043.py::PaymentDirectionTests::test_a_rejected_declaration_tells_the_player_nothing_changed`
- AC-009 -> `tests/test_spec_043.py::PaymentDirectionTests::test_an_open_debt_card_does_not_block_a_payment_card`
- AC-010 -> `tests/test_spec_contracts.py::SpecContractTests`

## Safety Constraints

- An unrecognised or missing direction fails closed; nothing is written.
- A declaration never writes a ledger row before an admin approves the card.
- A player can only declare for their own linked record.
- The balance shown to a player is produced by `Service.debt`, never by the
  model.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- `preview` prints a «نوع اعلام: …» line instead of a signed amount, so a raw
  `-150000` cannot be read as a balance change on its own.
- `notify_requester` is skipped when the requester's chat *is* the admin group,
  so an admin declaring for themselves is not messaged twice.

## Limitations

- The model still chooses between `owed` and `paid`. If it picks `owed` for a
  clear payment, the card now says «بدهی» explicitly and an admin must reject
  it, but nothing corrects it automatically. A follow-up may make the card ask
  the player to confirm the direction when intent is ambiguous.
- `adjustment` keeps accepting signed amounts from admins.
## Requirements
