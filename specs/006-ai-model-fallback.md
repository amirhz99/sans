# SPEC-006: AI model fallback when the primary model is unavailable

Status: verified  
Owner: agent  
Created: 2026-10-19

## Problem

The bot used a single `AI_MODEL`. When that model stopped answering — for
example the provider returned `HTTP 402 no_provider_under_bid` because every
provider under the configured price limit was busy — every conversational turn
failed with the generic line «سرویس هوش مصنوعی فعلاً پاسخ معتبر نداد» even
though another model on the same endpoint was healthy. Operators had to edit
`.env` and restart the service to recover.

## Goal

Allow an ordered list of fallback models. Each AI turn tries the primary model
first and, if it raises, retries the same request with the next configured
model before reporting a failure to the user.

## Scope

- Parse `AI_FALLBACK_MODEL` (or the plural alias `AI_FALLBACK_MODELS`) as a
  comma-separated, ordered, de-duplicated list that excludes the primary model.
- Apply the fallback chain in both routing engines: the legacy `AIClient.parse`
  path and the LangGraph `SansAgent.run` path.
- Record one `LLM_FALLBACK` agent-log event per failed attempt in the LangGraph
  path so operators can see which model was skipped.
- Keep the existing user-facing failure message when every model fails.

## Non-goals

- No change to prompts, tools, intents, financial rules, ledger semantics, or
  the SQLite schema.
- No per-model base URL or API key; every model uses the same `AI_BASE_URL` and
  `AI_API_KEY`.
- No automatic provider/model discovery, bidding, or cost optimisation.
- No change to timeouts, temperature, or retry counts of the primary model.

## Contracts

- `Config.ai_fallback_models` is a `tuple[str, ...]`, empty by default, so
  existing callers and tests that build `Config` positionally keep working.
- `parse_fallback_models(raw, primary)` strips whitespace, drops empty entries,
  preserves order, removes duplicates, and never returns the primary model.
- The legacy path builds one request body and reuses it for every candidate
  model; only the `model` field changes.
- The LangGraph path builds one tool-bound client per model from the same
  configuration values (`api_key`, `base_url`, `temperature`, `max_retries`,
  `timeout`).
- A fallback is attempted only after an exception; a successful response is
  never retried or duplicated, so no tool call or proposal is created twice.
- Within one turn the model that answered last is tried first, so a failing
  primary is not retried for the follow-up model call of the same turn.
- Logs contain model names and the exception class/message only — never the API
  key, request headers, user message text, or receipt data.

## Acceptance Criteria

- [x] AC-001: `AI_FALLBACK_MODEL`/`AI_FALLBACK_MODELS` is parsed into an ordered
  tuple with blanks, duplicates, and the primary model removed.
- [x] AC-002: `AIClient.parse` tries the primary model, then each fallback, and
  returns the first valid intent.
- [x] AC-003: `SansAgent.run` retries the same turn with the next model when the
  current one raises, records an `LLM_FALLBACK` event naming both models, and
  keeps using the working model for the remaining calls of that turn.
- [x] AC-004: When every model fails, the turn fails closed: the user sees the
  existing AI-failure message and no domain side effect is applied.
- [x] AC-005: The fallback path reuses the same endpoint, key, temperature,
  timeouts, and tool bindings, and does not change the database schema.

## Test Mapping

- AC-001 -> `tests/test_agent_contracts.py::AgentContractTests::test_config_parses_fallback_models`
- AC-002 -> `tests/test_agent_contracts.py::AgentContractTests::test_legacy_ai_client_falls_back_to_secondary_model`
- AC-003 -> `tests/test_agent_contracts.py::AgentContractTests::test_langgraph_falls_back_to_secondary_model`
- AC-004 -> `tests/test_agent_contracts.py::AgentContractTests::test_legacy_ai_client_raises_when_every_model_fails`
  and `tests/test_agent_contracts.py::AgentContractTests::test_langgraph_raises_when_every_model_fails`
- AC-005 -> `tests/test_agent_contracts.py::AgentContractTests::test_fallback_reuses_the_same_provider_configuration`

## Safety Constraints

- A failed primary model never causes a partial turn: the retry happens before
  any tool result is returned to the graph, so no proposal or ledger row is
  created twice.
- The fallback list is configuration only; the model cannot add or change it.
- Only the model name and the exception summary are logged.
- An empty or malformed fallback list disables the feature instead of raising,
  so a typo cannot stop the bot from starting.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

The live incident on 2026-10-19 was `HTTP 402` from the configured primary
model while `deepseek-v4.1-flash` answered normally on the same endpoint, so the
operational configuration now sets:

```text
AI_FALLBACK_MODEL=deepseek-v4.1-flash
```

The primary model is still tried first, which costs one fast failed request per
turn while it is unavailable. Operators can promote a healthy model by changing
`AI_MODEL`, and may list several fallbacks separated by commas. Restart
`sans-bot.service` after changing `.env`.

2026-10-22: `AC-005` was mapped to prose instead of a test. It now points at
`test_fallback_reuses_the_same_provider_configuration`, which asserts that the
fallback instances share `api_key`, `base_url`, `temperature`, `timeout`, and
`max_retries` with the primary and that no table is created.
