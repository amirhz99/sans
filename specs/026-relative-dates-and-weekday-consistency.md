# SPEC-026: Relative dates and weekday consistency

Status: verified  
Owner: agent  
Created: 2026-10-27

## Problem

`parse_session_datetime` (SPEC-008) resolves Jalali and Gregorian dates, but the
relative branch is a chain of substring tests that has three real defects and
one missing guarantee. Verified against a `now` of Tuesday 2026-10-27 10:00
Tehran:

| Operator phrase | Today resolves to | Correct |
| --- | --- | --- |
| «پس فردا ساعت ۲۱» | 2026-10-28 | 2026-10-29 |
| «هفته بعد ساعت ۲۱» | 2026-10-27 (today) | 2026-11-03 |
| «جمعه هفته بعد ساعت ۲۱» | 2026-10-30 | 2026-11-06 |

Cause of the first row: the branch order is `'پسفردا' in raw` then `'فردا' in
raw`, but `normalize()` turns the ZWNJ into a space, so «پس فردا» never matches
`'پسفردا'` and falls through to the «فردا» branch. Rows two and three are simply
not implemented, so the phrase silently degrades to a weekday or to today.

Separately there is no consistency check between a weekday the operator writes
and the date that date arithmetic produces. «۹ آذر ساعت ۲۱» is accepted even
though ۹ آذر ۱۴۰۵ is a Wednesday. Only the historical roster importer
(`app/backfill.py::planned_sessions`) rejects a non-Friday, and it does so with a
hardcoded rule and a message that claims «همهٔ سانس‌های این دوره جمعه بوده‌اند».
A single hardcoded Friday rule is wrong for this deployment: every day is a
valid session day, Friday is only the habit.

Finally the model is given `now_tehran` as `2026-10-27 10:00 (Tuesday)`. A
Persian-speaking model has to translate both the calendar and the weekday name
before it can reason about «پس‌فردا», which is where the residual errors come
from.

## Goal

An operator can write «فردا»، «پس‌فردا»، «هفته بعد»، «جمعه هفته بعد»، «آخر هفته»
or a weekday, and the bot resolves it to the same Tehran date the operator
means. When the operator writes both a date and a weekday and they disagree, the
bot refuses to store anything and proposes the correct date, while accepting any
weekday that does match.
## Scope

- `app/service.py`
  - A single `resolve_relative_date(date_text, now)` helper replacing the
    `elif` chain, handling, in this order: explicit Gregorian, explicit Jalali,
    «پس فردا»/«پس‌فردا»، «فردا»، a weekday qualified by «این …»/«… این هفته»/
    «… هفته بعد»/«… هفته بعدی»/«… بعد»، a bare «هفته بعد»، «آخر هفته»، then a
    bare weekday (next occurrence), then «امروز»/«امشب» and a bare clock.
  - A shared `weekday_phrase(raw)` that returns the weekday the operator wrote
    (`(index, phrase)`), tolerant of both ZWNJ and space forms.
  - Weekday consistency: when the operator stated a weekday, the resolved date
    must fall on it; otherwise `UserError` with the actual and the nearest
    matching Jalali date («۹ آذر ۱۴۰۵ چهارشنبه است، نه جمعه. نزدیک‌ترین جمعه ۱۱ آذر
    ۱۴۰۵ است؛ منظورتان همان است؟»).
  - **No day is forbidden.** Any weekday is accepted; Friday is only the default
    habit and is never enforced by the parser.
- `app/backfill.py`: `planned_sessions` keeps its Friday rule for the historical
  roster (that period really was Fridays) but reports the offending date with
  the shared weekday helper instead of a duplicated literal.
- `app/agent.py`: the live snapshot exposes `now_tehran` in Jalali with the
  Persian weekday plus `tomorrow`/`day_after` Jalali strings, so the model never
  has to convert calendars or weekday names. The prompt states that a relative
  phrase must be passed through to the tool **verbatim**, never precomputed into
  an ISO date.

## Non-goals

- No new schema column, no setting for «allowed weekdays», no financial change.
- No inference of a missing hour, a missing period of day, or a missing year.
- No change to `Service.session_time_display` or to the stored `when_text`.
- No fuzzy calendar arithmetic for months: «ماه بعد» stays unsupported and fails
  closed with the existing message.
- `parse_session_datetime` keeps its signature and remains importable from
  `app.service`.

## Contracts

- `parse_session_datetime(value, now=None) -> str` returns a Tehran ISO string
  exactly as before. `now` is injectable and is the only clock; nothing reads
  the wall clock when `now` is given.
- Resolution order is deterministic: an explicit Gregorian or Jalali date always
  wins over a relative word, which wins over a weekday, which wins over a bare
  clock.
- A weekday written together with an explicit date is a **constraint**, not an
  offset: the date decides the day, the weekday must agree.
- A bare weekday means the next occurrence in Tehran time, counting today when
  the session hour is still ahead; otherwise the same weekday next week. This is
  the SPEC-008 behaviour and is preserved.
- «هفته بعد» alone means `now.date() + 7 days` at the stated hour.
- «آخر هفته» means the next Friday at the stated hour.
- The failure path raises `UserError` and writes nothing; `Service.prepare`
## Acceptance Criteria

