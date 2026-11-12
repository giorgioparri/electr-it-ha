# SPDX-License-Identifier: GPL-3.0-or-later
"""Monthly averages of the PUN Index GME by time band.

GME publishes one price per hour (or per quarter hour) of each day, numbered
from 1: a day has 23 hours when clocks go forward and 25 when they go back.
The monthly value of a band is the simple mean of the prices of its slots,
rounded to the cent of EUR/MWh, half up. F23 is the mean over the F2 and F3
slots together, F0 the mean over every slot of the month.

Pure Python: no Home Assistant imports are allowed in this package.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .bands import ITALY, Band, band_at
from .market import PUN_PREFIX

_CENT = Decimal("0.01")
_MWH_TO_KWH = Decimal(1000)
_SLOT_MINUTES = (15, 60)


class PunError(ValueError):
    """The prices do not fit the month, or a slot does not exist."""


@dataclass(frozen=True, slots=True)
class PunAverages:
    """Averages of one month in EUR/MWh; None for a band with no price yet."""

    month: date
    f0: Decimal | None
    f1: Decimal | None
    f2: Decimal | None
    f3: Decimal | None
    f23: Decimal | None
    slots: int
    expected: int

    @property
    def complete(self) -> bool:
        """True when every slot of the month has a price."""
        return self.slots == self.expected

    def market_values(self) -> dict[str, Decimal]:
        """Return the known averages as market values in EUR/kWh."""
        values: dict[str, Decimal] = {}
        for band in ("f0", "f1", "f2", "f3", "f23"):
            average = getattr(self, band)
            if average is not None:
                values[f"{PUN_PREFIX}{band}"] = average / _MWH_TO_KWH
        return values


def _check_minutes(minutes: int) -> None:
    if minutes not in _SLOT_MINUTES:
        raise PunError(f"slots of {minutes} minutes are not published")


def _midnight(day: date) -> datetime:
    """Return the start of `day` in Italian time, as UTC."""
    return datetime(day.year, day.month, day.day, tzinfo=ITALY).astimezone(UTC)


def slots_in_day(day: date, minutes: int = 60) -> int:
    """Return how many slots of `minutes` the Italian day `day` has."""
    _check_minutes(minutes)
    length = _midnight(day + timedelta(days=1)) - _midnight(day)
    return int(length / timedelta(minutes=minutes))


def slot_start(day: date, index: int, minutes: int = 60) -> datetime:
    """Return the start, in Italian time, of slot `index` (from 1) of `day`."""
    count = slots_in_day(day, minutes)
    if not 1 <= index <= count:
        raise PunError(f"{day.isoformat()} has slots 1 to {count}, not {index}")
    start = _midnight(day) + timedelta(minutes=minutes * (index - 1))
    return start.astimezone(ITALY)


def _first_day(month: date) -> date:
    return month.replace(day=1)


def _next_month(month: date) -> date:
    return (_first_day(month) + timedelta(days=32)).replace(day=1)


def slots_in_month(month: date, minutes: int = 60) -> int:
    """Return how many slots of `minutes` the month of `month` has."""
    _check_minutes(minutes)
    length = _midnight(_next_month(month)) - _midnight(_first_day(month))
    return int(length / timedelta(minutes=minutes))


def _mean(prices: list[Decimal]) -> Decimal | None:
    if not prices:
        return None
    return (sum(prices, Decimal(0)) / len(prices)).quantize(_CENT, ROUND_HALF_UP)


def monthly_averages(
    month: date, prices: Iterable[tuple[datetime, Decimal]], minutes: int = 60
) -> PunAverages:
    """Average the prices of `month` by band.

    Each price comes with the aware start of its slot, as slot_start returns
    it. Prices may cover only part of the month: the averages then cover the
    slots published so far, and `complete` is False.
    """
    _check_minutes(minutes)
    month = _first_day(month)
    start, end = _midnight(month), _midnight(_next_month(month))
    step = timedelta(minutes=minutes)
    seen: set[datetime] = set()
    by_band: dict[Band, list[Decimal]] = {band: [] for band in Band}
    for moment, price in prices:
        if moment.tzinfo is None:
            raise PunError(f"{moment.isoformat()} has no time zone")
        moment = moment.astimezone(UTC)
        if not start <= moment < end:
            raise PunError(f"{moment.isoformat()} is outside {month:%Y-%m}")
        if (moment - start) % step:
            raise PunError(f"{moment.isoformat()} is not the start of a slot")
        if moment in seen:
            raise PunError(f"{moment.isoformat()} has more than one price")
        seen.add(moment)
        by_band[band_at(moment)].append(price)
    return PunAverages(
        month=month,
        f0=_mean([price for band in Band for price in by_band[band]]),
        f1=_mean(by_band[Band.F1]),
        f2=_mean(by_band[Band.F2]),
        f3=_mean(by_band[Band.F3]),
        f23=_mean(by_band[Band.F2] + by_band[Band.F3]),
        slots=len(seen),
        expected=slots_in_month(month, minutes),
    )
