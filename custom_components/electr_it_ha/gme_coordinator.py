# SPDX-License-Identifier: GPL-3.0-or-later
"""Coordinator of the GME account: the hourly PUN and its monthly averages.

It keeps the hourly prices of the running month and of the month before on
disk, and downloads only the days still missing: today's prices are known
since yesterday, tomorrow's are published around 13:00. Usually that is one
download a day, shared by every supply.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    GME_PUBLICATION_HOUR,
    GME_STORAGE_KEY,
    GME_UPDATE_INTERVAL_MINUTES,
    STORAGE_VERSION,
)
from .engine.bands import ITALY
from .engine.gme import GmeAuthError, GmeError
from .engine.pun import PunAverages, monthly_averages, slots_in_day
from .gme_client import GmeClient

_LOGGER = logging.getLogger(__name__)

type GmeConfigEntry = ConfigEntry[GmeCoordinator]


@dataclass(frozen=True, slots=True)
class GmeData:
    """Averages of the running month and of the month before, in EUR/MWh."""

    current: PunAverages
    previous: PunAverages


def _month_key(month: date) -> str:
    return f"{month:%Y-%m}"


def _previous_month(month: date) -> date:
    return (month - timedelta(days=1)).replace(day=1)


class GmeCoordinator(DataUpdateCoordinator[GmeData]):
    """Download the PUN for the GME account and average it by band."""

    config_entry: GmeConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: GmeConfigEntry, client: GmeClient
    ) -> None:
        """Nothing is loaded or downloaded yet."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} GME",
            update_interval=timedelta(minutes=GME_UPDATE_INTERVAL_MINUTES),
        )
        self._client = client
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, GME_STORAGE_KEY
        )
        # First day of the month -> start of the hour (UTC) -> price.
        self._prices: dict[date, dict[datetime, Decimal]] = {}
        self._failing = False

    async def async_setup(self) -> None:
        """Load the saved prices and refresh at midnight and after publication."""
        saved = await self._store.async_load()
        for key, prices in (saved or {}).get("months", {}).items():
            month = date.fromisoformat(f"{key}-01")
            self._prices[month] = {
                datetime.fromisoformat(moment): Decimal(price)
                for moment, price in prices.items()
            }
        for hour in (0, GME_PUBLICATION_HOUR):
            self.config_entry.async_on_unload(
                async_track_time_change(
                    self.hass, self._scheduled_refresh, hour=hour, minute=0, second=5
                )
            )

    @callback
    def _scheduled_refresh(self, _now: datetime) -> None:
        self.hass.async_create_task(self.async_request_refresh())

    def _missing_days(self, month: date, last: date) -> list[date]:
        """Return the days of `month` up to `last` without every hourly price."""
        stored = self._prices.get(month, {})
        counts: dict[date, int] = {}
        for moment in stored:
            day = moment.astimezone(ITALY).date()
            counts[day] = counts.get(day, 0) + 1
        missing = []
        day = month
        while day.month == month.month and day <= last:
            if counts.get(day, 0) < slots_in_day(day):
                missing.append(day)
            day += timedelta(days=1)
        return missing

    async def _async_update_data(self) -> GmeData:
        """Download what is missing, then average both months."""
        now = dt_util.now().astimezone(ITALY)
        today = now.date()
        current = today.replace(day=1)
        previous = _previous_month(current)
        for month in [month for month in self._prices if month < previous]:
            del self._prices[month]

        last = today
        if now.hour >= GME_PUBLICATION_HOUR:
            last += timedelta(days=1)
        missing = self._missing_days(previous, last) + self._missing_days(current, last)
        if missing:
            await self._download(missing[0], missing[-1], (previous, current))
        return GmeData(
            current=self._averages(current), previous=self._averages(previous)
        )

    async def _download(self, start: date, end: date, months: tuple[date, ...]) -> None:
        """Download the PUN from `start` to `end` and keep it on disk."""
        try:
            prices = await self._client.async_pun_prices(start, end)
        except GmeAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except GmeError as err:
            if not self._prices.get(months[-1]) and self.data is None:
                raise UpdateFailed(str(err)) from err
            if not self._failing:
                _LOGGER.warning("Could not download the PUN from GME: %s", err)
            self._failing = True
            return
        if self._failing:
            _LOGGER.info("Downloading the PUN from GME works again")
        self._failing = False
        for moment, price in prices:
            month = moment.astimezone(ITALY).date().replace(day=1)
            if month in months:
                self._prices.setdefault(month, {})[moment.astimezone(UTC)] = price
        await self._store.async_save(self._to_save())

    def _averages(self, month: date) -> PunAverages:
        return monthly_averages(month, self._prices.get(month, {}).items())

    def _to_save(self) -> dict[str, Any]:
        return {
            "months": {
                _month_key(month): {
                    moment.isoformat(): str(price) for moment, price in prices.items()
                }
                for month, prices in self._prices.items()
            }
        }
