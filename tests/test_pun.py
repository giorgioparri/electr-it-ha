# SPDX-License-Identifier: GPL-3.0-or-later
"""Monthly PUN averages by time band. Run as a script: python3 tests/test_pun.py

Every price here is synthetic: no GME data is stored in the repository.
"""

import sys
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parents[1] / "custom_components" / "electr_it_ha")
)

from engine.bands import ITALY, Band, band_at
from engine.contract import Contract
from engine.market import build_market
from engine.pun import (
    PunAverages,
    PunError,
    monthly_averages,
    slot_start,
    slots_in_day,
    slots_in_month,
)

D = Decimal
_failures: list[str] = []

NOVEMBER = date(2026, 11, 1)  # starts on a Sunday, no other holiday
OCTOBER = date(2026, 10, 1)  # clocks go back on Sunday 25: 745 hours
MARCH = date(2026, 3, 1)  # clocks go forward on Sunday 29: 743 hours
BAND_PRICE = {Band.F1: D(100), Band.F2: D(200), Band.F3: D(300)}


def check(what: str, actual: object, expected: object) -> None:
    """Compare a value with the expected one."""
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {what}: {actual}, expected {expected}")
    if not ok:
        _failures.append(what)


def raises(what: str, call: Callable[..., object], *args: object) -> None:
    """Check that a call raises PunError."""
    try:
        call(*args)
    except PunError as err:
        print(f"PASS  {what}: {err}")
    else:
        print(f"FAIL  {what}: no error raised")
        _failures.append(what)


def local(day: date, hour: int, minute: int = 0, offset_hours: int = 1) -> str:
    """Format the expected Italian local time with its UTC offset."""
    return f"{day.isoformat()}T{hour:02}:{minute:02}:00+{offset_hours:02}:00"


def month_days(month: date) -> list[date]:
    """Return every day of `month`."""
    days = []
    day = month
    while day.month == month.month:
        days.append(day)
        day += timedelta(days=1)
    return days


def month_prices(
    month: date,
    price: Callable[[datetime], Decimal],
    minutes: int = 60,
    days: list[date] | None = None,
) -> list[tuple[datetime, Decimal]]:
    """Return a price for every slot of the given days (default: the whole month)."""
    prices = []
    for day in days if days is not None else month_days(month):
        for index in range(1, slots_in_day(day, minutes) + 1):
            start = slot_start(day, index, minutes)
            prices.append((start, price(start)))
    return prices


def by_band(start: datetime) -> Decimal:
    """Price that depends only on the band of the slot."""
    return BAND_PRICE[band_at(start)]


def test_slots() -> None:
    """GME numbers the slots of a day from 1; DST days have 23 or 25 hours."""
    day = date(2026, 11, 12)
    check("hour 1", slot_start(day, 1).isoformat(), local(day, 0))
    check("hour 9", slot_start(day, 9).isoformat(), local(day, 8))
    check("hour 24", slot_start(day, 24).isoformat(), local(day, 23))
    check("quarter 33", slot_start(day, 33, 15).isoformat(), local(day, 8))
    check("quarter 96", slot_start(day, 96, 15).isoformat(), local(day, 23, 45))
    check("start is Italian time", slot_start(day, 1).tzinfo, ITALY)
    raises("hour 0", slot_start, day, 0)
    raises("hour 25 on an ordinary day", slot_start, day, 25)

    spring = date(2026, 3, 29)
    check("spring day, hour 2", slot_start(spring, 2).isoformat(), local(spring, 1))
    check(
        "spring day, hour 3",
        slot_start(spring, 3).isoformat(),
        local(spring, 3, offset_hours=2),
    )
    check(
        "spring day, hour 23",
        slot_start(spring, 23).isoformat(),
        local(spring, 23, offset_hours=2),
    )
    raises("spring day, hour 24", slot_start, spring, 24)

    autumn = date(2026, 10, 25)
    check(
        "autumn day, hour 3",
        slot_start(autumn, 3).isoformat(),
        local(autumn, 2, offset_hours=2),
    )
    check("autumn day, hour 4", slot_start(autumn, 4).isoformat(), local(autumn, 2))
    check("autumn day, hour 25", slot_start(autumn, 25).isoformat(), local(autumn, 23))
    check(
        "autumn day, quarter 100",
        slot_start(autumn, 100, 15).isoformat(),
        local(autumn, 23, 45),
    )
    raises("autumn day, hour 26", slot_start, autumn, 26)
    raises("slots of 7 minutes", slot_start, day, 1, 7)

    for what, actual, expected in (
        ("hours in an ordinary day", slots_in_day(day), 24),
        ("hours in the spring day", slots_in_day(spring), 23),
        ("hours in the autumn day", slots_in_day(autumn), 25),
        ("quarters in the autumn day", slots_in_day(autumn, 15), 100),
        ("hours in November", slots_in_month(NOVEMBER), 720),
        ("hours in October", slots_in_month(OCTOBER), 745),
        ("hours in March", slots_in_month(MARCH), 743),
        ("quarters in March", slots_in_month(MARCH, 15), 2972),
        ("any day names its month", slots_in_month(date(2026, 3, 17)), 743),
    ):
        check(what, actual, expected)


