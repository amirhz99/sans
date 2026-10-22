# SPEC-013: Expired proposal cards are swept proactively

Status: verified
Owner: agent
Created: 2026-10-22

## Problem

SPEC-011 gave every proposal a 24 hour TTL, but expiry only happened lazily:
`Bot.confirm` expired a card when somebody clicked it, and `/pending` expired
the rest. Nobody clicked the 61 stale cards that were sitting in
`data/sans.sqlite3`, so:

- `/pending` had to scan and print cards that could never be applied;
- `Service._pending_reference` treated those dead cards as live references, so
  `merge` and `delete` of a player could be blocked by a proposal from the
  previous day;
- the admin group still showed «تأیید» buttons that only produced an error.

The bot also had no way to know which chat message a proposal card was, because
`pending` did not store the delivered message id and `outbox` did not record
what an outbound row was for. So it could not tidy up its own stale cards.

## Goal

Stale proposals are closed without a human click, at startup and then at a
bounded interval, and the chat message of a card that is closed is edited so
its buttons can no longer be pressed. Cards created before this change (no
stored message id) are still closed, just without an edit.

## Scope

- Additive `pending.message_id INTEGER` and `outbox.ref TEXT` columns, migrated
  in `Database.__init__` the same way `sessions.session_at` and `pending.guard`
  are.
- `Database.queue(method, payload, ref=None)` and
  `Database.say(chat_id, text, buttons=None, ref=None)` carry the reference on
  the last chunk (the one that owns the inline keyboard).
- `flush_outbox` records the delivered provider `message_id` against the
  referenced row after a successful call.
- `Bot.proposal_staleness(now=None)` returns the cards that must be closed;
  `Bot.sweep_proposals(now=None)` closes them and queues
  `editMessageReplyMarkup` with an empty keyboard for cards that have a known
  message id.
- `Bot.expire_stale_proposals` delegates to `sweep_proposals`, so `/pending`
  and the timer share one rule.
- `app.main.run` sweeps once at startup and at most every five minutes.

## Non-goals

- No change to the TTL, the guard fingerprint, or the two expiry messages.
- No automatic deletion of admin messages: a closed card keeps its text, only
  its buttons are removed.
- No change to outbox retry/backoff behavior other than recording delivery.
- No new dependency.

## Contracts

`pending.message_id` is `NULL` until the provider confirms delivery. A sweep
touches only rows with `status='pending'`, `kind='commands'`, and
`created_at < now - 86400`; it never changes `ledger`, `players`, `sessions`, or
`receipts`.

`outbox.ref` is `NULL` for ordinary messages and `proposal:<id>` for an
approval card. A successful `sendMessage`/`editMessageText` stores the returned
`message_id` (or the payload `message_id` for an edit) on the pending row.

`editMessageReplyMarkup` with `reply_markup: {"inline_keyboard": []}` is queued
only when a positive integer `message_id` and a chat id are known. Cards
without a message id are still marked `expired`.

`Bot.expire_stale_proposals(now=None)` keeps its return value (the number of
closed cards) so the `/pending` reply text is unchanged.

## Acceptance Criteria

- [x] AC-001: a card older than the TTL is closed by `Bot.sweep_proposals`
  without a click, and a fresh card stays `pending`.
- [x] AC-002: the sweep queues exactly one `editMessageReplyMarkup` with an
  empty keyboard for a closed card whose message id is known.
- [x] AC-003: a closed card without a message id produces no edit row and no
  error.
- [x] AC-004: a successful `sendMessage` with `ref='proposal:<id>'` stores the
  provider message id on that proposal.
- [x] AC-005: `Database` adds `pending.message_id` and `outbox.ref` to an
  existing database that predates them.
- [x] AC-006: `/pending` still closes old cards and reports the same count.
- [x] AC-007: the polling loop calls the sweep at startup and periodically,
  without sending any API request of its own.

## Test Mapping

- AC-001 -> `tests/test_sans.py::ProposalSweepTests.test_sweep_closes_only_cards_older_than_the_ttl`
- AC-002 -> `tests/test_sans.py::ProposalSweepTests.test_sweep_removes_buttons_of_a_delivered_card`
- AC-003 -> `tests/test_sans.py::ProposalSweepTests.test_sweep_tolerates_cards_without_a_message_id`
- AC-004 -> `tests/test_sans.py::ProposalSweepTests.test_delivery_records_the_proposal_message_id`
- AC-005 -> `tests/test_sans.py::ProposalSweepTests.test_existing_database_gains_the_new_columns`
- AC-006 -> `tests/test_sans.py::ProposalStalenessTests.test_pending_list_closes_old_proposals`
- AC-007 -> `tests/test_sans.py::ProposalSweepTests.test_run_sweeps_before_polling`

## Safety Constraints

- The sweep is bookkeeping only: it marks proposals `expired` and queues a
  keyboard edit; it never applies a financial command.
- A missing or non-integer `message_id` is ignored instead of raising, so one
  legacy row cannot stop the sweep.
- No message text, balance, or player name is added to the sweep row.
- Existing `/pending` behavior and its Persian text remain valid.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- The first sweep after deployment closes the 61 legacy cards; they have no
  stored message id, so their buttons remain until the admin deletes the
  message, but pressing them now fails closed with the TTL message.
- The interval is an in-process timer, not a new setting, so it cannot drift
  from the deployed code and needs no migration.

## Validation Result

- `python3 -m unittest discover -s tests -t . -v`: 177 tests, OK (1 skipped).
- `python3 -m compileall -q app tests`: OK.
- `git diff --check`: clean.
- Dry run on a backup copy of `data/sans.sqlite3`: migration added
  `pending.message_id` and `outbox.ref`, created `contacts`/`outreach`, and the
  sweep closed 37 of 61 pending cards (the other 24 were younger than the TTL).
- After the real restart, `bot.log` shows `Closed 37 expired proposals at
  startup` and the live database reports 37 `expired` rows.
