# SPDX-License-Identifier: GPL-3.0-or-later
"""The offers of the Portale Offerte, downloaded while a contract is set up.

A workflow of the repository keeps the offers/ folder on its market-data
branch (see tools/update_offers.py), split into files by the hash of the
offer code. A lookup downloads the index, then the one file that can hold
the code typed: a few hundred KB, never the whole archive. Nothing is
downloaded in the background, and nothing is saved to disk.

Both are kept in memory for OFFERS_CACHE_MINUTES, so that a second lookup,
after a mistyped code, downloads only a file it does not have yet.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import (
    DOMAIN,
    OFFERS_BASE_URL,
    OFFERS_CACHE_MINUTES,
    OFFERS_TIMEOUT_SECONDS,
)
from .engine.catalogue import INDEX_FILE, Catalogue, CatalogueIndex


@dataclass(slots=True)
class _Cache:
    """What was downloaded, with the time it was."""

    index: tuple[datetime, CatalogueIndex] | None = None
    shards: dict[str, tuple[datetime, Catalogue]] = field(default_factory=dict)


_CACHE: HassKey[_Cache] = HassKey(f"{DOMAIN}_offers")
_LOCK: HassKey[asyncio.Lock] = HassKey(f"{DOMAIN}_offers_lock")


class OffersUnavailableError(Exception):
    """A file of the offers could not be downloaded, or cannot be read."""


def _fresh(entry: tuple[datetime, Any] | None, now: datetime) -> bool:
    """True when a cached entry is recent enough to use."""
    return entry is not None and now - entry[0] < timedelta(
        minutes=OFFERS_CACHE_MINUTES
    )


def _read_index(text: str) -> CatalogueIndex:
    """Parse index.json (a few hundred bytes)."""
    return CatalogueIndex.from_dict(json.loads(text))


def _read_shard(index: CatalogueIndex, name: str, text: str) -> Catalogue:
    """Parse one file of offers (runs in the executor: a few hundred KB)."""
    return Catalogue.from_shard(index, name, json.loads(text))


async def async_get_catalogue(hass: HomeAssistant, text: str) -> Catalogue:
    """Return the offers of the file that can hold the code typed as `text`.

    An empty code gives no offers. Raise OffersUnavailableError when a file
    cannot be downloaded or read.
    """
    lock = hass.data.setdefault(_LOCK, asyncio.Lock())
    async with lock:
        cache = hass.data.setdefault(_CACHE, _Cache())
        now = dt_util.utcnow()

        if cache.index is not None and _fresh(cache.index, now):
            index = cache.index[1]
        else:
            raw = await _download(hass, INDEX_FILE)
            try:
                index = _read_index(raw)
            except ValueError as err:
                # CatalogueError is a ValueError, and so is a broken JSON.
                raise OffersUnavailableError(
                    f"cannot read {INDEX_FILE}: {err}"
                ) from err
            # A new index may come with another layout: forget the files.
            cache.index, cache.shards = (now, index), {}

        name = index.shard_file(text)
        if name is None:
            return Catalogue(index.updated, {})
        cached = cache.shards.get(name)
        if cached is not None and _fresh(cached, now):
            return cached[1]
        raw = await _download(hass, name)
        try:
            catalogue = await hass.async_add_executor_job(_read_shard, index, name, raw)
        except ValueError as err:
            raise OffersUnavailableError(f"cannot read {name}: {err}") from err
        cache.shards[name] = (now, catalogue)
        return catalogue


async def _download(hass: HomeAssistant, name: str) -> str:
    """Download one file of the folder, as text."""
    url = f"{OFFERS_BASE_URL}{name}"
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=OFFERS_TIMEOUT_SECONDS)
        ) as response:
            response.raise_for_status()
            # GitHub serves raw files as text/plain: decode the text ourselves.
            return await response.text()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise OffersUnavailableError(f"cannot download {url}: {err}") from err
