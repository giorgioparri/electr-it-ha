# SPDX-License-Identifier: GPL-3.0-or-later
"""Regulated values read from the Portale Offerte, as kept in regulated_values.json.

The Portale Offerte publishes once a day a file of the parameters it uses for
the yearly cost of every offer: the regulated tariffs, system charges and
taxes of the running quarter. A workflow of the repository reads it (see
tools/update_portale_parameters.py) and keeps this archive; the integration
uses it before its built-in table (engine.regulated).

The file of a day gives its values to the whole calendar quarter of that day,
and the last value read in a quarter wins. On the first days of a quarter the
Portale may still show the quarter before: those values stand in until the
new ones appear, and then they are replaced for the whole quarter. Each entry
keeps the day its value was first read, which tells when an update came.

The file format:

    {
      "schema_version": 1,
      "updated": "2026-11-27",
      "attribution": "...",
      "values": {
        "asos_eur_kwh": [
          {"from": "2026-10-01", "to": "2026-12-31", "value": "0.031515",
           "first_seen": "2026-11-27",
           "source": "PO_Parametri_Mercato_Libero_E_20261127.csv"}
        ],
        ...
      }
    }

Values are keyed by the names of engine.regulated.Component, whose unit is
part of the name. A reader ignores names it does not know, so components can
be added without breaking older versions of the integration; a different
`schema_version` means a format it cannot read.

Pure Python: no Home Assistant imports are allowed in this package.
"""

import calendar
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Self

from .regulated import Component, Rate, table_problems

SCHEMA_VERSION = 1
ATTRIBUTION = (
    "Parameters published by the Portale Offerte of ARERA (Autorità di "
    "Regolazione per Energia Reti e Ambiente) in its open data. Collected by "
    "electrITHA."
)
_SOURCE_PREFIX = "Portale Offerte, "


class PublishedRatesError(ValueError):
    """The file cannot be read, or its values contradict each other."""


def quarter_bounds(day: date) -> tuple[date, date]:
    """Return the first and the last day of the calendar quarter of `day`."""
    first = (day.month - 1) // 3 * 3 + 1
    last = first + 2
    return (
        date(day.year, first, 1),
        date(day.year, last, calendar.monthrange(day.year, last)[1]),
    )


@dataclass(frozen=True, slots=True)
class Entry:
    """The value of a component for one quarter, from `valid_from` to `valid_to`."""

    component: Component
    valid_from: date
    valid_to: date
    value: Decimal
    first_seen: date
    source: str

    def rate(self) -> Rate:
        """Return the entry as a rate of the calculator's table."""
        return Rate(
            self.component,
            self.valid_from,
            self.valid_to,
            self.value,
            f"{_SOURCE_PREFIX}{self.source}",
        )


def _date(text: Any, what: str) -> date:
    if not isinstance(text, str):
        raise PublishedRatesError(f"{what}: expected a date as YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise PublishedRatesError(f"{what}: '{text}' is not a date") from None


def _decimal(value: Any, what: str) -> Decimal:
    # A float would carry its binary error into the bill: numbers are strings.
    if not isinstance(value, str):
        raise PublishedRatesError(f"{what}: expected a number written as a string")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise PublishedRatesError(f"{what}: '{value}' is not a number") from None
    if not number.is_finite():
        raise PublishedRatesError(f"{what}: '{value}' is not a finite number")
    return number


def _entry(component: Component, position: int, item: Any) -> Entry:
    what = f"{component}[{position}]"
    if not isinstance(item, Mapping):
        raise PublishedRatesError(f"{what}: expected an object")
    source = item.get("source")
    if not isinstance(source, str) or not source.strip():
        raise PublishedRatesError(f"{what}: the source is missing")
    entry = Entry(
        component,
        _date(item.get("from"), f"{what}.from"),
        _date(item.get("to"), f"{what}.to"),
        _decimal(item.get("value"), f"{what}.value"),
        _date(item.get("first_seen"), f"{what}.first_seen"),
        source.strip(),
    )
    if entry.valid_to < entry.valid_from:
        raise PublishedRatesError(f"{what}: it ends before it starts")
    return entry


def _sorted(entries: list[Entry] | tuple[Entry, ...]) -> tuple[Entry, ...]:
    return tuple(sorted(entries, key=lambda item: (item.component, item.valid_from)))


@dataclass(frozen=True, slots=True)
class PublishedRates:
    """The content of regulated_values.json."""

    updated: date
    entries: tuple[Entry, ...]

    def __post_init__(self) -> None:
        """Refuse two values of the same component on the same day."""
        problems = table_problems(tuple(item.rate() for item in self.entries))
        if problems:
            raise PublishedRatesError("; ".join(problems))

    @classmethod
    def empty(cls) -> Self:
        """Return an archive with nothing read yet."""
        return cls(date.min, ())

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        """Read the file content; PublishedRatesError when it is not sound."""
        if not isinstance(data, Mapping):
            raise PublishedRatesError("the file does not hold an object")
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise PublishedRatesError(
                f"schema_version {version!r} is not supported (expected {SCHEMA_VERSION})"
            )
        groups = data.get("values")
        if not isinstance(groups, Mapping):
            raise PublishedRatesError("'values' is missing")
        entries: list[Entry] = []
        for component in Component:
            items = groups.get(component.value, [])
            if not isinstance(items, list):
                raise PublishedRatesError(f"{component}: expected a list")
            entries.extend(_entry(component, n, item) for n, item in enumerate(items))
        return cls(_date(data.get("updated"), "updated"), _sorted(entries))

    def to_dict(self) -> dict[str, Any]:
        """Return the file content, sorted so that a change reads as a small diff."""
        groups: dict[str, list[dict[str, str]]] = {}
        for item in self.entries:
            groups.setdefault(item.component.value, []).append(
                {
                    "from": item.valid_from.isoformat(),
                    "to": item.valid_to.isoformat(),
                    "value": str(item.value),
                    "first_seen": item.first_seen.isoformat(),
                    "source": item.source,
                }
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "updated": self.updated.isoformat(),
            "attribution": ATTRIBUTION,
            "values": dict(sorted(groups.items())),
        }

    def observe(
        self, day: date, values: Mapping[Component, Decimal], source: str
    ) -> Self:
        """Add the values of the file of `day`; return self when nothing changes.

        A value gives its quarter one entry. A value equal to the one already
        there changes nothing; a different one replaces it for the whole
        quarter, and its first day becomes `day`.
        """
        start, end = quarter_bounds(day)
        by_key = {(item.component, item.valid_from): item for item in self.entries}
        changed = False
        for component, value in values.items():
            old = by_key.get((component, start))
            if old is not None and old.value == value:
                continue
            by_key[(component, start)] = Entry(
                component, start, end, value, day, source
            )
            changed = True
        if not changed:
            return self
        return replace(self, updated=day, entries=_sorted(list(by_key.values())))

    def previous(self, component: Component, day: date) -> Decimal | None:
        """Return the value of `component` in the quarter before the one of `day`."""
        start, _ = quarter_bounds(day)
        before = start - timedelta(days=1)
        for item in self.entries:
            if (
                item.component is component
                and item.valid_from <= before <= item.valid_to
            ):
                return item.value
        return None

    def rates(self) -> tuple[Rate, ...]:
        """Return the archive as a rate table for the calculator."""
        return tuple(item.rate() for item in self.entries)
