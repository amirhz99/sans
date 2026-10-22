# SPEC-015: An admin can close open proposal cards on request

Status: verified
Owner: agent
Created: 2026-10-22

## Problem

SPEC-013 closes a proposal card automatically once it is older than
`PROPOSAL_TTL`, at startup and every five minutes. That covers cards that have
already aged out, but it leaves two gaps that the admins actually hit:

- A card that is younger than 24 hours but is already known to be unwanted
  (a wrong amount, a session that was cancelled, a batch the admin regrets)
  can only be closed by clicking its «رد» button. When the chat has scrolled
  past it, or when the card was delivered before `pending.message_id` existed
  and has no visible buttons left, there is no way to close it at all.
- Asking the assistant in Persian («کارت‌های منقضی را کنسل کن») reaches the
  model, not the sweep. The model has no such tool, so it correctly answers
  «ابزارش را ندارم» and nothing happens. The only implicit triggers were
  `/pending` and a restart, neither of which the admin was told about.

The live database shows the effect: 24 `commands` cards, 6 to 10 hours old,
`message_id IS NULL`, still `pending`.

## Goal

An admin in the configured admin group can close open proposal cards with one
explicit command or one explicit request to the assistant, and the reply states
exactly how many cards were closed, how many stayed open, and that no money,
session, or receipt changed.

## Scope

- `Bot.proposal_staleness(now=None, cutoff=None)` and
  `Bot.sweep_proposals(now=None, cutoff=None)` accept an explicit epoch cutoff
  so the TTL sweep and an admin request share one code path.
- New `Bot.expire_proposals(older_than_hours, actor=None)`:
  - `older_than_hours` is validated as an integer in `[0, 8760]`;
  - `0` means "every open `commands` card";
  - only rows with `status='pending'` and `kind='commands'` are touched;
  - the number of closed cards is returned and written to `audit_logs`.
- New `/expire [hours]` admin slash command. Without an argument it applies the
  24 hour TTL, i.e. it behaves exactly like the background sweep. `/expire 0`
  closes every open `commands` card.
- New `manage_records` agent operation `expire_proposals` with
  `data_json='{"older_than_hours": N}'`, so the assistant can satisfy the
  request instead of answering that it lacks the tool.
- `content/bot-content.json` documents `/expire` in `admin_help` and the
  system prompt maps «کارت‌ها/پیشنهادهای منتظر را ببند» to that operation.
- The `/pending` reply names the command that would close the cards it lists.

## Non-goals

- No change to `PROPOSAL_TTL`, the guard fingerprint, or the two expiry
  messages produced by `Bot.confirm`.
- No new ability to approve, edit, or re-send a card: closing is one-way
  bookkeeping and the admin must ask for the work again.
- `identity`, `claim`, and `binding` cards are not closed by this command. They
  are a human identity decision and the README already states that they stay
  until they are reviewed or cancelled.
- No schema change, no new dependency, no new setting.
- The legacy JSON intent route still cannot produce this action; only a slash
  command and the agent tool can.


## Contracts

`Bot.expire_proposals(older_than_hours=PROPOSAL_TTL // 3600, actor=None)`:

- raises `UserError` for a non-integer, negative, or larger-than-`8760` value;
- deletes nothing: closed rows keep `status='expired'`, so `Bot.confirm` and
  `Service._pending_reference` already treat them as dead;
- queues `editMessageReplyMarkup` with an empty keyboard only for cards that
  have a positive integer `message_id`;
- never writes `ledger`, `players`, `sessions`, `attendance`, or `receipts`.

`/expire` is parsed only when the sender is an admin in the admin group, the
same boundary as `/setcard`, `/merge`, and `/rename`. In any other chat it falls
through to the existing help reply, so it leaks nothing and does nothing.

`manage_records(operation='expire_proposals')` is behind the existing
`if not admin: return 'خطا: ...'` guard, and rejects unknown keys in
`data_json` like every other operation of that tool.

The reply is a fixed sentence, not a preview of the cards:
`N کارت پیشنهاد بسته شد؛ هیچ حساب، سانس یا رسیدی تغییر نکرد.` It is followed by
one line per remaining group: command cards newer than the threshold, and
identity/binding cards left for review.

## Acceptance Criteria

- [x] AC-001: `/expire 0` closes every open `commands` card and reports the
  count, while balances, sessions, and receipts are unchanged.
