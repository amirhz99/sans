# SPEC-037: Links — the untested half

Status: verified  
Owner: agent  
Created: 2026-10-28

## Problem

SPEC-025 introduced player links and their approval cards. A coverage sweep
after SPEC-036 showed that the *add* side was tested but the *remove* side was
not, and one card renderer was never executed at all:

- `Service.unlink_players` — never reached by any test. No test had ever
  removed a link, so a broken `DELETE`, a missing audit row, or a link that
  cannot be removed would ship silently.
- `Service.format_link_candidates` — never reached by any test, although it
  builds the text an admin reads before approving a suggestion.
- `/suggestlinks` was exercised only through its approval path, never checked
  for what it actually says.

The direct symptom was invisible: everything worked when tried by hand. The
risk is regression, in a feature that grants one player the right to see
another player's balance.

## Goal

Removing a link and rendering a suggestion card are covered by tests, so a
regression in either fails the suite instead of reaching production.

## Scope

- Tests for `Service.unlink_players`: success, idempotence failure, self-unlink,
  ledger untouched, link-row count, order insensitivity.
- Tests for `Service.format_link_candidates`: populated and empty.
- Tests for `/suggestlinks`: the card text and that nothing links by itself.
- Tests for `/unlink2`: the approval card closes, and a player cannot unlink.

## Non-goals

- No behaviour change. This specification adds tests only.
- No change to linking rules, the `player_links` schema, or SPEC-025 semantics.

## Contracts

- `unlink_players(left, right)` keeps raising `UserError` when the pair is not
  linked and when both sides are the same player. It never writes to `ledger`.
- `format_link_candidates([])` returns the «nothing found» sentence exactly.
- `/unlink2` still only builds an approval card; only an admin click mutates.
- A player sending `/unlink2` is refused before any card is created.

## Acceptance Criteria

- [ ] AC-001: `unlink_players` removes the row, in either argument order, and
  leaves the player's balance unchanged.
- [ ] AC-002: unlinking a pair that is not linked raises `UserError` and
  changes nothing.
- [ ] AC-003: unlinking a player from himself raises `UserError`.
- [ ] AC-004: `format_link_candidates` names the family and both players for a
  real pair, and returns the «nothing found» sentence for an empty list.
- [ ] AC-005: `/suggestlinks` builds an approval card and creates no link by
  itself.
- [ ] AC-006: `/unlink2` builds a card that removes the link when approved.
- [ ] AC-007: a player sending `/unlink2` is refused and creates no card.

## Test Mapping

- AC-001 -> `tests/test_sans.py::UnlinkCase::test_unlink_removes_the_link_and_keeps_the_ledger`
- AC-002 -> `tests/test_sans.py::UnlinkCase::test_unlinking_an_unlinked_pair_fails_closed`
- AC-003 -> `tests/test_sans.py::UnlinkCase::test_unlinking_yourself_fails_closed`
- AC-004 -> `tests/test_sans.py::LinkCandidateTests::test_the_suggestion_card_names_the_pair`
- AC-005 -> `tests/test_sans.py::LinkCandidateTests::test_suggestlinks_builds_a_card_and_creates_no_link`
- AC-006 -> `tests/test_sans.py::UnlinkCase::test_the_unlink_card_closes_after_a_real_unlink`
- AC-007 -> `tests/test_sans.py::UnlinkCase::test_a_player_cannot_unlink`

## Safety Constraints

- Invalid or unauthorized requests fail closed.
- No unrelated private data is exposed to AI or users.
- Financial and persistence behavior is unchanged unless explicitly scoped.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- How the gap was found: instrumented each `Service` public method with a
  counting wrapper and ran the whole suite. Most names that no test mentions
  are still *reached* through another path (`rename_player`, `review_receipt`,
  `void_preview`, `shared_links`, …). Only `unlink_players` and
  `format_link_candidates` were never executed at all, and both are now
  covered.
- An earlier version of that instrumentation reported 16 errors. They were
  caused by the probe itself: the wrapper was installed with `setattr` on the
  class, so bound-method binding differed. With the corrected probe the same
  suite reports 0 failures and 0 errors, and the real gate is green.

## Found while writing the tests (not fixed here)

`/suggestlinks` builds the advisory text with `format_link_candidates` and
then **discards it**: `Bot` returns `propose_commands(...)` straight away, and
that card is rendered from `service.preview()`, which is generic. Measured on
the fixture: the outbox had **0 rows** after `/suggestlinks`.

So the admin gets an approval card listing the pairs but **not** the sentence
explaining that the suggestions come from matching family names and that
nothing is linked without approval. The text is reachable only through the
agent tool path (`app/agent.py:647`), where it *is* returned.

This is a UI-completeness question, not a correctness or safety bug: the card
still names both players and approving it is still the only way a link is
created. Changing the card text is a deliberate UX decision, so it is left out
of this test-only specification and recorded here instead.