- [x] AC-001: «پس فردا ساعت ۲۱» and «پس‌فردا ساعت ۲۱» both resolve to two days after `now`; «فردا ساعت ۲۱» still resolves to one day after.
- [x] AC-002: «هفته بعد ساعت ۲۱» resolves to `now.date() + 7 days`; «جمعه هفته بعد ساعت ۲۱» resolves to the Friday of the following week; «جمعه این هفته» resolves to the Friday of the current week.
- [x] AC-003: «آخر هفته ساعت ۲۱» resolves to the next Friday, and a bare weekday keeps the SPEC-008 next-occurrence rule including the "today but already past" rollover.
- [x] AC-004: A date and a weekday that disagree are refused with no write, and the error names the actual weekday and the nearest matching Jalali date.
- [x] AC-005: A date and a weekday that agree are accepted for any weekday — Friday, Saturday, Wednesday — with no allow-list.
- [x] AC-006: An explicit Gregorian or Jalali date still wins over a relative word and over a weekday constraint.
- [x] AC-007: The model context carries a Jalali `now_tehran` with a Persian weekday and adds no player, ledger, or receipt data.
- [x] AC-008: The historical roster importer still refuses a non-Friday label and the SPEC-022 gate tests still pass unchanged.

## Test Mapping

- AC-001 -> `tests/test_sans.py::RelativeDateTests::test_post_tomorrow_works_with_and_without_zwnj`
- AC-002 -> `tests/test_sans.py::RelativeDateTests::test_next_week_phrases_resolve_forward`
- AC-003 -> `tests/test_sans.py::RelativeDateTests::test_weekend_and_bare_weekday_use_next_occurrence`
- AC-004 -> `tests/test_sans.py::RelativeDateTests::test_weekday_that_contradicts_the_date_fails_closed`
- AC-005 -> `tests/test_sans.py::RelativeDateTests::test_any_weekday_is_allowed_when_it_matches`
- AC-005 -> `tests/test_sans.py::RelativeDateTests::test_weekday_names_are_matched_longest_first_on_normalized_text`
- AC-005 -> `tests/test_sans.py::RelativeDateTests::test_every_weekday_resolves_to_its_own_day`
- AC-006 -> `tests/test_sans.py::RelativeDateTests::test_explicit_dates_win_over_relative_words`
- AC-007 -> `tests/test_agent_contracts.py::SnapshotDateContextTests::test_snapshot_now_is_jalali_with_persian_weekday`
- AC-008 -> `tests/test_sans.py::RelativeDateTests::test_roster_importer_still_requires_fridays`
- AC-008 -> `tests/test_backfill.py::ReconcileTests` (existing gate tests)

## Safety Constraints

- Invalid or contradictory dates fail closed before any database write.
- The weekday check never relaxes into a silent "fix": it refuses and proposes,
  it does not rewrite the operator's date.
- The snapshot addition is limited to dates; no player, ledger, or receipt data
  is added to the model context.
- Finance, ledger semantics and the SQLite schema are untouched.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- The three defect rows in the Problem table were reproduced on the shipped code
  before this spec was written; each becomes a test so the regression is pinned.
- A fourth defect surfaced during implementation: the Jalali month lookup used a
  bare substring test, and the short month «دی» matches inside «هفته بعدی». The
  phrase therefore looked like a month with no day and failed with a confusing
  message. Month names are now matched as whole words
  (`(?<![\u0600-\u06FF])name(?![\u0600-\u06FF])`).
- A fifth, more serious defect was found only while smoke-testing the deployed
  code: «شنبه» is a suffix of «سه‌شنبه»، «پنج‌شنبه» and «یک‌شنبه», so a naive
  substring scan reported **Saturday for three different days**. `normalize()`
  also turns the ZWNJ into a space, so «پنج‌شنبه» arrives as «پنج شنبه» and never
  matched the ZWNJ-free key. `weekday_phrase` now normalizes its own input and
  scans longest-key-first over both spellings, and
  `test_weekday_names_are_matched_longest_first_on_normalized_text` pins it.
  This is why the smoke test after deployment mattered: the unit tests only
  exercised «پنج شنبه» and happened to agree with the bug.
- «پنج شنبه» and «پنج‌شنبه» already worked because `normalize()` collapses the
  ZWNJ into a space and `WEEKDAYS` happened to contain the space form for that
  entry. The new helper normalizes the key lookup instead of relying on that
  accident, and it scans longest-first because «شنبه» is a suffix of «پنجشنبه»
  and «یکشنبه».
- The Friday rule stays in the historical importer only. It is a property of the
  imported period, not of the bot. Its message now also names the real weekday,
  which is why `tests/test_backfill.py::PlanTests::test_a_non_friday_date_is_rejected`
  and the SPEC-022 rejection assertion check «نه جمعه» instead of «جمعه نیست».
- Verified on three different days (Tuesday, Friday 22:00, Saturday 09:00) so a
  weekday phrase that happens to be today cannot hide a drift.
- Known limitation: weekday names inside a personal name or a note (a player
  literally called «جمعه») are also read as a weekday constraint. This is
  acceptable because the phrase is free text the operator can reword, and a
  false rejection never writes anything.
  therefore leaves the pending card untouched, exactly like an invalid date does
  today.