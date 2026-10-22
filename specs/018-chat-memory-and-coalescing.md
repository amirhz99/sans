# SPEC-018: Deeper chat memory and one turn for back-to-back messages

Status: verified
Owner: agent
Created: 2026-10-22

## Problem

Two everyday behaviors are wrong in production:

- **Memory is short and uneven.** `conversations.history` keeps 20 entries, the
  legacy JSON route replays 4 of them, the agent replays 10, and the agent's live
  snapshot shows 14 stored chat messages. A conversation that spans a session
  creation and two follow-up questions falls out of the model's view, so the
  model re-asks for a name or a date the admin already gave.
- **Back-to-back messages become separate turns.** People write in fragments:
  «سانس فردا» then «ساعت ۹ شب لغو شد» then «به همه بگو». Each fragment is
  currently its own transaction, its own model call, and its own reply, so the
  bot answers «کدام سانس؟» and «چه بگویم؟» while the answer is already in the
  next message. It also multiplies cost and can create two proposal cards for
  one intent.

## Goal

The bot remembers a longer, clearly bounded conversation, and messages that one
person sends within a short window are answered once, with the full text.

## Scope

- Memory depth as named constants instead of scattered numbers:
  - `app/db.py`: `HISTORY_LIMIT = 40` for `save_conversation`;
  - `app/agent.py`: `CHAT_MEMORY_WINDOW = 30`, `SNAPSHOT_MESSAGES = 20`,
    `CONVERSATION_HISTORY = 20`;
  - `app/clients.py`: `HISTORY_TAIL = 8` for the legacy JSON route.
- New `app/turns.py::TurnBuffer` with `add(update, now)`, `ready(now)`,
  `flush()`, and `__len__`.
- `main.run` buffers plain-text messages for `TURN_COALESCE_SECONDS` (default
  `1.2`, overridable with the `TURN_COALESCE_SECONDS` environment variable,
  clamped to `[0, 10]`), acknowledges the first fragment immediately, and
  processes one synthetic update when the window closes.
- `Bot.process` records every coalesced `update_id` in `processed_updates` and
  advances `offset` past the newest one.
- `content/bot-content.json` mentions that a multi-line message is one request,
  so the model does not answer the fragments separately.

## Non-goals

- No new table, no schema change, no new dependency.
- No change to `Intent`, the action list, permissions, or the financial rules.
- No merge across different chats or different people, and no merge of media,
  contacts, commands, or callbacks.
- No retry semantics change: a buffered fragment is not marked processed until
  its turn is actually applied, so a crash replays it instead of dropping it.

## Contracts

`TurnBuffer.add(update, now) -> 'new' | 'joined' | None`:

- `None` means "handle it now": window is `0`, the update is not a message, it
  is not plain text, or its text starts with `/`;
- `'new'` starts a buffer for `(chat_id, user_id)`;
- `'joined'` appends to an existing buffer and extends its deadline.

`TurnBuffer.ready(now) -> list[update]`:

- returns the turns whose deadline passed or that reached `max_messages`;
- a multi-fragment turn carries `message.text` joined with `\n` in arrival order
  and `_coalesced` = every real `update_id` in that turn;
- a single-fragment turn is returned unchanged (no `_coalesced`).

`Bot.process`:

- `offset` becomes `max(update_id, *_coalesced) + 1`;
- every coalesced id is inserted into `processed_updates` in the same
  transaction as the turn's effects.

## Acceptance Criteria

- [x] AC-001: Two plain-text messages from one person within the window become one turn with both texts.
- [x] AC-002: Fragments from different people or different chats are never merged.
- [x] AC-003: A buffered turn is released once the window closes.
- [x] AC-004: Media, contacts, callbacks, and slash commands are never coalesced.
- [x] AC-005: The buffer is bounded by `max_messages`.
- [x] AC-006: Every coalesced update id is marked processed and the offset passes the newest.
- [x] AC-007: A zero window disables coalescing and restores one reply per message.
- [x] AC-008: `save_conversation` keeps the last 40 turns and no more.
- [x] AC-009: The agent snapshot carries at most 20 chat messages out of a 30-message window.
- [x] AC-010: The legacy JSON route replays 8 turns, each capped at 1000 characters.
- [x] AC-011: A coalesced turn is stored once in `chat_messages`, not once per fragment.
- [x] AC-012: A command from the same sender flushes that sender's pending fragments first.

## Test Mapping

- AC-001 -> `tests/test_sans.py::ChatMemoryTests::test_back_to_back_messages_become_one_turn`
- AC-002 -> `tests/test_sans.py::ChatMemoryTests::test_fragments_from_different_people_are_never_merged`
- AC-003 -> `tests/test_sans.py::ChatMemoryTests::test_buffer_releases_the_turn_after_the_window`
- AC-004 -> `tests/test_sans.py::ChatMemoryTests::test_media_and_commands_are_never_coalesced`
- AC-005 -> `tests/test_sans.py::ChatMemoryTests::test_buffer_is_bounded`
- AC-006 -> `tests/test_sans.py::ChatMemoryTests::test_coalesced_updates_are_marked_processed`
- AC-007 -> `tests/test_sans.py::ChatMemoryTests::test_zero_window_disables_coalescing`
- AC-008 -> `tests/test_sans.py::ChatMemoryTests::test_conversation_history_keeps_the_last_forty_turns`
- AC-009 -> `tests/test_agent_contracts.py::ChatMemoryWindowTests::test_snapshot_is_bounded`
- AC-010 -> `tests/test_agent_contracts.py::ChatMemoryWindowTests::test_legacy_route_sends_a_deeper_but_capped_history`
- AC-011 -> `tests/test_sans.py::ChatMemoryTests::test_coalesced_turn_is_stored_once`
- AC-012 -> `tests/test_sans.py::ChatMemoryTests::test_a_command_flushes_the_pending_fragments_of_that_sender`

## Safety Constraints

- Coalescing never crosses a chat or a sender, so one person's text can never
  appear in another person's turn or context.
- A slash command is never merged with prose, so `/cancel` keeps its exact
  meaning and a command cannot be smuggled into a longer message.
- Nothing is written to the domain before the turn is processed; a crash replays
  the fragments instead of losing them.
- Memory depth stays bounded by named constants; no conversation can grow
  without limit.
- Financial rules, permissions, and the ledger are unchanged.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- Decision: coalescing lives in the polling loop, not in `Bot`, because `Bot`
  must stay a pure "one update in, one transaction out" unit for tests and for
  replay safety.
- Decision: the acknowledgement is sent for the first fragment only, so a
  three-message thought produces one «دریافت شد» and one answer.
- Decision: a message that cannot be coalesced (command, receipt, contact)
  flushes that sender's buffered fragments first, so `/cancel` can never run
  before the text the person wrote just before it.
- Limitation: the window adds up to `1.2` seconds of latency to a single
  message. That is the price of not answering fragments separately; setting
  `TURN_COALESCE_SECONDS=0` disables it.
- Limitation: the buffer is in memory, so a restart between the first fragment
  and the window closing loses the delay but not the messages: they were never
  marked processed and are fetched again.
- Validation (2026-10-22): 230 tests pass (1 pre-existing skip). A smoke script
  drove the real `Bot` and `TurnBuffer` with three fragments: one model call,
  one reply, `processed_updates = [1, 2, 3]`, `offset = 4`, and one stored user
  turn.
- Not verified here: the polling loop itself (`main.run`) needs a live Bale
  token. This environment has no outbound network (`bot.log` shows repeated
  `API unavailable`), so the coalescing window and the short poll were verified
  by unit tests and by the smoke script, not by a live bot restart.
