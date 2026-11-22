# SPDX-License-Identifier: GPL-3.0-or-later
"""The offers of the Portale Offerte, downloaded while a contract is set up.

A workflow of the repository keeps offers.json on its market-data branch (see
tools/update_offers.py). The file is downloaded only when the config flow
looks up an offer code, never in the background, and kept in memory for
OFFERS_CACHE_MINUTES so that a second lookup, after a mistyped code, does not
download it again. Nothing is saved to disk.
"""

import asyncio
import json
from datetime import datetime, timedelta

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN, OFFERS_CACHE_MINUTES, OFFERS_TIMEOUT_SECONDS, OFFERS_URL
from .engine.catalogue import Catalogue, CatalogueError

_CACHE: HassKey[tuple[datetime, Catalogue]] = HassKey(f"{DOMAIN}_offers")
_LOCK: HassKey[asyncio.Lock] = HassKey(f"{DOMAIN}_offers_lock")


class OffersUnavailableError(Exception):
    """offers.json could not be downloaded, or is not a file we can read."""


def _read(text: str) -> Catalogue:
    """Parse the file (runs in the executor: a few MB of JSON)."""
    return Catalogue.from_dict(json.loads(text))


async def async_get_catalogue(hass: HomeAssistant) -> Catalogue:
    """Return the offers, downloading them unless a recent copy is in memory."""
    lock = hass.data.setdefault(_LOCK, asyncio.Lock())
    async with lock:
        now = dt_util.utcnow()
        cached = hass.data.get(_CACHE)
        if cached is not None and now - cached[0] < timedelta(
            minutes=OFFERS_CACHE_MINUTES
        ):
            return cached[1]
        catalogue = await _download(hass)
        hass.data[_CACHE] = (now, catalogue)
        return catalogue


async def _download(hass: HomeAssistant) -> Catalogue:
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            OFFERS_URL,
            timeout=aiohttp.ClientTimeout(total=OFFERS_TIMEOUT_SECONDS),
        ) as response:
            response.raise_for_status()
            # GitHub serves raw files as text/plain: decode the text ourselves.
            text = await response.text()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise OffersUnavailableError(f"cannot download {OFFERS_URL}: {err}") from err
    try:
        return await hass.async_add_executor_job(_read, text)
    except (ValueError, CatalogueError) as err:
        # CatalogueError is a ValueError, and so is a broken JSON.
        raise OffersUnavailableError(f"cannot read {OFFERS_URL}: {err}") from err
