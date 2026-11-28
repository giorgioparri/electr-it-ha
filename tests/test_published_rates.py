# SPDX-License-Identifier: GPL-3.0-or-later
"""Regulated values read from the Portale Offerte. Run as a script:

    python3 tests/test_published_rates.py

The values below are those of the Portale's parameters file of 27 November
2026 (tests/fixtures/portale_parametri_20261127.csv), which match the
built-in table for the fourth quarter of 2026.
"""

import copy
import json
import sys
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(
    0, str(Path(__file__).parents[1] / "custom_components" / "electr_it_ha")
)

from engine.published_rates import (
    SCHEMA_VERSION,
    PublishedRates,
    PublishedRatesError,
    quarter_bounds,
)
from engine.regulated import RATES, Component, rate, table_problems

D = Decimal
C = Component
_failures: list[str] = []

SOURCE = "PO_Parametri_Mercato_Libero_E_20261127.csv"

# The thirteen values of the 27 November 2026 file, by component.
Q4_2026: dict[Component, Decimal] = {
    C.NETWORK_FIXED_EUR_POD_YEAR: D("23.04"),
    C.NETWORK_POWER_EUR_KW_YEAR: D("23.52"),
    C.NETWORK_ENERGY_EUR_KWH: D("0.0119"),
    C.UC3_EUR_KWH: D("0.00276"),
    C.UC6_EUR_KWH: D("0.00007"),
    C.UC6_POWER_EUR_KW_YEAR: D("0.1988"),
    C.ASOS_EUR_KWH: D("0.031515"),
    C.ARIM_EUR_KWH: D("0.001638"),
    C.ASOS_NON_RESIDENT_EUR_POD_YEAR: D("95.0916"),
    C.DISP_BT_EUR_POD_YEAR: D("1.107"),
    C.TUTELA_PCV_EUR_POD_YEAR: D("41.18"),
    C.EXCISE_EUR_KWH: D("0.0227"),
    C.VAT_RATE: D("0.1"),
}


def check(what: str, actual: object, expected: object) -> None:
    """Compare a value with the expected one."""
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {what}: {actual}, expected {expected}")
    if not ok:
        _failures.append(what)


def raises(
    what: str, error: type[Exception], call: Callable[..., object], *args: object
) -> None:
    """Check that a call raises `error`."""
    try:
        call(*args)
    except error as err:
        print(f"PASS  {what}: {err}")
    else:
        print(f"FAIL  {what}: no {error.__name__} raised")
        _failures.append(what)


def first_read() -> PublishedRates:
    """The archive after reading the file of 27 November 2026."""
    return PublishedRates.empty().observe(date(2026, 11, 27), Q4_2026, SOURCE)


def entry(published: PublishedRates, component: Component, day: date) -> Any:
    """The entry of `component` that covers `day`, or None."""
    return next(
        (
            item
            for item in published.entries
            if item.component is component and item.valid_from <= day <= item.valid_to
        ),
        None,
    )


def test_quarters() -> None:
    """A day belongs to its calendar quarter."""
    check(
        "1 Jan", quarter_bounds(date(2027, 1, 1)), (date(2027, 1, 1), date(2027, 3, 31))
    )
    check(
        "31 Mar",
        quarter_bounds(date(2027, 3, 31)),
        (date(2027, 1, 1), date(2027, 3, 31)),
    )
    check(
        "1 Apr", quarter_bounds(date(2027, 4, 1)), (date(2027, 4, 1), date(2027, 6, 30))
    )
    check(
        "15 Aug",
        quarter_bounds(date(2027, 8, 15)),
        (date(2027, 7, 1), date(2027, 9, 30)),
    )
    check(
        "31 Dec",
        quarter_bounds(date(2026, 12, 31)),
        (date(2026, 10, 1), date(2026, 12, 31)),
    )


def test_first_read() -> None:
    """A file read in a quarter gives a value to the whole quarter."""
    published = first_read()
    check("one entry per component", len(published.entries), len(Q4_2026))
    check("updated", published.updated, date(2026, 11, 27))
    asos = entry(published, C.ASOS_EUR_KWH, date(2026, 10, 1))
    check("from the first day of the quarter", asos.valid_from, date(2026, 10, 1))
    check("to the last day of the quarter", asos.valid_to, date(2026, 12, 31))
    check("value", asos.value, D("0.031515"))
    check("source", asos.source, SOURCE)
    check("first seen", asos.first_seen, date(2026, 11, 27))
    check(
        "nothing before the quarter",
        entry(published, C.ASOS_EUR_KWH, date(2026, 9, 30)),
        None,
    )


def test_same_file_again() -> None:
    """A file with the same values changes nothing, not even the date."""
    published = first_read()
    again = published.observe(date(2026, 11, 28), Q4_2026, "next day.csv")
    check("same object", again is published, True)


def test_change_within_quarter() -> None:
    """A value that changes within a quarter replaces it for the whole quarter."""
    published = first_read()
    values = {**Q4_2026, C.ASOS_EUR_KWH: D("0.03")}
    changed = published.observe(date(2026, 12, 3), values, "3 Dec.csv")
    check("still one entry per component", len(changed.entries), len(Q4_2026))
    asos = entry(changed, C.ASOS_EUR_KWH, date(2026, 10, 1))
    check("new value from the first day", asos.value, D("0.03"))
    check("first seen on the change", asos.first_seen, date(2026, 12, 3))
    check("source of the change", asos.source, "3 Dec.csv")
    arim = entry(changed, C.ARIM_EUR_KWH, date(2026, 10, 1))
    check("unchanged value keeps its first day", arim.first_seen, date(2026, 11, 27))
    check("unchanged value keeps its source", arim.source, SOURCE)
    check("updated", changed.updated, date(2026, 12, 3))
    check(
        "the old archive is untouched",
        entry(published, C.ASOS_EUR_KWH, date(2026, 10, 1)).value,
        D("0.031515"),
    )


