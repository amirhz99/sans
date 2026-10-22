# SPEC-014: Proactive player messages with reply relay

Status: verified
Owner: agent
Created: 2026-10-22

## Problem

The bot could only answer a message that a player had just sent. The admin had
no way to tell the squad «سانس فردا لغو شد» or «شماره کارت عوض شد» from inside
the chat, and a player's answer to such a notice was treated as a brand new
question for the AI instead of being returned to the admin who asked.

Two concrete gaps:

- `flush_outbox` retried a private `sendMessage` forever when the recipient had
  blocked the bot (Bale answers `400`/`403`), which stalls the queue for
  everybody else.
- The bot stored no address book (`contacts`) and no record of what it had
  proactively sent (`outreach`), so it could neither report who received a
  notice nor recognize a reply to one.

## Goal

An admin can ask the bot, in natural Persian, to send one message to selected
players or to every linked player. Nothing is delivered until the admin presses
«تأیید». Each delivery is recorded, a permanently rejected private send is
dropped instead of retried, and a player's reply to that notice is relayed to
the admin group with the sender's identity.

## Scope

- New `contacts` table: one row per private user who has ever messaged the bot
  (`user_id`, `chat_id`, display name, username, phone, timestamps), upserted by
  `Bot.message`.
- New `outreach` table: one row per proactive message (`player_id`, `chat_id`,
  `text`, `status`, `message_id`, timestamps).
- New admin action `broadcast` (`prepare`/`guard`/`preview`/`apply`), reachable
  through the new agent tool `message_players` and through the existing approval
  card path.
- `flush_outbox` records delivery for `ref='outreach:<id>'` and drops a private
  `sendMessage` that fails with `400`/`403`, marking the outreach `failed`.
- `Bot.message` relays a player's reply to a proactive message to the admin
  group instead of routing it to the AI.
- Admin help text mentions the new capability.

## Non-goals

- No broadcast outside the configured admin group: only an admin can create the
  proposal, and the message is delivered only after approval.
- No messaging of players without a linked Bale account; they are reported as
  unreachable instead. **Superseded by SPEC-019**: a player with an open
  identity request and any private starter are addressable now.
- No change to receipt, ledger, session, or identity flows.

## Contracts

`broadcast` args: `{'text': str, 'players': list[int] | str | None}`.
`players=None` (or an empty string) means every linked player. Names are
resolved by `Service.player` at prepare time, so an unknown name fails closed
before any card exists. `text` is limited to 3500 characters.

Recipients are players with a positive integer `players.bale_id`. The guard
fingerprint is `{'recipients': [player ids], 'text': text}`, so a card expires
if the linked audience changed after the admin saw the preview.

`apply` inserts one `outreach` row per recipient and queues one private
`sendMessage` with `ref='outreach:<id>'`. `flush_outbox` then sets
`status='sent'`, `message_id`, and `delivered_at`.

`message_players(text, players='')` is available to admins only and always
returns a proposal, never a delivery.

`Bot.message` relays only when the private message is a reply to a message id
that belongs to an `outreach` row for that chat, and the sender is not an admin.

## Acceptance Criteria

- [x] AC-001: a private message upserts one `contacts` row and updates its
  display name on the next message.
- [x] AC-002: `message_players` by a non-admin is refused and creates nothing.
- [x] AC-003: an approved broadcast queues exactly one private message per
  linked recipient and no message for unlinked players.
- [x] AC-004: creating a broadcast proposal delivers nothing until approval.
- [x] AC-005: an empty text, an unknown player name, or an audience with no
  linked player fails closed and creates no proposal.
- [x] AC-006: a delivered outreach records `status='sent'` and its message id.
- [x] AC-007: a private `sendMessage` rejected with `400`/`403` is dropped and
  its outreach row becomes `failed` instead of retrying forever.
- [x] AC-008: a player's reply to a proactive message is relayed to the admin
  group and confirmed to the player without invoking the AI.
- [x] AC-009: an ordinary reply to a bot message that is not a proactive notice
  is not relayed.

## Test Mapping

- AC-001 -> `tests/test_sans.py::OutreachTests.test_private_message_records_and_updates_contact`
- AC-002 -> `tests/test_sans.py::OutreachTests.test_non_admin_cannot_message_players`
- AC-003 -> `tests/test_sans.py::OutreachTests.test_approved_broadcast_reaches_linked_players_only`
- AC-004 -> `tests/test_sans.py::OutreachTests.test_broadcast_proposal_delivers_nothing`
- AC-005 -> `tests/test_sans.py::OutreachTests.test_broadcast_fails_closed_without_a_valid_audience`
- AC-006 -> `tests/test_sans.py::OutreachTests.test_delivery_marks_outreach_sent`
- AC-007 -> `tests/test_sans.py::OutreachTests.test_blocked_private_send_is_dropped_and_marked_failed`
- AC-008 -> `tests/test_sans.py::OutreachTests.test_reply_to_proactive_message_is_relayed_to_admins`
- AC-009 -> `tests/test_sans.py::OutreachTests.test_reply_to_other_bot_message_is_not_relayed`

## Safety Constraints

- The model can only propose a broadcast; a human click applies it.
- Recipients are resolved from the database, never from model-supplied chat ids.
- Message text is sent only to the intended private chats and the admin group;
  it is not added to the AI snapshot or to `agent_logs`.
- A rejected private send never blocks the outbox for other chats.
- Existing approval, receipt, and financial paths are unchanged.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- `contacts` is an address book and an audit trail for private senders; delivery
  itself still requires a linked player (`players.bale_id`), which keeps a
  broadcast from reaching an unknown person.
- Relay needs the provider message id, so it only works for notices delivered
  after this change; older notices cannot be matched.
- The private-send drop uses the HTTP status carried by `APIError.code`.
- SPEC-019 widens the audience: `broadcast_recipients` now resolves a linked
  account, one open identity request, or any private starter, and `outreach`
  stores `user_id` with a nullable `player_id`. The rest of this specification
  (approval card, delivery record, reply relay) is unchanged.

## Validation Result

- `python3 -m unittest discover -s tests -t . -v`: 177 tests, OK (1 skipped).
- `python3 -m compileall -q app tests`: OK.
- `git diff --check`: clean.
- Live database has 26 players and 0 linked `bale_id` values, so a broadcast to
  «همه» currently fails closed with the "no linked player" message until players
  identify themselves with `/identify` (or an admin uses `/bind`). This is the
  documented limitation, not a silent drop.
