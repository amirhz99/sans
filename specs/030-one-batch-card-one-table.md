# SPEC-030: One batch card is one table, and it is never attached twice

Status: verified
Owner: agent
Created: 2026-10-27

## Problem

Two defects combined to make a multi-player approval card unreadable on the live
bot.

1. **The card repeated its own heading once per player.** `Bot.propose_commands`
   built the card body as
   `'\n\n'.join(Service.preview(c) for c in commands)`.  Each `preview` is a
   standalone block that starts with an action heading, so an eight-player settle
   produced eight stacked blocks of «اصلاح حساب / بازیکن / توضیح / مبلغ» and no
   reader could compare the amounts.
2. **The whole body could be appended to the model reply a second time.**
   `app/agent.py` guarded the append with `'کد:' not in reply`.  The card body
   *always* contains «کد:», so the guard answered "the model did not mention the
   card" only when the model omitted the code word entirely.  A model that echoed
   the tool result — the common case, because the tool returns the card as its
   result — therefore received the card pasted on top of its own copy.  One
   message could contain the same block three times: the model's own summary, the
   model's echo of the tool result, and the appended `proposal_preview`.

Live evidence: `agent_logs` id 963 is the tool result of an eight-entry
`propose_financial_change` and id 965 is the model reply to it.

## Goal

An admin who reads a batch card sees **one** table with a total, and a message
never carries two copies of the same card.

## Scope

- `Bot.batch_table(commands)` returns one Markdown table for a batch of two or
  more `adjustment`/`settle` commands, and `None` for anything else.
- `Bot.propose_commands` and `Bot.pending_summary` use it, so the card and the
  `/pending` list show the same shape.
- `SansAgent._finalize_reply(reply, effects)` owns the "attach the card at most
  once" rule; `SansAgent.run` calls it.

## Non-goals

- No change to `Service.preview`, so a one-player card is byte-for-byte identical
  (SPEC-011 compatibility).
- No change to the ledger, `settle` semantics, the proposal guard, or the schema.
- No change to when a card is created, who may approve it, or its keyboard.
- No table for mixed batches (money plus attendance, settings, broadcast): those
  keep the classic previews because their blocks are not rows of one table.

## Contracts

`Bot.batch_table(commands)`:

- returns `None` unless `commands` is a list of at least two commands whose
  actions are all in `{adjustment, settle}`;
- otherwise returns a header line «ثبت پرداخت برای N نفر», the header row
  `| بازیکن | مبلغ | توضیح |`, one row per command, and a bold total row;
- an `adjustment` row shows `args['amount']`; a `settle` row shows
  `-guard['balance']`, the exact amount the approval will write;
- the note is whitespace-collapsed and truncated to `TABLE_NOTE_LIMIT`
  characters with an ellipsis, so one long note cannot dominate the card;
- it reads players and balances but never writes.

`Bot.propose_commands` (unchanged elsewhere):

- keeps `ثبت شود؟ کد: <id>`, `تمام مبلغ‌ها تومان هستند.`, the SPEC-017 default
  warning, the 1–30 limit, and the duplicate-target rejection;
- uses the table when `batch_table` returns one and the classic previews
  otherwise.

`SansAgent._finalize_reply(reply, effects)`:

- returns `reply` unchanged when there is no `proposal_preview` or when the
  stripped preview already occurs in `reply`;
- returns the preview when the reply is empty;
- appends the preview with one blank line otherwise.

## Acceptance Criteria

- [x] AC-001: a card for two or more `adjustment`/`settle` commands contains exactly one `| بازیکن |` header and no repeated action heading.
- [x] AC-002: a one-player card is unchanged: it still starts with `Service.preview` and contains no table.
- [x] AC-003: a batch card keeps «کد: <id>» and «تمام مبلغ‌ها تومان هستند».
- [x] AC-004: a batch card shows a total row equal to the sum of its rows.
- [x] AC-005: a `settle` row shows `-balance`, and approving the card still brings every listed balance to zero.
- [x] AC-006: a mixed batch keeps the classic per-command previews.
- [x] AC-007: `Bot.pending_summary` shows the same single table for a pending batch card.
- [x] AC-008: a model reply that already contains the card is not given a second copy.
- [x] AC-009: a reply that mentions the code but omits the card still receives the card once (the old guard lost it).
- [x] AC-010: an empty reply is replaced by the card, and a turn with no card leaves the reply untouched.

## Test Mapping

- AC-001 -> `tests/test_sans.py::BatchCardLayoutTests::test_batch_card_is_one_table_not_one_block_per_player`
- AC-002 -> `tests/test_sans.py::BatchCardLayoutTests::test_single_card_is_unchanged`
- AC-003 -> `tests/test_sans.py::BatchCardLayoutTests::test_batch_card_keeps_the_code_and_the_currency_note`
- AC-004 -> `tests/test_sans.py::BatchCardLayoutTests::test_batch_card_totals_the_amounts`
- AC-005 -> `tests/test_sans.py::BatchCardLayoutTests::test_settle_row_shows_the_balance_the_approval_will_apply`
- AC-006 -> `tests/test_sans.py::BatchCardLayoutTests::test_mixed_batch_keeps_the_classic_previews`
- AC-007 -> `tests/test_sans.py::BatchCardLayoutTests::test_pending_list_shows_the_same_table`
- AC-008 -> `tests/test_agent_contracts.py::AgentReplyPreviewTests::test_model_reply_that_echoes_the_card_is_not_doubled`
- AC-009 -> `tests/test_agent_contracts.py::AgentReplyPreviewTests::test_reply_with_the_code_word_but_no_card_still_gets_the_card`
- AC-010 -> `tests/test_agent_contracts.py::AgentReplyPreviewTests::test_empty_reply_is_the_card_itself`, `tests/test_agent_contracts.py::AgentReplyPreviewTests::test_reply_without_a_card_is_left_alone`

## Safety Constraints

- The card remains non-executing: it only creates a `pending` row, and the money
  still moves only through `Bot.confirm` inside the `BEGIN IMMEDIATE`
  transaction with the SPEC-011 guard.
- A `settle` row is derived from `guard(command)['balance']`, so a stale card is
  still rejected rather than showing a number that is no longer true.
- No new data reaches the card: it shows the player name, the amount, and the
  note that the classic preview already showed.
- `_finalize_reply` is a pure string function; it cannot raise on a missing or
  malformed preview because it only checks truthiness and substring presence.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

Manual check on the live bot: ask for a multi-player settlement.  The card should
be a single table with a total, the answer should describe it without repeating
it, and `/pending` should show the same table.

## Notes

- The doubling is a *design* failure, not only a model failure: the tool result
  already contains the card, so the model naturally echoes it, and a guard that
  looks for a word the card always contains can never detect that.
- Truncating the note is deliberate.  Eight full notes made the card longer than
  the 3500-character chunk limit and hid the total below the fold.
- Markdown tables rely on the client rendering `|` rows.  If a future client stops
  rendering them the table stays readable line by line, which the eight stacked
  blocks were not.