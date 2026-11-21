# SPDX-License-Identifier: GPL-3.0-or-later
"""Published market values. Run as a script: python3 tests/test_published.py

The values below are the ones that match the bills in test_bills.py: Terna's
third quarter of 2026 and ARERA's CDispD of September 2026.
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

from engine.published import (
    LICENSE,
    SCHEMA_VERSION,
    Published,
    PublishedDataError,
    month_bounds,
    newest,
)

D = Decimal
_failures: list[str] = []


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


def entry(start: str, end: str, value: str, source: str = "test") -> dict[str, str]:
    """One value as the file writes it."""
    return {"from": start, "to": end, "eur_kwh": value, "source": source}


FILE: dict[str, Any] = {
    "schema_version": 1,
    "updated": "2026-11-21",
    "license": LICENSE,
    "attribution": "test",
    "values": {
        "dispatching": [
            entry("2026-07-01", "2026-09-30", "0.010501", "Terna, 3Q2026"),
        ],
        "capacity": [
            entry("2026-07-01", "2026-09-30", "0.003197", "Terna, capacity 3Q2026"),
        ],
        "cdispd": [
            entry("2026-08-01", "2026-08-31", "0.018468", "ARERA, August 2026"),
            entry("2026-09-01", "2026-09-30", "0.015068", "ARERA, September 2026"),
        ],
    },
}


def changed(change: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    """A copy of FILE with one change applied."""
    data = copy.deepcopy(FILE)
    change(data)
    return data


def test_read() -> None:
    """The file reads into exact decimals, each with its period and source."""
    published = Published.from_dict(FILE)
    check("updated", published.updated, date(2026, 11, 21))
    check("number of values", len(published.values), 4)
    september = published.for_month("dispatching", 2026, 9)
    check("dispatching in September", september and september.eur_kwh, D("0.010501"))
    check("its source", september and september.source, "Terna, 3Q2026")
    check(
        "values are exact decimals",
        type(september and september.eur_kwh),
        Decimal,
    )
    check(
        "the same quarter applies to July",
        published.for_month("capacity", 2026, 7).eur_kwh,
        D("0.003197"),
    )
    cdispd = published.for_month("cdispd", 2026, 9)
    check("CDispD of September", cdispd and cdispd.eur_kwh, D("0.015068"))
    check(
        "CDispD is dispatching plus capacity, times 1.10 for losses",
        ((D("0.010501") + D("0.003197")) * D("1.10")).quantize(D("0.000001")),
        cdispd and cdispd.eur_kwh,
    )


def test_months() -> None:
    """A month with no value has none; nothing is carried over silently."""
    published = Published.from_dict(FILE)
    check("no CDispD for October yet", published.for_month("cdispd", 2026, 10), None)
    check(
        "no dispatching for the fourth quarter yet",
        published.for_month("dispatching", 2026, 10),
        None,
    )
    check("unknown name", published.for_month("pun_f1", 2026, 9), None)
    half = Published.from_dict(
        changed(
            lambda d: d["values"].update(
                cdispd=[entry("2026-10-01", "2026-10-15", "0.01")]
            )
        )
    )
    check("half a month does not count", half.for_month("cdispd", 2026, 10), None)
    check(
        "bounds of February 2028",
        month_bounds(2028, 2),
        (date(2028, 2, 1), date(2028, 2, 29)),
    )


def test_latest() -> None:
    """For an estimate, the last value published before a day."""
    published = Published.from_dict(FILE)
    latest = published.latest("cdispd", date(2026, 11, 21))
    check("last CDispD known in November", latest and latest.eur_kwh, D("0.015068"))
    check(
        "and it says which month it is",
        latest and latest.valid_from,
        date(2026, 9, 1),
    )
    check(
        "a value of the past is not offered for an earlier day",
        published.latest("cdispd", date(2026, 7, 31)),
        None,
    )
    check(
        "the value in force wins over older ones",
        published.latest("cdispd", date(2026, 8, 20)).eur_kwh,
        D("0.018468"),
    )


def test_round_trip() -> None:
    """Writing and reading again gives the same values, in a stable order."""
    shuffled = changed(lambda d: d["values"]["cdispd"].reverse())
    published = Published.from_dict(shuffled)
    written = published.to_dict()
    check("schema version written", written["schema_version"], SCHEMA_VERSION)
    check("licence written", written["license"], LICENSE)
    check(
        "values sorted by date",
        [item["from"] for item in written["values"]["cdispd"]],
        ["2026-08-01", "2026-09-01"],
    )
    again = Published.from_dict(json.loads(json.dumps(written)))
    check("same values after a round trip", again, published)
    check(
        "numbers stay strings", written["values"]["capacity"][0]["eur_kwh"], "0.003197"
    )


def test_forward_compatible() -> None:
    """Names and keys a newer workflow adds are ignored, not refused."""
    newer = changed(
        lambda d: (
            d["values"].update(cdisp=[entry("2027-04-01", "2027-04-30", "0.02")]),
            d.update(notes="added later"),
            d["values"]["capacity"][0].update(peak_eur_kwh="0.05"),
        )
    )
    published = Published.from_dict(newer)
    check("unknown name skipped", len(published.values), 4)
    check(
        "unknown key in a value skipped",
        published.for_month("capacity", 2026, 8).eur_kwh,
        D("0.003197"),
    )
    check(
        "a name may be missing",
        len(Published.from_dict(changed(lambda d: d["values"].pop("capacity"))).values),
        3,
    )


def test_refused() -> None:
    """A file that cannot be trusted is refused as a whole."""
    for what, data in (
        ("not an object", ["values"]),
        ("newer schema", changed(lambda d: d.update(schema_version=2))),
        ("no schema", changed(lambda d: d.pop("schema_version"))),
        ("no values", changed(lambda d: d.pop("values"))),
        ("no update date", changed(lambda d: d.pop("updated"))),
        ("a list that is not a list", changed(lambda d: d["values"].update(cdispd={}))),
        (
            "a value that is not an object",
            changed(lambda d: d["values"].update(cdispd=["0.01"])),
        ),
        (
            "a float",
            changed(lambda d: d["values"]["cdispd"][0].update(eur_kwh=0.018468)),
        ),
        (
            "not a number",
            changed(lambda d: d["values"]["cdispd"][0].update(eur_kwh="n/a")),
        ),
        (
            "not finite",
            changed(lambda d: d["values"]["cdispd"][0].update(eur_kwh="NaN")),
        ),
        (
            "a bad date",
            changed(lambda d: d["values"]["cdispd"][0].update({"from": "2026-02-30"})),
        ),
        (
            "ends before it starts",
            changed(lambda d: d["values"]["cdispd"][0].update(to="2026-07-31")),
        ),
        ("no source", changed(lambda d: d["values"]["cdispd"][0].update(source=" "))),
        (
            "two values on the same day",
            changed(
                lambda d: d["values"]["cdispd"].append(
                    entry("2026-09-15", "2026-10-15", "0.02")
                )
            ),
        ),
    ):
        raises(what, PublishedDataError, Published.from_dict, data)


def test_newest() -> None:
    """Of the copies the integration holds, the most recent one is kept."""
    downloaded = Published.from_dict(FILE)
    saved = Published.from_dict(changed(lambda d: d.update(updated="2026-11-01")))
    shipped = Published.from_dict(changed(lambda d: d.update(updated="2026-12-01")))
    check("a newer shipped copy wins", newest(downloaded, saved, shipped), shipped)
    check("a newer download wins", newest(downloaded, saved), downloaded)
    check("missing copies are skipped", newest(None, saved, None), saved)
    check("nothing at all", newest(None, None), None)
    same_day = Published.from_dict(FILE)
    check(
        "on a tie the first one given", newest(downloaded, same_day) is downloaded, True
    )


def main() -> int:
    """Run every case and report."""
    for test in (
        test_read,
        test_months,
        test_latest,
        test_round_trip,
        test_forward_compatible,
        test_refused,
        test_newest,
    ):
        test()
    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