def test_late_update() -> None:
    """The Portale may still show last quarter's values on the first days."""
    published = first_read()
    late = published.observe(date(2027, 1, 2), Q4_2026, "2 Jan.csv")
    q1 = entry(late, C.ASOS_EUR_KWH, date(2027, 1, 1))
    check("a new quarter gets its own entry", q1.valid_from, date(2027, 1, 1))
    check("with the values read, even if old", q1.value, D("0.031515"))
    check(
        "the quarter before stays",
        entry(late, C.ASOS_EUR_KWH, date(2026, 12, 31)).value,
        D("0.031515"),
    )
    check("two quarters of entries", len(late.entries), 2 * len(Q4_2026))
    fixed = late.observe(
        date(2027, 1, 5), {**Q4_2026, C.ASOS_EUR_KWH: D("0.0325")}, "5 Jan.csv"
    )
    q1 = entry(fixed, C.ASOS_EUR_KWH, date(2027, 1, 1))
    check("the update covers the whole quarter", q1.value, D("0.0325"))
    check("its first day says when it came", q1.first_seen, date(2027, 1, 5))
    check(
        "the quarter before is not touched",
        entry(fixed, C.ASOS_EUR_KWH, date(2026, 12, 31)).value,
        D("0.031515"),
    )
    check("same entries after the update", len(fixed.entries), 2 * len(Q4_2026))


def test_previous_quarter() -> None:
    """The value of the quarter before, to tell whether a quarter changed."""
    late = first_read().observe(date(2027, 1, 2), Q4_2026, "2 Jan.csv")
    check(
        "previous quarter",
        late.previous(C.ASOS_EUR_KWH, date(2027, 1, 2)),
        D("0.031515"),
    )
    check(
        "none before the archive",
        late.previous(C.ASOS_EUR_KWH, date(2026, 11, 27)),
        None,
    )


def test_rates() -> None:
    """The archive as a rate table the calculator can use."""
    published = first_read()
    rates = published.rates()
    check("a sound table", table_problems(rates), [])
    day = date(2026, 11, 27)
    for component, value in Q4_2026.items():
        check(
            f"{component} as the built-in table",
            rate(component, day, rates),
            rate(component, day, RATES),
        )
        check(f"{component} as read", rate(component, day, rates), value)
    check("source names the Portale", "Portale Offerte" in rates[0].source, True)


def test_round_trip() -> None:
    """The file is written sorted, with numbers as strings, and read back."""
    published = first_read()
    data = published.to_dict()
    check("schema version", data["schema_version"], SCHEMA_VERSION)
    check("updated", data["updated"], "2026-11-27")
    check(
        "attribution names the Portale", "Portale Offerte" in data["attribution"], True
    )
    check(
        "groups by component, sorted",
        list(data["values"]),
        sorted(c.value for c in Q4_2026),
    )
    check(
        "one entry",
        data["values"]["asos_eur_kwh"],
        [
            {
                "from": "2026-10-01",
                "to": "2026-12-31",
                "value": "0.031515",
                "first_seen": "2026-11-27",
                "source": SOURCE,
            }
        ],
    )
    text = json.dumps(data)
    back = PublishedRates.from_dict(json.loads(text))
    check("read back", back, published)


def test_bad_files() -> None:
    """A file that cannot be trusted is refused as a whole."""
    good = first_read().to_dict()

    def edited(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        data = copy.deepcopy(good)
        change(data)
        return data

    def overlapping(data: dict[str, Any]) -> None:
        item = dict(
            data["values"]["asos_eur_kwh"][0],
            **{"from": "2026-11-01", "to": "2027-01-31"},
        )
        data["values"]["asos_eur_kwh"].append(item)

    raises("not an object", PublishedRatesError, PublishedRates.from_dict, [])
    raises(
        "other schema",
        PublishedRatesError,
        PublishedRates.from_dict,
        edited(lambda d: d.update(schema_version=2)),
    )
    raises(
        "float value",
        PublishedRatesError,
        PublishedRates.from_dict,
        edited(lambda d: d["values"]["asos_eur_kwh"][0].update(value=0.031515)),
    )
    raises(
        "not a number",
        PublishedRatesError,
        PublishedRates.from_dict,
        edited(lambda d: d["values"]["asos_eur_kwh"][0].update(value="abc")),
    )
    raises(
        "not a date",
        PublishedRatesError,
        PublishedRates.from_dict,
        edited(lambda d: d["values"]["asos_eur_kwh"][0].update(to="31/12/2026")),
    )
    raises(
        "no source",
        PublishedRatesError,
        PublishedRates.from_dict,
        edited(lambda d: d["values"]["asos_eur_kwh"][0].update(source=" ")),
    )
    raises(
        "ends before it starts",
        PublishedRatesError,
        PublishedRates.from_dict,
        edited(lambda d: d["values"]["asos_eur_kwh"][0].update(to="2026-09-30")),
    )
    raises(
        "two values on one day",
        PublishedRatesError,
        PublishedRates.from_dict,
        edited(overlapping),
    )

    newer = edited(lambda d: d["values"].update(future_component_eur_kwh=[]))
    check(
        "an unknown component is ignored",
        PublishedRates.from_dict(newer),
        PublishedRates.from_dict(good),
    )


def main() -> int:
    """Run every case and report."""
    for test in (
        test_quarters,
        test_first_read,
        test_same_file_again,
        test_change_within_quarter,
        test_late_update,
        test_previous_quarter,
        test_rates,
        test_round_trip,
        test_bad_files,
    ):
        test()
    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
