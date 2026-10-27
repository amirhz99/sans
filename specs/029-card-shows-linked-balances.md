# SPEC-029: The card reply also lists a linked player's balances

Status: verified
Owner: agent
Created: 2026-10-27

## Problem

A player who wants to pay has to send two commands. `/debt` shows what he owes
and, since SPEC-025, the balances of the people he is linked to; `/card` only
shows the account number. The natural move before a transfer — asking for the
card — therefore hides the very amount he is about to pay, and the linked
balances never appear next to it.

## Goal

A linked player who asks for the card sees the same «حساب‌های پیوندی» block he
already sees in his own report, so one message answers "how much do I send and
where". An unlinked player sees no change at all.

## Scope

- The `card` action in `Bot.handle_command` appends `Service.shared_links(...)`
  for the sender's linked player, exactly as the `debt` action already does.
- The account number itself is unchanged; the block is appended after it.

## Non-goals

- No change to SPEC-025: the block still carries only a partner's name and
  balance, never a phone number, a receipt, or another player's ledger history.
- No new permission: this reads the same data `/debt` already exposes to the
  same caller.
- No change to the ledger, the billing rules, or who may settle for whom.
- No block for an admin who asks for the card without being linked.

## Contracts

`card`:

- resolves the caller through `Service.linked(user_id)`, the same way `debt`
  does, so a group member who is not linked to a player gets the account number
  only;
- returns `Service.card()` followed by `Service.shared_links(player_id)` when a
  card is configured, the caller is linked, and the caller has at least one
  partner;
- never appends the block when no card is configured, so the "no card yet"
  message is never followed by a balances block that asks the player to settle;
- keeps the existing behaviour of asking the admin for a card when none is set;
- never fails the command: an unlinked caller or a missing player simply skips
  the block.

## Acceptance Criteria

- [x] AC-001: A linked player's card reply contains the partner's name and balance.
- [x] AC-002: An unlinked player's card reply is unchanged and contains no partner block.
- [x] AC-003: The account number is still present, before the block.
- [x] AC-004: A missing card setting still asks the admin and shows no block.

## Test Mapping

- AC-001 -> `tests/test_sans.py::CardShowsLinksTests::test_a_linked_player_card_lists_the_partner_balance`
- AC-002 -> `tests/test_sans.py::CardShowsLinksTests::test_an_unlinked_player_card_is_unchanged`
- AC-003 -> `tests/test_sans.py::CardShowsLinksTests::test_the_card_number_comes_first`
- AC-004 -> `tests/test_sans.py::CardShowsLinksTests::test_a_missing_card_setting_still_asks_the_admin`

## Safety Constraints

- The block is read-only and reuses SPEC-025's `shared_links`, so it cannot leak
  anything `/debt` would not already show.
- The partner's phone number, receipts, and full ledger stay private.
- Invalid or unlinked callers fail open to the plain card number.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- Decision: append the block instead of building a second report, so the two
  commands can never drift apart.