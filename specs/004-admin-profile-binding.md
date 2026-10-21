# SPEC-004: Admin-controlled trusted profile binding

Status: verified  
Owner: agent  
Created: 2026-10-17

## Problem

The existing identity flow lets a user propose a name, but an administrator
sometimes needs to match a registered player to a Bale profile already visible
in an administrator conversation. A raw username is not a safe way to resolve
that profile to Bale's numeric user id.

## Goal

Add an admin-only `/bind` command that accepts a player reference and a
replied-to structured Bale profile/message. The command must create a pending
binding and reuse the existing Yes/No confirmation flow; it must never bind on
parsing alone.

## Scope

- Extract a positive numeric user id only from structured profile fields on the
  replied-to message, including sender, contact, forward, and forwarded-origin
  user objects.
- Accept an optional username as a consistency check, never as an identity
  lookup.
- Reject bots, missing or malformed ids, username mismatches, existing links,
  duplicate pending bindings, and unauthorized commands without mutation.
- Confirm or reject the pending binding through the existing admin callback
  flow.

## Non-goals

- Resolving usernames through a Bale API, search, or external service.
- Adding a database table or changing the SQLite schema.
- Replacing the existing `/identify`, `/link`, receipt, or financial approval
  workflows.
- Automatically approving a binding.

## Contracts

- `/bind <player-or-id> [@username]` is valid only for an administrator in the
  configured admin group and must reply to the profile/message being bound.
- The replied-to message must contain a structured profile with a positive
  integer `id`; text, caption, or a username do not supply an identity.
- If `@username` is supplied, the structured profile must contain the same
  username after removing one leading `@`, case-insensitively.
- A valid request stores a `binding` pending record and leaves `players.bale_id`
  unchanged until the existing `yes:<id>` callback is approved.
- Approval calls the service-layer conflict checks again, so a race or changed
  state fails closed without overwriting a link.

## Acceptance Criteria

- [x] AC-001: An authorized `/bind` reply with a valid structured numeric
  profile creates one pending binding and does not bind before approval.
- [x] AC-002: Approval binds the requested player to the extracted user id;
  rejection leaves the player unbound.
- [x] AC-003: Username-only, missing/non-positive/non-integer ids, bots, and
  username mismatches are rejected with no pending binding or player mutation.
- [x] AC-004: Existing player links, existing user links, and duplicate pending
  bindings are rejected without overwriting or creating another request.
- [x] AC-005: Private/non-admin `/bind` requests and callbacks outside the
  configured admin group fail closed without a binding side effect.
- [x] AC-006: Profile extraction supports structured sender, contact, forward,
  and forwarded-origin user fields while ignoring untrusted message text.

## Test Mapping

- AC-001 -> `tests/test_sans.py::AdminBindingTests::test_bind_creates_pending_without_binding`
- AC-002 -> `tests/test_sans.py::AdminBindingTests::test_bind_approval_and_rejection`
- AC-003 -> `tests/test_sans.py::AdminBindingTests::test_bind_rejects_untrusted_profiles`
- AC-004 -> `tests/test_sans.py::AdminBindingTests::test_bind_rejects_duplicate_links_and_pending_requests`
- AC-005 -> `tests/test_sans.py::AdminBindingTests::test_bind_requires_admin_group`
- AC-006 -> `tests/test_sans.py::AdminBindingTests::test_bind_accepts_structured_profile_sources`

## Safety Constraints

- Never resolve a username to an id and never infer an id from text.
- Do not accept bot profiles or replace an existing `bale_id`.
- Validate authorization and all profile fields before writing `pending`.
- Keep binding separate from financial and ledger semantics.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

The profile payload is supplied by Bale's update structure. Bale may omit
forwarded-user details for privacy; such a message is intentionally rejected
instead of being resolved by username.