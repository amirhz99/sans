# SPEC-008: Jalali session dates

Status: verified  
Owner: agent  
Created: 2026-10-20

## Problem

Every operator writes session dates in the Solar Hijri (Jalali) calendar, but
`parse_session_datetime` rejected any Jalali month name with «تبدیل تاریخ
ماه‌های تقویم شمسی در این نسخه انجام نمی‌شود». Importing the old sessions was
therefore impossible through the bot: the only Jalali row in the live database
was inserted manually, with `session_at` empty, so the bot could not say whether
a session was past or upcoming. The stored display also rendered Gregorian ISO
dates, which the operators do not use.

## Goal

An admin can write «۹ خرداد ساعت ۲۱» or «۱۴۰۵/۰۳/۰۹ ۲۱:۰۰» and get a real
`session_at` in Tehran time, while the original phrase stays in `when_text`.
Dates are shown back in the Jalali calendar.

## Scope

- `app/jalali.py`: pure-stdlib conversion between the Jalali and Gregorian
  calendars (`to_gregorian`, `from_gregorian`, `is_leap`, `month_length`,
  `format_date`), using the break-year algorithm shared by `jalaali-js`.
- `parse_session_datetime` accepts, in addition to the existing relative and
  Gregorian forms:
  - day + month name: «۹ خرداد ساعت ۲۱»، «خرداد ۹ ساعت ۲۱»؛
  - optional year: «۹ خرداد ۱۴۰۴ ساعت ۲۱»؛
  - numeric: «۱۴۰۵/۰۳/۰۹ ۲۱:۰۰»، «1405-03-09 21:00».
- A Jalali date without a year uses the **current Jalali year**.
- The flexible/historical `save_session_flexible` path rejects an impossible
  Jalali day before writing, while still accepting a month-only or hour-less
  phrase as historical text with `session_at = NULL`.
- `Service.session_at_display` and `Service.session_time` render the stored
  timestamp as «۹ خرداد ۱۴۰۵، ساعت ۲۱:۰۰».
- The system prompt and the `save_session` tool description state that Jalali
  input is accepted.

## Non-goals

- No change to the schema: `session_at` remains an ISO string with the Tehran
  offset and `when_text` remains the untouched operator phrase.
- No automatic migration of old rows. `session_at` is never inferred from
  `when_text` after the fact; historical sessions are filled through
  `update_session`/`save_session`.
- No guessing of a missing day, month, year, or hour: those still fail closed.
- No lunar Hijri (قمری) support and no timezone other than `Asia/Tehran`.

## Contracts

- `jalali.to_gregorian` validates the day against the month length (Esfand has
  29 days, or 30 in a leap year) and raises `ValueError` for an impossible date;
  `parse_session_datetime` converts that into `UserError` with no write.
- The Jalali year is taken from `now` in Tehran, so the same phrase resolves
  deterministically for the same day.
- Clock parsing is unchanged: «ساعت ۹» without صبح/شب, «عصر» without an hour,
  and a missing clock are rejected.
- The clock span is removed before the date is scanned, so the hour is never
  mistaken for a day number.
- Gregorian `20xx-mm-dd`, «امروز»، «فردا»، «پس‌فردا» and weekdays keep working.
- Display is Jalali for every surface that uses `session_time_display`
  (`/sessions`, `/config`, `/report`, `SessionView`) and for the approval
  preview.

## Acceptance Criteria

- [x] AC-001: «۹ خرداد ساعت ۲۱» resolves to the current Jalali year and stores `2026-05-30T21:00+03:30` for a 2026-10-20 clock.
- [x] AC-002: An explicit year («۹ خرداد ۱۴۰۴») and numeric input («۱۴۰۵/۰۳/۰۹ ۲۱:۰۰») resolve to the same day as the month-name form.
- [x] AC-003: Impossible Jalali days («۳۰ اسفند ۱۴۰۴»، «۳۱ مهر»، «۰ خرداد»، «۳۲ خرداد») and a month without a day are refused with no write.
- [x] AC-004: Conversion matches known anchors (1405-01-01 = 2026-03-21، 1403-12-30 = 2025-03-20) and round-trips Gregorian dates.
- [x] AC-005: A session created with a Jalali phrase keeps `when_text` unchanged and is displayed as «۹ خرداد ۱۴۰۵، ساعت ۲۱:۰۰».
- [x] AC-006: Relative dates, weekdays, Gregorian dates, and the ambiguous-hour rejection behave exactly as before.
- [x] AC-007: The flexible/historical session path refuses an impossible Jalali day without writing, but still stores «مهرماه گذشته» or «۹ خرداد» (no hour) with `session_at = NULL`.

## Test Mapping

- AC-001 -> `tests/test_sans.py::JalaliDateTests::test_jalali_dates_use_the_current_jalali_year`
- AC-002 -> `tests/test_sans.py::JalaliDateTests::test_jalali_dates_use_the_current_jalali_year`
- AC-002 -> `tests/test_sans.py::JalaliDateTests::test_explicit_jalali_year_is_respected`
- AC-003 -> `tests/test_sans.py::JalaliDateTests::test_invalid_jalali_dates_fail_closed`
- AC-004 -> `tests/test_sans.py::JalaliDateTests::test_jalali_conversion_matches_known_anchors_and_round_trips`
- AC-005 -> `tests/test_sans.py::JalaliDateTests::test_sessions_store_and_display_jalali_time`
- AC-006 -> `tests/test_sans.py::FinancialTests::test_exact_session_time_is_parsed_in_tehran_and_vague_time_is_rejected`
- AC-006 -> `tests/test_sans.py::FinancialTests::test_session_confirmation_shows_tehran_date_and_rejects_vague_time`
- AC-006 -> `tests/test_sans.py::FinancialTests::test_new_session_keeps_original_phrase_and_structured_time`
- AC-007 -> `tests/test_sans.py::JalaliDateTests::test_flexible_session_path_rejects_impossible_days_but_keeps_history`

## Safety Constraints

- Invalid dates fail closed before any database write, like every other
  validation in `Service.prepare`.
- No new dependency: conversion is pure Python, and no network or locale data is
  consulted.
- The year is never inferred from the surrounding conversation or from
  `last_entity`; it is either written by the operator or taken from the current
  Tehran date.
- Financial rules, ledger rows, and the schema are untouched.

## Validation

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
git diff --check
```

## Notes

- The algorithm is the break-year variant used by `jalaali-js`; it is exact for
  the years the bot can see. A 13k-day round-trip test guards the port,
  including the truncation-toward-zero division the algorithm relies on.
- The live session «سانس ۹ خرداد» was inserted manually before this feature and
  keeps `session_at = NULL`; it can be filled with `update_session` now that
  Jalali parsing exists (the hour is still required, because it is never
  guessed).
- `save_session_flexible` keeps its historical tolerance on purpose: a phrase
  without a day or without a clock is stored as text so old sessions can be
  imported. Only a day that cannot exist is refused, because no historical
  record can justify it.
