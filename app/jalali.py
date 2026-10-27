"""Jalali (Solar Hijri) calendar helpers built only on the standard library.

The conversion follows the well-known break-year algorithm used by the
``jalaali-js``/``jdate`` family of libraries.  It is exact for the years the
bot can encounter (1300..1500) and needs no external package.
"""

from datetime import date

MONTHS = ('فروردین', 'اردیبهشت', 'خرداد', 'تیر', 'مرداد', 'شهریور',
          'مهر', 'آبان', 'آذر', 'دی', 'بهمن', 'اسفند')

_PERSIAN_DIGITS = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')
_BREAKS = (-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210, 1635,
           2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178)


def _div(a, b):
    """Integer division that truncates toward zero (the algorithm's contract)."""
    return a // b if a >= 0 else -((-a) // b)


def _mod(a, b):
    return a - b * _div(a, b)


def _jal_cal(jy, without_leap=False):
    """Return ``(leap, gregorian_year, march_day)`` for a Jalali year."""
    if jy < _BREAKS[0] or jy >= _BREAKS[-1]:
        raise ValueError('Jalali year out of range')
    gy = jy + 621
    leap_j, jp = -14, _BREAKS[0]
    jump = 0
    for jm in _BREAKS[1:]:
        jump = jm - jp
        if jy < jm:
            break
        leap_j += _div(jump, 33) * 8 + _div(_mod(jump, 33), 4)
        jp = jm
    n = jy - jp
    leap_j += _div(n, 33) * 8 + _div(_mod(n, 33) + 3, 4)
    if _mod(jump, 33) == 4 and jump - n == 4:
        leap_j += 1
    leap_g = _div(gy, 4) - _div((_div(gy, 100) + 1) * 3, 4) - 150
    march = 20 + leap_j - leap_g
    leap = None
    if not without_leap:
        if jump - n < 6:
            n = n - jump + _div(jump + 4, 33) * 33
        leap = _mod(_mod(n + 1, 33) - 1, 4)
        if leap == -1:
            leap = 4
    return leap, gy, march


def _g2d(gy, gm, gd):
    """Gregorian date to Julian day number."""
    d = _div((gy + _div(gm - 8, 6) + 100100) * 1461, 4) \
        + _div(153 * _mod(gm + 9, 12) + 2, 5) + gd - 34840408
    return d - _div(_div(gy + 100100 + _div(gm - 8, 6), 100) * 3, 4) + 752


def _d2g(jdn):
    """Julian day number to ``(year, month, day)`` in the Gregorian calendar."""
    j = 4 * jdn + 139361631
    j += _div(_div(4 * jdn + 183187720, 146097) * 3, 4) * 4 - 3908
    i = _div(_mod(j, 1461), 4) * 5 + 308
    gd = _div(_mod(i, 153), 5) + 1
    gm = _mod(_div(i, 153), 12) + 1
    gy = _div(j, 1461) - 100100 + _div(8 - gm, 6)
    return gy, gm, gd


def is_leap(jy):
    """Return True when the Jalali year has 366 days (Esfand 30 exists)."""
    return _jal_cal(jy)[0] == 0


def month_length(jy, jm):
    if not 1 <= jm <= 12:
        raise ValueError('Jalali month out of range')
    if jm <= 6:
        return 31
    if jm <= 11:
        return 30
    return 30 if is_leap(jy) else 29


def to_gregorian(jy, jm, jd):
    """Convert a Jalali date to ``datetime.date`` (validating the day)."""
    if not 1 <= jm <= 12 or not 1 <= jd <= month_length(jy, jm):
        raise ValueError('invalid Jalali date')
    leap, gy, march = _jal_cal(jy, without_leap=True)
    jdn = _g2d(gy, 3, march) + (jm - 1) * 31 - (jm // 7) * (jm - 7) + jd - 1
    return date(*_d2g(jdn))


def from_gregorian(value):
    """Convert a ``datetime.date`` to ``(jalali_year, month, day)``."""
    jdn = _g2d(value.year, value.month, value.day)
    gy = _d2g(jdn)[0]
    jy = gy - 621
    leap, _, march = _jal_cal(jy)
    start = _g2d(gy, 3, march)
    k = jdn - start
    if k >= 0:
        if k <= 185:
            return jy, 1 + k // 31, k % 31 + 1
        k -= 186
    else:
        jy -= 1
        k += 179
        if leap == 1:
            k += 1
    return jy, 7 + k // 30, k % 30 + 1


def persian_digits(value):
    return str(value).translate(_PERSIAN_DIGITS)


# ``date.weekday()`` order: Monday is 0 and Sunday is 6.
WEEKDAY_NAMES = ('دوشنبه', 'سه‌شنبه', 'چهارشنبه', 'پنجشنبه', 'جمعه', 'شنبه', 'یکشنبه')


def weekday_name(value):
    """Return the Persian weekday of a ``datetime.date`` («سه‌شنبه»)."""
    return WEEKDAY_NAMES[value.weekday()]


def format_date(value, with_year=True):
    """Render a Gregorian date as «۹ خرداد ۱۴۰۵»."""
    jy, jm, jd = from_gregorian(value)
    text = f'{persian_digits(jd)} {MONTHS[jm - 1]}'
    return f'{text} {persian_digits(jy)}' if with_year else text
