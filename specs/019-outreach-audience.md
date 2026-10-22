# SPEC-019: Message every known contact, and let admins see phone numbers

Status: verified
Owner: agent
Created: 2026-10-22

## Problem

The admin can only reach a player who has a linked Bale account, and the
assistant refuses the three things an admin actually asks for:

- **«این هنوز مچ نشده»** — `Service.broadcast_recipients` treats only
  `players.bale_id` as an address. A player who has already started the bot and
  filed an open identity request still counts as unreachable, and an ordinary
  person who started the bot in private cannot be reached at all. The live
  database proves it: 26 players, 0 linked accounts, and one open identity
  request (`9b7898198d6e47ac`), so every broadcast fails closed.
- **«من شروع‌کنندهٔ پیام نیستم»** — the bot *does* send proactively after an
  admin click, but `AGENT_SYSTEM_PROMPT` never says so, so the model invents a
  reply-only limitation and tells the admin the feature is impossible.
- **«شماره را به ادمین‌ها نشان نمی‌دهم»** — the data is already there
  (`query_database` target `players` returns `players.phone`, `target='pending'`
  returns the raw payload, and the identity card prints the number), but the
  prompt contains an absolute «هرگز» rule with no admin exception, so the model
  refuses. There is also no readable way to list the open identity requests or
  the private starters, so the model cannot discover a card code or a number
  even when it is allowed to.
- A player's shared number is stored silently: the admin group never learns that
  it arrived, so the number stays invisible unless someone runs `/players`.

## Goal

An admin can ask, in natural Persian, for one message to reach either the
players or **everyone who ever started the bot**. The approval card states which
audience it will hit, how many people that is, and who is not a confirmed
player. The message may optionally carry the «ارسال شمارهٔ من» button. After
approval each delivery is recorded per private chat. Admins can read a player's
phone number, username, and Bale user id, and every newly stored number is
announced once in the admin group.

## Scope

- `outreach` gains `user_id` and a nullable `player_id` (one delivery per
  private chat, not per player). Existing rows are preserved by a table rebuild
  guarded by `PRAGMA table_info(outreach)` in `Database.__init__`.
- New `Service.open_identity_addresses()` and `Service.known_address(player_id)`.
- `Service.broadcast_recipients(player_ids=None, audience='players')` resolves
  recipients from three sources: `players.bale_id` (`bound`), an open
  `identity`/`claim` request (`identity`), and the `contacts` address book
  (`contact`).
- `broadcast` gains `audience` (`players` | `all`) and `request_phone` (bool);
  `guard` fingerprints the resolved chat ids, the text, and the phone flag;
  `preview` names the audience and the unconfirmed receivers; `apply` writes
  `user_id` and attaches the reply keyboard when asked.
- New `app/keyboards.py` so the service can attach the contact keyboard without
  importing `Bot`. `Bot.contact_keyboard()` / `Bot.hide_keyboard()` keep working
  as thin delegates.
- `Bot.receive_contact` announces a newly stored number once in the admin group.
- `Bot.confirm` offers the contact button when an approved identity belongs to a
  player who still has no number.
- `Bot.proactive_answer` keeps working for a recipient that is not a player.
- `AGENT_SYSTEM_PROMPT` states that the bot does send after approval, allows
  admins to see phone numbers, usernames, and Bale user ids, and explains the two
  audiences and `request_phone`.
- `query_database` gains `identities` and `contacts` targets, admin only.
- `message_players(text, players='', audience='', request_phone=False)`.
- `content/bot-content.json` admin help mentions both audiences and the number
  request.

## Non-goals

- No message to a Bale user who never started the bot. The provider refuses it;
  `flush_outbox` already marks such an outreach `failed`.
- No change to the ledger, receipts, sessions, identity approval, or `/bind`.
- No private-chat admin powers: `is_admin` keeps the group boundary (SPEC-020).
- No phone number in the AI snapshot, in `agent_logs`, or in `outreach.text`.
  Numbers are read on demand by an admin-only tool call.


## Contracts

`known_address(player_id) -> {'user_id', 'chat_id', 'source'} | None`

- `players.bale_id` wins (`source='bound'`, `chat_id = user_id = bale_id`);
- otherwise one single open `identity`/`claim` request for that player
  (`source='identity'`); the address is that request's `user_id` and its private
  `chat_id`;
