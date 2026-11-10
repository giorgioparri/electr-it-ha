# SPDX-License-Identifier: GPL-3.0-or-later
"""Monthly counters by band. Run as a script: python3 tests/test_counters.py"""

import json
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parents[1] / "custom_components" / "electr_it_ha")
)

from engine.bands import Band
from engine.contract import TimeBands
from engine.counters import Counters, MonthEnergy, month_of

D = Decimal
_failures: list[str] = []

# Italian local times in November 2026: the 11th is a Wednesday, the 15th a Sunday.
WED_10 = datetime(2026, 11, 11, 10, 0)  # F1
WED_20 = datetime(2026, 11, 11, 20, 0)  # F2
SUN_12 = datetime(2026, 11, 15, 12, 0)  # F3


def check(what: str, actual: object, expected: object) -> None:
    """Compare a value with the expected one."""
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {what}: {actual}, expected {expected}")
    if not ok:
        _failures.append(what)


def bands(month: MonthEnergy) -> tuple[Decimal, Decimal, Decimal]:
    """Return the kWh of a month as (F1, F2, F3)."""
    return (month.kwh[Band.F1], month.kwh[Band.F2], month.kwh[Band.F3])


def test_readings() -> None:
    """Increases go to the band in force when the reading arrives."""
    counters = Counters(WED_10)
    check("first reading adds nothing", counters.record("a", D("1000"), WED_10), D(0))
    check("increase in F1", counters.record("a", D("1001.5"), WED_10), D("1.5"))
    check("increase in F2", counters.record("a", D("1003.5"), WED_20), D("2.0"))
    check("same reading adds nothing", counters.record("a", D("1003.5"), WED_20), D(0))
    check("increase in F3", counters.record("a", D("1004"), SUN_12), D("0.5"))
    check("kWh by band", bands(counters.current), (D("1.5"), D("2.0"), D("0.5")))
    check("total", counters.current.total, D("4.0"))
    check("month", counters.current.month, date(2026, 11, 1))


def test_several_meters() -> None:
    """Each meter has its own starting point; their increases add up."""
    counters = Counters(WED_10)
    counters.record("l1", D("500"), WED_10)
    counters.record("l2", D("20"), WED_10)
    counters.record("l1", D("501"), WED_10)
    counters.record("l2", D("22"), WED_10)
    check("two meters in F1", bands(counters.current), (D("3"), D(0), D(0)))
    check(
        "a meter added later starts from its reading",
        counters.record("l3", D("9999"), WED_10),
        D(0),
    )
    counters.record("l3", D("10000"), WED_20)
    check("third meter in F2", bands(counters.current), (D("3"), D("1"), D(0)))


def test_reset() -> None:
    """A reading that goes down is a new starting point, not consumption."""
    counters = Counters(WED_10)
    counters.record("a", D("1000"), WED_10)
    counters.record("a", D("1002"), WED_10)
    check("reset adds nothing", counters.record("a", D("3"), WED_10), D(0))
    check("counts again after the reset", counters.record("a", D("4"), WED_10), D("1"))
    check("kWh after a reset", bands(counters.current), (D("3"), D(0), D(0)))


def test_month_change() -> None:
    """At the month change the running month becomes the previous one."""
    counters = Counters(WED_10)
    counters.record("a", D("1000"), WED_10)
    counters.record("a", D("1010"), WED_10)
    check("no change within the month", counters.roll(SUN_12), False)
    # 23:30 UTC on 30 November is 00:30 on 1 December in Italy.
    check(
        "change at Italian midnight",
        counters.roll(datetime(2026, 11, 30, 23, 30, tzinfo=UTC)),
        True,
    )
    check("new month", counters.current.month, date(2026, 12, 1))
    check("new month is empty", counters.current.total, D(0))
    assert counters.previous is not None
    check("previous month", counters.previous.month, date(2026, 11, 1))
    check("previous month kWh", bands(counters.previous), (D("10"), D(0), D(0)))
    # Tuesday 1 December 2026 at 10:00: the meter kept its starting point.
    counters.record("a", D("1012"), datetime(2026, 12, 1, 10, 0))
    check("first increase of December", bands(counters.current), (D("2"), D(0), D(0)))
    check("going back in time changes nothing", counters.roll(WED_10), False)
    check(
        "year end",
        month_of(datetime(2026, 12, 31, 23, 30, tzinfo=UTC)),
        date(2027, 1, 1),
    )


def test_long_gap() -> None:
    """After more than a month of silence there is no reliable last month."""
    counters = Counters(WED_10)
    counters.record("a", D("1000"), WED_10)
    counters.record("a", D("1010"), WED_10)
    check("change after two months", counters.roll(datetime(2027, 1, 5, 9, 0)), True)
    check("no previous month", counters.previous, None)
    check("running month", counters.current.month, date(2027, 1, 1))


def test_contract_bands() -> None:
    """Counters are grouped the way the contract bills them."""
    month = MonthEnergy(
        date(2026, 9, 1), {Band.F1: D(21), Band.F2: D(98), Band.F3: D(372)}
    )
    check(
        "three bands",
        month.by_contract_band(TimeBands.F1_F2_F3),
        {"f1": D(21), "f2": D(98), "f3": D(372)},
    )
    check(
        "two bands",
        month.by_contract_band(TimeBands.F1_F23),
        {"f1": D(21), "f23": D(470)},
    )
    check("single band", month.by_contract_band(TimeBands.MONO), {"f0": D(491)})


def test_storage() -> None:
    """Counters survive a round trip through JSON."""
    counters = Counters(WED_10)
    counters.record("a", D("1000"), WED_10)
    counters.record("a", D("1001.234"), WED_20)
    counters.roll(datetime(2026, 12, 1, 0, 30))
    counters.record("b", D("7"), datetime(2026, 12, 1, 10, 0))
    restored = Counters.from_dict(json.loads(json.dumps(counters.to_dict())))
    check("running month restored", restored.current, counters.current)
    check("previous month restored", restored.previous, counters.previous)
    check(
        "meter a keeps its starting point",
        restored.record("a", D("1002.234"), datetime(2026, 12, 1, 10, 0)),
        D("1.000"),
    )
    check(
        "meter b keeps its starting point",
        restored.record("b", D("8"), datetime(2026, 12, 1, 10, 0)),
        D("1"),
    )


def test_removed_meter() -> None:
    """A meter taken out of the configuration starts over if it comes back."""
    counters = Counters(WED_10)
    counters.record("a", D("1000"), WED_10)
    counters.record("b", D("50"), WED_10)
    counters.keep_meters(["a"])
    check("kept meter still counts", counters.record("a", D("1001"), WED_10), D("1"))
    check("removed meter starts over", counters.record("b", D("60"), WED_10), D(0))


def main() -> int:
    """Run every case and report."""
    for test in (
        test_readings,
        test_several_meters,
        test_reset,
        test_month_change,
        test_long_gap,
        test_contract_bands,
        test_storage,
        test_removed_meter,
    ):
        test()
    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
