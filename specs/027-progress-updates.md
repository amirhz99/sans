# SPEC-027: Live progress on the acknowledgement message

Status: superseded
Owner: agent
Created: 2026-10-27
Superseded-by: 028-live-progress-message

> **Superseded.** SPEC-027 showed progress by editing the acknowledgement
> message, but that message was already where the final answer was written, so a
> long answer left a frozen progress line on screen, coalesced turns had no
> acknowledgement at all, and the command path never passed a client.  Its
> AC-007 («at most one edit per turn») also made a real multi-step feed
> impossible.  See `specs/028-live-progress-message.md`, which keeps the
> acknowledgement as a progress feed and always sends the answer as a new
> message.  The AC list below is kept as history only.

## Problem

`app/main.py` sends a single `ACKNOWLEDGEMENT` message and then nothing happens
until the whole turn finishes.  `Bot.process` calls `Agent.run`, which wraps
`graph.invoke`, so a slow model call leaves the person staring at one static
line for up to 35 seconds with no signal that anything is still working.

## Goal

While a turn is running, the existing acknowledgement message is edited in place
with one short, plain-language progress line, so the waiting person can see that
the bot is still working.  The final answer keeps its current behaviour.

## Scope

- One extra `editMessageText` per turn, sent directly (not through the
  transactional outbox) right before the conversational model is invoked.
- The progress text is a fixed module constant, never data.
- The edit reuses the acknowledgement `message_id` that the polling loop already
  holds in `update['_ack']`.

## Non-goals

- No token streaming and no `graph.stream`; `Agent.run` keeps `graph.invoke`.
- No per-tool progress events; a per-tool feed would need `stream_mode` and was
  rejected as the higher-risk option.
- No change to the final reply path, chunking, keyboards, or the outbox.
- No progress for turns that carry no acknowledgement (commands, receipts and
  contacts produce one themselves, or have no ack message).

## Contracts

- `Bot.progress(client, acknowledgement)` returns a one-shot callable or `None`.
  It returns `None` when no client is given, when `PROGRESS_UPDATES` is disabled,
  or when the acknowledgement lacks positive integer `chat_id`/`message_id`.
- Calling the returned callable performs at most one `client.call(
  'editMessageText', ...)` per turn and returns `True`/`False`.  Every
  `APIError` or unexpected exception is logged at debug level and swallowed:
  progress is a UX aid and must never fail a turn.
- The progress payload mirrors the acknowledgement: same `chat_id`, same
  `message_id`, plain text, no `reply_markup` (SPEC-016 keeps keyboards off edits).
- `Bot.process(update, acknowledgement=None, client=None)` accepts the client as
  a new keyword; every existing call site stays valid.
- `PROGRESS_UPDATES=0|false|no|off` disables the edit without any other change.

## Acceptance Criteria

- [x] AC-001: with an acknowledgement and progress enabled, a turn that reaches the model emits exactly one `editMessageText` to the acknowledgement `message_id` before the final reply.
- [x] AC-002: the progress edit is sent directly, so it never appears in the `outbox` table and cannot be rolled back with the turn.
- [x] AC-003: without an acknowledgement no `editMessageText` is emitted.
- [x] AC-004: `PROGRESS_UPDATES=0` emits no `editMessageText` and the reply is unchanged.
- [x] AC-005: an `APIError` from the progress edit does not stop the turn; the final reply is still produced.
- [x] AC-006: the progress text is a fixed constant and contains no amount, phone number, player name, or receipt content.
- [x] AC-007: progress is emitted at most once per turn even if `step()` is called twice.

## Test Mapping

Superseded by `specs/028-live-progress-message.md`; these tests were replaced.

- AC-001 -> `tests/test_sans.py::ProgressUpdateTests::test_answer_is_a_new_message_not_an_edit`
- AC-002 -> `tests/test_sans.py::ProgressUpdateTests::test_steps_follow_the_tools_that_run`
- AC-003 -> `tests/test_sans.py::ProgressUpdateTests::test_missing_acknowledgement_emits_no_progress`
- AC-004 -> `tests/test_sans.py::ProgressUpdateTests::test_disabled_progress_sends_no_edit`
- AC-005 -> `tests/test_sans.py::ProgressUpdateTests::test_progress_failure_never_breaks_the_turn`
- AC-006 -> `tests/test_sans.py::ProgressUpdateTests::test_progress_text_carries_no_data`
- AC-007 -> `tests/test_sans.py::ProgressUpdateTests::test_repeated_step_is_not_resent`

## Safety Constraints

- Progress output is best-effort and fail-open: it may be missing but never
  blocks, retries, or mutates domain state.
- No player data, money, or receipt content leaves the bot through this channel.
- The final reply, the outbox, and the `BEGIN IMMEDIATE` transaction are
  untouched.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v   # 362 tests, all green
python3 -m compileall -q app tests
git diff --check
```

Manual check on the live bot: send a plain question and watch the
acknowledgement change to the progress line before the answer replaces it.

## Notes

- The final reply already edits the acknowledgement in place
  (`app/bot.py`, the `can_edit` branch), so the progress edit is the only
  additional request per turn.
- Rate limiting is inherent: one edit per turn, so the provider never sees a
  burst, and `PROGRESS_UPDATES=0` is a one-line rollback.