- two open requests with two different `user_id` values are ambiguous, so the
  player is not addressed;
- anything else returns `None`.

`broadcast_recipients(player_ids, audience) -> (recipients, unreachable)`

- each recipient is `{'player': int | None, 'user_id': int, 'chat_id': int,
  'name': str, 'source': 'bound' | 'identity' | 'contact'}`;
- recipients are de-duplicated by `chat_id`, so nobody gets two copies;
- `audience='players'`: every requested player (or every player when
  `player_ids is None`), with a name in `unreachable` when no address exists;
- `audience='all'`: the `contacts` address book first, then every player whose
  address is missing from it; `player_ids` must be `None`, otherwise the request
  fails closed;
- an unknown `audience` raises `UserError`.

`broadcast` args: `{'text': str, 'players': list[int] | str | None,
'audience': str, 'request_phone': bool}`. `text` stays capped at 3500
characters and `players` at `MAX_RECIPIENTS` names. The resolved audience is
capped at `MAX_RECIPIENTS` private chats, and a larger audience fails closed
before a card exists.

`guard` fingerprint: `{'chat_ids': [sorted chat ids], 'text': text,
'request_phone': bool}`. A card expires when the reachable audience changed
after the admin saw the preview.

`apply` inserts one `outreach(player_id, user_id, chat_id, text, status='queued')`
row per recipient and queues one private `sendMessage` with
`ref='outreach:<id>'`; `request_phone=True` adds
`reply_markup={'keyboard': [[{'text': CONTACT_BUTTON, 'request_contact': True}]]}`.

`Bot.receive_contact` sends exactly one admin-group line for a changed
`players.phone` value and nothing for an unchanged one.

`query_database` targets `identities` (open identity/claim cards with code,
declared name, username, phone, Bale user id) and `contacts` (address book with
the linked player name when there is one) are admin only and return at most 50
rows.


## Acceptance Criteria

- [x] AC-001: `known_address` prefers the linked account, then one unambiguous open identity request, and returns `None` otherwise.
- [x] AC-002: two open identity requests with different user ids for one player make that player unaddressable.
- [x] AC-003: `audience='players'` reaches a player who only has an open identity request (`source='identity'`) and reports a player with neither as unreachable.
- [x] AC-004: `audience='all'` returns the address book plus every remaining player, de-duplicated by chat id, with `player=None` only for a non-player.
- [x] AC-005: `prepare('broadcast')` succeeds for an identified but unbound player instead of failing with "no linked player".
- [x] AC-006: the preview states the audience, the recipient count, the unconfirmed receivers, the non-players, and the unreachable names.
- [x] AC-007: approving an `all` broadcast creates one outreach row and one private message per contact, with `player_id IS NULL` only for a non-player.
- [x] AC-008: `request_phone=True` attaches only the documented `keyboard` with `request_contact`, and `False` attaches no keyboard.
- [x] AC-009: a change to the reachable audience between preview and approval expires the card and delivers nothing.
- [x] AC-010: an admin can read a phone number, username, and Bale user id of an open identity request; a non-admin is refused.
- [x] AC-011: the `contacts` target is admin only and lists the private starters.
- [x] AC-012: each stored number produces one admin-group line, and repeating the same number produces none.
- [x] AC-013: approving an identity for a player without a number sends the confirmation with the contact button.
- [x] AC-014: a non-player's reply to an admin message is still relayed to the admin group.
- [x] AC-015: a legacy `outreach` table with `player_id NOT NULL` is upgraded in place, keeps its rows, and gains `user_id`.
- [x] AC-016: `message_players` by a non-admin still creates nothing (SPEC-014 regression).
- [x] AC-017: an audience larger than `MAX_RECIPIENTS` fails closed with the real count and delivers nothing.

## Test Mapping

