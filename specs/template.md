# SPEC-XXX: Short title

Status: draft  
Owner: agent  
Created: YYYY-MM-DD

## Problem

What problem or ambiguity requires a change?

## Goal

What observable result should users or maintainers get?

## Scope

- Explicitly included behavior.

## Non-goals

- Behavior that must remain unchanged or is intentionally deferred.

## Contracts

Describe relevant input/output, action, state, permission, or persistence
contracts. State which existing compatibility paths must remain valid.

## Acceptance Criteria

- [ ] AC-001: A concrete, externally observable requirement.
- [ ] AC-002: A failure or safety requirement.

## Test Mapping

- AC-001 -> `tests/test_example.py::test_behavior`
- AC-002 -> `tests/test_example.py::test_failure_is_safe`

## Safety Constraints

- Invalid or unauthorized requests fail closed.
- No unrelated private data is exposed to AI or users.
- Financial and persistence behavior is unchanged unless explicitly scoped.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

Record decisions, limitations, and follow-up work here.