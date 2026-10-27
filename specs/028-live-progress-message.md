# SPEC-028: Live progress message with a separate final answer

Status: verified
Owner: agent
Created: 2026-10-27
Supersedes: SPEC-027

## Problem

SPEC-027 tried to show progress by editing the acknowledgement message, but that
same message was already the place where the final answer was written
(`app/bot.py`, the `can_edit` branch inherited from SPEC-002).  One message had
two owners, which produced three user-visible failures on the live bot:

1. **A frozen progress line.** When the answer exceeded 3500 characters,
   `can_edit` was false, so the bot sent a new message and the line
   «⏳ پیامتان دریافت شد…» stayed on screen forever with no answer attached.
2. **No progress at all for coalesced turns.** `TurnBuffer._pop` keeps only the
   *last* fragment, and `_ack` lives on the *first* one, so
   `acknowledgement=None` and nothing was edited.  Sending several messages in a
   row therefore made progress *less* likely.
3. **No progress on commands.** `app/main.py` line ~315 called
   `bot.process(update, acknowledgement)` without `client=client`, so
   `progress()` always returned `None`.

SPEC-027 also capped progress at **one** edit per turn (its AC-007) and listed
per-tool progress as an explicit non-goal.  A single edit is a start notice, not
a progress feed, so even the working path could not show that the bot was
moving from one step to the next.

## Goal

Give the person one dedicated message that visibly moves through the real work
of the turn, and keep the answer in a separate message of its own.

```
user sends a message
  |
  +-- progress message (the acknowledgement, edited step by step):
  |     «⏳ پیامتان دریافت شد؛ در حال بررسی…»
  |     «🔎 در حال خواندن اطلاعات…»
  |     «✍️ در حال ثبت تغییرات…»
  |     «💬 در حال آماده‌سازی پاسخ…»
  |
  +-- final answer (always a NEW message, never an edit of the progress message)
```

The progress message belongs to the progress feed.  Once the turn ends it is
left alone for good; the answer never replaces it and never edits it.

## Scope

- `Bot.progress` becomes `TurnProgress`: a multi-step editor over the
  acknowledgement message with de-duplication and a minimum interval.
- Real steps, derived from what the agent is actually doing (the tool it is
  about to run or has just run), not a timer and not invented text.
- `Agent.run` receives the progress callable and reports steps from
  `call_model` and `run_tools_sequentially`.
- `TurnBuffer._pop` carries `_ack` across coalescing so a merged turn still has
  a progress message.
- The final answer always goes through `Service`/`Database.say`, i.e. a new
  message.  The `can_edit` branch is removed.

## Non-goals

- No token streaming and no `graph.stream`; `Agent.run` keeps `graph.invoke`.
  Steps are reported from inside the graph nodes, which needs no new dependency
  and no change to the invoke contract.
- No progress when the turn has no acknowledgement (a bare «سلام», a callback
  query, or an update the bot ignores).
- No change to financial rules, ledger semantics, or the SQLite schema.
## Contracts

- `Bot.progress(client, acknowledgement)` returns a `TurnProgress` or `None`.
  It returns `None` when no client is given, when `PROGRESS_UPDATES` is
  disabled, or when the acknowledgement lacks positive integer
  `chat_id`/`message_id`.
- `TurnProgress.step(text)` returns `True` when it sent an edit and `False`
  otherwise.  It never raises: every `APIError` and every unexpected exception
  is logged at debug level and swallowed, because progress must never fail a
  turn.
- Unknown step texts are ignored: only the fixed catalogue in `PROGRESS_STEPS`
  may reach a user, so a tool name can never leak a payload.
- A step that repeats the currently displayed text sends nothing.  Two steps are
  never sent closer than `PROGRESS_MIN_INTERVAL` seconds apart, so a fast turn
  cannot burst the provider.
- `TurnProgress.close()` marks the feed finished and **removes** the progress
  message, because the answer is already on its way as a new message and a
  leftover «در حال …» line would contradict it.  Later `step()` calls are
  no-ops.  The final answer path calls it before replying.
- The removal is fail-open like every other feed call: an `APIError` or any
  unexpected exception is swallowed at debug level, so a message the bot can no
  longer delete (older than 48 hours, already deleted by the person) never fails
  the turn.
- A feed that never showed a step has nothing to remove, so `close()` sends
  nothing for it.  This keeps the "no progress" routes at zero extra requests.
- The progress payload mirrors the acknowledgement: same `chat_id`, same
  `message_id`, plain text, no `reply_markup` (SPEC-016 keeps keyboards off
  edits).  Each edit is a direct `client.call`, never an outbox row.
- `Bot.process(update, acknowledgement=None, client=None)` keeps its signature;
  every call site that can carry an acknowledgement must pass `client`.
