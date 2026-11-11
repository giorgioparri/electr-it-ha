# SPDX-License-Identifier: GPL-3.0-or-later
"""Time bands F1/F2/F3 (ARERA, delibera 181/06) and Italian national holidays.

- F1: Monday to Friday, 8:00-19:00, national holidays excluded.
- F2: Monday to Friday, 7:00-8:00 and 19:00-23:00; Saturday, 7:00-23:00;
  national holidays excluded.
- F3: Monday to Saturday, 23:00-7:00; Sundays and national holidays, all day.

Pure Python: no Home Assistant imports are allowed in this package.
"""

from datetime import date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from .contract import TimeBands

ITALY = ZoneInfo("Europe/Rome")

# 4 October is a national holiday since 2026 (law 151/2025).
_SAINT_FRANCIS_FROM_YEAR = 2026
_FIXED_HOLIDAYS: frozenset[tuple[int, int]] = frozenset(
    {
        (1, 1),
        (1, 6),
        (4, 25),
        (5, 1),
        (6, 2),
        (8, 15),
        (11, 1),
        (12, 8),
        (12, 25),
        (12, 26),
    }
)


class Band(StrEnum):
    """Time band of an instant."""

    F1 = "f1"
    F2 = "f2"
    F3 = "f3"


def easter(year: int) -> date:
    """Return Easter Sunday of `year` (Gregorian calendar)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 19 * m) // 433
    month = (h + m - 7 * n + 90) // 25
    day = (h + m - 7 * n + 33 * month + 19) % 32
    return date(year, month, day)


def is_holiday(day: date) -> bool:
    """Return True on a national holiday (Sundays are not holidays here)."""
    if (day.month, day.day) in _FIXED_HOLIDAYS:
        return True
    if (day.month, day.day) == (10, 4) and day.year >= _SAINT_FRANCIS_FROM_YEAR:
        return True
    return day == easter(day.year) + timedelta(days=1)


def band_at(moment: datetime) -> Band:
    """Return the band of `moment`.

    An aware datetime is converted to Italian time; a naive one is taken as
    Italian local time already.
    """
    if moment.tzinfo is not None:
        moment = moment.astimezone(ITALY)
    weekday = moment.weekday()
    hour = moment.hour
    if weekday == 6 or is_holiday(moment.date()) or hour < 7 or hour >= 23:
        return Band.F3
    if weekday == 5 or hour < 8 or hour >= 19:
        return Band.F2
    return Band.F1


def contract_band(band: Band, time_bands: TimeBands) -> str:
    """Return the contract key (f0, f1, f2, f3, f23) that `band` is billed under."""
    if time_bands is TimeBands.MONO:
        return "f0"
    if time_bands is TimeBands.F1_F23 and band is not Band.F1:
        return "f23"
    return band.value
