# SPDX-License-Identifier: GPL-3.0-or-later
"""Monthly energy counters by time band, fed by cumulative meter readings.

Each meter reports a total that only grows. The increase between two readings
of the same meter is added to the band in force when the second reading
arrives. Counters are kept by physical band (F1, F2, F3) and grouped the way
the contract bills them only when asked, so changing contract loses nothing.

Known limits: the energy between two readings all goes to the band and the
month of the second one. With meters that report every few minutes the error
is a few minutes of consumption at each band change.

Pure Python: no Home Assistant imports are allowed in this package.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Self

from .bands import ITALY, Band, band_at, contract_band
from .contract import TimeBands

_ZERO = Decimal(0)


def month_of(moment: datetime) -> date:
    """Return the first day of the Italian calendar month `moment` falls in."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(ITALY)
    return moment.date().replace(day=1)


def _next_month(month: date) -> date:
    """Return the first day of the month after `month`."""
    if month.month == 12:
        return date(month.year + 1, 1, 1)
    return date(month.year, month.month + 1, 1)


@dataclass(frozen=True, slots=True)
class MonthEnergy:
    """Energy drawn from the grid in one calendar month, in kWh by band."""

    month: date
    kwh: Mapping[Band, Decimal]

    @classmethod
    def empty(cls, month: date) -> Self:
        """Return a month with no consumption."""
        return cls(month, dict.fromkeys(Band, _ZERO))

    @property
    def total(self) -> Decimal:
        """Return the kWh of the month, all bands."""
        return sum(self.kwh.values(), _ZERO)

    def by_contract_band(self, time_bands: TimeBands) -> dict[str, Decimal]:
        """Return the kWh grouped under the contract band keys."""
        grouped: dict[str, Decimal] = {}
        for band in Band:
            key = contract_band(band, time_bands)
            grouped[key] = grouped.get(key, _ZERO) + self.kwh[band]
        return grouped

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return {
            "month": self.month.isoformat(),
            "kwh": {band.value: str(self.kwh[band]) for band in Band},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Rebuild a month from `to_dict` output."""
        return cls(
            date.fromisoformat(data["month"]),
            {band: Decimal(data["kwh"][band.value]) for band in Band},
        )


class Counters:
    """The running month, the one before it, and the last reading of each meter."""

    def __init__(self, moment: datetime) -> None:
        """Start empty in the month of `moment`."""
        self.current = MonthEnergy.empty(month_of(moment))
        self.previous: MonthEnergy | None = None
        self._last_reading: dict[str, Decimal] = {}

    def roll(self, moment: datetime) -> bool:
        """Close the running month if `moment` is in a later one.

        Returns True when the month changed. The closed month becomes
        `previous` only if it is the one right before the new month: after a
        longer gap there is no reliable "last month" and `previous` is None.
        """
        month = month_of(moment)
        if month <= self.current.month:
            return False
        adjacent = _next_month(self.current.month) == month
        self.previous = self.current if adjacent else None
        self.current = MonthEnergy.empty(month)
        return True

    def record(self, meter: str, reading_kwh: Decimal, moment: datetime) -> Decimal:
        """Take a cumulative reading of `meter` and return the kWh it added.

        The first reading of a meter only sets its starting point. A reading
        lower than the last one means the meter was reset or replaced: it
        becomes the new starting point and adds nothing.
        """
        self.roll(moment)
        last = self._last_reading.get(meter)
        self._last_reading[meter] = reading_kwh
        if last is None or reading_kwh <= last:
            return _ZERO
        added = reading_kwh - last
        band = band_at(moment)
        kwh = dict(self.current.kwh)
        kwh[band] += added
        self.current = MonthEnergy(self.current.month, kwh)
        return added

    def keep_meters(self, meters: Iterable[str]) -> None:
        """Forget the last reading of every meter not in `meters`."""
        wanted = set(meters)
        self._last_reading = {
            meter: reading
            for meter, reading in self._last_reading.items()
            if meter in wanted
        }

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return {
            "current": self.current.to_dict(),
            "previous": None if self.previous is None else self.previous.to_dict(),
            "last_reading": {
                meter: str(reading) for meter, reading in self._last_reading.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Rebuild the counters from `to_dict` output."""
        counters = cls.__new__(cls)
        counters.current = MonthEnergy.from_dict(data["current"])
        previous = data.get("previous")
        counters.previous = (
            None if previous is None else MonthEnergy.from_dict(previous)
        )
        counters._last_reading = {
            meter: Decimal(reading)
            for meter, reading in data.get("last_reading", {}).items()
        }
        return counters
