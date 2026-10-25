# SPEC-025: Linked players share a report and can settle for each other

Status: verified  
Owner: agent  
Created: 2026-10-25

## Problem

Brothers, a father and his son, or two close friends usually pay for each other.
Today every player is an island: a link has to be made by hand, and nobody can
see or settle somebody else's balance.  Family names also hint at a relation,
but a guess must never become a financial relationship on its own.

## Scope

- A symmetric `player_links` table (one deterministic row per pair).
- A player request in a private chat, confirmed by an admin.
- An admin command and an agent tool to link/unlink.
- A suggestion of pairs that share a family name, always confirmed by an admin.
- A short shared block in a player's own report, and a settle request for a
  linked player that still needs the admin's approval click.

## Non-goals

- No transitive grouping: A–B and B–C do not let A see C.
- No automatic link from a family name, a phone number, or a shared receipt.
- No change to the ledger rule, `settle` semantics, or the billing formula.
- No full history of another player is ever shown to a player.

## Contracts

- `Service.link_players(left, right, actor)` and `unlink_players(...)`.
- `Service.links(player_id)` returns the direct partners only.
- `Bot.request_link(partner, user_id, chat_id)` files an approval card.
- `Bot.settle_partner(partner, user_id, chat_id)` files a `settle` proposal.

## Goal

Let a player see the balance of the people he pays for, and let him settle it,
without ever moving money without an admin click.

## Acceptance Criteria

- [x] AC-001: A link is symmetric, unique per pair, and refuses a self link.
- [x] AC-002: Only an approved card (or an admin tool through that card) creates
      or removes a link; nothing links without a click.
- [x] AC-003: A player can request a link from a private chat and the request
      becomes one card in the admin group with no financial side effect.
- [x] AC-004: A linked player's own report shows the partner's name and balance;
      an unlinked player never sees anybody else's numbers.
- [x] AC-005: A settle request for a partner writes no ledger row before the
      admin approves it, and exactly one `settle` row after.
- [x] AC-006: A settle request for somebody who is not a direct partner fails
      closed with no side effect.
- [x] AC-007: The suggestion lists only pairs that share a family name and skips
      relation words, single-token names, and existing links; it never links on
      its own.
- [x] AC-008: The model context carries only id, name and balance of direct
      partners, never phones, receipts, or ledger notes.
- [x] AC-009: Deleting or merging a linked player never leaves a dangling link,
      and the merge carries the links to the survivor.

## Test Mapping

- AC-001 -> `tests/test_sans.py::PlayerLinkTests::test_a_link_is_symmetric_unique_and_refuses_self`
- AC-002 -> `tests/test_sans.py::PlayerLinkTests::test_linking_happens_only_through_the_approved_card`
- AC-003 -> `tests/test_sans.py::LinkRequestTests::test_a_player_request_becomes_one_admin_card`
- AC-003 -> `tests/test_sans.py::LinkRequestTests::test_rejecting_the_request_creates_no_link`
- AC-004 -> `tests/test_sans.py::SharedReportTests::test_a_linked_player_sees_the_partner_balance`
- AC-004 -> `tests/test_sans.py::SharedReportTests::test_an_unlinked_player_sees_no_other_balance`
- AC-005 -> `tests/test_sans.py::LinkedSettleTests::test_settling_a_partner_waits_for_the_admin`
- AC-006 -> `tests/test_sans.py::LinkedSettleTests::test_settling_a_stranger_fails_closed`
- AC-007 -> `tests/test_sans.py::LinkCandidateTests::test_suggestions_use_family_names_only`
- AC-008 -> `tests/test_agent_contracts.py::LinkContextTests::test_context_carries_only_direct_partners`
- AC-009 -> `tests/test_sans.py::PlayerLinkTests::test_delete_and_merge_keep_links_consistent`

## Safety Constraints

A link is a read permission plus a proposal path, never a payment path.  Every
money movement behind it still goes through the existing approval card, the
`guard` fingerprint and the audit log.  The schema change is additive and the
live database is only touched by the operator.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

The family-name suggestion is advisory by design: `similar_players` already shows
how expensive a false identity guess is, so this feature only proposes.
