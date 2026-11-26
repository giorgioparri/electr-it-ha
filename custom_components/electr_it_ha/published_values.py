# SPDX-License-Identifier: GPL-3.0-or-later
"""Market values published by Terna and ARERA, shared by every supply.

A workflow of the repository keeps market_values.json up to date on its
market-data branch (see tools/update_market_values.py). This downloads the
file once a day and saves the last good copy to disk; the integration also
ships a copy of its own. Of the copies that can be read, the one updated
most recently is used, so a download that fails changes nothing.

When no download has worked for PUBLISHED_STALE_DAYS, a repair issue says
so; it goes away by itself at the first download that works.
"""

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import (
    DOMAIN,
    ISSUE_PUBLISHED_DOWNLOAD,
    PUBLISHED_FILE,
    PUBLISHED_REFRESH_HOURS,
    PUBLISHED_RETRY_MINUTES,
    PUBLISHED_STALE_DAYS,
    PUBLISHED_STORAGE_KEY,
    PUBLISHED_TIMEOUT_SECONDS,
    PUBLISHED_URL,
    STORAGE_VERSION,
)
from .engine.published import Published, PublishedDataError, PublishedValue, newest

_LOGGER = logging.getLogger(__name__)

_KEY: HassKey[PublishedValues] = HassKey(f"{DOMAIN}_published")
_LOCK: HassKey[asyncio.Lock] = HassKey(f"{DOMAIN}_published_lock")


async def async_get_published(hass: HomeAssistant) -> PublishedValues:
    """Return the shared published values, setting them up on first use."""
    lock = hass.data.setdefault(_LOCK, asyncio.Lock())
    async with lock:
        if _KEY not in hass.data:
            published = PublishedValues(hass)
            await published.async_setup()
            hass.data[_KEY] = published
    return hass.data[_KEY]


def _read_shipped() -> Any:
    """Read the copy shipped with the integration (runs in the executor)."""
    path = Path(__file__).parent / PUBLISHED_FILE
    return json.loads(path.read_text(encoding="utf-8"))


def _parse(data: Any, what: str) -> Published | None:
    """Read a copy of the file; None, with a warning, when it is not sound."""
    if data is None:
        return None
    try:
        return Published.from_dict(data)
    except PublishedDataError as err:
        _LOGGER.warning("Ignoring the %s of the market values: %s", what, err)
        return None


def _moment(text: Any) -> datetime | None:
    """Read a saved time; None when there is none."""
    return dt_util.parse_datetime(text) if isinstance(text, str) else None


class PublishedValues:
    """Download, keep and look up the values published by Terna and ARERA."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Nothing is read or downloaded yet."""
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, PUBLISHED_STORAGE_KEY
        )
        self._shipped: Published | None = None
        self._saved: dict[str, Any] | None = None
        self.current: Published | None = None
        self._downloaded_at: datetime | None = None
        self._failing_since: datetime | None = None
        self._refreshing = asyncio.Lock()

    async def async_setup(self) -> None:
        """Read the shipped and the saved copies, then refresh in the background."""
        try:
            shipped = await self.hass.async_add_executor_job(_read_shipped)
        except (OSError, ValueError) as err:
            _LOGGER.warning("Cannot read the shipped market values: %s", err)
            shipped = None
        self._shipped = _parse(shipped, "shipped copy")
        saved = await self._store.async_load() or {}
        self._saved = saved.get("content")
        self._downloaded_at = _moment(saved.get("downloaded_at"))
        self._failing_since = _moment(saved.get("failing_since"))
        self.current = newest(_parse(self._saved, "saved copy"), self._shipped)

        stop_timer = async_track_time_interval(
            self.hass, self._tick, timedelta(minutes=PUBLISHED_RETRY_MINUTES)
        )

        @callback
        def _stop(_event: Event) -> None:
            stop_timer()

        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)
        self.hass.async_create_background_task(
            self.async_refresh_if_due(), f"{DOMAIN} market values"
        )

    @callback
    def _tick(self, _now: datetime) -> None:
        self.hass.async_create_background_task(
            self.async_refresh_if_due(), f"{DOMAIN} market values"
        )

    def _due(self, now: datetime) -> bool:
        """True when the last good download is older than a day, or missing."""
        last = self._downloaded_at
        return last is None or now - last >= timedelta(hours=PUBLISHED_REFRESH_HOURS)

    async def async_refresh_if_due(self) -> None:
        """Download the file when it is due; retried every hour until it works."""
        if self._refreshing.locked() or not self._due(dt_util.utcnow()):
            return
        async with self._refreshing:
            await self.async_refresh()

    async def async_refresh(self) -> None:
        """Download the file now and keep it when it is sound."""
        now = dt_util.utcnow()
        try:
            data = await self._download()
            downloaded = Published.from_dict(data)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            # PublishedDataError is a ValueError, and so is a broken JSON.
            await self._failed(now, err)
            return
        if self._failing_since is not None:
            _LOGGER.info("Downloading the market values works again")
        self._saved = data
        self._downloaded_at = now
        self._failing_since = None
        self.current = newest(downloaded, self._shipped)
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_PUBLISHED_DOWNLOAD)
        await self._store.async_save(self._to_save())

    async def _download(self) -> Any:
        session = async_get_clientsession(self.hass)
        async with session.get(
            PUBLISHED_URL,
            timeout=aiohttp.ClientTimeout(total=PUBLISHED_TIMEOUT_SECONDS),
        ) as response:
            response.raise_for_status()
            # GitHub serves raw files as text/plain: decode the text ourselves.
            return json.loads(await response.text())

    async def _failed(self, now: datetime, err: Exception) -> None:
        """Remember since when downloads fail, and raise the issue in time."""
        if self._failing_since is None:
            _LOGGER.warning("Cannot download the market values: %s", err)
            self._failing_since = now
            await self._store.async_save(self._to_save())
        else:
            _LOGGER.debug("Cannot download the market values: %s", err)
        since = self._downloaded_at or self._failing_since
        if now - since >= timedelta(days=PUBLISHED_STALE_DAYS):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_PUBLISHED_DOWNLOAD,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_PUBLISHED_DOWNLOAD,
                translation_placeholders={
                    "url": PUBLISHED_URL,
                    "since": dt_util.as_local(since).date().isoformat(),
                    "updated": (
                        "-"
                        if self.current is None
                        else self.current.updated.isoformat()
                    ),
                },
            )

    def _to_save(self) -> dict[str, Any]:
        return {
            "content": self._saved,
            "downloaded_at": (
                None if self._downloaded_at is None else self._downloaded_at.isoformat()
            ),
            "failing_since": (
                None if self._failing_since is None else self._failing_since.isoformat()
            ),
        }

    def month_value(self, name: str, month: date) -> PublishedValue | None:
        """Return the value of `name` for the whole of `month`, if published."""
        if self.current is None:
            return None
        return self.current.for_month(name, month.year, month.month)

    def latest_value(self, name: str, day: date) -> PublishedValue | None:
        """Return the last value of `name` published for `day` or before it."""
        if self.current is None:
            return None
        return self.current.latest(name, day)
