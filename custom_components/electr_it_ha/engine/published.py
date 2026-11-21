# SPDX-License-Identifier: GPL-3.0-or-later
"""Market values published by ARERA and Terna, as kept in market_values.json.

A workflow of the repository reads the official publications and keeps one
JSON file up to date; the integration downloads it and falls back to the copy
it ships with. This module reads and writes that file and answers which value
applies to a month. It never guesses: a month with no published value has
none, and the caller decides what to do.

The values, by name (the same names as engine.market):

- `dispatching`: Terna's dispatching charge under the TIDE, one per quarter,
  published before the quarter starts;
- `capacity`: Terna's capacity market charge for the hours outside the
  system peak, one per quarter, published before the quarter starts;
- `cdispd`: ARERA's CDispD for household customers in the free market, one
  per month, published after the month starts. Losses are already in it.

All in EUR/kWh. Each entry keeps the document it was read from.

The file format:

    {
      "schema_version": 1,
      "updated": "2026-11-21",
      "license": "CC-BY-SA-4.0",
      "attribution": "...",
      "values": {
        "dispatching": [
          {"from": "2026-07-01", "to": "2026-09-30",
           "eur_kwh": "0.010501", "source": "..."}
        ],
        ...
      }
    }

A reader ignores names and keys it does not know, so values can be added
without breaking older versions of the integration; a different
`schema_version` means a format it cannot read.

Pure Python: no Home Assistant imports are allowed in this package.
"""

import calendar
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any, Self

from .market import CAPACITY, CDISPD, DISPATCHING

SCHEMA_VERSION = 1
NAMES = (DISPATCHING, CAPACITY, CDISPD)
LICENSE = "CC-BY-SA-4.0"
ATTRIBUTION = (
    "Values published by ARERA (Autorità di Regolazione per Energia Reti e "
    "Ambiente), reused under CC BY-SA 4.0, and by Terna S.p.A. Collected by "
    "electrITHA; this file is distributed under the same licence."
)


class PublishedDataError(ValueError):
    """The file cannot be read, or its values contradict each other."""


@dataclass(frozen=True, slots=True)
class PublishedValue:
    """One value, valid from `valid_from` to `valid_to`, both included."""

    name: str
    valid_from: date
    valid_to: date
    eur_kwh: Decimal
    source: str

    def covers(self, start: date, end: date) -> bool:
        """True when the value applies to every day from `start` to `end`."""
        return self.valid_from <= start and end <= self.valid_to


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """Return the first and the last day of a month."""
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _date(text: Any, what: str) -> date:
    if not isinstance(text, str):
        raise PublishedDataError(f"{what}: expected a date as YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise PublishedDataError(f"{what}: '{text}' is not a date") from None


def _decimal(value: Any, what: str) -> Decimal:
    # A float would carry its binary error into the bill: numbers are strings.
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise PublishedDataError(f"{what}: expected a number written as a string")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise PublishedDataError(f"{what}: '{value}' is not a number") from None
    if not number.is_finite():
        raise PublishedDataError(f"{what}: '{value}' is not a finite number")
    return number


def _entry(name: str, position: int, item: Any) -> PublishedValue:
    what = f"{name}[{position}]"
    if not isinstance(item, Mapping):
        raise PublishedDataError(f"{what}: expected an object")
    source = item.get("source")
    if not isinstance(source, str) or not source.strip():
        raise PublishedDataError(f"{what}: the source is missing")
    value = PublishedValue(
        name,
        _date(item.get("from"), f"{what}.from"),
        _date(item.get("to"), f"{what}.to"),
        _decimal(item.get("eur_kwh"), f"{what}.eur_kwh"),
        source.strip(),
    )
    if value.valid_to < value.valid_from:
        raise PublishedDataError(f"{what}: it ends before it starts")
    return value


def _overlaps(values: Iterable[PublishedValue]) -> list[str]:
    by_name: dict[str, list[PublishedValue]] = {}
    for value in values:
        by_name.setdefault(value.name, []).append(value)
    problems: list[str] = []
    for name, items in by_name.items():
        items.sort(key=lambda item: item.valid_from)
        problems.extend(
            f"{name}: two values on {later.valid_from.isoformat()}"
            for earlier, later in pairwise(items)
            if later.valid_from <= earlier.valid_to
        )
    return problems


@dataclass(frozen=True, slots=True)
class Published:
    """The content of market_values.json."""

    updated: date
    values: tuple[PublishedValue, ...]

    def __post_init__(self) -> None:
        """Refuse two values of the same name on the same day."""
        problems = _overlaps(self.values)
        if problems:
            raise PublishedDataError("; ".join(problems))

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        """Read the file content; PublishedDataError when it is not sound."""
        if not isinstance(data, Mapping):
            raise PublishedDataError("the file does not hold an object")
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise PublishedDataError(
                f"schema_version {version!r} is not supported (expected {SCHEMA_VERSION})"
            )
        groups = data.get("values")
        if not isinstance(groups, Mapping):
            raise PublishedDataError("'values' is missing")
        values: list[PublishedValue] = []
        for name in NAMES:
            items = groups.get(name, [])
            if not isinstance(items, list):
                raise PublishedDataError(f"{name}: expected a list")
            entries = [_entry(name, n, item) for n, item in enumerate(items)]
            values.extend(sorted(entries, key=lambda value: value.valid_from))
        return cls(_date(data.get("updated"), "updated"), tuple(values))

    def to_dict(self) -> dict[str, Any]:
        """Return the file content, sorted so that a change reads as a small diff."""
        groups: dict[str, list[dict[str, str]]] = {}
        for name in NAMES:
            items = sorted(
                (value for value in self.values if value.name == name),
                key=lambda value: value.valid_from,
            )
            groups[name] = [
                {
                    "from": value.valid_from.isoformat(),
                    "to": value.valid_to.isoformat(),
                    "eur_kwh": str(value.eur_kwh),
                    "source": value.source,
                }
                for value in items
            ]
        return {
            "schema_version": SCHEMA_VERSION,
            "updated": self.updated.isoformat(),
            "license": LICENSE,
            "attribution": ATTRIBUTION,
            "values": groups,
        }

    def for_month(self, name: str, year: int, month: int) -> PublishedValue | None:
        """Return the value of `name` for a whole month, or None if not published.

        A value that covers only part of the month does not count: the bill of
        a month uses one value.
        """
        start, end = month_bounds(year, month)
        for value in self.values:
            if value.name == name and value.covers(start, end):
                return value
        return None

    def latest(self, name: str, day: date) -> PublishedValue | None:
        """Return the most recent value of `name` starting on or before `day`.

        For an estimate of a month whose value is not out yet: the caller
        must say that the value is not the month's own.
        """
        candidates = [
            value
            for value in self.values
            if value.name == name and value.valid_from <= day
        ]
        return max(candidates, key=lambda value: value.valid_from, default=None)


def newest(*copies: Published | None) -> Published | None:
    """Return the copy updated most recently; on a tie, the first one given.

    The integration holds up to three copies (downloaded now, saved from an
    earlier download, shipped with it) and keeps the newest that is sound.
    """
    best: Published | None = None
    for copy in copies:
        if copy is not None and (best is None or copy.updated > best.updated):
            best = copy
    return best
