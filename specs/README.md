# Specifications

This directory contains the repository's lightweight, tool-independent
specifications. Markdown is intentional: specifications must remain readable
without an agent framework, build service, or additional parser.

## SDD lifecycle

Every behavior change follows this loop:

```text
spec -> plan -> implement -> test -> review
```

1. Select an existing spec or create one from `template.md`.
2. Describe the problem, goal, scope, and non-goals.
3. Give each acceptance criterion an `AC-XXX` identifier.
4. Map each criterion to an executable test or explain why it is a manual
   review criterion.
5. Implement the smallest change that satisfies the approved criteria.
6. Run targeted tests and then the complete validation commands in `AGENTS.md`.
7. Set the spec to `verified` only after the acceptance criteria and regression
   suite pass. Use `superseded` when a later specification replaces it.

Valid statuses are:

```text
draft | ready | in_progress | verified | superseded
```

## Repository conventions

- `app/domain/` owns stable contracts such as `Intent`, `EntityRef`, and
  `ConversationState`.
- `app/ai_context.py` controls the deliberately small, role-aware model
  context.
- `app/presentation.py` controls user-facing response levels.
- `app/service.py` remains the source of truth for business and financial
  rules.
- `tests/test_sans.py` contains end-to-end and financial regression scenarios.
- `tests/test_agent_contracts.py` contains focused AI/domain boundary tests.
- `tests/test_spec_contracts.py` protects the structure of this SDD process.

## What a specification is not

A spec is not a second implementation, a prompt transcript, or a promise to
introduce a new framework. It records observable behavior and safety limits.
The existing Python code and tests remain authoritative for implementation
details.