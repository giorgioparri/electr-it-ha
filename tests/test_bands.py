# SPDX-License-Identifier: GPL-3.0-or-later
"""Time bands and national holidays. Run as a script: python3 tests/test_bands.py"""

import sys
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parents[1] / "custom_components" / "electr_it_ha")
)

from engine.bands import Band, band_at, contract_band, easter, is_holiday
from engine.contract import TimeBands

_failures: list[str] = []


def check(what: str, actual: object, expected: object) -> None:
    """Compare a value with the expected one."""
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {what}: {actual}, expected {expected}")
    if not ok:
        _failures.append(what)


def test_working_day() -> None:
    """Wednesday 11 November 2026: every boundary of the day."""
    for clock, expected in (
        ("00:00", Band.F3),
        ("06:59", Band.F3),
        ("07:00", Band.F2),
        ("07:59", Band.F2),
        ("08:00", Band.F1),
        ("18:59", Band.F1),
        ("19:00", Band.F2),
        ("22:59", Band.F2),
        ("23:00", Band.F3),
    ):
        moment = datetime.fromisoformat(f"2026-11-11T{clock}")
        check(f"Wednesday {clock}", band_at(moment), expected)


def test_weekend() -> None:
    """Saturday has no F1; Sunday is all F3."""
    for stamp, expected in (
        ("2026-11-14T06:59", Band.F3),
        ("2026-11-14T07:00", Band.F2),
        ("2026-11-14T12:00", Band.F2),
        ("2026-11-14T22:59", Band.F2),
        ("2026-11-14T23:00", Band.F3),
        ("2026-11-15T12:00", Band.F3),
    ):
        check(f"weekend {stamp}", band_at(datetime.fromisoformat(stamp)), expected)


def test_holidays() -> None:
    """National holidays are F3 all day, whatever the weekday."""
    check("Easter 2026", easter(2026), date(2026, 4, 5))
    check("Easter 2027", easter(2027), date(2027, 3, 28))
    check("Easter 2038", easter(2038), date(2038, 4, 25))
    for day in (
        date(2026, 1, 1),
        date(2026, 1, 6),
        date(2026, 4, 6),  # Easter Monday
        date(2026, 4, 25),
        date(2026, 5, 1),
        date(2026, 6, 2),
        date(2026, 8, 15),
        date(2026, 11, 1),
        date(2026, 12, 8),
        date(2026, 12, 25),
        date(2026, 12, 26),
        date(2027, 3, 29),  # Easter Monday
        date(2027, 10, 4),  # Saint Francis, a Monday
    ):
        check(f"holiday {day}", is_holiday(day), True)
        noon = datetime(day.year, day.month, day.day, 12)
        check(f"band on {day} at noon", band_at(noon), Band.F3)
    check("4 October before 2026", is_holiday(date(2025, 10, 4)), False)
    check("ordinary Tuesday", is_holiday(date(2026, 11, 10)), False)
    check("Easter Sunday is not in the list", is_holiday(date(2026, 4, 5)), False)


def test_time_zones() -> None:
    """Aware datetimes are converted to Italian time, summer and winter."""
    for stamp, expected in (
        (datetime(2026, 11, 11, 7, 30, tzinfo=UTC), Band.F1),  # 08:30 CET
        (datetime(2026, 11, 11, 6, 30, tzinfo=UTC), Band.F2),  # 07:30 CET
        (datetime(2026, 7, 1, 6, 30, tzinfo=UTC), Band.F1),  # 08:30 CEST
        (datetime(2026, 7, 1, 5, 30, tzinfo=UTC), Band.F2),  # 07:30 CEST
        (datetime(2026, 7, 1, 21, 30, tzinfo=UTC), Band.F3),  # 23:30 CEST
    ):
        check(f"UTC {stamp:%Y-%m-%d %H:%M}", band_at(stamp), expected)


def test_week_totals() -> None:
    """A week without holidays has 55 hours of F1, 41 of F2 and 72 of F3."""
    start = datetime(2026, 11, 9)
    hours = Counter(band_at(start + timedelta(hours=hour)) for hour in range(168))
    check("F1 hours in a week", hours[Band.F1], 55)
    check("F2 hours in a week", hours[Band.F2], 41)
    check("F3 hours in a week", hours[Band.F3], 72)


def test_contract_bands() -> None:
    """Bands are grouped as the contract bills them."""
    for time_bands, expected in (
        (TimeBands.F1_F2_F3, ("f1", "f2", "f3")),
        (TimeBands.F1_F23, ("f1", "f23", "f23")),
        (TimeBands.MONO, ("f0", "f0", "f0")),
    ):
        actual = tuple(contract_band(band, time_bands) for band in Band)
        check(f"contract keys for {time_bands}", actual, expected)


def main() -> int:
    """Run every case and report."""
    for test in (
        test_working_day,
        test_weekend,
        test_holidays,
        test_time_zones,
        test_week_totals,
        test_contract_bands,
    ):
        test()
    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
