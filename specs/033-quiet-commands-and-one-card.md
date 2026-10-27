# SPEC-033: No processing notice for commands, one identity card

Status: verified
Owner: agent
Created: 2026-10-28

## Problem

Two annoyances remained after SPEC-032:

1. **A deterministic command still announced itself.** `Bot.message()` answers
   `/help`, `/card`, `/players`, `/debt` and every other slash command without
   calling the model, yet `Bot.acknowledgement_chat()` only exempted `/start`
   and bare greetings. So `/help` produced "پیامتان دریافت شد" and then
   immediately replaced it with the real answer. The notice exists to explain a
   wait that did not happen.
2. **A name without a number produced two admin cards.** `identify()` sent the
   card at once, and `receive_contact()` sent a second, fuller card when the
   number arrived. The admin saw the same request twice, and the first copy was
   the less useful one.

## Goal

Send the processing notice only when the model is actually being asked to work.
Send the identity card once, complete, after both the name and the number are
known.

## Scope

- `Bot.acknowledgement_chat()` returns no chat for a message that
  `Bot.command()` answers on its own.
- `Bot.identify()` keeps the name and asks for the number instead of sending a
  card when no number is known.
- `Bot.receive_contact()` builds the card from the saved name once the number
  arrives, in either order.
- `WELCOME_NEW` no longer tells the user to send both facts at once.

## Non-goals

- No change to routing, financial rules, ledger semantics, or the schema.
- No change to the reply text of the commands themselves.
- The identity message is not edited in place when the number arrives; the
  card is simply built later and sent once. A second card is not sent.

## Contracts

- `acknowledgement_chat(update)` keeps returning the chat id for every turn that
  reaches the model, and for `/support`, which builds its own answer. It
  returns `None` for a plain slash command, for `/start`, and for a bare
  greeting.
- A photo or document message keeps its notice: those always reach the model or
  the receipt flow, and the upload itself takes time.
- `Bot.command()` stays the single source of truth for "is this a command". The
  acknowledgement path asks it rather than duplicating the command list.
- `identify(name, user_id, chat_id, sender, phone_number)` returns the card when
  a number is on file, and otherwise returns a request for the number with
  `contact_keyboard()` and stores the name in `state['identity_name']`.
- `receive_contact()` calls `identify()` with the saved name, so the card is
  created once and carries the number from the start.
- `state['identity_name']` is cleared once used, and by `/cancel`.
- Only the message a user sends is inspected; nothing new is persisted to
  answer the acknowledgement question.

## Acceptance Criteria

- [x] AC-001: `/help` from a player produces no "پیامتان دریافت شد" notice and
      still produces the help guide.
- [x] AC-002: `/card` and `/players` likewise produce no notice.
- [x] AC-003: a free-text question still produces the notice, because it reaches
      the model.
- [x] AC-004: a photo message still produces the notice.
- [x] AC-005: a name without a number sends no identity card and asks for the
      number with the share button.
- [x] AC-006: the number that follows builds exactly one admin card carrying both
      the name and the number.
- [x] AC-007: the number first and the name second also builds exactly one card.
- [x] AC-008: `/cancel` forgets a saved name, so a later number does not resurrect
      a cancelled introduction.

## Test Mapping

- AC-001 -> `tests/test_sans.py::QuietCommandTests::test_help_gets_no_processing_notice`
- AC-002 -> `tests/test_sans.py::QuietCommandTests::test_slash_commands_get_no_processing_notice`
- AC-003 -> `tests/test_sans.py::QuietCommandTests::test_a_question_still_gets_the_processing_notice`
- AC-004 -> `tests/test_sans.py::QuietCommandTests::test_a_photo_still_gets_the_processing_notice`
- AC-005 -> `tests/test_sans.py::OneIdentityCardTests::test_a_name_without_a_number_sends_no_card`
- AC-006 -> `tests/test_sans.py::OneIdentityCardTests::test_the_number_completes_the_card_once`
- AC-007 -> `tests/test_sans.py::OneIdentityCardTests::test_the_number_first_then_the_name_makes_one_card`
- AC-008 -> `tests/test_sans.py::OneIdentityCardTests::test_cancel_forgets_a_saved_name`
- AC-006 -> `tests/test_sans.py::AgentHeldNameTests::test_the_held_name_builds_the_card_when_the_contact_arrives`
- AC-005 -> `tests/test_sans.py::AgentHeldNameTests::test_the_tool_holds_the_name_in_the_conversation_state`

## Safety Constraints

- Invalid or unauthorized requests fail closed.
- A command answered without the model must not gain a model call.
- No unrelated private data is exposed to AI or users.
- Financial and persistence behavior is unchanged.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- The Bale contact button sends a `user_id`, never a number, so the number can
  never arrive in the same message as the name. At least one round trip between
  the two facts is unavoidable.
- Because the card is now built after the number, a user who never shares a
  number never produces a card. This is accepted: an admin can still reach that
  person, and the request was never complete enough to act on.
- Earlier drafts considered sending the incomplete card and editing it when the
  number arrived. That was rejected: `db.say` does not return a `message_id`, so
  it would need an outbox round trip for no user-visible benefit, and an admin
  could approve a card that carried no number at all.
- `identify()` is now a union: a string when the card is built, and
  `(reply, keyboard, state)` when it waits for a number. `identify_request()`
  hides that from callers, and `agent.py` uses it so a tool result stays a
  string.
- The agent path needed one non-obvious fix. `identify()` runs inside a tool,
  which writes the conversation row directly, but `message()` saves the `state`
  it loaded *before* the turn and would drop the held name. The turn now copies
  `identity_name` out of the row after `agent.run()` returns. This is the only
  place a tool and the turn share conversation state, and it is why
  `AgentHeldNameTests` exists.
- `/identify` deliberately keeps its processing notice even though it no longer
  calls the model: it may still match, rename, and now waits for a number.