- AC-001 -> `tests/test_sans.py::AudienceOutreachTests::test_known_address_prefers_the_linked_account`
- AC-002 -> `tests/test_sans.py::AudienceOutreachTests::test_ambiguous_identity_requests_are_not_addressed`
- AC-003 -> `tests/test_sans.py::AudienceOutreachTests::test_players_audience_reaches_an_identified_player`
- AC-004 -> `tests/test_sans.py::AudienceOutreachTests::test_all_audience_includes_every_starter_once`
- AC-005 -> `tests/test_sans.py::AudienceOutreachTests::test_broadcast_prepares_for_an_identified_player`
- AC-006 -> `tests/test_sans.py::AudienceOutreachTests::test_preview_names_the_audience_and_the_receivers`
- AC-007 -> `tests/test_sans.py::AudienceOutreachTests::test_all_audience_queues_one_message_per_starter`
- AC-008 -> `tests/test_sans.py::AudienceOutreachTests::test_request_phone_attaches_only_the_contact_button`
- AC-009 -> `tests/test_sans.py::AudienceOutreachTests::test_changed_audience_expires_the_card`
- AC-010 -> `tests/test_sans.py::AudienceOutreachTests::test_admin_reads_open_identities_and_contacts`
- AC-011 -> `tests/test_sans.py::AudienceOutreachTests::test_admin_reads_open_identities_and_contacts`
- AC-012 -> `tests/test_sans.py::AudienceOutreachTests::test_stored_phone_is_announced_once_to_the_admins`
- AC-013 -> `tests/test_sans.py::AudienceOutreachTests::test_identity_approval_offers_the_contact_button`
- AC-014 -> `tests/test_sans.py::AudienceOutreachTests::test_non_player_reply_is_relayed`
- AC-015 -> `tests/test_sans.py::AudienceOutreachTests::test_legacy_outreach_table_is_upgraded`
- AC-016 -> `tests/test_sans.py::OutreachTests::test_non_admin_cannot_message_players`
- AC-017 -> `tests/test_sans.py::AudienceOutreachTests::test_audience_above_the_limit_fails_closed`
- AC-006 -> `tests/test_sans.py::AudienceOutreachTests::test_preview_reports_an_unconfirmed_audience`
- AC-010 -> `tests/test_agent_contracts.py::OutreachPromptTests::test_prompt_allows_admins_and_proactive_sending`
- AC-004 -> `tests/test_agent_contracts.py::OutreachPromptTests::test_message_players_exposes_both_audiences`

## Safety Constraints

- The model can only propose a broadcast; a human click applies it.
- Recipients are resolved from the database, never from model-supplied chat ids.
- A player with two conflicting identity requests is never guessed.
- An audience above `MAX_RECIPIENTS` fails closed instead of sending partially.
- Phone numbers stay out of the AI snapshot and `agent_logs`; only an admin tool
  call returns them, and only for the rows asked for.
- Financial rules, the ledger, and the receipt flow are unchanged.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- Decision: `audience` defaults to `players`. A model that forgets the field can
  only reach players, and the preview always prints the mode, so an unintended
  `all` is visible before the click.
- Decision: `all` is the union of `contacts`, `players.bale_id`, and open
  identity requests, not `contacts` alone. The live database had an empty
  `contacts` table (SPEC-014 shipped after the only starter), so a
  `contacts`-only audience would have promised "everyone" and reached nobody.
- Decision: the identity request is a *proposal*, not proof. The preview labels
  those receivers and the guard expires the card when the request set changes.
- Limitation: Bale cannot deliver to a user who never started the bot. Such a
  row ends as `outreach.status='failed'`; the admin sees the count in the audit
  row, not a per-recipient error.
- Limitation: a person who only ever wrote in the group and never opened a
  private chat is not recoverable, because the bot has no private address.
- Limitation: `contacts` only starts filling from SPEC-014 onward; older
  starters are reachable only through `players.bale_id` or an open identity.

## Validation Result

- `python3 -m unittest discover -s tests -t . -v`: 263 tests, OK (1 pre-existing
  skip).
- `python3 -m compileall -q app tests`: OK.
- `git diff --check`: clean.
- Live migration (2026-10-22 16:51, bot stopped first, backup written to
  `data/sans-backup-20261022-165115.sqlite3`): `outreach.player_id` became
  nullable and `user_id` was added. The live table was empty, so no row changed.
- Live audience: `contacts` was empty and no player had a `bale_id`, so
  `audience='all'` resolved to the single open identity request
  (`9b7898198d6e47ac`, player #16, Bale user `1590533468`). One recipient is the
  honest answer for a bot that exactly one person has started so far.
- Not verified live: a real delivery and the «ارسال شمارهٔ من» round trip need a
  message from the admin. The unit tests cover the queued payloads, the
  keyboard shape, and the delivery bookkeeping.