- `PROGRESS_UPDATES=0|false|no|off` disables the feed without any other change.
- The final reply is always sent with `Database.say`; no reply path calls
  `editMessageText` on the acknowledgement message.

### Step mapping

| Moment | Step text |
| --- | --- |
| The model is called for the first time in a turn | `PROGRESS_START` |
| A read-only tool is about to run | `PROGRESS_READING` |
| A mutating tool is about to run | `PROGRESS_WRITING` |
| The model is called after at least one tool result | `PROGRESS_ANSWERING` |

## Acceptance Criteria

Each criterion is written from what the person sees on the phone, not from the
number of API calls, so a test can fail for a UX reason.

- [x] AC-001: after a turn ends, the person has **two separate messages**: the progress message and the answer, with different text. The answer never replaces the progress line.
- [x] AC-002: a turn that runs at least one tool shows at least two different progress texts on the same acknowledgement message, in the order of the step mapping.
- [x] AC-003: no progress text ever remains the last thing the person sees about an answered turn — every turn that acknowledges also delivers an answer in its own message, even when the answer is longer than the edit limit.
- [x] AC-004: a coalesced turn (several fragments merged into one) still edits its progress message, because the acknowledgement survives the merge.
- [x] AC-005: a command/receipt/contact turn that reaches `Bot.process` with a client edits its progress message.
- [x] AC-006: `PROGRESS_UPDATES=0` sends no progress edit and the answer is still a new message.
- [x] AC-007: a step that repeats the displayed text sends nothing, and two steps are never sent closer than the minimum interval.
- [x] AC-008: an `APIError` or unexpected exception from a progress edit does not stop the turn; the answer is still produced.
- [x] AC-009: every progress text comes from the fixed catalogue and contains no amount, phone number, player name, or receipt content; an unknown text is not sent.
- [x] AC-010: `close()` stops the feed: no edit is sent after the answer is produced.
- [x] AC-011: after an answered turn the progress message is gone — the person is left with the answer only, and a failure to remove it never breaks the turn or suppresses the answer.
- [x] AC-012: a feed that never displayed a step sends no removal request, so deterministic routes add no extra traffic.

## Test Mapping

- AC-001 -> `tests/test_sans.py::ProgressUpdateTests::test_answer_is_a_new_message_not_an_edit`
- AC-002 -> `tests/test_sans.py::ProgressUpdateTests::test_steps_follow_the_tools_that_run`
- AC-003 -> `tests/test_sans.py::ProgressUpdateTests::test_long_answer_never_leaves_a_frozen_progress_line`
- AC-004 -> `tests/test_sans.py::ProgressUpdateTests::test_coalesced_turn_keeps_its_acknowledgement`
- AC-005 -> `tests/test_sans.py::ProgressUpdateTests::test_command_turn_reports_progress`
- AC-006 -> `tests/test_sans.py::ProgressUpdateTests::test_disabled_progress_sends_no_edit`
- AC-007 -> `tests/test_sans.py::ProgressUpdateTests::test_repeated_step_is_not_resent`
- AC-008 -> `tests/test_sans.py::ProgressUpdateTests::test_progress_failure_never_breaks_the_turn`
- AC-009 -> `tests/test_sans.py::ProgressUpdateTests::test_progress_text_carries_no_data`
- AC-010 -> `tests/test_sans.py::ProgressUpdateTests::test_progress_stops_after_close`
- AC-011 -> `tests/test_sans.py::ProgressUpdateTests::test_progress_message_is_removed_after_the_answer`
- AC-012 -> `tests/test_sans.py::ProgressUpdateTests::test_close_without_a_step_sends_no_request`

## Safety Constraints

- Progress is best-effort and fail-open: it may be missing but never blocks,
  retries, or mutates domain state.
- No player data, money, or receipt content leaves the bot through this channel;
  the catalogue is fixed and unknown texts are dropped.
- The answer, chunking, keyboards, the outbox, and the `BEGIN IMMEDIATE`
  transaction are untouched.
- The progress message is never used to carry an answer, so a long answer can no
  longer leave a stale progress line on screen.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

Manual check on the live bot: send a question that needs a tool (for example one
that lists sessions).  The acknowledgement should advance through the steps and
the answer should arrive as a separate message, leaving the progress message
unchanged afterwards.

## Notes

- `SPEC-027` is superseded.  Its AC-007 (at most one edit per turn) is replaced
  by AC-002/AC-007 here; its single-edit design is what made a real progress feed
  impossible.
- The `can_edit` branch from SPEC-002 is removed on purpose: that branch was the
  second owner of the acknowledgement message, and two owners of one message is
  the root cause of the frozen line.