- [x] AC-002: `/expire` without an argument closes only cards older than the
  24 hour TTL and leaves younger ones `pending`.
- [x] AC-003: `/expire 12` closes cards older than 12 hours and leaves younger
  ones `pending`.
- [x] AC-004: `/expire 0` does not touch `identity`, `claim`, or `binding`
  cards and says so in the reply.
- [x] AC-005: a closed card can no longer be approved; clicking it produces the
  existing "not available" message and applies nothing.
- [x] AC-006: a non-integer, negative, or absurd hour value is refused with a
  `UserError` and closes nothing.
- [x] AC-007: `/expire` from a non-admin, or outside the admin group, closes
  nothing.
- [x] AC-008: closing a delivered card queues exactly one empty-keyboard
  `editMessageReplyMarkup`, and a card without `message_id` queues none.
- [x] AC-009: the action is recorded in `audit_logs` with the threshold and the
  number of closed cards.
- [x] AC-010: the agent tool `expire_proposals` closes the cards for an admin
  and is refused for a non-admin.

## Test Mapping

- AC-001 -> `tests/test_sans.py::ProposalExpiryCommandTests.test_expire_zero_closes_every_open_card`
- AC-002 -> `tests/test_sans.py::ProposalExpiryCommandTests.test_expire_defaults_to_the_ttl`
- AC-003 -> `tests/test_sans.py::ProposalExpiryCommandTests.test_expire_accepts_a_custom_threshold`
- AC-004 -> `tests/test_sans.py::ProposalExpiryCommandTests.test_expire_keeps_identity_cards_for_review`
- AC-005 -> `tests/test_sans.py::ProposalExpiryCommandTests.test_closed_card_cannot_be_approved_afterwards`
- AC-006 -> `tests/test_sans.py::ProposalExpiryCommandTests.test_invalid_threshold_closes_nothing`
- AC-007 -> `tests/test_sans.py::ProposalExpiryCommandTests.test_expire_is_admin_only`
- AC-008 -> `tests/test_sans.py::ProposalExpiryCommandTests.test_expire_removes_buttons_only_when_the_message_is_known`
- AC-009 -> `tests/test_sans.py::ProposalExpiryCommandTests.test_expire_is_audited`
- AC-010 -> `tests/test_agent_contracts.py::ExpireProposalsToolTests.test_admin_closes_cards_through_the_tool`

## Safety Constraints

- Unauthorized or malformed requests fail closed and change no row.
- The command is bookkeeping: it cannot apply a financial command, cannot
  create a ledger row, and cannot finalize a session.
- No player name, balance, phone number, or receipt content is added to the
  reply or to the audit row.
- The 24 hour default keeps the destructive-looking `0` value explicit: an
  admin who just wants the automatic behavior never has to type it.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- The 24 live cards were created before SPEC-013 recorded message ids, so
  closing them cannot remove buttons that are no longer there; their text stays
  in the chat and any click now fails closed with the "not available" message.
- `expire_proposals` defaults to the TTL rather than `0` on purpose: "close the
  expired cards" must not silently discard a card the admin created a minute
  ago. `/expire 0` is the explicit "close everything" form.
- `expire_proposals` does not commit: like `sweep_proposals` it relies on the
  caller's transaction. `Bot.process` and `app.main.run` already provide one, so
  a direct caller must open `with db.conn:` or the change is rolled back.

## Validation Result

- `python3 -m unittest discover -s tests -t . -v`: 190 tests, OK (1 skipped).
- `python3 -m compileall -q app tests`: OK.
- `git diff --check`: clean.
- Dry run on a backup copy of `data/sans.sqlite3`: 23 `commands` cards closed,
  the single `identity` card kept, `ledger`/`sessions`/`receipts`/`players`
  unchanged, audit row `{"older_than_hours":0,"closed":23}`.
- Live configuration smoke test on a copy (real `Config` and
  `content/bot-content.json`): `/expire 0` reports the closed count, `/expire`
  reports that nothing was old enough, `/help` lists `/expire`, and the same
  command in a private chat only returns the help text.
- Applied to `data/sans.sqlite3` after backing it up to
  `data/backups/pre-spec-015.sqlite3`: pending went from 24 to 1 (identity
  only), and the bot was restarted to load the new code.