def test_full_month() -> None:
    """A complete month, the price set by the band of each hour.

    November 2026: 21 working days, 4 Saturdays, 5 Sundays (the 1st is also
    a holiday). F1 231 hours, F2 21 x 5 + 4 x 16 = 169, F3 720 - 400 = 320.
    F23 = (169 x 200 + 320 x 300) / 489 = 265.4396 -> 265.44
    F0 = (231 x 100 + 169 x 200 + 320 x 300) / 720 = 212.3611 -> 212.36
    """
    averages = monthly_averages(NOVEMBER, month_prices(NOVEMBER, by_band))
    check("November month", averages.month, NOVEMBER)
    check("November F1", averages.f1, D("100.00"))
    check("November F2", averages.f2, D("200.00"))
    check("November F3", averages.f3, D("300.00"))
    check("November F23", averages.f23, D("265.44"))
    check("November F0", averages.f0, D("212.36"))
    check("November slots", (averages.slots, averages.expected), (720, 720))
    check("November complete", averages.complete, True)

    # October: 22 working days, 5 Saturdays, 4 Sundays plus the 4th (a
    # Sunday and, from 2026, a holiday). F1 242, F2 110 + 80 = 190, F3 313.
    # F0 = (24200 + 38000 + 93900) / 745 = 209.5302 -> 209.53
    october = monthly_averages(OCTOBER, month_prices(OCTOBER, by_band))
    check("October F0, 745 hours", october.f0, D("209.53"))
    check("October complete", october.complete, True)

    quarters = month_prices(
        NOVEMBER,
        lambda start: by_band(start) + D("1.5") - D(start.minute) / 15,
        minutes=15,
    )
    by_quarter = monthly_averages(NOVEMBER, quarters, minutes=15)
    check(
        "quarter-hour prices give the same averages",
        (by_quarter.f0, by_quarter.f1, by_quarter.f2, by_quarter.f3, by_quarter.f23),
        (averages.f0, averages.f1, averages.f2, averages.f3, averages.f23),
    )
    check("quarter-hour slots", (by_quarter.slots, by_quarter.expected), (2880, 2880))


def test_rounding() -> None:
    """Averages are rounded to the cent of EUR/MWh, half up, as GME publishes."""
    averages = monthly_averages(
        NOVEMBER, month_prices(NOVEMBER, lambda start: D("100.005"))
    )
    check("half a cent rounds up", averages.f0, D("100.01"))
    averages = monthly_averages(
        NOVEMBER, month_prices(NOVEMBER, lambda start: D("100.00499"))
    )
    check("less than half a cent rounds down", averages.f1, D("100.00"))


def test_partial_month() -> None:
    """Before the month is over the averages cover the hours published so far."""
    sunday = monthly_averages(
        NOVEMBER, month_prices(NOVEMBER, by_band, days=[NOVEMBER])
    )
    check("Sunday only: no F1 yet", sunday.f1, None)
    check("Sunday only: no F2 yet", sunday.f2, None)
    check("Sunday only: F3", sunday.f3, D("300.00"))
    check("Sunday only: F23", sunday.f23, D("300.00"))
    check("Sunday only: slots", (sunday.slots, sunday.expected), (24, 720))
    check("Sunday only: not complete", sunday.complete, False)
    check(
        "Sunday only: values for the bands known",
        sunday.market_values(),
        {"pun_f0": D("0.3"), "pun_f3": D("0.3"), "pun_f23": D("0.3")},
    )
    check("no prices at all", monthly_averages(NOVEMBER, []).f0, None)


def test_errors() -> None:
    """Prices outside the month, repeated or without a time zone are refused."""
    prices = month_prices(NOVEMBER, by_band, days=[NOVEMBER])
    outside = (slot_start(date(2026, 10, 31), 24), D(1))
    raises("price outside the month", monthly_averages, NOVEMBER, [*prices, outside])
    raises(
        "same slot twice", monthly_averages, NOVEMBER, [*prices, (prices[5][0], D(1))]
    )
    naive = (datetime(2026, 11, 2, 10), D(1))
    raises("time without a zone", monthly_averages, NOVEMBER, [naive])
    misaligned = (prices[0][0] + timedelta(minutes=30), D(1))
    raises("time not on a slot", monthly_averages, NOVEMBER, [misaligned])


def test_market_values() -> None:
    """Averages become market values in EUR/kWh, ready for the contract.

    F1, F2 and F3 are the September 2026 averages printed on the Ajo bill;
    F0 and F23 are made up.
    """
    september = PunAverages(
        month=date(2026, 9, 1),
        f0=D("209.84"),
        f1=D("206.35"),
        f2=D("227.58"),
        f3=D("197.02"),
        f23=D("210.47"),
        slots=720,
        expected=720,
    )
    values = september.market_values()
    check("PUN F1 in EUR/kWh", values["pun_f1"], D("0.20635"))
    check("PUN F23 in EUR/kWh", values["pun_f23"], D("0.21047"))
    check(
        "all bands", sorted(values), ["pun_f0", "pun_f1", "pun_f2", "pun_f23", "pun_f3"]
    )
    indexed = Contract.from_dict(
        {
            "schema_version": 1,
            "supplier": "Test",
            "price_type": "indexed",
            "time_bands": "f1_f2_f3",
            "index": {"name": "pun_index_gme", "period": "monthly", "coefficient": 1},
            "dispatching": {
                "mode": "included",
                "fixed_value": None,
                "capacity_market": False,
            },
        }
    )
    check(
        "the contract takes the bands it is priced with",
        build_market(indexed, values).pun,
        {"f1": D("0.20635"), "f2": D("0.22758"), "f3": D("0.19702")},
    )


def main() -> int:
    """Run every case and report."""
    for test in (
        test_slots,
        test_full_month,
        test_rounding,
        test_partial_month,
        test_errors,
        test_market_values,
    ):
        test()
    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
